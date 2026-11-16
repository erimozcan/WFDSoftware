# Fusion 360 Add-in (Python)
# Generates propeller blade section sketches from root/mid/tip Selig-format
# airfoil .dat files, creates 3D leading/trailing edge rails, and performs a
# single loft to create a smooth blade body.
#
# UNITS (IMPORTANT):
# - Fusion userParameter.value for LENGTH is returned in Fusion internal units (centimeters),
#   even if the parameter is defined/displayed as inches in the UI.
# - This script works internally in centimeters for all geometry.
#
# Placement:
# - Root station is at X = HubR (absolute)
# - Tip station is at X = Radius (absolute)
#
# Additions included:
# (A) Tip thickness shaping:
#     TipShapeStartS / TipShapeExp / TipThickScale
# (B) Explicit final-station tip chord:
#     stations before the final tip use the ChordRoot/ChordPeak/ChordTip law;
#     only the final station is reduced to FINAL_STATION_CHORD_PREV_FACTOR
#     of the second-to-last chord
# (C) Tip TE-offset:
#     TipTEOffset / TipTEOffsetStartS / TipTEOffsetExp

import adsk.core
import adsk.fusion
import csv
import json
import os
import re
import struct
import traceback
import math

CMD_ID = 'cmd_airfoil_sections_selig_dat'
CMD_NAME = 'Generate Airfoil Sections + Rails + Loft (Selig DAT)'
CMD_DESC = 'Creates airfoil sections, LE/TE rails, and lofts a blade body.'

# --- Batch-generation controls ---
# When True, the command asks for variants.csv after the three airfoil .dat files.
# Cancel the CSV dialog to keep the original one-design behavior using the
# current Fusion parameter table.
FINAL_PROP_MODE = True
BATCH_MODE = True
VARIANTS_CSV_NAME = 'batch_spinner1.csv'
EXPORTS_DIR_NAME = 'exports_with_spinner'
CLEAR_PREVIOUS_BATCH_EXPORTS = True
BATCH_STATION_COUNT = 30
FIXED_RADIUS_EXPR = "3 in"
TWIST_MODE = "pitch_based"  # Supported values: "linear", "pitch_based"

# --- Batch spinner controls ---
# When enabled, each batch prop STL is exported as usual, then merged with this
# spinner STL into one binary STL for CFD. Leave SPINNER_STL_PATH blank to choose
# the spinner file when the Fusion command starts.
BATCH_ATTACH_SPINNER = True
SPINNER_STL_PATH = 'SpinnerCapBlank.stl'
SPINNER_STL_UNITS = 'mm'  # Supported values: "mm", "cm", "in"
SPINNER_SOURCE_AXIS = 'Y'  # Spinner STL cone axis; maps onto the prop Z axis.
SPINNER_SOURCE_BOTTOM = 'min'  # Which end of SPINNER_SOURCE_AXIS is the hub-contact plane.
SPINNER_CENTER_ON_PROP_AXIS = True
SPINNER_BOTTOM_ON_HUB_BOTTOM = True
SPINNER_OFFSET_X_MM = 0.0
SPINNER_OFFSET_Y_MM = 0.0
SPINNER_OFFSET_Z_MM = 0.0

# --- Final prop controls ---
# Generates the selected winning prop without opening the batch CSV dialog.
# Tip closure is preserved from the 30-station batch: the last full ChordTip
# station remains at the same span location, with extra stations added inboard.
FINAL_PROP_VARIANT_ID = 'p24_2b_r40p51t21'
FINAL_PROP_EXPORT_STEP = True
# Exports land in FINAL_PROP_EXPORTS_DIR_PREFIX + the active Fusion document
# name, so each new file gets its own folder instead of overwriting the last
# run's STEPs. The fallback is used only if the document name cannot be read.
FINAL_PROP_EXPORTS_DIR_PREFIX = 'exports_final_'
FINAL_PROP_EXPORTS_DIR_FALLBACK = 'exports_final_p24_2b_r40p51t21'
FINAL_PROP_LEGACY_TIP_STATIONS = 30
FINAL_PROP_ROTATE_ABOUT_X_DEG = -90.0
FINAL_PROP_BOTTOM_ON_XY_PLANE = True

# Station count for the final prop. station_span_values keeps the spacing
# perfectly uniform across the whole span (tip-closure segment included) by
# snapping the segment count to a multiple of the legacy layout's 29 segments,
# with one station pinned exactly on the legacy last-full-chord station at
# s = 28/29. Valid counts are therefore 30, 59, 88, ... (29k + 1); other values
# snap to the nearest of these. 30 reproduces the successfully manufactured
# prop's stations exactly (chords are bit-identical to the legacy law at this
# count) and is the safest choice for manifold CAM-ready exports; 59 doubles
# the resolution if needed. Non-uniform spacing (the old 120-station layout)
# made the fitted LE/TE rail splines oscillate at the tip and the STEP export
# produce sliver faces / open edges there.
FINAL_PROP_DEBUG_FAST = False
FINAL_PROP_DEBUG_STATIONS = 30
FINAL_PROP_PRODUCTION_STATIONS = 117
FINAL_PROP_ACTIVE_STATIONS = (
    FINAL_PROP_DEBUG_STATIONS if FINAL_PROP_DEBUG_FAST else FINAL_PROP_PRODUCTION_STATIONS
)

FINAL_PROP_VALUES = {
    'variant_id': FINAL_PROP_VARIANT_ID,
    'GeometricPitch': '24.0 in',
    'ChordRoot': '0.40 in',
    'ChordPeak': '0.51 in',
    'ChordTip': '0.21 in',
    'nStations': str(FINAL_PROP_ACTIVE_STATIONS)
}

# --- Geometry/orientation controls ---
BASE_ROT_DEG = 90.0
CENTER_TO_ORIGIN = True
INVERT_TWIST = True

# --- Loft controls ---
MAKE_SOLID_LOFT = True
CREATE_LOFT = True

# --- Single-run speed controls ---
# When BATCH_MODE is False, these preview settings reduce sketch/loft complexity
# so baseline tip/chord iterations run much faster. Batch mode always keeps the
# production-resolution values above.
FAST_SINGLE_RUN_MODE = True
FAST_SINGLE_RUN_AIRFOIL_POINTS = 80
FAST_SINGLE_RUN_MAX_STATIONS = 12
FAST_SINGLE_RUN_SURFACE_LOFT = False

# --- Airfoil blending controls ---
# Leave paths blank to select the root/mid/tip Selig .dat files with file dialogs.
# These defaults use the cambered WFD 16-series airfoils generated by
# make_cambered_16_series.py:
#   root: thickest / hub-friendly
#   mid:  moderate high-speed section
#   tip:  thinnest / tip-friendly section
CAMBERED_AIRFOIL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'CamberedAirfoils')
RootAirfoilPath = os.path.join(CAMBERED_AIRFOIL_DIR, 'wfd_16-212_root.dat')
MidAirfoilPath = os.path.join(CAMBERED_AIRFOIL_DIR, 'wfd_16-209_mid.dat')
TipAirfoilPath = os.path.join(CAMBERED_AIRFOIL_DIR, 'wfd_16-206_tip.dat')

# s is normalized span: s=0 at HubR, s=1 at Radius.
# In the transition bands, airfoil coordinates are smoothly blended so the
# blade remains one continuous lofted body rather than three abrupt shapes.
ROOT_TO_MID_START_S = 0.20
ROOT_TO_MID_END_S = 0.40
MID_TO_TIP_START_S = 0.65
MID_TO_TIP_END_S = 0.85

# Airfoils from different .dat files often have different point counts. They
# must share the same ordered point count before point-by-point blending.
N_AIRFOIL_POINTS = 160

# Diagnostic option: writes station-specific blended normalized coordinates.
EXPORT_AIRFOIL_DEBUG_CSV = False

# --- Tip chord closure ---
# The second-to-last station is pinned to ChordTip (so the CSV ChordTip is the
# last full station chord), and the final station tapers to this fraction of it
# to close the tip. This avoids hidden tapering from older tip-shaping paths.
FINAL_STATION_CHORD_PREV_FACTOR = 0.65

# --- Root extension / blade count / hub controls ---
ROOT_INBOARD_EXTENSION_IN = 0.30
BLADE_COUNT_MODE = 'two_blade'  # Supported values: "two_blade", "three_blade", "custom"
# Backward-compatible manual count. Used only when BLADE_COUNT_MODE == "custom".
N_BLADES = 2
HUB_OUTER_DIAMETER_MM = 16.0
HUB_THICKNESS_MM = 12.5
HUB_OUTER_DIAMETER_IN = HUB_OUTER_DIAMETER_MM / 25.4
HUB_THICKNESS_IN = HUB_THICKNESS_MM / 25.4
M5_CLEARANCE_HOLE_DIAMETER_MM = 5.2

# Tolerance for identifying LE/TE x locations in normalized airfoil
X_TOL = 1e-5
MIN_TWIST_RADIUS_CM = 1e-6
# The airfoil .dat files leave a small open trailing-edge gap that each
# section closes with a straight line. Near the tip the scaled gap shrinks to
# ~1 micron, and a closing line that short sits at Fusion's modeling
# tolerance, producing degenerate "open edge" defects in the exported STEP.
# Gaps below this size are welded shut (last point snapped onto the first) so
# the section spline closes on itself instead.
# WELDING IS DISABLED (tolerance 0): every weld configuration tried so far
# broke the loft. A 5e-3 all-station weld hairpinned the root sections
# (~43 micron TE gap after ThickRootScale) into self-intersecting profiles,
# and even tip-only 5e-4 welds made the 30-station loft fail with
# ASM_LOFT_SURFACE_SELF_INTERSECTS. The straight TE closing line at every
# station is the configuration that lofted and manufactured successfully.
TE_WELD_TOL_CM = 0.0

# Minimum physical trailing-edge gap per station. The loft turns the per-
# station TE closing lines into one long blunt-TE strip face whose width
# equals the local gap: ~45 um at the root but only 10-25 um outboard and
# ~1.3 um at the tip. CAM kernels import with ~10-25 um tolerance, so the
# narrow outboard strip collapses into a zipper of "non-manifold" edges.
# Stations whose gap is below this value get their TE endpoints spread apart
# (the opposite of welding, so it cannot hairpin the spline) to keep the
# strip everywhere wider than CAM tolerance. 30 um is far below manufacturing
# resolution, so the flying shape is unchanged in practice.
MIN_TE_GAP_CM = 3e-3

# Minimum physical max-thickness per station (manufacturing floor). Chord tapers
# toward the tip faster than the airfoil can stay cuttable, so outboard stations
# thin below what the manufacturer can hold. A station whose max thickness would
# fall under this value gets its thickness scale raised just enough to reach it;
# stations already above are untouched, so the chord taper and the inboard
# sections are unchanged. Only y is scaled, so chord and planform never move.
# Set to 0.0 to disable.
MIN_SECTION_THICKNESS_CM = 0.08
GENERATED_BLADE_BASE_NAME = 'WFD_Blade'
GENERATED_HUB_BASE_NAME = 'WFD_Hub'
GENERATED_PROP_BASE_NAME = 'WFD_Prop'
IGNORED_CSV_PARAMETER_NAMES = {
    'ChordEnd',
    'TipTaperStartS',
    'TipTaperExp',
    'TipChordScale'
}

_handlers = []

BASE_REQUIRED_PARAMETER_NAMES = [
    'Radius', 'HubR',
    'ChordRoot', 'ChordPeak', 'ChordTip',
    'ChordPeakS',
    'ThickRootScale', 'ThickEndS', 'ThickExp',
    'nStations',

    # Tip thickness shaping:
    'TipShapeStartS', 'TipShapeExp', 'TipThickScale',

    # Tip TE-offset:
    'TipTEOffset', 'TipTEOffsetStartS', 'TipTEOffsetExp'
]


# Default expressions for auto-creating missing user parameters, so the add-in
# can run in a fresh empty document (recommended: documents that accumulate
# many generation runs recompute very slowly). Values match the p24_2b_r40p51t21
# export metadata; FINAL_PROP_VALUES / FIXED_RADIUS_EXPR still override the
# swept ones per run. Format: name -> (expression, unit).
DEFAULT_PARAMETER_EXPRESSIONS = {
    'Radius': ('3 in', 'in'),
    'HubR': ('0.45 in', 'in'),
    'ChordRoot': ('0.40 in', 'in'),
    'ChordPeak': ('0.51 in', 'in'),
    'ChordTip': ('0.21 in', 'in'),
    'ChordPeakS': ('0.31', ''),
    'ThickRootScale': ('2.5', ''),
    'ThickEndS': ('0.6', ''),
    'ThickExp': ('1.5', ''),
    'nStations': ('30', ''),
    'TipShapeStartS': ('0.6', ''),
    'TipShapeExp': ('1.0', ''),
    'TipThickScale': ('1', ''),
    'TipTEOffset': ('0.04 in', 'in'),
    'TipTEOffsetStartS': ('0.6', ''),
    'TipTEOffsetExp': ('1.5', ''),
    'GeometricPitch': ('24.0 in', 'in')
}


def ensure_required_parameters(design):
    """Create any missing required user parameters with known defaults.

    Lets the generator run in a fresh, empty document instead of the heavily
    accumulated one. Parameters that already exist keep their current values.
    Returns the list of parameter names that were created.
    """
    created = []
    for name in required_parameter_names():
        if get_param(design, name):
            continue
        default = DEFAULT_PARAMETER_EXPRESSIONS.get(name)
        if default is None:
            continue  # No default known; read_current_parameters reports it.
        expression, unit = default
        design.userParameters.add(
            name,
            adsk.core.ValueInput.createByString(expression),
            unit,
            ''
        )
        created.append(name)
    return created


def required_parameter_names():
    names = list(BASE_REQUIRED_PARAMETER_NAMES)
    if TWIST_MODE == "linear":
        names.extend(['TwistRoot', 'TwistTip'])
    elif TWIST_MODE == "pitch_based":
        names.append('GeometricPitch')
    else:
        raise ValueError(
            f'Unsupported TWIST_MODE "{TWIST_MODE}". Use "linear" or "pitch_based".'
        )
    return names


def is_fast_single_run():
    return bool(FAST_SINGLE_RUN_MODE and not BATCH_MODE and not FINAL_PROP_MODE)


def active_airfoil_point_count():
    if is_fast_single_run():
        return max(20, int(FAST_SINGLE_RUN_AIRFOIL_POINTS))
    return int(N_AIRFOIL_POINTS)


def active_make_solid_loft():
    if is_fast_single_run() and FAST_SINGLE_RUN_SURFACE_LOFT:
        return False
    return bool(MAKE_SOLID_LOFT)


def inch_to_cm(value):
    return float(value) * 2.54


def mm_to_cm(value):
    return float(value) * 0.1


def active_blade_count():
    mode = str(BLADE_COUNT_MODE).strip().lower()
    if mode == 'two_blade':
        return 2
    if mode == 'three_blade':
        return 3
    if mode == 'custom':
        count = int(N_BLADES)
        if count < 1:
            raise ValueError('N_BLADES must be at least 1 when BLADE_COUNT_MODE is "custom".')
        return count
    raise ValueError(
        f'Unsupported BLADE_COUNT_MODE "{BLADE_COUNT_MODE}". '
        'Use "two_blade", "three_blade", or "custom".'
    )


def blade_count_text(count=None):
    count = active_blade_count() if count is None else int(count)
    suffix = '' if count == 1 else 's'
    return f'{count} blade{suffix}'


# ---------- Utilities ----------
def get_param(design, name):
    for p in design.userParameters:
        if p.name == name:
            return p
    return None


def require_params(design, names):
    found, missing = {}, []
    for n in names:
        p = get_param(design, n)
        if p:
            found[n] = p
        else:
            missing.append(n)
    return found, missing


def ensure_component(design, name):
    root = design.rootComponent
    if root:
        return root
    raise RuntimeError(
        f'Could not access the root component for generated geometry "{name}".'
    )


def is_generated_body_name(name):
    return (
        name.startswith('BladeBody') or
        name.startswith('BladeLoft') or
        name.startswith(GENERATED_BLADE_BASE_NAME) or
        name.startswith(GENERATED_HUB_BASE_NAME) or
        name.startswith(GENERATED_PROP_BASE_NAME)
    )


def generated_blade_body_name(variant_id=None):
    if variant_id:
        return f'{GENERATED_BLADE_BASE_NAME}_{safe_filename(variant_id)}'
    return GENERATED_BLADE_BASE_NAME


def generated_prop_body_name(variant_id=None):
    if variant_id:
        return f'{GENERATED_PROP_BASE_NAME}_{safe_filename(variant_id)}'
    return GENERATED_PROP_BASE_NAME


def delete_old(comp):
    log_progress(
        f'Clearing previous generated geometry '
        f'({comp.sketches.count} sketches, {comp.constructionPlanes.count} planes in doc)...'
    )
    try:
        for i in reversed(range(comp.features.combineFeatures.count)):
            feat = comp.features.combineFeatures.item(i)
            if feat.name.startswith(GENERATED_PROP_BASE_NAME) or feat.name.startswith(GENERATED_BLADE_BASE_NAME):
                feat.deleteMe()
    except:
        pass
    try:
        for i in reversed(range(comp.features.circularPatternFeatures.count)):
            feat = comp.features.circularPatternFeatures.item(i)
            if feat.name.startswith(GENERATED_BLADE_BASE_NAME):
                feat.deleteMe()
    except:
        pass
    try:
        for i in reversed(range(comp.features.extrudeFeatures.count)):
            feat = comp.features.extrudeFeatures.item(i)
            if feat.name.startswith(GENERATED_HUB_BASE_NAME):
                feat.deleteMe()
    except:
        pass
    # Delete dependent loft features before deleting their input sketches/planes.
    try:
        for i in reversed(range(comp.features.loftFeatures.count)):
            feat = comp.features.loftFeatures.item(i)
            if feat.name.startswith('BladeLoft') or feat.name.startswith(GENERATED_BLADE_BASE_NAME):
                feat.deleteMe()
    except:
        pass
    try:
        for i in reversed(range(comp.bRepBodies.count)):
            body = comp.bRepBodies.item(i)
            if is_generated_body_name(body.name):
                body.deleteMe()
    except:
        pass
    try:
        for i in reversed(range(comp.sketches.count)):
            sk = comp.sketches.item(i)
            if (
                sk.name.startswith('AF_') or sk.name.startswith('Rails_') or
                sk.name.startswith('Hub_') or sk.name.startswith('RootFairing')
            ):
                sk.deleteMe()
    except:
        pass
    try:
        for i in reversed(range(comp.constructionPlanes.count)):
            pl = comp.constructionPlanes.item(i)
            if (
                pl.name.startswith('Plane_s=') or pl.name.startswith('Plane_Hub') or
                pl.name.startswith('RootFairing')
            ):
                pl.deleteMe()
    except:
        pass


def read_selig_dat(path):
    with open(path, 'r') as f:
        lines = [l.strip() for l in f.readlines() if l.strip()]
    name = lines[0]
    pts = []
    for l in lines[1:]:
        try:
            x, y = map(float, l.split())
            pts.append((x, y))
        except:
            pass
    if len(pts) < 20:
        raise ValueError('Invalid or too-short Selig DAT file.')
    return name, pts


def remove_consecutive_duplicate_points(pts, tol=1e-12):
    cleaned = []
    for x, y in pts:
        if not cleaned:
            cleaned.append((x, y))
            continue
        px, py = cleaned[-1]
        if abs(x - px) > tol or abs(y - py) > tol:
            cleaned.append((x, y))
    return cleaned


def resample_airfoil_loop(pts, n_points):
    """
    Resample a Selig-style ordered airfoil curve by arc length.

    The input point order is preserved. We do not sort by x because Selig files
    normally travel TE -> upper -> LE -> lower -> TE, and sorting would destroy
    the section loop needed by Fusion's fitted spline/profile creation.
    """
    if n_points < 3:
        raise ValueError('n_points must be at least 3.')
    pts = remove_consecutive_duplicate_points(pts)
    if len(pts) < 3:
        raise ValueError('Airfoil has too few unique points to resample.')

    cumulative = [0.0]
    for i in range(1, len(pts)):
        x0, y0 = pts[i - 1]
        x1, y1 = pts[i]
        cumulative.append(cumulative[-1] + math.hypot(x1 - x0, y1 - y0))

    total = cumulative[-1]
    if total <= 1e-12:
        raise ValueError('Airfoil arc length is too small to resample.')

    resampled = []
    seg = 1
    for i in range(n_points):
        target = total * i / (n_points - 1)
        while seg < len(cumulative) - 1 and cumulative[seg] < target:
            seg += 1
        d0 = cumulative[seg - 1]
        d1 = cumulative[seg]
        x0, y0 = pts[seg - 1]
        x1, y1 = pts[seg]
        if d1 <= d0:
            t = 0.0
        else:
            t = (target - d0) / (d1 - d0)
        resampled.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    return resampled


def blend_airfoils(pts_a, pts_b, weight):
    if len(pts_a) != len(pts_b):
        raise ValueError('Airfoils must have the same point count before blending.')
    w = max(0.0, min(1.0, weight))
    return [
        ((1.0 - w) * ax + w * bx, (1.0 - w) * ay + w * by)
        for (ax, ay), (bx, by) in zip(pts_a, pts_b)
    ]


def validate_airfoil_transition_ranges():
    if active_airfoil_point_count() < 3:
        raise ValueError('Active airfoil point count must be at least 3.')
    if not (0.0 <= ROOT_TO_MID_START_S <= 1.0 and 0.0 <= ROOT_TO_MID_END_S <= 1.0):
        raise ValueError('Root-to-mid transition values must be within [0, 1].')
    if not (0.0 <= MID_TO_TIP_START_S <= 1.0 and 0.0 <= MID_TO_TIP_END_S <= 1.0):
        raise ValueError('Mid-to-tip transition values must be within [0, 1].')
    if not (ROOT_TO_MID_START_S < ROOT_TO_MID_END_S):
        raise ValueError('Invalid airfoil transitions: ROOT_TO_MID_START_S must be < ROOT_TO_MID_END_S.')
    if not (ROOT_TO_MID_END_S <= MID_TO_TIP_START_S):
        raise ValueError('Invalid airfoil transitions: ROOT_TO_MID_END_S must be <= MID_TO_TIP_START_S.')
    if not (MID_TO_TIP_START_S < MID_TO_TIP_END_S):
        raise ValueError('Invalid airfoil transitions: MID_TO_TIP_START_S must be < MID_TO_TIP_END_S.')


def airfoil_for_station(s, root_pts, mid_pts, tip_pts):
    """
    Select or smoothly blend the airfoil for normalized span station s.

    Abrupt airfoil switching can create section discontinuities that make rails
    and lofts kink or fail. Smoothstep eases the root->mid and mid->tip blends
    so adjacent stations evolve gradually.
    """
    if s < ROOT_TO_MID_START_S:
        return root_pts
    if s <= ROOT_TO_MID_END_S:
        denom = ROOT_TO_MID_END_S - ROOT_TO_MID_START_S
        w = smoothstep((s - ROOT_TO_MID_START_S) / denom)
        return blend_airfoils(root_pts, mid_pts, w)
    if s < MID_TO_TIP_START_S:
        return mid_pts
    if s <= MID_TO_TIP_END_S:
        denom = MID_TO_TIP_END_S - MID_TO_TIP_START_S
        w = smoothstep((s - MID_TO_TIP_START_S) / denom)
        return blend_airfoils(mid_pts, tip_pts, w)
    return tip_pts


def airfoil_metadata(airfoil_set):
    return {
        'root_airfoil_filename': os.path.basename(airfoil_set['root_path']),
        'mid_airfoil_filename': os.path.basename(airfoil_set['mid_path']),
        'tip_airfoil_filename': os.path.basename(airfoil_set['tip_path']),
        'root_airfoil_path': airfoil_set['root_path'],
        'mid_airfoil_path': airfoil_set['mid_path'],
        'tip_airfoil_path': airfoil_set['tip_path'],
        'ROOT_TO_MID_START_S': ROOT_TO_MID_START_S,
        'ROOT_TO_MID_END_S': ROOT_TO_MID_END_S,
        'MID_TO_TIP_START_S': MID_TO_TIP_START_S,
        'MID_TO_TIP_END_S': MID_TO_TIP_END_S,
        'N_AIRFOIL_POINTS': active_airfoil_point_count()
    }


def geometry_metadata():
    blade_count = active_blade_count()
    return {
        'FINAL_STATION_CHORD_PREV_FACTOR': FINAL_STATION_CHORD_PREV_FACTOR,
        'MIN_SECTION_THICKNESS_CM': MIN_SECTION_THICKNESS_CM,
        'ROOT_INBOARD_EXTENSION_IN': ROOT_INBOARD_EXTENSION_IN,
        'BLADE_COUNT_MODE': BLADE_COUNT_MODE,
        'N_BLADES': blade_count,
        'BATCH_STATION_COUNT': BATCH_STATION_COUNT,
        'BATCH_ATTACH_SPINNER': BATCH_ATTACH_SPINNER,
        'SPINNER_STL_PATH': SPINNER_STL_PATH,
        'SPINNER_STL_UNITS': SPINNER_STL_UNITS,
        'SPINNER_SOURCE_AXIS': SPINNER_SOURCE_AXIS,
        'SPINNER_SOURCE_BOTTOM': SPINNER_SOURCE_BOTTOM,
        'SPINNER_CENTER_ON_PROP_AXIS': SPINNER_CENTER_ON_PROP_AXIS,
        'SPINNER_BOTTOM_ON_HUB_BOTTOM': SPINNER_BOTTOM_ON_HUB_BOTTOM,
        'SPINNER_OFFSET_X_MM': SPINNER_OFFSET_X_MM,
        'SPINNER_OFFSET_Y_MM': SPINNER_OFFSET_Y_MM,
        'SPINNER_OFFSET_Z_MM': SPINNER_OFFSET_Z_MM,
        'HUB_OUTER_DIAMETER_MM': HUB_OUTER_DIAMETER_MM,
        'HUB_THICKNESS_MM': HUB_THICKNESS_MM,
        'HUB_OUTER_DIAMETER_IN': HUB_OUTER_DIAMETER_IN,
        'HUB_THICKNESS_IN': HUB_THICKNESS_IN,
        'M5_CLEARANCE_HOLE_DIAMETER_MM': M5_CLEARANCE_HOLE_DIAMETER_MM,
        'FINAL_PROP_MODE': FINAL_PROP_MODE,
        'FINAL_PROP_VARIANT_ID': FINAL_PROP_VARIANT_ID,
        'FINAL_PROP_LEGACY_TIP_STATIONS': FINAL_PROP_LEGACY_TIP_STATIONS,
        'FINAL_PROP_ROTATE_ABOUT_X_DEG': FINAL_PROP_ROTATE_ABOUT_X_DEG,
        'FINAL_PROP_BOTTOM_ON_XY_PLANE': FINAL_PROP_BOTTOM_ON_XY_PLANE,
        'CLEAR_PREVIOUS_BATCH_EXPORTS': CLEAR_PREVIOUS_BATCH_EXPORTS,
        'FAST_SINGLE_RUN_MODE': FAST_SINGLE_RUN_MODE,
        'FAST_SINGLE_RUN_AIRFOIL_POINTS': FAST_SINGLE_RUN_AIRFOIL_POINTS,
        'FAST_SINGLE_RUN_MAX_STATIONS': FAST_SINGLE_RUN_MAX_STATIONS,
        'FAST_SINGLE_RUN_SURFACE_LOFT': FAST_SINGLE_RUN_SURFACE_LOFT
    }


def linspace(n):
    if n < 2:
        return [0.0, 1.0]
    return [i / (n - 1) for i in range(n)]


def final_prop_tip_closure_start_s(nStations, variant_id=None):
    """s of the legacy last-full-chord station, or None outside final-prop mode.

    From this station to the tip (s=1) the chord tapers from ChordTip down to
    FINAL_STATION_CHORD_PREV_FACTOR * ChordTip, regardless of how many stations
    subdivide the closure region.
    """
    if (
        FINAL_PROP_MODE and
        variant_id == FINAL_PROP_VARIANT_ID and
        nStations > 2 and
        FINAL_PROP_LEGACY_TIP_STATIONS > 2
    ):
        return (
            (FINAL_PROP_LEGACY_TIP_STATIONS - 2) /
            (FINAL_PROP_LEGACY_TIP_STATIONS - 1)
        )
    return None


def station_span_values(nStations, variant_id=None):
    closure_s = final_prop_tip_closure_start_s(nStations, variant_id)
    if closure_s is not None:
        # Uniform spacing over the whole span, with one station landing exactly
        # on the legacy last-full-chord station. That requires the segment
        # count to be a multiple of the legacy layout's segments (29), so the
        # requested count snaps to the nearest 29k + 1 stations (30, 59, 88...).
        # A non-uniform tip segment kinks the fitted LE/TE rail splines and
        # breaks the STEP export at the tip.
        legacy_segments = FINAL_PROP_LEGACY_TIP_STATIONS - 1
        multiplier = max(1, round((nStations - 1) / legacy_segments))
        segments = multiplier * legacy_segments
        return [i / segments for i in range(segments + 1)]
    return linspace(nStations)


def smoothstep(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


# ---------- Math helpers for chord law ----------
def solve_3x3(A, b):
    M = [A[0][:] + [b[0]], A[1][:] + [b[1]], A[2][:] + [b[2]]]
    for col in range(3):
        pivot = col
        for r in range(col, 3):
            if abs(M[r][col]) > abs(M[pivot][col]):
                pivot = r
        if abs(M[pivot][col]) < 1e-12:
            raise ValueError("Singular system while solving cubic coefficients.")
        if pivot != col:
            M[col], M[pivot] = M[pivot], M[col]
        piv = M[col][col]
        for c in range(col, 4):
            M[col][c] /= piv
        for r in range(col + 1, 3):
            factor = M[r][col]
            for c in range(col, 4):
                M[r][c] -= factor * M[col][c]
    x = [0.0, 0.0, 0.0]
    for r in reversed(range(3)):
        x[r] = M[r][3] - sum(M[r][c] * x[c] for c in range(r + 1, 3))
    return x


def cubic_coeffs_with_peak(Croot, Cpeak, Ctip, s_peak):
    """
    chord(s)=a s^3 + b s^2 + c s + d with:
      chord(0)=Croot
      chord(s_peak)=Cpeak
      chord'(s_peak)=0
      chord(1)=Ctip
    all in cm
    """
    if not (0.0 < s_peak < 1.0):
        raise ValueError("ChordPeakS must be between 0 and 1.")
    d = Croot
    sp = s_peak
    A = [
        [1.0, 1.0, 1.0],
        [sp**3, sp**2, sp],
        [3.0 * sp**2, 2.0 * sp, 1.0],
    ]
    bvec = [Ctip - d, Cpeak - d, 0.0]
    a, b, c = solve_3x3(A, bvec)
    return a, b, c, d


def chord_cubic(s, a, b, c, d):
    return a * s**3 + b * s**2 + c * s + d


def catmull_rom_uniform(values, x):
    """C1 Catmull-Rom spline through values at uniform integer knots.

    x is in knot units (0 .. len(values)-1). Evaluations exactly on a knot
    return that knot's value; endpoints use mirrored tangents.
    """
    n = len(values)
    if x <= 0.0:
        return values[0]
    if x >= n - 1:
        return values[-1]
    j = int(x)
    t = x - j
    if t < 1e-9:
        return values[j]
    if t > 1.0 - 1e-9:
        return values[j + 1]
    p1 = values[j]
    p2 = values[j + 1]
    p0 = values[j - 1] if j > 0 else 2.0 * p1 - p2
    p3 = values[j + 2] if j + 2 < n else 2.0 * p2 - p1
    return 0.5 * (
        2.0 * p1 +
        (-p0 + p2) * t +
        (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t * t +
        (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t * t * t
    )


def final_prop_station_chords(s_list, a, b, c, d, chord_tip_cm):
    """Station chords for the final prop, smoothed through the legacy layout.

    The legacy 30-station chords (cubic law, last full station pinned to
    ChordTip, final station FINAL_STATION_CHORD_PREV_FACTOR of that) define the
    manufactured blade, including the deliberate chord drop at the pinned tip
    station. Stations that land on legacy knots get exactly those values;
    intermediate stations from higher counts (59, 88, ...) take theirs from a
    C1 Catmull-Rom spline through the legacy values. Sampling the raw
    pinned/linear law instead puts slope corners into the LE/TE rail points
    near the tip, and the fitted rail splines turn those corners into a
    visible surface bump just inboard of the tip.
    """
    legacy_n = FINAL_PROP_LEGACY_TIP_STATIONS
    legacy_chords = [
        max(chord_cubic(s, a, b, c, d), 1e-6)
        for s in linspace(legacy_n)
    ]
    legacy_chords[-2] = max(chord_tip_cm, 1e-6)
    legacy_chords[-1] = max(
        legacy_chords[-2] * FINAL_STATION_CHORD_PREV_FACTOR,
        1e-6
    )
    return [
        max(catmull_rom_uniform(legacy_chords, s * (legacy_n - 1)), 1e-6)
        for s in s_list
    ]


# ---------- Thickness scaling near root ----------
def thickness_scale_at_s(s, root_scale, end_s, expn):
    end_s = max(1e-6, min(1.0, end_s))
    if s >= end_s:
        return 1.0
    t = s / end_s
    w = smoothstep(t)
    w = w ** max(1.0, expn)
    return (1.0 - w) * root_scale + w * 1.0


# ---------- Tip shaping helpers ----------
def tip_multiplier(s, start_s, end_mult, expn):
    start_s = max(0.0, min(0.999999, start_s))
    if s <= start_s:
        return 1.0
    t = (s - start_s) / (1.0 - start_s)
    w = smoothstep(t)
    w = w ** max(1.0, expn)
    return (1.0 - w) * 1.0 + w * end_mult


def tip_weight(s, start_s, expn):
    start_s = max(0.0, min(0.999999, start_s))
    if s <= start_s:
        return 0.0
    t = (s - start_s) / (1.0 - start_s)
    w = smoothstep(t)
    w = w ** max(1.0, expn)
    return w


def find_le_te_norm_points(airfoil_pts):
    xs = [p[0] for p in airfoil_pts]
    minx = min(xs)
    maxx = max(xs)

    le_candidates = [p for p in airfoil_pts if abs(p[0] - minx) <= X_TOL]
    te_candidates = [p for p in airfoil_pts if abs(p[0] - maxx) <= X_TOL]

    if not le_candidates:
        le_candidates = [min(airfoil_pts, key=lambda p: p[0])]
    if not te_candidates:
        te_candidates = [max(airfoil_pts, key=lambda p: p[0])]

    le_y = sum(p[1] for p in le_candidates) / len(le_candidates)
    te_y = sum(p[1] for p in te_candidates) / len(te_candidates)

    return (minx, le_y), (maxx, te_y)


def surface_y_at(curve, x):
    """Linearly interpolated y on a curve whose x values increase monotonically."""
    if x <= curve[0][0]:
        return curve[0][1]
    if x >= curve[-1][0]:
        return curve[-1][1]
    lo, hi = 0, len(curve) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if curve[mid][0] <= x:
            lo = mid
        else:
            hi = mid
    x0, y0 = curve[lo]
    x1, y1 = curve[hi]
    if x1 == x0:
        return y0
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def normalized_max_thickness(pts):
    """Max upper-minus-lower thickness of a normalized airfoil, in chord fractions."""
    if len(pts) < 3:
        return 0.0
    le_index = min(range(len(pts)), key=lambda i: pts[i][0])
    upper = list(reversed(pts[:le_index + 1]))
    lower = pts[le_index:]
    if len(upper) < 2 or len(lower) < 2:
        return 0.0
    return max(y - surface_y_at(lower, x) for x, y in upper)


def enforce_min_section_thickness(pts, chord_cm, t_scale):
    """Raise t_scale so the station's max thickness reaches MIN_SECTION_THICKNESS_CM.

    Returns t_scale unchanged for stations already at or above the floor, so only
    the thin outboard sections are affected (see MIN_SECTION_THICKNESS_CM).
    """
    if MIN_SECTION_THICKNESS_CM <= 0.0 or chord_cm <= 0.0:
        return t_scale
    tc_norm = normalized_max_thickness(pts)
    if tc_norm <= 1e-9:
        return t_scale
    return max(t_scale, MIN_SECTION_THICKNESS_CM / (chord_cm * tc_norm))


def enforce_min_te_gap(pts, chord_cm, t_scale):
    """Spread a station's TE endpoints so its physical TE gap >= MIN_TE_GAP_CM.

    Operates in normalized airfoil space, before LE/TE detection and the
    section transform, so the rails and closing line stay consistent. The
    per-station transform scales x by chord and y by chord * t_scale;
    rotations preserve length, so scaling the endpoint offsets about the gap
    midpoint by k scales the physical gap by exactly k. Only the two endpoint
    points move; stations already at or above the minimum are untouched.
    """
    if MIN_TE_GAP_CM <= 0.0 or len(pts) < 3:
        return pts
    x0, y0 = pts[0]
    xN, yN = pts[-1]
    gap_cm = math.hypot((xN - x0) * chord_cm, (yN - y0) * chord_cm * t_scale)
    if gap_cm >= MIN_TE_GAP_CM or gap_cm < 1e-12:
        return pts
    k = MIN_TE_GAP_CM / gap_cm
    mx, my = 0.5 * (x0 + xN), 0.5 * (y0 + yN)
    out = list(pts)
    out[0] = (mx + k * (x0 - mx), my + k * (y0 - my))
    out[-1] = (mx + k * (xN - mx), my + k * (yN - my))
    return out


def close_profile_if_needed(sketch, pts_coll):
    if pts_coll.count < 3:
        return
    p0 = pts_coll.item(0)
    pN = pts_coll.item(pts_coll.count - 1)
    if p0.distanceTo(pN) < 1e-7:
        # Endpoints coincide (welded TE); the fitted spline is already closed.
        return
    sketch.sketchCurves.sketchLines.addByTwoPoints(pN, p0)


def pick_primary_profile(sketch):
    if sketch.profiles.count == 0:
        return None
    best = sketch.profiles.item(0)
    best_area = abs(best.areaProperties().area)
    for i in range(1, sketch.profiles.count):
        p = sketch.profiles.item(i)
        a = abs(p.areaProperties().area)
        if a > best_area:
            best = p
            best_area = a
    return best


def make_3d_rail_sketch(comp, name):
    sk = comp.sketches.add(comp.xYConstructionPlane)
    sk.is3D = True
    sk.name = name
    return sk


def add_3d_fitted_spline(sketch3d, model_points):
    pts = adsk.core.ObjectCollection.create()
    for p in model_points:
        pts.add(p)
    return sketch3d.sketchCurves.sketchFittedSplines.add(pts)


# ---------- Batch / parameter helpers ----------
def script_dir():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except:
        return os.getcwd()


def safe_filename(name):
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '_', str(name).strip().lower())
    return cleaned or 'variant'


def final_prop_exports_dir_name(design):
    """Export folder name for this run, taken from the Fusion document name."""
    doc_name = ''
    try:
        doc = design.parentDocument
        if doc:
            doc_name = doc.name or ''
    except:
        doc_name = ''
    if not doc_name.strip():
        return FINAL_PROP_EXPORTS_DIR_FALLBACK
    return FINAL_PROP_EXPORTS_DIR_PREFIX + safe_filename(doc_name)


def is_angle_parameter(param):
    unit = (getattr(param, 'unit', '') or '').lower()
    return unit in ('deg', 'degree', 'degrees', 'rad', 'radian', 'radians') or param.name.startswith('Twist')


def is_unitless_parameter(param):
    unit = (getattr(param, 'unit', '') or '').lower()
    return unit in ('', 'unitless', 'ul')


def normalize_csv_expression(param, raw_value, variant_id):
    text = str(raw_value).strip()
    if text == '':
        return None

    # Expressions with units/formulas are delegated to Fusion so values like
    # "1.2 in", "3 cm", and "24 deg" keep Fusion's normal unit conversion.
    try:
        num = float(text)
    except:
        return text

    if is_angle_parameter(param):
        return f'{num} deg'
    if is_unitless_parameter(param):
        return str(num)

    # Fusion length parameter .value uses internal centimeters; raw CSV length
    # numbers follow that same convention. Explicit units override this.
    return f'{num} cm'


def validate_parameter_expression(design, param, expression, variant_id):
    units_mgr = design.unitsManager
    try:
        if not units_mgr.isValidExpression(expression, param.unit):
            raise ValueError()
    except Exception as exc:
        label = f'Variant "{variant_id}" ' if variant_id else ''
        raise ValueError(
            f'{label}parameter "{param.name}" could not be parsed: {expression}'
        ) from exc


def recompute_design(design, variant_id=None):
    try:
        design.computeAll()
    except Exception as exc:
        label = f'Variant "{variant_id}" ' if variant_id else ''
        raise RuntimeError(f'{label}failed while recomputing the Fusion design.') from exc
    try:
        adsk.doEvents()
    except:
        pass


def read_current_parameters(design, names=None):
    if names is None:
        names = required_parameter_names()
    params, missing = require_params(design, names)
    if missing:
        raise ValueError('Missing parameters:\n- ' + '\n- '.join(missing))

    data = {}
    for name in names:
        p = params[name]
        data[name] = {
            'expression': p.expression,
            'value': p.value,
            'unit': p.unit
        }
    return params, data


def snapshot_user_parameter_expressions(design):
    return {p.name: p.expression for p in design.userParameters}


def restore_user_parameter_expressions(design, expressions, variant_id=None):
    for name, expression in expressions.items():
        param = get_param(design, name)
        if not param:
            continue
        try:
            param.expression = expression
        except Exception as exc:
            raise RuntimeError(
                parameter_error('could not restore baseline expression.', name, variant_id)
            ) from exc
    recompute_design(design, variant_id)


def force_fixed_radius(design, variant_id=None):
    radius_param = get_param(design, 'Radius')
    if not radius_param:
        raise ValueError(parameter_error('missing required Fusion parameter.', 'Radius', variant_id))
    try:
        validate_parameter_expression(design, radius_param, FIXED_RADIUS_EXPR, variant_id)
        radius_param.expression = FIXED_RADIUS_EXPR
    except Exception as exc:
        raise ValueError(
            parameter_error(
                f'could not be forced to fixed radius expression "{FIXED_RADIUS_EXPR}".',
                'Radius',
                variant_id
            )
        ) from exc
    recompute_design(design, variant_id)


def force_batch_station_count(design, variant_id=None):
    station_count = int(BATCH_STATION_COUNT)
    if station_count < 2:
        raise ValueError('BATCH_STATION_COUNT must be at least 2.')

    station_param = get_param(design, 'nStations')
    if not station_param:
        raise ValueError(parameter_error('missing required Fusion parameter.', 'nStations', variant_id))

    expression = str(station_count)
    try:
        validate_parameter_expression(design, station_param, expression, variant_id)
        station_param.expression = expression
    except Exception as exc:
        raise ValueError(
            parameter_error(
                f'could not be forced to batch station count "{expression}".',
                'nStations',
                variant_id
            )
        ) from exc
    recompute_design(design, variant_id)


def update_fusion_parameters_from_dict(design, values, variant_id):
    updated = {}
    for name, raw_value in values.items():
        if name is None:
            continue
        if name == 'variant_id' or str(raw_value).strip() == '':
            continue
        if name in IGNORED_CSV_PARAMETER_NAMES:
            continue
        if name == 'Radius':
            # Radius is controlled by FIXED_RADIUS_EXPR, not by the sweep CSV.
            continue

        param = get_param(design, name)
        if not param:
            raise ValueError(
                f'Variant "{variant_id}" references missing Fusion parameter "{name}".'
            )

        expression = normalize_csv_expression(param, raw_value, variant_id)
        if expression is None:
            continue

        try:
            validate_parameter_expression(design, param, expression, variant_id)
            param.expression = expression
            updated[name] = expression
        except Exception as exc:
            raise ValueError(
                f'Variant "{variant_id}" parameter "{name}" could not be assigned from CSV value "{raw_value}" '
                f'as Fusion expression "{expression}".'
            ) from exc
    recompute_design(design, variant_id)
    return updated


def read_variants_csv(path):
    with open(path, 'r', newline='') as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError('variants.csv is empty.')
    if 'variant_id' not in (rows[0].keys() if rows else []):
        raise ValueError('variants.csv must include a required "variant_id" column.')
    return rows


def clear_previous_batch_exports(export_dir, variants):
    if not CLEAR_PREVIOUS_BATCH_EXPORTS:
        return []

    deleted = []
    targets = set()
    for row in variants:
        variant_id = str(row.get('variant_id', '')).strip()
        if not variant_id:
            continue
        base_name = safe_filename(variant_id)
        targets.add(base_name + '.stl')
        targets.add(base_name + '.step')
        targets.add(base_name + '.json')

    targets.add('airfoil_blend_debug.csv')

    for filename in sorted(targets):
        path = os.path.join(export_dir, filename)
        if not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            deleted.append(path)
        except Exception as exc:
            raise RuntimeError(f'Could not delete previous batch export "{path}": {exc}') from exc
    return deleted


def write_metadata_json(
    path,
    variant_id,
    parameter_data,
    csv_values,
    twist_metadata=None,
    airfoil_info=None,
    geometry_info=None
):
    metadata = {
        'variant_id': variant_id,
        'TWIST_MODE': TWIST_MODE,
        'parameters': parameter_data,
        'csv_values': csv_values
    }
    if twist_metadata:
        metadata.update(twist_metadata)
    if airfoil_info:
        metadata['airfoils'] = airfoil_info
    if geometry_info:
        metadata['geometry'] = geometry_info
    with open(path, 'w') as f:
        json.dump(metadata, f, indent=2, sort_keys=True)


def find_blade_body(comp, variant_id=None):
    preferred_names = []
    if variant_id:
        preferred_names.append(generated_prop_body_name(variant_id))
        preferred_names.append(generated_blade_body_name(variant_id))
    preferred_names.append(GENERATED_PROP_BASE_NAME)
    preferred_names.append(GENERATED_BLADE_BASE_NAME)
    preferred_names.append('BladeBody')

    for preferred_name in preferred_names:
        for i in range(comp.bRepBodies.count):
            body = comp.bRepBodies.item(i)
            if body.name == preferred_name:
                return body

    for i in range(comp.bRepBodies.count):
        body = comp.bRepBodies.item(i)
        if is_generated_body_name(body.name):
            return body

    for i in range(comp.features.loftFeatures.count):
        feat = comp.features.loftFeatures.item(i)
        if feat.name.startswith('BladeLoft') or feat.name.startswith(GENERATED_BLADE_BASE_NAME):
            for j in range(feat.bodies.count):
                return feat.bodies.item(j)
    return None


def make_hub_body(comp):
    outer_radius_cm = 0.5 * mm_to_cm(HUB_OUTER_DIAMETER_MM)
    hole_radius_cm = 0.5 * mm_to_cm(M5_CLEARANCE_HOLE_DIAMETER_MM)
    thickness_cm = mm_to_cm(HUB_THICKNESS_MM)

    planes = comp.constructionPlanes
    pinput = planes.createInput()
    pinput.setByOffset(comp.xYConstructionPlane, adsk.core.ValueInput.createByReal(-0.5 * thickness_cm))
    plane = planes.add(pinput)
    plane.name = 'Plane_Hub_Start'

    sk = comp.sketches.add(plane)
    sk.name = 'Hub_M5_Clearance'
    center = adsk.core.Point3D.create(0.0, 0.0, 0.0)
    sk.sketchCurves.sketchCircles.addByCenterRadius(center, outer_radius_cm)
    sk.sketchCurves.sketchCircles.addByCenterRadius(center, hole_radius_cm)

    if sk.profiles.count == 0:
        raise RuntimeError('Hub sketch did not create a valid annular profile.')

    hub_profile = pick_primary_profile(sk)
    ext_input = comp.features.extrudeFeatures.createInput(
        hub_profile,
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation
    )
    ext_input.setDistanceExtent(False, adsk.core.ValueInput.createByReal(thickness_cm))
    ext = comp.features.extrudeFeatures.add(ext_input)
    ext.name = GENERATED_HUB_BASE_NAME

    if ext.bodies.count == 0:
        raise RuntimeError('Hub extrusion did not create a body.')
    hub_body = ext.bodies.item(0)
    hub_body.name = GENERATED_HUB_BASE_NAME
    return hub_body


def delete_existing_export_file(path, variant_id=None):
    if not os.path.isfile(path):
        return False
    try:
        os.remove(path)
        return True
    except Exception as exc:
        label = f'Variant "{variant_id}" ' if variant_id else ''
        raise RuntimeError(f'{label}could not overwrite existing export "{path}": {exc}') from exc


def make_blade_pattern(comp, blade_body):
    blade_count = active_blade_count()
    bodies = [blade_body]
    if blade_count <= 1:
        return bodies

    source_name = blade_body.name or GENERATED_BLADE_BASE_NAME
    entities = adsk.core.ObjectCollection.create()
    entities.add(blade_body)
    pattern_input = comp.features.circularPatternFeatures.createInput(
        entities,
        comp.zConstructionAxis
    )
    pattern_input.quantity = adsk.core.ValueInput.createByReal(blade_count)
    pattern_input.totalAngle = adsk.core.ValueInput.createByString('360 deg')

    pattern = comp.features.circularPatternFeatures.add(pattern_input)
    pattern.name = source_name + '_CircularPattern'

    try:
        for i in range(pattern.bodies.count):
            body = pattern.bodies.item(i)
            if body and body.isValid and body not in bodies:
                body.name = f'{source_name}_{len(bodies) + 1}'
                bodies.append(body)
    except:
        pass
    return bodies


def join_tools_into_target(comp, target_body, tool_bodies, feature_name):
    """Join tool_bodies into target_body with a single combine feature.

    Returns True on success. Raises on failure so callers can fall back.
    """
    tools = adsk.core.ObjectCollection.create()
    for body in tool_bodies:
        if body and body.isValid and body != target_body:
            tools.add(body)
    if tools.count == 0:
        return False

    combine_input = comp.features.combineFeatures.createInput(target_body, tools)
    combine_input.operation = adsk.fusion.FeatureOperations.JoinFeatureOperation
    combine_input.isKeepToolBodies = False
    combine = comp.features.combineFeatures.add(combine_input)
    combine.name = feature_name
    return True


def combine_generated_prop(comp, hub_body, blade_bodies):
    if not hub_body or not hub_body.isValid:
        return None

    tools = [b for b in blade_bodies if b and b.isValid and b != hub_body]
    if not tools:
        hub_body.name = GENERATED_PROP_BASE_NAME
        return hub_body

    # First try one combine with every tool. Fusion rejects the entire feature
    # if any single tool body produces an invalid result, which previously left
    # the whole prop as separate, un-oriented bodies. If the all-at-once join
    # fails, fall back to joining tools individually so one bad body cannot
    # break the assembly.
    try:
        if join_tools_into_target(comp, hub_body, tools, GENERATED_PROP_BASE_NAME + '_Join'):
            hub_body.name = GENERATED_PROP_BASE_NAME
            return hub_body
    except:
        pass

    joined_any = False
    for index, tool in enumerate(tools):
        if not tool or not tool.isValid:
            continue
        try:
            if join_tools_into_target(comp, hub_body, [tool], f'{GENERATED_PROP_BASE_NAME}_Join_{index + 1}'):
                joined_any = True
                try:
                    adsk.doEvents()
                except:
                    pass
        except:
            # Skip this tool rather than aborting; the rest of the prop still
            # combines into one body.
            continue

    if not (hub_body and hub_body.isValid):
        return None
    hub_body.name = GENERATED_PROP_BASE_NAME
    return hub_body if joined_any else None


def collect_orientable_prop_bodies(comp, body=None):
    """All generated prop bodies that should move/rotate together.

    The final orientation/placement must transform every generated body, not
    just the primary one. If the combine could not merge everything into a
    single body, the blades and hub still need to rotate and
    drop to the XY plane as one rigid group so the prop stays consistent.
    """
    bodies = []
    if comp:
        for i in range(comp.bRepBodies.count):
            candidate = comp.bRepBodies.item(i)
            if candidate and candidate.isValid and is_generated_body_name(candidate.name):
                bodies.append(candidate)
    if body and body.isValid and body not in bodies:
        bodies.append(body)
    return bodies


def rotate_body_about_x_axis(comp, body):
    if abs(FINAL_PROP_ROTATE_ABOUT_X_DEG) < 1e-9:
        return 'Orientation: no X-axis rotation requested.'

    bodies = collect_orientable_prop_bodies(comp, body)
    if not bodies:
        return 'Orientation: skipped because no valid prop bodies were found.'

    entities = adsk.core.ObjectCollection.create()
    for b in bodies:
        entities.add(b)
    transform = adsk.core.Matrix3D.create()
    transform.setToRotation(
        math.radians(FINAL_PROP_ROTATE_ABOUT_X_DEG),
        adsk.core.Vector3D.create(1.0, 0.0, 0.0),
        adsk.core.Point3D.create(0.0, 0.0, 0.0)
    )
    move_input = comp.features.moveFeatures.createInput(entities, transform)
    move = comp.features.moveFeatures.add(move_input)
    move.name = 'Final_Rotate_About_X'
    return f'Orientation: rotated {len(bodies)} body/bodies {FINAL_PROP_ROTATE_ABOUT_X_DEG:.1f} deg about the X axis.'


def move_body_bottom_to_xy_plane(comp, body):
    if not FINAL_PROP_BOTTOM_ON_XY_PLANE:
        return 'Placement: bottom-on-XY-plane disabled.'

    bodies = collect_orientable_prop_bodies(comp, body)
    if not bodies:
        return 'Placement: skipped because no valid prop bodies were found.'

    # Use the lowest point across every body so the whole group lands on the XY
    # plane together, not just the primary body.
    min_z_cm = min(b.boundingBox.minPoint.z for b in bodies)
    dz_cm = -min_z_cm
    if abs(dz_cm) < 1e-6:
        return 'Placement: prop bottom already on XY plane.'

    entities = adsk.core.ObjectCollection.create()
    for b in bodies:
        entities.add(b)
    transform = adsk.core.Matrix3D.create()
    transform.translation = adsk.core.Vector3D.create(0.0, 0.0, dz_cm)
    move_input = comp.features.moveFeatures.createInput(entities, transform)
    move = comp.features.moveFeatures.add(move_input)
    move.name = 'Final_Move_Bottom_To_XY'
    return f'Placement: moved {len(bodies)} body/bodies {dz_cm:.4f} cm in Z so the lowest point is on the XY plane.'


def mirror_prop_to_ccw(comp, body, variant_id=None):
    """Mirror the (clockwise) prop into its counter-clockwise counterpart.

    A CCW propeller is the mirror image of the CW one. We reflect across the YZ
    construction plane (x = 0), which contains the prop's spin axis after the
    final orientation, so the mirrored prop keeps the same placement (still flat
    on the XY plane) and only its handedness flips. The original CW body is left
    intact; the new CCW body is returned for its own STEP export.
    """
    if not comp:
        return None, 'CCW mirror: skipped because the component is unavailable.'
    if not body or not body.isValid:
        return None, 'CCW mirror: skipped because the prop body is not valid.'

    try:
        entities = adsk.core.ObjectCollection.create()
        entities.add(body)
        mirror_input = comp.features.mirrorFeatures.createInput(
            entities, comp.yZConstructionPlane
        )
        mirror = comp.features.mirrorFeatures.add(mirror_input)
        mirror.name = 'Final_CCW_Mirror'
        ccw_body = None
        if mirror.bodies.count > 0:
            ccw_body = mirror.bodies.item(0)
            ccw_body.name = generated_prop_body_name(variant_id) + '_ccw'
        if ccw_body and ccw_body.isValid:
            return ccw_body, 'CCW mirror: created counter-clockwise prop body.'
        return None, 'CCW mirror: mirror feature produced no usable body.'
    except Exception as exc:
        return None, f'CCW mirror: FAILED ({exc}).'


def export_step_isolated_body(design, comp, keep_body, path, variant_id=None):
    """Export a single body to STEP when it shares its component with others.

    Fusion's STEP export only accepts a Component, and a "Part" document is
    capped at one component, so a lone body cannot be split into its own
    component. Instead every other generated solid body is hidden behind a
    parametric Remove feature so the whole-component export contains only
    keep_body, and the Remove features are deleted afterward to restore the
    document unchanged. Each Remove is added after the CCW mirror in the
    timeline, so the mirror's input body still exists at the mirror's own
    timeline position and the mirrored geometry stays valid.
    """
    label = f'Variant "{variant_id}" ' if variant_id else ''
    if not comp:
        raise RuntimeError(f'{label}isolated STEP export requires a component.')
    keep_name = keep_body.name

    root = design.rootComponent
    to_remove = []
    for i in range(root.bRepBodies.count):
        b = root.bRepBodies.item(i)
        if b.name == keep_name:
            continue
        if b.isValid and b.isSolid:
            to_remove.append(b)

    remove_feats = []
    exported = False
    try:
        for b in to_remove:
            try:
                rf = comp.features.removeFeatures.add(b)
                if rf:
                    remove_feats.append(rf)
            except:
                # If a body cannot be removed, skip it; worst case the export
                # contains an extra body, which is caught when the file is checked.
                pass
        try:
            adsk.doEvents()
        except:
            pass
        export_mgr = design.exportManager
        opts = export_mgr.createSTEPExportOptions(path, comp)
        exported = export_mgr.execute(opts)
    finally:
        # Restore the document: deleting a Remove feature brings its body back.
        for rf in reversed(remove_feats):
            try:
                rf.deleteMe()
            except:
                pass
        try:
            recompute_design(design, variant_id)
        except:
            pass
    if not exported:
        raise RuntimeError(f'{label}Fusion STEP export failed: {path}')


def export_step(design, body, path, variant_id=None, comp=None, isolate=False):
    """Export the generated geometry to a STEP (B-rep) file.

    STEP is a boundary-representation format, so unlike the previous STL path
    there is no mesh refinement: the exact lofted geometry is written as-is.

    Normal case: the root component contains only the intended prop, so the
    whole component is exported directly. The CCW prop, however, shares the
    component with the CW prop, so isolate=True routes it through
    export_step_isolated_body, which temporarily removes the other bodies so
    only the CCW prop is written.
    """
    try:
        adsk.doEvents()
    except:
        pass
    delete_existing_export_file(path, variant_id)

    if (not body or not body.isValid) and comp:
        body = find_blade_body(comp, variant_id)
    if not body or not body.isValid:
        label = f'Variant "{variant_id}" ' if variant_id else ''
        raise RuntimeError(f'{label}generated blade body is not valid for STEP export.')

    if not isolate and comp:
        # The component holds only this prop (the CW STEP is written before the
        # CCW mirror exists), so a direct whole-component export is exact.
        export_mgr = design.exportManager
        opts = export_mgr.createSTEPExportOptions(path, comp)
        if not export_mgr.execute(opts):
            label = f'Variant "{variant_id}" ' if variant_id else ''
            raise RuntimeError(f'{label}Fusion STEP export failed: {path}')
        return

    export_step_isolated_body(design, comp, body, path, variant_id)


def export_stl(design, body, path, variant_id=None, comp=None, force_component_export=False):
    """Export generated batch geometry to STL for CFD meshing."""
    try:
        adsk.doEvents()
    except:
        pass
    delete_existing_export_file(path, variant_id)

    export_mgr = design.exportManager
    if force_component_export and comp:
        opts = export_mgr.createSTLExportOptions(comp, path)
        try:
            opts.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementMedium
        except:
            pass
        if not export_mgr.execute(opts):
            label = f'Variant "{variant_id}" ' if variant_id else ''
            raise RuntimeError(f'{label}Fusion STL export failed: {path}')
        return

    if (not body or not body.isValid) and comp:
        body = find_blade_body(comp, variant_id)
    if not body or not body.isValid:
        label = f'Variant "{variant_id}" ' if variant_id else ''
        raise RuntimeError(f'{label}generated blade body is not valid for STL export.')

    opts = export_mgr.createSTLExportOptions(body, path)
    try:
        opts.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementMedium
    except:
        pass
    if not export_mgr.execute(opts):
        label = f'Variant "{variant_id}" ' if variant_id else ''
        raise RuntimeError(f'{label}Fusion STL export failed: {path}')


def spinner_unit_scale_to_mm():
    units = str(SPINNER_STL_UNITS).strip().lower()
    if units in ('mm', 'millimeter', 'millimeters'):
        return 1.0
    if units in ('cm', 'centimeter', 'centimeters'):
        return 10.0
    if units in ('in', 'inch', 'inches'):
        return 25.4
    raise ValueError(
        f'Unsupported SPINNER_STL_UNITS "{SPINNER_STL_UNITS}". Use "mm", "cm", or "in".'
    )


def resolve_configured_spinner_path():
    configured = str(SPINNER_STL_PATH or '').strip()
    if not configured:
        return None
    if os.path.isabs(configured):
        return configured if os.path.isfile(configured) else None
    candidate = os.path.join(script_dir(), configured)
    if os.path.isfile(candidate):
        return candidate
    return configured if os.path.isfile(configured) else None


def select_spinner_stl(ui):
    if not BATCH_ATTACH_SPINNER:
        return None

    configured_path = resolve_configured_spinner_path()
    if configured_path:
        return configured_path

    dlg = ui.createFileDialog()
    dlg.title = 'Select spinner STL to merge into batch prop STLs'
    dlg.filter = 'STL files (*.stl)'
    try:
        dlg.initialDirectory = script_dir()
    except:
        pass
    if dlg.showOpen() != adsk.core.DialogResults.DialogOK:
        raise RuntimeError('Batch spinner merge is enabled, but no spinner STL was selected.')
    return dlg.filename


def stl_data_is_binary(data):
    if len(data) < 84:
        return False
    tri_count = struct.unpack('<I', data[80:84])[0]
    return 84 + tri_count * 50 == len(data)


def read_stl_triangles(path):
    with open(path, 'rb') as f:
        data = f.read()

    triangles = []
    if stl_data_is_binary(data):
        tri_count = struct.unpack('<I', data[80:84])[0]
        offset = 84
        for _idx in range(tri_count):
            values = struct.unpack('<12fH', data[offset:offset + 50])[:12]
            triangles.append((
                (values[3], values[4], values[5]),
                (values[6], values[7], values[8]),
                (values[9], values[10], values[11])
            ))
            offset += 50
        return triangles

    text = data.decode('utf-8', errors='ignore')
    vertices = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 4 and parts[0].lower() == 'vertex':
            vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            if len(vertices) == 3:
                triangles.append(tuple(vertices))
                vertices = []
    if not triangles:
        raise ValueError(f'STL file has no readable triangles: {path}')
    return triangles


def triangle_bbox(triangles):
    mins = [float('inf'), float('inf'), float('inf')]
    maxs = [float('-inf'), float('-inf'), float('-inf')]
    for tri in triangles:
        for vertex in tri:
            for axis in range(3):
                mins[axis] = min(mins[axis], vertex[axis])
                maxs[axis] = max(maxs[axis], vertex[axis])
    return tuple(mins), tuple(maxs)


def spinner_axis_index():
    axis = str(SPINNER_SOURCE_AXIS).strip().lower()
    axes = {'x': 0, 'y': 1, 'z': 2}
    if axis not in axes:
        raise ValueError(
            f'Unsupported SPINNER_SOURCE_AXIS "{SPINNER_SOURCE_AXIS}". Use "X", "Y", or "Z".'
        )
    return axes[axis]


def spinner_bottom_sign():
    bottom = str(SPINNER_SOURCE_BOTTOM).strip().lower()
    if bottom == 'min':
        return 1.0
    if bottom == 'max':
        return -1.0
    raise ValueError(
        f'Unsupported SPINNER_SOURCE_BOTTOM "{SPINNER_SOURCE_BOTTOM}". Use "min" or "max".'
    )


def orient_spinner_triangles_to_prop_axes(triangles):
    axis_idx = spinner_axis_index()
    bottom_sign = spinner_bottom_sign()
    radial_indices = [idx for idx in range(3) if idx != axis_idx]

    return [
        tuple((
            vertex[radial_indices[0]],
            vertex[radial_indices[1]],
            bottom_sign * vertex[axis_idx]
        ) for vertex in tri)
        for tri in triangles
    ]


def transformed_spinner_triangles(spinner_triangles):
    scale = spinner_unit_scale_to_mm()
    scaled = [
        tuple((v[0] * scale, v[1] * scale, v[2] * scale) for v in tri)
        for tri in spinner_triangles
    ]
    oriented = orient_spinner_triangles_to_prop_axes(scaled)
    mins, maxs = triangle_bbox(oriented)

    dx = SPINNER_OFFSET_X_MM
    dy = SPINNER_OFFSET_Y_MM
    dz = SPINNER_OFFSET_Z_MM

    if SPINNER_CENTER_ON_PROP_AXIS:
        dx += -0.5 * (mins[0] + maxs[0])
        dy += -0.5 * (mins[1] + maxs[1])
    if SPINNER_BOTTOM_ON_HUB_BOTTOM:
        hub_bottom_z_mm = -0.5 * HUB_THICKNESS_MM
        dz += hub_bottom_z_mm - mins[2]

    transformed = [
        tuple((v[0] + dx, v[1] + dy, v[2] + dz) for v in tri)
        for tri in oriented
    ]
    return transformed, {
        'source_bbox_min_mm': mins,
        'source_bbox_max_mm': maxs,
        'translation_mm': (dx, dy, dz)
    }


def normal_for_triangle(triangle):
    a, b, c = triangle
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length <= 1e-12:
        return (0.0, 0.0, 0.0)
    return (nx / length, ny / length, nz / length)


def write_binary_stl(path, triangles, header_text):
    header = str(header_text or 'WFD prop with spinner').encode('ascii', errors='ignore')[:80]
    header = header + b' ' * (80 - len(header))
    with open(path, 'wb') as f:
        f.write(header)
        f.write(struct.pack('<I', len(triangles)))
        for triangle in triangles:
            normal = normal_for_triangle(triangle)
            packed = normal + triangle[0] + triangle[1] + triangle[2]
            f.write(struct.pack('<12fH', *(packed + (0,))))


def merge_spinner_into_stl(prop_stl_path, spinner_stl_path, variant_id=None):
    if not spinner_stl_path:
        return None
    label = f'Variant "{variant_id}" ' if variant_id else ''
    if not os.path.isfile(spinner_stl_path):
        raise RuntimeError(f'{label}spinner STL does not exist: {spinner_stl_path}')

    prop_triangles = read_stl_triangles(prop_stl_path)
    spinner_triangles = read_stl_triangles(spinner_stl_path)
    transformed_spinner, transform_info = transformed_spinner_triangles(spinner_triangles)
    combined = prop_triangles + transformed_spinner
    write_binary_stl(
        prop_stl_path,
        combined,
        f'WFD prop + spinner {safe_filename(variant_id or "variant")}'
    )
    translation = transform_info['translation_mm']
    return (
        f'Spinner merge: added {len(transformed_spinner)} triangle(s), '
        f'translated ({translation[0]:.3f}, {translation[1]:.3f}, {translation[2]:.3f}) mm.'
    )


def show_progress(ui, message):
    try:
        ui.palettes.itemById('TextCommands').writeText(message)
    except:
        pass


def log_progress(message):
    """Progress line in the Text Commands palette from anywhere in the build.

    The long final-prop rebuild otherwise gives no feedback until the exports
    are written at the very end. Open View > Show Text Commands to watch it.
    """
    try:
        app = adsk.core.Application.get()
        app.userInterface.palettes.itemById('TextCommands').writeText(message)
        adsk.doEvents()
    except:
        pass


def parameter_error(message, parameter_name=None, variant_id=None):
    pieces = []
    if variant_id:
        pieces.append(f'Variant "{variant_id}"')
    if parameter_name:
        pieces.append(f'parameter "{parameter_name}"')
    if pieces:
        return ' '.join(pieces) + f': {message}'
    return message


def twist_deg_at_station(s, x_cm, params, variant_id=None):
    if TWIST_MODE == "linear":
        twist_root_deg = math.degrees(params['TwistRoot'].value)
        twist_tip_deg = math.degrees(params['TwistTip'].value)
        return twist_root_deg + (twist_tip_deg - twist_root_deg) * s

    if TWIST_MODE == "pitch_based":
        if x_cm <= MIN_TWIST_RADIUS_CM:
            raise ValueError(
                parameter_error(
                    'local radius is too close to zero for pitch-based twist.',
                    'HubR',
                    variant_id
                )
            )

        # GeometricPitch is an axial advance distance per revolution. Fusion
        # stores this length parameter in centimeters internally, matching x_cm.
        # The local blade angle beta is in degrees. For constant pitch, beta is
        # large near the hub and decreases toward the tip because each revolution
        # travels a larger circumference at larger radius.
        pitch_cm = params['GeometricPitch'].value
        if pitch_cm <= 0:
            raise ValueError(parameter_error('must be > 0.', 'GeometricPitch', variant_id))
        beta_rad = math.atan(pitch_cm / (2.0 * math.pi * x_cm))
        return math.degrees(beta_rad)

    raise ValueError(f'Unsupported TWIST_MODE "{TWIST_MODE}". Use "linear" or "pitch_based".')


def pitch_based_metadata(params, radius_cm, hub_radius_cm, variant_id=None):
    metadata = {'TWIST_MODE': TWIST_MODE}
    if TWIST_MODE != "pitch_based":
        return metadata

    pitch_cm = params['GeometricPitch'].value
    diameter_cm = 2.0 * radius_cm
    metadata['GeometricPitch'] = {
        'expression': params['GeometricPitch'].expression,
        'value_cm': pitch_cm,
        'unit': params['GeometricPitch'].unit
    }
    metadata['pitch_to_diameter_ratio'] = pitch_cm / diameter_cm if diameter_cm > 0 else None
    metadata['root_blade_angle_deg'] = twist_deg_at_station(0.0, hub_radius_cm, params, variant_id)
    metadata['tip_blade_angle_deg'] = twist_deg_at_station(1.0, radius_cm, params, variant_id)
    return metadata


def generate_blade_geometry(design, airfoil_set, variant_id=None):
    blade_count = active_blade_count()
    params, _ = read_current_parameters(design)
    root_airfoil = airfoil_set['root_pts']
    mid_airfoil = airfoil_set['mid_pts']
    tip_airfoil = airfoil_set['tip_pts']

    # LENGTHS: Fusion .value is cm
    Radius_cm = params['Radius'].value
    HubR_param_cm = params['HubR'].value
    HubR_cm = HubR_param_cm - inch_to_cm(ROOT_INBOARD_EXTENSION_IN)

    ChordRoot_cm = params['ChordRoot'].value
    ChordPeak_cm = params['ChordPeak'].value
    ChordTip_cm = params['ChordTip'].value

    TipTEOffset_cm = params['TipTEOffset'].value

    # UNITLESS
    s_peak = float(params['ChordPeakS'].value)

    ThickRootScale = float(params['ThickRootScale'].value)
    ThickEndS = float(params['ThickEndS'].value)
    ThickExp = float(params['ThickExp'].value)

    TipShapeStartS = float(params['TipShapeStartS'].value)
    TipShapeExp = float(params['TipShapeExp'].value)
    TipThickScale = float(params['TipThickScale'].value)

    TipTEOffsetStartS = float(params['TipTEOffsetStartS'].value)
    TipTEOffsetExp = float(params['TipTEOffsetExp'].value)

    # COUNTS
    nStations = int(round(params['nStations'].value))
    if nStations < 2:
        nStations = 2
    requested_nStations = nStations
    if is_fast_single_run():
        nStations = min(nStations, max(3, int(FAST_SINGLE_RUN_MAX_STATIONS)))

    Span_cm = Radius_cm - HubR_cm
    if Span_cm <= 0:
        raise ValueError(
            parameter_error(
                f'Radius must be greater than HubR minus ROOT_INBOARD_EXTENSION_IN ({ROOT_INBOARD_EXTENSION_IN} in).',
                'Radius',
                variant_id
            )
        )
    if TWIST_MODE == "pitch_based":
        if HubR_cm <= MIN_TWIST_RADIUS_CM:
            raise ValueError(
                parameter_error(
                    f'minus ROOT_INBOARD_EXTENSION_IN ({ROOT_INBOARD_EXTENSION_IN} in) must stay greater than zero for pitch-based twist.',
                    'HubR',
                    variant_id
                )
            )

    # Validation (minimal)
    if not (0.0 < s_peak < 1.0):
        raise ValueError(parameter_error('must be between 0 and 1.', 'ChordPeakS', variant_id))
    if not (0.0 < FINAL_STATION_CHORD_PREV_FACTOR <= 1.0):
        raise ValueError('FINAL_STATION_CHORD_PREV_FACTOR must be > 0 and <= 1.')
    if TipTEOffset_cm < 0:
        raise ValueError(parameter_error('must be >= 0.', 'TipTEOffset', variant_id))

    # Cubic chord law (cm)
    a, b, c, d = cubic_coeffs_with_peak(ChordRoot_cm, ChordPeak_cm, ChordTip_cm, s_peak)

    # Component
    comp = ensure_component(design, 'AirfoilSections')
    delete_old(comp)

    # Planes along +X using YZ base plane
    base_plane = comp.yZConstructionPlane
    planes = comp.constructionPlanes
    pinput = planes.createInput()

    # Base rotation in sketch plane
    base_theta = math.radians(BASE_ROT_DEG)
    cb, sb = math.cos(base_theta), math.sin(base_theta)

    section_profiles = []
    le_model_points = []
    te_model_points = []

    s_list = station_span_values(nStations, variant_id)
    if final_prop_tip_closure_start_s(nStations, variant_id) is not None:
        # Final prop: chords come from a smooth spline through the legacy
        # 30-station values (see final_prop_station_chords), so stations on
        # legacy knots reproduce the manufactured blade exactly and extra
        # stations never introduce slope corners near the tip.
        chord_by_station = final_prop_station_chords(s_list, a, b, c, d, ChordTip_cm)
    else:
        chord_by_station = [max(chord_cubic(s, a, b, c, d), 1e-6) for s in s_list]
        if len(chord_by_station) >= 2:
            # ChordTip is the chord of the last full station: pin the
            # second-to-last station to ChordTip, then taper the final station
            # to FINAL_STATION_CHORD_PREV_FACTOR of it to close the tip.
            chord_by_station[-2] = max(ChordTip_cm, 1e-6)
            chord_by_station[-1] = max(
                chord_by_station[-2] * FINAL_STATION_CHORD_PREV_FACTOR,
                1e-6
            )

    for i, s in enumerate(s_list):
        log_progress(f'Blade section {i + 1}/{len(s_list)} (s={s:.3f})')
        station_airfoil = airfoil_for_station(s, root_airfoil, mid_airfoil, tip_airfoil)

        # ABSOLUTE placement: root at HubR, tip at Radius
        x_cm = HubR_cm + (Radius_cm - HubR_cm) * s

        # Chord path is intentionally simple:
        # - stations up to the third-to-last follow only the cubic
        #   ChordRoot/ChordPeak/ChordTip law
        # - the second-to-last station is pinned to ChordTip
        # - the final station is FINAL_STATION_CHORD_PREV_FACTOR of ChordTip
        Chord_cm = chord_by_station[i]

        # Root thickness scaling * tip thinning
        t_root = thickness_scale_at_s(s, ThickRootScale, ThickEndS, ThickExp)
        t_tip = tip_multiplier(s, TipShapeStartS, TipThickScale, TipShapeExp)
        t_scale = t_root * t_tip

        # Manufacturing floor: outboard stations are too thin to cut, so fatten
        # only those below MIN_SECTION_THICKNESS_CM. Chord is untouched.
        t_scale = enforce_min_section_thickness(station_airfoil, Chord_cm, t_scale)

        # Keep the blunt-TE strip wide enough for CAM import (see MIN_TE_GAP_CM),
        # then detect LE/TE on the adjusted points so the rails stay on-curve.
        station_airfoil = enforce_min_te_gap(station_airfoil, Chord_cm, t_scale)
        le_norm, te_norm = find_le_te_norm_points(station_airfoil)

        # Twist:
        # - linear mode interpolates blade angle in degrees between TwistRoot and TwistTip.
        # - pitch_based mode uses geometric pitch length to compute local blade angle:
        #   beta(r)=atan(P/(2*pi*r)), with P and r both in centimeters.
        twist_deg = twist_deg_at_station(s, x_cm, params, variant_id)
        twist_theta = -math.radians(twist_deg) if INVERT_TWIST else math.radians(twist_deg)
        ct, st = math.cos(twist_theta), math.sin(twist_theta)

        # Create plane at x_cm
        pinput.setByOffset(base_plane, adsk.core.ValueInput.createByReal(x_cm))
        plane = planes.add(pinput)
        plane.name = f'Plane_s={s:.3f}'

        # Sketch on plane
        sk = comp.sketches.add(plane)
        sk.name = f'AF_s={s:.3f}'

        # Center profile to origin (bbox center in sketch space)
        if CENTER_TO_ORIGIN:
            xs = [x * Chord_cm for (x, y) in station_airfoil]
            ys = [(y * Chord_cm) * t_scale for (x, y) in station_airfoil]
            cx0 = 0.5 * (min(xs) + max(xs))
            cy0 = 0.5 * (min(ys) + max(ys))
        else:
            cx0 = cy0 = 0.0

        # --- Tip TE-offset: compute chord direction (LE->TE) in sketch coords, then offset ---
        w_off = tip_weight(s, TipTEOffsetStartS, TipTEOffsetExp)
        off_cm = w_off * TipTEOffset_cm

        def transformed_xy_no_offset(norm_xy):
            xn, yn = norm_xy
            px = xn * Chord_cm - cx0
            py = (yn * Chord_cm) * t_scale - cy0

            bx = px * cb - py * sb
            by = px * sb + py * cb

            rx = bx * ct - by * st
            ry = bx * st + by * ct
            return rx, ry

        le_rx, le_ry = transformed_xy_no_offset(le_norm)
        te_rx, te_ry = transformed_xy_no_offset(te_norm)

        vx, vy = (te_rx - le_rx), (te_ry - le_ry)
        vlen2 = vx * vx + vy * vy
        if vlen2 < 1e-12:
            ux, uy = 1.0, 0.0
        else:
            vlen = math.sqrt(vlen2)
            ux, uy = vx / vlen, vy / vlen

        dx, dy = off_cm * ux, off_cm * uy

        # Build the section curve points in sketch coords (cm), WITH TE offset
        section_profile_points = []
        for x_norm, y_norm in station_airfoil:
            px = x_norm * Chord_cm - cx0
            py = (y_norm * Chord_cm) * t_scale - cy0

            bx = px * cb - py * sb
            by = px * sb + py * cb

            rx = bx * ct - by * st
            ry = bx * st + by * ct

            y_cm = rx + dx
            z_cm = ry + dy
            section_profile_points.append((y_cm, z_cm))

        # Weld a sub-tolerance TE gap: snapping the last point onto the first
        # closes the fitted spline on itself, instead of bridging the gap with a
        # micron-length line that degenerates in the STEP B-rep. The TE rail is
        # unaffected because te_norm resolves to the first (upper-TE) point.
        y0_cm, z0_cm = section_profile_points[0]
        yN_cm, zN_cm = section_profile_points[-1]
        if math.hypot(yN_cm - y0_cm, zN_cm - z0_cm) < TE_WELD_TOL_CM:
            section_profile_points[-1] = section_profile_points[0]

        pts = adsk.core.ObjectCollection.create()
        for y_cm, z_cm in section_profile_points:
            pts.add(adsk.core.Point3D.create(y_cm, z_cm, 0.0))

        sk.sketchCurves.sketchFittedSplines.add(pts)
        close_profile_if_needed(sk, pts)

        prof = pick_primary_profile(sk)
        if prof:
            section_profiles.append(prof)

        # LE/TE points for rails (apply same dx/dy)
        le_sk_pt = adsk.core.Point3D.create(le_rx + dx, le_ry + dy, 0.0)
        te_sk_pt = adsk.core.Point3D.create(te_rx + dx, te_ry + dy, 0.0)

        le_model_points.append(sk.sketchToModelSpace(le_sk_pt))
        te_model_points.append(sk.sketchToModelSpace(te_sk_pt))

    # Create rails in 3D sketch
    log_progress('Building LE/TE rails...')
    rails_sk = make_3d_rail_sketch(comp, 'Rails_LE_TE')
    le_spline = add_3d_fitted_spline(rails_sk, le_model_points)
    te_spline = add_3d_fitted_spline(rails_sk, te_model_points)

    # Loft
    loft_msg = ""
    blade_body = None
    if CREATE_LOFT:
        try:
            log_progress(f'Lofting blade through {len(section_profiles)} sections...')
            loftFeats = comp.features.loftFeatures
            op = adsk.fusion.FeatureOperations.NewBodyFeatureOperation
            loftInput = loftFeats.createInput(op)

            loftInput.isSolid = active_make_solid_loft()

            for prof in section_profiles:
                loftInput.loftSections.add(prof)

            loftInput.centerLineOrRails.addRail(le_spline)
            loftInput.centerLineOrRails.addRail(te_spline)

            loft = loftFeats.add(loftInput)
            loft.name = 'BladeLoft'
            try:
                adsk.doEvents()
            except:
                pass
            if loft.bodies.count > 0:
                blade_body = loft.bodies.item(0)
                blade_body.name = generated_blade_body_name()
            else:
                blade_body = find_blade_body(comp)
            loft_msg = "Loft: created (BladeLoft)."
        except:
            loft_msg = "Loft: FAILED (sections+rails still created).\n" + traceback.format_exc()

    if blade_body and blade_body.isValid:
        blade_body.name = generated_blade_body_name()
    else:
        blade_body = find_blade_body(comp)

    prop_body = None
    generated_body_count = 1 if blade_body and blade_body.isValid else 0
    if blade_body and blade_body.isValid and active_make_solid_loft():
        try:
            log_progress('Patterning blades...')
            blade_bodies = make_blade_pattern(comp, blade_body)
            log_progress('Creating hub...')
            hub_body = make_hub_body(comp)
            log_progress(f'Combining {len(blade_bodies) + 1} bodies into one prop...')
            generated_body_count = len([b for b in blade_bodies if b and b.isValid]) + (1 if hub_body and hub_body.isValid else 0)
            prop_body = combine_generated_prop(comp, hub_body, blade_bodies)
            if prop_body and prop_body.isValid:
                generated_body_count = 1
                blade_body = prop_body
                loft_msg += f'\nProp assembly: {blade_count_text(blade_count)} + hub created and joined.'
            else:
                loft_msg += f'\nProp assembly: {blade_count_text(blade_count)} + hub created; join skipped or failed.'
        except:
            loft_msg += '\nProp assembly: FAILED.\n' + traceback.format_exc()

    return {
        'airfoil_name': airfoil_set['display_name'],
        'airfoil_metadata': airfoil_metadata(airfoil_set),
        'sections_found': len(section_profiles),
        'requested_stations': requested_nStations,
        'stations_used': len(s_list),
        'airfoil_points_used': active_airfoil_point_count(),
        'fast_single_run': is_fast_single_run(),
        'solid_loft': active_make_solid_loft(),
        'hub_radius_cm': HubR_cm,
        'hub_radius_parameter_cm': HubR_param_cm,
        'root_inboard_extension_in': ROOT_INBOARD_EXTENSION_IN,
        'radius_cm': Radius_cm,
        'twist_metadata': pitch_based_metadata(params, Radius_cm, HubR_cm, variant_id),
        'loft_msg': loft_msg,
        'body': blade_body,
        'component': comp,
        'generated_body_count': generated_body_count
    }


def select_airfoil_dat(ui, label='airfoil'):
    dlg = ui.createFileDialog()
    dlg.title = f'Select {label} Selig-format airfoil .dat'
    dlg.filter = 'DAT files (*.dat)'
    if dlg.showOpen() != adsk.core.DialogResults.DialogOK:
        return None
    return dlg.filename


def load_resampled_airfoil(path, label):
    try:
        name, pts = read_selig_dat(path)
    except Exception as exc:
        raise ValueError(f'Failed to load {label} airfoil file:\n{path}\n{exc}') from exc
    try:
        resampled = resample_airfoil_loop(pts, active_airfoil_point_count())
    except Exception as exc:
        raise ValueError(f'Failed to resample {label} airfoil "{name}" from:\n{path}\n{exc}') from exc
    return name, resampled


def load_airfoil_set(ui):
    validate_airfoil_transition_ranges()

    paths = {
        'root': RootAirfoilPath,
        'mid': MidAirfoilPath,
        'tip': TipAirfoilPath
    }
    labels = {
        'root': 'root/thick',
        'mid': 'mid-span',
        'tip': 'tip/thin'
    }

    for key in ('root', 'mid', 'tip'):
        if not paths[key]:
            path = select_airfoil_dat(ui, labels[key])
            if not path:
                return None
            paths[key] = path

    root_name, root_pts = load_resampled_airfoil(paths['root'], 'root')
    mid_name, mid_pts = load_resampled_airfoil(paths['mid'], 'mid')
    tip_name, tip_pts = load_resampled_airfoil(paths['tip'], 'tip')

    return {
        'root_path': paths['root'],
        'mid_path': paths['mid'],
        'tip_path': paths['tip'],
        'root_name': root_name,
        'mid_name': mid_name,
        'tip_name': tip_name,
        'root_pts': root_pts,
        'mid_pts': mid_pts,
        'tip_pts': tip_pts,
        'display_name': f'Root: {root_name} | Mid: {mid_name} | Tip: {tip_name}'
    }


def export_airfoil_debug_csv(output_dir, airfoil_set):
    if not EXPORT_AIRFOIL_DEBUG_CSV:
        return None
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)
    path = os.path.join(output_dir, 'airfoil_blend_debug.csv')
    debug_stations = [0.0, 0.3, 0.5, 0.75, 1.0]
    with open(path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['s', 'point_index', 'x_norm', 'y_norm'])
        for s in debug_stations:
            pts = airfoil_for_station(
                s,
                airfoil_set['root_pts'],
                airfoil_set['mid_pts'],
                airfoil_set['tip_pts']
            )
            for idx, (x, y) in enumerate(pts):
                writer.writerow([s, idx, x, y])
    return path


def select_variants_csv(ui):
    dlg = ui.createFileDialog()
    dlg.title = 'Select variants.csv (Cancel for single run)'
    dlg.filter = 'CSV files (*.csv)'
    default_path = os.path.join(script_dir(), VARIANTS_CSV_NAME)
    if os.path.exists(default_path):
        try:
            dlg.initialDirectory = script_dir()
            dlg.filename = default_path
        except:
            pass
    if dlg.showOpen() != adsk.core.DialogResults.DialogOK:
        return None
    return dlg.filename


def run_single_variant(ui, design, airfoil_set):
    force_fixed_radius(design)
    result = generate_blade_geometry(design, airfoil_set)
    debug_path = export_airfoil_debug_csv(script_dir(), airfoil_set)
    debug_msg = f'\nAirfoil debug CSV:\n{debug_path}' if debug_path else ''
    ui.messageBox(
        f'Airfoils:\n{result["airfoil_name"]}\n'
        f'Radius fixed at: {FIXED_RADIUS_EXPR}\n'
        f'Twist mode: {TWIST_MODE}\n'
        f'Fast single-run preview: {result["fast_single_run"]}\n'
        f'Airfoil points per station: {result["airfoil_points_used"]}\n'
        f'Stations used: {result["stations_used"]} of requested {result["requested_stations"]}\n'
        f'Solid loft: {result["solid_loft"]}\n'
        f'Sections (profiles found): {result["sections_found"]}\n'
        f'Root X after inboard extension (cm): {result["hub_radius_cm"]:.3f}\n'
        f'Tip X should equal Radius (cm): {result["radius_cm"]:.3f}\n'
        f'Final station chord factor: {FINAL_STATION_CHORD_PREV_FACTOR:.3f} of previous station\n'
        f'Root moved inboard: {ROOT_INBOARD_EXTENSION_IN:.3f} in\n'
        f'Blade mode: {BLADE_COUNT_MODE} ({blade_count_text()}); '
        f'hub OD: {HUB_OUTER_DIAMETER_MM:.1f} mm; hub height: {HUB_THICKNESS_MM:.1f} mm; '
        f'hole: {M5_CLEARANCE_HOLE_DIAMETER_MM:.1f} mm\n'
        f'{result["loft_msg"]}'
        f'{debug_msg}'
    )


def run_final_prop(ui, design, airfoil_set):
    variant_id = FINAL_PROP_VARIANT_ID
    export_dir = None
    if FINAL_PROP_EXPORT_STEP:
        # Create the exports folder up front (like batch mode does) so a fresh
        # run is visible on disk immediately; the STEP files themselves only
        # land here after the slow geometry rebuild completes.
        export_dir = os.path.join(script_dir(), final_prop_exports_dir_name(design))
        if not os.path.isdir(export_dir):
            os.makedirs(export_dir)
    log_progress(f'Final prop "{variant_id}": starting geometry rebuild...')
    force_fixed_radius(design, variant_id)
    update_fusion_parameters_from_dict(design, FINAL_PROP_VALUES, variant_id)
    force_fixed_radius(design, variant_id)

    result = generate_blade_geometry(design, airfoil_set, variant_id)
    recompute_design(design, variant_id)
    body = result['body']
    if (not body or not body.isValid) and result.get('component'):
        body = find_blade_body(result['component'], variant_id)
    if not body or not body.isValid:
        raise RuntimeError(result['loft_msg'] or 'No final prop body was created.')
    body.name = generated_prop_body_name(variant_id)
    comp = result.get('component')

    orientation_msg = rotate_body_about_x_axis(comp, body) if comp else 'Orientation: skipped because component is unavailable.'
    recompute_design(design, variant_id)
    if comp:
        refreshed_body = find_blade_body(comp, variant_id)
        if refreshed_body and refreshed_body.isValid:
            body = refreshed_body

    placement_msg = move_body_bottom_to_xy_plane(comp, body) if comp else 'Placement: skipped because component is unavailable.'
    recompute_design(design, variant_id)
    if comp:
        refreshed_body = find_blade_body(comp, variant_id)
        if refreshed_body and refreshed_body.isValid:
            body = refreshed_body

    _, parameter_data = read_current_parameters(design)
    debug_path = None
    export_msg = ''
    mirror_msg = ''
    if FINAL_PROP_EXPORT_STEP:
        debug_path = export_airfoil_debug_csv(export_dir, airfoil_set)
        base_name = safe_filename(variant_id)

        # Clockwise prop (as generated). Export before the mirror is created so a
        # whole-component fallback export cannot pick up the CCW body too.
        cw_step_path = os.path.join(export_dir, base_name + '_cw.step')
        cw_json_path = os.path.join(export_dir, base_name + '_cw.json')
        log_progress('Exporting CW STEP...')
        export_step(design, body, cw_step_path, variant_id, comp)
        write_metadata_json(
            cw_json_path,
            variant_id + '_cw',
            parameter_data,
            FINAL_PROP_VALUES,
            result.get('twist_metadata'),
            result.get('airfoil_metadata'),
            geometry_metadata()
        )

        # Counter-clockwise prop: mirror image of the clockwise prop, exported to
        # the same folder.
        log_progress('Mirroring to CCW prop...')
        ccw_body, mirror_msg = mirror_prop_to_ccw(comp, body, variant_id)
        recompute_design(design, variant_id)
        ccw_step_path = os.path.join(export_dir, base_name + '_ccw.step')
        ccw_json_path = os.path.join(export_dir, base_name + '_ccw.json')
        if ccw_body and ccw_body.isValid:
            log_progress('Exporting CCW STEP...')
            export_step(design, ccw_body, ccw_step_path, variant_id, comp, isolate=True)
            write_metadata_json(
                ccw_json_path,
                variant_id + '_ccw',
                parameter_data,
                FINAL_PROP_VALUES,
                result.get('twist_metadata'),
                result.get('airfoil_metadata'),
                geometry_metadata()
            )
            ccw_export_line = f'\nExported CCW STEP:\n{ccw_step_path}'
        else:
            ccw_export_line = '\nCCW STEP not exported (mirror unavailable).'

        export_msg = (
            f'\nExported CW STEP:\n{cw_step_path}'
            f'{ccw_export_line}'
            f'\nMetadata JSON folder:\n{export_dir}'
        )
    elif EXPORT_AIRFOIL_DEBUG_CSV:
        debug_path = export_airfoil_debug_csv(script_dir(), airfoil_set)

    debug_msg = f'\nAirfoil debug CSV:\n{debug_path}' if debug_path else ''
    mirror_line = f'{mirror_msg}\n' if mirror_msg else ''
    ui.messageBox(
        f'Final prop complete.\n'
        f'Variant: {variant_id}\n'
        f'Radius fixed at: {FIXED_RADIUS_EXPR}\n'
        f'GeometricPitch: {FINAL_PROP_VALUES["GeometricPitch"]}\n'
        f'ChordRoot/ChordPeak/ChordTip: '
        f'{FINAL_PROP_VALUES["ChordRoot"]} / {FINAL_PROP_VALUES["ChordPeak"]} / {FINAL_PROP_VALUES["ChordTip"]}\n'
        f'Stations used: {result["stations_used"]} of requested {result["requested_stations"]}\n'
        f'Tip closure preserved from {FINAL_PROP_LEGACY_TIP_STATIONS}-station batch; '
        f'final station chord factor: {FINAL_STATION_CHORD_PREV_FACTOR:.3f}\n'
        f'Blade mode: {BLADE_COUNT_MODE} ({blade_count_text()}); hub OD: {HUB_OUTER_DIAMETER_MM:.1f} mm; '
        f'hub height: {HUB_THICKNESS_MM:.1f} mm; hole: {M5_CLEARANCE_HOLE_DIAMETER_MM:.1f} mm\n'
        f'{orientation_msg}\n'
        f'{placement_msg}\n'
        f'{mirror_line}'
        f'{result["loft_msg"]}'
        f'{export_msg}'
        f'{debug_msg}'
    )


def export_dir_for_csv(csv_path):
    """Per-CSV exports folder so each batch's STL/JSON stay separated.

    A run of "prop_batch_430mph.csv" writes to "exports_prop_batch_430mph"
    beside the CSV, rather than a shared "exports" folder.
    """
    csv_stem = os.path.splitext(os.path.basename(csv_path))[0]
    folder_name = f'{EXPORTS_DIR_NAME}_{safe_filename(csv_stem)}'
    return os.path.join(os.path.dirname(csv_path), folder_name)


def run_batch_variants(ui, design, airfoil_set, csv_path):
    variants = read_variants_csv(csv_path)
    export_dir = export_dir_for_csv(csv_path)
    if not os.path.isdir(export_dir):
        os.makedirs(export_dir)
    deleted_exports = clear_previous_batch_exports(export_dir, variants)
    debug_path = export_airfoil_debug_csv(export_dir, airfoil_set)
    baseline_expressions = snapshot_user_parameter_expressions(design)
    spinner_stl_path = select_spinner_stl(ui)

    succeeded = 0
    failed = 0
    failures = []
    spinner_merge_count = 0

    # Batch workflow:
    # 1. Read one CSV row.
    # 2. Push matching CSV columns into Fusion user parameters.
    # 3. Rebuild the same section/rail/loft geometry.
    # 4. Export STL and write JSON metadata beside it.
    for index, row in enumerate(variants, start=1):
        variant_id = str(row.get('variant_id', '')).strip()
        if not variant_id:
            failed += 1
            failures.append(f'Row {index}: missing variant_id')
            continue

        show_progress(
            ui,
            f'Generating variant {index}/{len(variants)}: {variant_id} '
            f'({BATCH_STATION_COUNT} stations)'
        )
        try:
            restore_user_parameter_expressions(design, baseline_expressions, variant_id)
            force_fixed_radius(design, variant_id)
            update_fusion_parameters_from_dict(design, row, variant_id)
            force_fixed_radius(design, variant_id)
            force_batch_station_count(design, variant_id)
            result = generate_blade_geometry(design, airfoil_set, variant_id)
            recompute_design(design, variant_id)
            body = result['body']
            if (not body or not body.isValid) and result.get('component'):
                body = find_blade_body(result['component'], variant_id)
            if not body or not body.isValid:
                raise RuntimeError(result['loft_msg'] or 'No blade body was created.')
            body.name = generated_prop_body_name(variant_id)

            _, parameter_data = read_current_parameters(design)
            base_name = safe_filename(variant_id)
            stl_path = os.path.join(export_dir, base_name + '.stl')
            json_path = os.path.join(export_dir, base_name + '.json')

            export_stl(
                design,
                body,
                stl_path,
                variant_id,
                result.get('component'),
                result.get('generated_body_count', 1) > 1
            )
            if spinner_stl_path:
                show_progress(ui, f'Merging spinner into STL for variant {index}/{len(variants)}: {variant_id}')
                merge_spinner_into_stl(stl_path, spinner_stl_path, variant_id)
                spinner_merge_count += 1

            geometry_info = geometry_metadata()
            if spinner_stl_path:
                geometry_info['SPINNER_STL_RESOLVED_PATH'] = spinner_stl_path
            write_metadata_json(
                json_path,
                variant_id,
                parameter_data,
                row,
                result.get('twist_metadata'),
                result.get('airfoil_metadata'),
                geometry_info
            )
            succeeded += 1
        except Exception as exc:
            failed += 1
            failures.append(f'{variant_id}: {exc}')
            show_progress(ui, f'FAILED variant {variant_id}: {exc}')

    summary = (
        f'Batch complete.\n'
        f'Radius fixed at {FIXED_RADIUS_EXPR} for all variants.\n'
        f'Twist mode: {TWIST_MODE}\n'
        f'Blade mode: {BLADE_COUNT_MODE} ({blade_count_text()})\n'
        f'Stations per batch variant: {BATCH_STATION_COUNT}\n'
        f'Spinner merged into STL variants: {spinner_merge_count}\n'
        f'Previous batch exports deleted: {len(deleted_exports)}\n'
        f'Variants successfully exported: {succeeded}\n'
        f'Failed: {failed}\n'
        f'Exports folder:\n{export_dir}'
    )
    if spinner_stl_path:
        summary += f'\nSpinner STL:\n{spinner_stl_path}'
    if debug_path:
        summary += f'\nAirfoil debug CSV:\n{debug_path}'
    if failures:
        summary += '\n\nFailures:\n- ' + '\n- '.join(failures[:10])
        if len(failures) > 10:
            summary += f'\n- ...and {len(failures) - 10} more.'
    ui.messageBox(summary)


# ---------- Command ----------
class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        app = adsk.core.Application.get()
        ui = app.userInterface
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if not design:
                ui.messageBox('No active design.')
                return

            airfoil_set = load_airfoil_set(ui)
            if not airfoil_set:
                return

            # Create any missing parameters (fresh document support), then
            # validate the base design once before either single-run or batch.
            created_params = ensure_required_parameters(design)
            if created_params:
                log_progress(
                    'Created missing user parameters with defaults: '
                    + ', '.join(created_params)
                )
            read_current_parameters(design)

            if FINAL_PROP_MODE:
                run_final_prop(ui, design, airfoil_set)
                return

            csv_path = None
            if BATCH_MODE:
                csv_path = select_variants_csv(ui)

            if csv_path:
                run_batch_variants(ui, design, airfoil_set, csv_path)
            else:
                run_single_variant(ui, design, airfoil_set)

        except:
            ui.messageBox(traceback.format_exc())


class CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        h = ExecuteHandler()
        args.command.execute.add(h)
        _handlers.append(h)


def run(context):
    app = adsk.core.Application.get()
    ui = app.userInterface
    cmd = ui.commandDefinitions.itemById(CMD_ID)
    if cmd:
        cmd.deleteMe()
    cmd = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)
    h = CreatedHandler()
    cmd.commandCreated.add(h)
    _handlers.append(h)
    cmd.execute()
    adsk.autoTerminate(False)


def stop(context):
    app = adsk.core.Application.get()
    ui = app.userInterface
    cmd = ui.commandDefinitions.itemById(CMD_ID)
    if cmd:
        cmd.deleteMe()

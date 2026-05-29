#!/usr/bin/env python3
"""Generate OpenFOAM cases from variants.csv."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import struct
import sys
from pathlib import Path


REQUIRED_COLUMNS = {
    "variant_id",
    "stl_file",
    "diameter_m",
}
MRF_OMEGA_RE = re.compile(
    r"^(?P<indent>\s*omega\s+)(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)(?P<suffix>\s*;.*)$",
    re.MULTILINE,
)


class CaseGenerationError(Exception):
    """Raised when a case cannot be generated from a variant row."""


AIR_DEFAULTS = {
    "Tinf_K": 288.15,
    "pinf_Pa": 101325.0,
    "R": 287.0,
    "gamma": 1.4,
    "Cp": 1004.5,
    "mu": 1.7894e-5,
    "Pr": 0.71,
}
SOLVER_TEMPLATES = {
    "incompressible_mrf": "prop_MRF_template",
    "compressible_forward_mrf": "compressible_forward_mrf",
}
STL_SOURCE_MODES = {"library", "imported_batch"}


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def sanitize_case_stl_name(variant_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", variant_id).strip("._-")
    return f"{cleaned or 'propeller'}.stl"


def ensure_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_stl_source(root: Path, row: dict[str, str], row_number: int) -> tuple[Path, str, str]:
    source_mode = row.get("stl_source_mode", "library").strip() or "library"
    stl_file = row["stl_file"].strip()
    if source_mode not in STL_SOURCE_MODES:
        supported = ", ".join(sorted(STL_SOURCE_MODES))
        raise CaseGenerationError(f"Row {row_number}: unsupported stl_source_mode '{source_mode}'. Use: {supported}")

    if source_mode == "library":
        if "/" in stl_file or "\\" in stl_file:
            raise CaseGenerationError(
                f"Row {row_number}: stl_file must be a file name inside prop_stls/ for library mode"
            )
        source = (root / "prop_stls" / stl_file).resolve()
        if not ensure_inside(source, root / "prop_stls"):
            raise CaseGenerationError(f"Row {row_number}: library STL path escapes prop_stls/")
        return source, stl_file, source_mode

    imported_path_text = row.get("imported_stl_path", "").strip() or stl_file
    imported_path = Path(imported_path_text)
    if not imported_path.is_absolute():
        imported_path = root / imported_path
    imported_path = imported_path.resolve()
    imported_root = root / "imported_exports"
    if not ensure_inside(imported_path, imported_root):
        raise CaseGenerationError(
            f"Row {row_number}: imported batch STL must be inside imported_exports/<batch_id>/"
        )
    case_stl_file = row.get("case_stl_file", "").strip() or sanitize_case_stl_name(row["variant_id"].strip())
    if Path(case_stl_file).name != case_stl_file or not case_stl_file.lower().endswith(".stl"):
        raise CaseGenerationError(f"Row {row_number}: case_stl_file must be a case-local STL file name")
    return imported_path, case_stl_file, source_mode


def read_variants(csv_path: Path) -> list[dict[str, str]]:
    if not csv_path.is_file():
        raise CaseGenerationError(f"Missing variants CSV: {csv_path}")

    with csv_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise CaseGenerationError(f"CSV has no header: {csv_path}")

        missing = REQUIRED_COLUMNS.difference(reader.fieldnames)
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise CaseGenerationError(f"CSV is missing required columns: {missing_list}")

        rows = list(reader)

    if not rows:
        raise CaseGenerationError(f"CSV contains no variants: {csv_path}")

    return rows


def parse_variant(row: dict[str, str], row_number: int) -> dict[str, object]:
    variant_id = row["variant_id"].strip()
    stl_file = row["stl_file"].strip()
    solver_mode = row.get("solver_mode", "").strip() or "incompressible_mrf"
    stl_source_mode = row.get("stl_source_mode", "library").strip() or "library"

    if not variant_id:
        raise CaseGenerationError(f"Row {row_number}: variant_id is empty")
    if not stl_file:
        raise CaseGenerationError(f"Row {row_number}: stl_file is empty")
    if stl_source_mode == "library" and ("/" in stl_file or "\\" in stl_file):
        raise CaseGenerationError(
            f"Row {row_number}: stl_file must be a file name inside prop_stls/"
        )
    if stl_source_mode not in STL_SOURCE_MODES:
        supported = ", ".join(sorted(STL_SOURCE_MODES))
        raise CaseGenerationError(f"Row {row_number}: unsupported stl_source_mode '{stl_source_mode}'. Use: {supported}")
    if solver_mode not in SOLVER_TEMPLATES:
        supported = ", ".join(sorted(SOLVER_TEMPLATES))
        raise CaseGenerationError(f"Row {row_number}: unsupported solver_mode '{solver_mode}'. Use: {supported}")

    try:
        rpm = float(row.get("rpm", "10000") or "10000")
        freestream_value = row.get("Vinf_mps", row.get("freestream_velocity_mps", "0"))
        freestream_velocity = float(freestream_value)
        diameter = float(row["diameter_m"])
        air = {
            key: float(row[key]) if row.get(key, "").strip() else default
            for key, default in AIR_DEFAULTS.items()
        }
    except ValueError as exc:
        raise CaseGenerationError(
            f"Row {row_number}: rpm, Vinf_mps/freestream_velocity_mps, diameter_m, and air properties must be numeric"
        ) from exc

    if rpm < 0:
        raise CaseGenerationError(f"Row {row_number}: rpm must be non-negative")
    if freestream_velocity < 0:
        raise CaseGenerationError(f"Row {row_number}: Vinf_mps must be non-negative")
    if diameter <= 0:
        raise CaseGenerationError(f"Row {row_number}: diameter_m must be positive")

    radius = diameter / 2.0
    omega = rpm * 2.0 * math.pi / 60.0
    reference_area = math.pi * radius**2
    speed_of_sound = math.sqrt(air["gamma"] * air["R"] * air["Tinf_K"])
    tip_speed = omega * radius
    freestream_mach = freestream_velocity / speed_of_sound if speed_of_sound > 0 else 0.0
    helical_tip_mach = math.hypot(freestream_velocity, tip_speed) / speed_of_sound if speed_of_sound > 0 else 0.0
    advance_ratio = ""
    rev_per_second = rpm / 60.0
    if rev_per_second > 0:
        advance_ratio = freestream_velocity / (rev_per_second * diameter)

    warnings = []
    if solver_mode == "incompressible_mrf" and freestream_mach > 0.3:
        warnings.append(f"freestream_Mach {freestream_mach:.3g} > 0.3 for incompressible mode")
    if freestream_mach > 0.8:
        warnings.append(f"freestream_Mach {freestream_mach:.3g} > 0.8")
    if helical_tip_mach > 1.1:
        warnings.append(f"helical_tip_Mach {helical_tip_mach:.3g} > 1.1")
    elif helical_tip_mach > 0.95:
        warnings.append(f"helical_tip_Mach {helical_tip_mach:.3g} > 0.95")

    return {
        "variant_id": variant_id,
        "stl_file": stl_file,
        "stl_source_mode": stl_source_mode,
        "imported_stl_path": row.get("imported_stl_path", "").strip(),
        "case_stl_file": row.get("case_stl_file", "").strip() or (stl_file if stl_source_mode == "imported_batch" else "propeller.stl"),
        "solver_mode": solver_mode,
        "template": SOLVER_TEMPLATES[solver_mode],
        "rpm": rpm,
        "omega_rad_s": omega,
        "Vinf_mps": freestream_velocity,
        "freestream_velocity_mps": freestream_velocity,
        "diameter_m": diameter,
        "radius_m": radius,
        "reference_area_m2": reference_area,
        **air,
        "freestream_Mach": freestream_mach,
        "tip_speed_mps": tip_speed,
        "helical_tip_Mach": helical_tip_mach,
        "advance_ratio_J": advance_ratio,
        "rotation_axis": [0, 0, 1],
        "thrust_axis": "z",
        "thrust_sign": 1,
        "torque_axis": "z",
        "torque_sign": 1,
        "shaft_torque_sign": 1,
        "torque_sign_convention": (
            "shaft_torque_Nm = torque_sign * raw_moment_axis_Nm; power_W = shaft_torque_Nm * omega_rad_s"
        ),
        "batch_enabled": row.get("batch_enabled", "true").strip().lower() not in ("0", "false", "no"),
        "generation_warnings": warnings,
        "notes": (
            f"Generated from templates/{SOLVER_TEMPLATES[solver_mode]}."
        ),
    }


def update_mrf_omega(case_dir: Path, variant_id: str, omega_rad_s: float) -> None:
    mrf_path = case_dir / "constant" / "MRFProperties"
    if not mrf_path.is_file():
        raise CaseGenerationError(f"Variant {variant_id}: missing MRFProperties: {mrf_path}")

    text = mrf_path.read_text()

    def replacement(match: re.Match[str]) -> str:
        return f"{match.group('indent')}{omega_rad_s:.12g}{match.group('suffix')}"

    updated, count = MRF_OMEGA_RE.subn(replacement, text, count=1)
    if count != 1:
        raise CaseGenerationError(
            f"Variant {variant_id}: could not find a single 'omega <number>;' entry in {mrf_path}"
        )

    mrf_path.write_text(updated)
    print(f"[generate] Wrote MRF omega {omega_rad_s:.6f} rad/s for {variant_id}")


def read_mrf_omega(case_dir: Path) -> float:
    mrf_path = case_dir / "constant" / "MRFProperties"
    if not mrf_path.is_file():
        raise CaseGenerationError(f"Missing MRFProperties: {mrf_path}")

    text = mrf_path.read_text()
    match = MRF_OMEGA_RE.search(text)
    if match is None:
        raise CaseGenerationError(f"Could not read omega entry from: {mrf_path}")

    return float(match.group("value"))


def verify_mrf_omega(case_dir: Path, case_info_path: Path, tolerance: float = 1e-6) -> None:
    with case_info_path.open() as handle:
        case_info = json.load(handle)

    expected = float(case_info["omega_rad_s"])
    actual = read_mrf_omega(case_dir)
    if abs(actual - expected) > tolerance:
        raise CaseGenerationError(
            f"MRF omega mismatch in {case_dir}: MRFProperties has {actual}, "
            f"case_info.json has {expected}"
        )


def replace_tokens(path: Path, replacements: dict[str, object]) -> None:
    if not path.is_file():
        return
    text = path.read_text()
    for token, value in replacements.items():
        text = text.replace(token, str(value))
    path.write_text(text)


def update_compressible_template_values(case_dir: Path, info: dict[str, object]) -> None:
    vinf = float(info["Vinf_mps"])
    replacements = {
        "__VINF_VECTOR__": f"0 0 {vinf:.12g}",
        "__PINF__": f"{float(info['pinf_Pa']):.12g}",
        "__TINF__": f"{float(info['Tinf_K']):.12g}",
        "__KINF__": "0.01",
        "__OMEGA_TURB__": "10",
        "__CP__": f"{float(info['Cp']):.12g}",
        "__MU__": f"{float(info['mu']):.12g}",
        "__PR__": f"{float(info['Pr']):.12g}",
        "__GAMMA__": f"{float(info['gamma']):.12g}",
        "__R__": f"{float(info['R']):.12g}",
    }
    for path in [
        case_dir / "0" / "U",
        case_dir / "0" / "p",
        case_dir / "0" / "T",
        case_dir / "0" / "k",
        case_dir / "0" / "omega",
        case_dir / "constant" / "thermophysicalProperties",
    ]:
        replace_tokens(path, replacements)


def update_case_stl_references(case_dir: Path, case_stl_file: str) -> None:
    if case_stl_file == "propeller.stl":
        return
    stem = Path(case_stl_file).stem
    replacements = {
        "../triSurface/propeller.stl": f"../triSurface/{case_stl_file}",
        "propeller.extendedFeatureEdgeMesh": f"{stem}.extendedFeatureEdgeMesh",
    }
    for path in [case_dir / "system" / "snappyHexMeshDict", case_dir / "system" / "surfaceFeaturesDict"]:
        if not path.is_file():
            continue
        text = path.read_text()
        for old, new in replacements.items():
            text = text.replace(old, new)
        path.write_text(text)


def looks_like_binary_stl(path: Path) -> bool:
    with path.open("rb") as handle:
        header = handle.read(84)
    if len(header) < 84:
        return False
    if header[:5].lower() == b"solid":
        try:
            text = path.read_text(errors="strict")
        except UnicodeDecodeError:
            return True
        return "\0" in text[:512]
    triangle_count = struct.unpack("<I", header[80:84])[0]
    return path.stat().st_size == 84 + triangle_count * 50


def iter_binary_stl_facets(path: Path):
    with path.open("rb") as handle:
        handle.seek(80)
        triangle_count = struct.unpack("<I", handle.read(4))[0]
        for _ in range(triangle_count):
            record = handle.read(50)
            if len(record) != 50:
                raise CaseGenerationError(f"Binary STL is truncated: {path}")
            values = struct.unpack("<12fH", record)
            yield values[0:3], values[3:6], values[6:9], values[9:12]


def iter_ascii_stl_facets(path: Path):
    normal: tuple[float, float, float] | None = None
    vertices: list[tuple[float, float, float]] = []
    for line in path.read_text(errors="replace").splitlines():
        parts = line.strip().split()
        if len(parts) == 5 and parts[0] == "facet" and parts[1] == "normal":
            try:
                normal = (float(parts[2]), float(parts[3]), float(parts[4]))
            except ValueError:
                normal = (0.0, 0.0, 0.0)
            vertices = []
        elif len(parts) == 4 and parts[0] == "vertex":
            try:
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            except ValueError as exc:
                raise CaseGenerationError(f"Could not parse STL vertex in {path}: {' '.join(parts)}") from exc
        elif parts[:1] == ["endfacet"] and normal is not None and len(vertices) == 3:
            yield normal, vertices[0], vertices[1], vertices[2]
            normal = None
            vertices = []


def stl_bounding_box(source: Path) -> tuple[tuple[float, float, float], tuple[float, float, float], int]:
    facets = iter_binary_stl_facets(source) if looks_like_binary_stl(source) else iter_ascii_stl_facets(source)
    mins = [math.inf, math.inf, math.inf]
    maxs = [-math.inf, -math.inf, -math.inf]
    count = 0
    for _normal, v1, v2, v3 in facets:
        count += 1
        for vertex in (v1, v2, v3):
            for index, value in enumerate(vertex):
                mins[index] = min(mins[index], value)
                maxs[index] = max(maxs[index], value)
    if count <= 0:
        raise CaseGenerationError(f"STL contains no parseable facets: {source}")
    return (mins[0], mins[1], mins[2]), (maxs[0], maxs[1], maxs[2]), count


def infer_imported_stl_scale(source: Path, diameter_m: float) -> tuple[float, dict[str, object]]:
    mins, maxs, facet_count = stl_bounding_box(source)
    extents = [maxs[index] - mins[index] for index in range(3)]
    max_extent = max(extents)
    scale = 1.0
    reason = "input coordinates already appear to be meters"
    if max_extent > 2.0 and diameter_m < 2.0:
        scale = 0.001
        reason = "input coordinates appear to be millimeters; converted to meters"
    if diameter_m > 0 and max_extent > diameter_m * 5.0 and 0.25 <= (max_extent * 0.001) / diameter_m <= 4.0:
        scale = 0.001
        reason = "input coordinates are much larger than diameter_m and match millimeter scale"
    return scale, {
        "source_stl_bbox_min": mins,
        "source_stl_bbox_max": maxs,
        "source_stl_bbox_extents": extents,
        "source_stl_facet_count": facet_count,
        "stl_coordinate_scale_to_meters": scale,
        "stl_coordinate_scale_reason": reason,
    }


def write_single_region_ascii_stl(source: Path, target: Path, solid_name: str = "propeller", scale: float = 1.0) -> None:
    facets = iter_binary_stl_facets(source) if looks_like_binary_stl(source) else iter_ascii_stl_facets(source)
    count = 0
    with target.open("w") as handle:
        handle.write(f"solid {solid_name}\n")
        for normal, v1, v2, v3 in facets:
            count += 1
            handle.write(f"  facet normal {normal[0]:.9g} {normal[1]:.9g} {normal[2]:.9g}\n")
            handle.write("    outer loop\n")
            for vertex in (v1, v2, v3):
                handle.write(f"      vertex {vertex[0] * scale:.9g} {vertex[1] * scale:.9g} {vertex[2] * scale:.9g}\n")
            handle.write("    endloop\n")
            handle.write("  endfacet\n")
        handle.write(f"endsolid {solid_name}\n")
    if count <= 0:
        raise CaseGenerationError(f"STL contains no parseable facets: {source}")


def generate_variant_case(
    root: Path,
    row: dict[str, str],
    row_number: int,
    overwrite: bool = False,
) -> Path:
    info = parse_variant(row, row_number)
    variant_id = str(info["variant_id"])
    stl_name = str(info["stl_file"])
    template_dir = root / "templates" / str(info["template"])
    if not template_dir.is_dir():
        raise CaseGenerationError(f"Missing template directory: {template_dir}")

    source_stl, case_stl_file, stl_source_mode = resolve_stl_source(root, row, row_number)
    if not source_stl.is_file():
        raise CaseGenerationError(f"Variant {variant_id}: missing STL file: {source_stl}")

    case_dir = root / "cases" / variant_id
    if case_dir.exists():
        if not overwrite:
            raise CaseGenerationError(
                f"Case already exists: {case_dir}. Re-run with --overwrite to replace it."
            )
        print(f"[generate] Removing existing case: {case_dir}")
        shutil.rmtree(case_dir)

    print(f"[generate] Copying template for variant '{variant_id}'")
    shutil.copytree(template_dir, case_dir)

    tri_surface_dir = case_dir / "constant" / "triSurface"
    tri_surface_dir.mkdir(parents=True, exist_ok=True)
    target_stl = tri_surface_dir / case_stl_file
    if stl_source_mode == "imported_batch":
        stl_scale, stl_metadata = infer_imported_stl_scale(source_stl, float(info["diameter_m"]))
        write_single_region_ascii_stl(source_stl, target_stl, scale=stl_scale)
        info.update(stl_metadata)
        print(f"[generate] Imported STL normalized to one propeller region: {target_stl}")
        if stl_scale != 1.0:
            print(f"[generate] Imported STL coordinates scaled by {stl_scale:g} to meters")
    else:
        shutil.copy2(source_stl, target_stl)
        print(f"[generate] STL copied to: {target_stl}")
    update_case_stl_references(case_dir, case_stl_file)

    update_mrf_omega(case_dir, variant_id, float(info["omega_rad_s"]))
    if info["solver_mode"] == "compressible_forward_mrf":
        update_compressible_template_values(case_dir, info)
    for warning in info.get("generation_warnings", []):  # type: ignore[union-attr]
        print(f"WARNING: {variant_id}: {warning}")

    case_info_path = case_dir / "case_info.json"
    with case_info_path.open("w") as handle:
        info["source_stl_path"] = str(source_stl)
        info["case_stl_file"] = case_stl_file
        info["stl_source_mode"] = stl_source_mode
        json.dump(info, handle, indent=2)
        handle.write("\n")
    print(f"[generate] Metadata written to: {case_info_path}")

    verify_mrf_omega(case_dir, case_info_path)

    return case_dir


def generate_cases(root: Path, overwrite: bool = False) -> list[Path]:
    variants_path = root / "variants.csv"
    rows = read_variants(variants_path)
    case_paths = []

    for row_number, row in enumerate(rows, start=2):
        case_paths.append(generate_variant_case(root, row, row_number, overwrite))

    return case_paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate OpenFOAM cases from variants.csv")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing case directories under cases/",
    )
    args = parser.parse_args()

    root = project_root()
    try:
        case_paths = generate_cases(root, overwrite=args.overwrite)
    except CaseGenerationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"[generate] Generated {len(case_paths)} case(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

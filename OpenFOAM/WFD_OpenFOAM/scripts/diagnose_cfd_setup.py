#!/usr/bin/env python3
"""Diagnose first-pass OpenFOAM propeller CFD setup issues."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path


NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
VECTOR_RE = re.compile(r"\(([-+0-9.eE\s]+)\)")


class DiagnosticError(Exception):
    pass


def read_text(path: Path) -> str:
    if not path.is_file():
        raise DiagnosticError(f"Missing file: {path}")
    return path.read_text(errors="replace")


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//.*", "", text)


def parse_vector(text: str) -> tuple[float, float, float]:
    values = [float(value) for value in NUMBER_RE.findall(text)]
    if len(values) != 3:
        raise DiagnosticError(f"Expected vector with 3 values, got: {text}")
    return values[0], values[1], values[2]


def vector_sub(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return a[0] - b[0], a[1] - b[1], a[2] - b[2]


def cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def mag(a: tuple[float, float, float]) -> float:
    return math.sqrt(a[0] ** 2 + a[1] ** 2 + a[2] ** 2)


def polygon_area(points: list[tuple[float, float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    origin = points[0]
    area = 0.0
    for i in range(1, len(points) - 1):
        area += 0.5 * mag(cross(vector_sub(points[i], origin), vector_sub(points[i + 1], origin)))
    return area


def parse_boundary(path: Path) -> dict[str, dict[str, object]]:
    text = read_text(path)
    patches: dict[str, dict[str, object]] = {}
    pattern = re.compile(
        r"^\s*(\w+)\s*\{(?P<body>.*?)^\s*\}",
        re.MULTILINE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        name = match.group(1)
        body = match.group("body")
        type_match = re.search(r"\btype\s+(\w+)\s*;", body)
        nfaces_match = re.search(r"\bnFaces\s+(\d+)\s*;", body)
        start_match = re.search(r"\bstartFace\s+(\d+)\s*;", body)
        if nfaces_match and start_match:
            patches[name] = {
                "type": type_match.group(1) if type_match else "",
                "nFaces": int(nfaces_match.group(1)),
                "startFace": int(start_match.group(1)),
            }
    return patches


def parse_points(path: Path) -> list[tuple[float, float, float]]:
    text = read_text(path)
    return [parse_vector(match.group(0)) for match in VECTOR_RE.finditer(text)]


def parse_faces(path: Path) -> list[list[int]]:
    faces: list[list[int]] = []
    for line in read_text(path).splitlines():
        stripped = line.strip()
        match = re.match(r"^\d+\(([^()]*)\)$", stripped)
        if match:
            faces.append([int(value) for value in match.group(1).split()])
    return faces


def parse_ascii_stl_facets(path: Path) -> int | None:
    if not path.is_file():
        return None
    text = path.read_text(errors="ignore")
    if "\0" in text[:1024]:
        return None
    return len(re.findall(r"^\s*facet\s+normal\b", text, flags=re.MULTILINE))


def parse_stl_bounding_box(path: Path) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    if not path.is_file():
        return None
    points = []
    for line in path.read_text(errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped.startswith("vertex "):
            continue
        values = [float(value) for value in NUMBER_RE.findall(stripped)]
        if len(values) == 3:
            points.append((values[0], values[1], values[2]))
    if not points:
        return None
    return (
        tuple(min(point[i] for point in points) for i in range(3)),  # type: ignore[return-value]
        tuple(max(point[i] for point in points) for i in range(3)),  # type: ignore[return-value]
    )


def parse_rotating_zone_cylinder(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    text = read_text(path)
    match = re.search(
        r"name\s+rotatingCells\s*;.*?source\s+cylinderToCell\s*;.*?sourceInfo\s*\{(?P<body>.*?)\}",
        text,
        flags=re.DOTALL,
    )
    if not match:
        return None
    body = match.group("body")
    point1 = parse_entry(body, "point1")
    point2 = parse_entry(body, "point2")
    radius = parse_entry(body, "radius")
    if point1 is None or point2 is None or radius is None:
        return None
    p1 = parse_vector(point1)
    p2 = parse_vector(point2)
    radius_value = float(NUMBER_RE.search(radius).group(0))  # type: ignore[union-attr]
    return {
        "point1": p1,
        "point2": p2,
        "radius": radius_value,
        "length": mag(vector_sub(p2, p1)),
    }


def bbox_fits_in_z_cylinder(
    bbox: tuple[tuple[float, float, float], tuple[float, float, float]],
    cylinder: dict[str, object],
) -> tuple[bool, float, float]:
    p1 = cylinder["point1"]  # type: ignore[assignment]
    p2 = cylinder["point2"]  # type: ignore[assignment]
    radius = float(cylinder["radius"])
    min_pt, max_pt = bbox
    max_radial = max(abs(min_pt[0]), abs(max_pt[0]), abs(min_pt[1]), abs(max_pt[1]))
    z_min = min(p1[2], p2[2])  # type: ignore[index]
    z_max = max(p1[2], p2[2])  # type: ignore[index]
    fits = max_radial <= radius and min_pt[2] >= z_min and max_pt[2] <= z_max
    radial_margin = radius - max_radial
    axial_margin = min(min_pt[2] - z_min, z_max - max_pt[2])
    return fits, radial_margin, axial_margin


def patch_area(case_path: Path, patch: dict[str, object]) -> float:
    points = parse_points(case_path / "constant" / "polyMesh" / "points")
    faces = parse_faces(case_path / "constant" / "polyMesh" / "faces")
    start = int(patch["startFace"])
    nfaces = int(patch["nFaces"])
    area = 0.0
    for face in faces[start : start + nfaces]:
        area += polygon_area([points[index] for index in face])
    return area


def parse_entry(text: str, name: str) -> str | None:
    match = re.search(rf"\b{name}\s+([^;]+);", text)
    return match.group(1).strip() if match else None


def parse_cell_zones(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {}
    text = read_text(path)
    zones: dict[str, int] = {}
    body_match = re.search(r"\n\s*\d+\s*\(\s*(.*)\s*\)\s*", text, flags=re.DOTALL)
    if not body_match:
        return zones
    body = body_match.group(1)
    pattern = re.compile(r"^\s*(\w+)\s*\{.*?cellLabels\s+List<label>\s+(\d+)", re.MULTILINE | re.DOTALL)
    for match in pattern.finditer(body):
        zones[match.group(1)] = int(match.group(2))
    return zones


def parse_block_bounds(path: Path) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    text = strip_comments(read_text(path))
    vertices_match = re.search(r"\bvertices\s*\((.*?)\)\s*;", text, flags=re.DOTALL)
    if not vertices_match:
        return None
    vectors = [parse_vector(match.group(0)) for match in VECTOR_RE.finditer(vertices_match.group(1))]
    if not vectors:
        return None
    return (
        tuple(min(point[i] for point in vectors) for i in range(3)),  # type: ignore[return-value]
        tuple(max(point[i] for point in vectors) for i in range(3)),  # type: ignore[return-value]
    )


def boundary_field_types(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    text = read_text(path)
    start_match = re.search(r"\bboundaryField\s*\{", text)
    if not start_match:
        return {}
    start = start_match.end()
    depth = 1
    end = start
    while end < len(text) and depth > 0:
        if text[end] == "{":
            depth += 1
        elif text[end] == "}":
            depth -= 1
        end += 1
    if depth != 0:
        return {}

    boundary_text = text[start : end - 1]
    fields: dict[str, str] = {}
    lines = boundary_text.splitlines()
    for index, line in enumerate(lines):
        name_match = re.match(r"^\s*(\"[^\"]+\"|\w+)\s*$", line)
        if not name_match:
            continue
        patch_name = name_match.group(1).strip('"')
        body_lines = []
        depth = 0
        started = False
        for body_line in lines[index + 1 :]:
            if "{" in body_line:
                depth += body_line.count("{")
                started = True
            if started:
                body_lines.append(body_line)
            if "}" in body_line:
                depth -= body_line.count("}")
                if started and depth <= 0:
                    break
        body = "\n".join(body_lines)
        type_match = re.search(r"\btype\s+(\w+)\s*;", body)
        fields[patch_name] = type_match.group(1) if type_match else "unknown"
    return fields


def extract_checkmesh_summary(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    text = read_text(path)
    summary: dict[str, str] = {}
    patterns = {
        "cells": r"\bcells:\s+(\d+)",
        "cell_zones": r"\bcell zones:\s+(\d+)",
        "max_aspect_ratio": r"Max aspect ratio = ([^\s]+)",
        "min_volume": r"Min volume = ([^\s]+)",
        "max_non_orthogonality": r"Mesh non-orthogonality Max: ([^\s]+)",
        "max_skewness": r"Max skewness = ([^\s]+)",
        "mesh_status": r"(Mesh OK\.)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            summary[key] = match.group(1)
    return summary


def force_uses_propeller(case_path: Path) -> tuple[bool, list[Path]]:
    files = [case_path / "system" / "functions", case_path / "system" / "controlDict"]
    checked = []
    found = False
    for path in files:
        if not path.is_file():
            continue
        checked.append(path)
        text = read_text(path)
        if re.search(r"\bpatches\s*\([^;]*\bpropeller\b[^;]*\)\s*;", text, flags=re.DOTALL):
            found = True
    return found, checked


def load_case_info(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        return json.loads(read_text(path))
    except (json.JSONDecodeError, OSError):
        return {}


def mach_warnings(info: dict[str, object]) -> list[str]:
    if not info:
        return []
    try:
        solver_mode = str(info.get("solver_mode", "incompressible_mrf"))
        vinf = float(info.get("Vinf_mps", info.get("freestream_velocity_mps", 0)))
        rpm = float(info.get("rpm", 0))
        radius = float(info.get("radius_m", 0))
        gamma = float(info.get("gamma", 1.4))
        gas_constant = float(info.get("R", 287))
        temperature = float(info.get("Tinf_K", 288.15))
    except (TypeError, ValueError):
        return ["could not evaluate Mach diagnostics from case_info.json"]

    speed_of_sound = math.sqrt(gamma * gas_constant * temperature)
    omega = rpm * 2.0 * math.pi / 60.0
    freestream_mach = vinf / speed_of_sound if speed_of_sound > 0 else 0.0
    helical_tip_mach = math.hypot(vinf, omega * radius) / speed_of_sound if speed_of_sound > 0 else 0.0
    warnings = []
    if solver_mode == "incompressible_mrf" and freestream_mach > 0.3:
        warnings.append(f"freestream_Mach {freestream_mach:.3g} > 0.3 for incompressible mode")
    if freestream_mach > 0.8:
        warnings.append(f"freestream_Mach {freestream_mach:.3g} > 0.8")
    if helical_tip_mach > 1.1:
        warnings.append(f"helical_tip_Mach {helical_tip_mach:.3g} > 1.1")
    elif helical_tip_mach > 0.95:
        warnings.append(f"helical_tip_Mach {helical_tip_mach:.3g} > 0.95")
    return warnings


def print_section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose a generated OpenFOAM propeller case")
    parser.add_argument("case_path", type=Path, help="Path to generated case")
    args = parser.parse_args()

    case_path = args.case_path.resolve()
    warnings: list[str] = []

    if not case_path.is_dir():
        print(f"ERROR: case directory does not exist: {case_path}", file=sys.stderr)
        return 1

    print(f"case_path: {case_path}")

    try:
        patches = parse_boundary(case_path / "constant" / "polyMesh" / "boundary")
    except DiagnosticError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print_section("Boundary Patches")
    for name, patch in patches.items():
        area_text = ""
        if name == "propeller":
            try:
                area_text = f", area_m2={patch_area(case_path, patch):.8g}"
            except DiagnosticError as exc:
                area_text = f", area_error={exc}"
        print(f"{name}: type={patch['type']}, nFaces={patch['nFaces']}, startFace={patch['startFace']}{area_text}")

    prop_patch = patches.get("propeller")
    if prop_patch is None:
        warnings.append("missing propeller patch")
    elif int(prop_patch["nFaces"]) < 5000:
        warnings.append(f"propeller patch has fewer than 5000 faces: {prop_patch['nFaces']}")

    stl_path = case_path / "constant" / "triSurface" / "propeller.stl"
    stl_facets = parse_ascii_stl_facets(stl_path)
    propeller_bbox = parse_stl_bounding_box(stl_path)
    print_section("STL Coverage")
    print(f"stl_file: {stl_path}")
    print(f"stl_ascii_facets: {stl_facets if stl_facets is not None else 'unknown/binary'}")
    print(f"propeller_bounding_box: {propeller_bbox if propeller_bbox else 'unknown'}")
    if prop_patch is not None:
        print(f"propeller_patch_faces: {prop_patch['nFaces']}")
        print("coverage_note: patch name exists; exact all-surface coverage requires visual/STL region inspection.")
        if stl_facets and int(prop_patch["nFaces"]) < max(50, stl_facets * 0.05):
            warnings.append("propeller patch face count is tiny compared with STL facet count")

    print_section("MRFProperties")
    case_info = load_case_info(case_path / "case_info.json")
    if case_info:
        print(f"solver_mode: {case_info.get('solver_mode', 'incompressible_mrf')}")
        print(f"Vinf_mps: {case_info.get('Vinf_mps', case_info.get('freestream_velocity_mps', 'unknown'))}")
        print(f"freestream_Mach: {case_info.get('freestream_Mach', 'unknown')}")
        print(f"helical_tip_Mach: {case_info.get('helical_tip_Mach', 'unknown')}")
        print(f"advance_ratio_J: {case_info.get('advance_ratio_J', 'unknown')}")
        warnings.extend(mach_warnings(case_info))
    mrf_path = case_path / "constant" / "MRFProperties"
    if mrf_path.is_file():
        mrf_text = read_text(mrf_path)
        cell_zone = parse_entry(mrf_text, "cellZone")
        origin = parse_entry(mrf_text, "origin")
        axis = parse_entry(mrf_text, "axis")
        omega = parse_entry(mrf_text, "omega")
        print("active: yes")
        print(f"cellZone: {cell_zone}")
        print(f"origin: {origin}")
        print(f"axis: {axis}")
        print(f"omega: {omega}")
        info_path = case_path / "case_info.json"
        if info_path.is_file() and omega is not None:
            info = json.loads(read_text(info_path))
            expected = float(info["omega_rad_s"])
            actual = float(NUMBER_RE.search(omega).group(0))  # type: ignore[union-attr]
            print(f"case_info_omega_rad_s: {expected:.12g}")
            if abs(actual - expected) > 1e-6:
                warnings.append(f"omega inconsistent with case_info.json: MRF={actual}, case_info={expected}")
    else:
        print("active: no")
        warnings.append("missing MRFProperties")
        cell_zone = None

    print_section("MRF Zone Coverage")
    cylinder = parse_rotating_zone_cylinder(case_path / "system" / "topoSetDict")
    if cylinder:
        print(f"rotating_zone_point1: {cylinder['point1']}")
        print(f"rotating_zone_point2: {cylinder['point2']}")
        print(f"rotating_zone_radius: {cylinder['radius']}")
        print(f"rotating_zone_length: {cylinder['length']}")
        if propeller_bbox:
            fits, radial_margin, axial_margin = bbox_fits_in_z_cylinder(propeller_bbox, cylinder)
            print(f"propeller_fits_in_rotating_zone: {fits}")
            print(f"rotating_zone_radial_margin_m: {radial_margin:.8g}")
            print(f"rotating_zone_axial_margin_m: {axial_margin:.8g}")
            if not fits:
                warnings.append("propeller bounding box does not fit inside rotating zone")
            elif radial_margin < 0.005 or axial_margin < 0.005:
                warnings.append("rotating zone margin around propeller is small")
    else:
        print("rotating zone cylinder: not found in system/topoSetDict")
        warnings.append("could not determine rotating zone dimensions")

    print_section("Cell Zones")
    zones = parse_cell_zones(case_path / "constant" / "polyMesh" / "cellZones")
    if zones:
        for name, count in zones.items():
            print(f"{name}: cells={count}")
            if name == cell_zone and count < 1000:
                warnings.append(f"suspiciously small MRF zone '{name}': {count} cells")
    else:
        print("none")
        warnings.append("no cellZones found")

    checkmesh_summary = extract_checkmesh_summary(case_path / "log.checkMesh")
    total_cells = int(checkmesh_summary["cells"]) if "cells" in checkmesh_summary else None

    print_section("Mesh Counts")
    print(f"total_cells: {total_cells if total_cells is not None else 'unknown'}")
    if prop_patch is not None:
        print(f"propeller_boundary_faces: {prop_patch['nFaces']}")
    rotating_zone_cells = zones.get("rotatingZone")
    print(f"rotatingZone_cells: {rotating_zone_cells if rotating_zone_cells is not None else 'unknown'}")
    if total_cells is not None and total_cells > 3000000:
        warnings.append(f"total cell count is extremely high: {total_cells}")

    print_section("snappyHexMeshDict")
    snappy_text = read_text(case_path / "system" / "snappyHexMeshDict")
    add_layers = parse_entry(snappy_text, "addLayers")
    prop_level_match = re.search(r"\bpropeller\s*\{.*?\blevel\s+\(([^)]+)\)\s*;", snappy_text, re.DOTALL)
    regions_match = re.search(r"\brefinementRegions\s*\{(.*?)\n\s*\}", snappy_text, re.DOTALL)
    print(f"addLayers: {add_layers}")
    print(f"propeller_surface_level: {prop_level_match.group(1).strip() if prop_level_match else 'not found'}")
    print(f"refinementRegions: {regions_match.group(1).strip() if regions_match else 'not found'}")
    if add_layers == "false":
        warnings.append("addLayers false: no boundary layer mesh on propeller")
    if prop_level_match:
        levels = [int(value) for value in NUMBER_RE.findall(prop_level_match.group(1))]
        if levels and max(levels) <= 3:
            warnings.append(f"very coarse propeller refinement level: {levels}")
    if "tip" not in snappy_text.lower():
        warnings.append("no explicit refinement around blade tips")

    print_section("0/ Boundary Conditions")
    for field in ["U", "p", "k", "omega", "nut"]:
        path = case_path / "0" / field
        if not path.is_file():
            print(f"{field}: missing")
            continue
        types = boundary_field_types(path)
        print(f"{field}: {types}")
        if field == "U":
            prop_u = types.get("propeller")
            print(f"propeller_U_boundary_condition: {prop_u if prop_u else 'missing'}")
            if prop_u == "MRFnoSlip":
                print("propeller_U_boundary_condition_status: OK for steady MRF")
            elif prop_u == "noSlip":
                warnings.append(
                    "propeller U boundary condition is noSlip in an MRF rotating zone; "
                    "installed MRF examples use MRFnoSlip"
                )
            else:
                warnings.append(
                    f"propeller U boundary condition is {prop_u}; installed MRF examples use MRFnoSlip"
                )

    print_section("Domain Bounds")
    bounds = parse_block_bounds(case_path / "system" / "blockMeshDict")
    print(f"blockMesh_bounds: {bounds if bounds else 'not found'}")

    print_section("Force Function")
    force_ok, checked = force_uses_propeller(case_path)
    print(f"checked_files: {[str(path.relative_to(case_path)) for path in checked]}")
    print(f"uses_propeller_patch: {force_ok}")
    if not force_ok:
        warnings.append("force function not using propeller patch")

    print_section("checkMesh Summary")
    summary = checkmesh_summary
    if summary:
        for key, value in summary.items():
            print(f"{key}: {value}")
    else:
        print("log.checkMesh not available or no summary parsed")

    print_section("Warnings")
    if warnings:
        for warning in warnings:
            print(f"WARNING: {warning}")
    else:
        print("No diagnostic warnings")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

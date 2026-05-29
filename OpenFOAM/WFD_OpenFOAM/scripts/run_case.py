#!/usr/bin/env python3
"""Run selected sanity stages for one OpenFOAM case."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_STAGES = [
    ("blockMesh", ["blockMesh"]),
    ("checkMesh", ["checkMesh"]),
]
SNAPPY_STAGES = [
    ("blockMesh", ["blockMesh"]),
    # OpenFOAM 13 replaced surfaceFeatureExtract with surfaceFeatures.
    ("surfaceFeatureExtract", ["surfaceFeatures"]),
    ("snappyHexMesh", ["snappyHexMesh", "-overwrite"]),
    ("propellerPatchPreflight", []),
    ("topoSet", ["topoSet"]),
    ("checkMesh", ["checkMesh"]),
]
SOLVE_STAGES = [
    ("blockMesh", ["blockMesh"]),
    # OpenFOAM 13 replaced surfaceFeatureExtract with surfaceFeatures.
    ("surfaceFeatureExtract", ["surfaceFeatures"]),
    ("snappyHexMesh", ["snappyHexMesh", "-overwrite"]),
    ("propellerPatchPreflight", []),
    ("topoSet", ["topoSet"]),
    ("checkMesh", ["checkMesh"]),
    ("solve", []),
]
SOLVE_ONLY_STAGES = [
    ("solve", []),
]
NUMERIC_TIME_RE = re.compile(r"^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?$")


class CaseRunError(Exception):
    """Raised when a case command fails."""


def parse_boundary_patches(boundary_path: Path) -> dict[str, dict[str, str]]:
    if not boundary_path.is_file():
        return {}

    patches: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line in boundary_path.read_text(errors="replace").splitlines():
        stripped = line.strip().rstrip(";")
        if stripped and re.match(r"^[A-Za-z_]\w*$", stripped) and stripped not in {"FoamFile"}:
            current = stripped
            patches.setdefault(current, {})
        elif current and stripped.startswith("type"):
            patches[current]["type"] = stripped.split(None, 1)[1] if len(stripped.split(None, 1)) == 2 else ""
        elif current and stripped.startswith("nFaces"):
            patches[current]["nFaces"] = stripped.split(None, 1)[1] if len(stripped.split(None, 1)) == 2 else ""
    return patches


def snappy_patch_names(case_path: Path) -> list[str]:
    log_path = case_path / "log.snappyHexMesh"
    if not log_path.is_file():
        return []
    names = re.findall(r"\b(propeller_patch\d+)\b", log_path.read_text(errors="replace"))
    return sorted(set(names), key=lambda name: int(re.search(r"\d+$", name).group(0)) if re.search(r"\d+$", name) else 0)


def write_patch_diagnostic(case_path: Path, patches: dict[str, dict[str, str]], message: str) -> None:
    snappy_names = snappy_patch_names(case_path)
    payload = {
        "error_code": "MESH_PATCH_ERROR",
        "message": message,
        "expected_patch": "propeller",
        "actual_boundary_patches": patches,
        "snappy_propeller_patch_names_found": snappy_names[:200],
        "snappy_propeller_patch_count": len(snappy_names),
        "recommended_fix": "Regenerate the case with imported STL normalized to one propeller STL region, then rerun snappyHexMesh.",
    }
    (case_path / "patch_preflight_diagnostic.json").write_text(json.dumps(payload, indent=2) + "\n")
    lines = [
        "MESH_PATCH_ERROR",
        message,
        f"expected_patch: propeller",
        f"actual_boundary_patches: {', '.join(patches) if patches else 'none'}",
        f"snappy_propeller_patch_count: {len(snappy_names)}",
        "solver_was_not_launched: true",
    ]
    (case_path / "patch_preflight_diagnostic.txt").write_text("\n".join(lines) + "\n")
    (case_path / "log.patchPreflight").write_text("\n".join(lines) + "\n")
    (case_path / "log.rhoPimpleFoam").write_text("\n".join(lines) + "\n")


def write_create_patch_dict(case_path: Path, patch_names: list[str]) -> Path:
    create_patch = case_path / "system" / "createPatchDict"
    patch_list = "\n            ".join(patch_names)
    create_patch.write_text(
        """/*--------------------------------*- C++ -*----------------------------------*\\
| =========                 |                                                 |
\\*---------------------------------------------------------------------------*/
FoamFile
{
    format      ascii;
    class       dictionary;
    object      createPatchDict;
}

pointSync false;

patches
(
    {
        name propeller;
        patchInfo
        {
            type wall;
            inGroups (wall);
        }
        constructFrom patches;
        patches
        (
            %s
        );
    }
);
"""
        % patch_list
    )
    return create_patch


def ensure_propeller_patch(case_path: Path) -> None:
    boundary = case_path / "constant" / "polyMesh" / "boundary"
    patches = parse_boundary_patches(boundary)
    if "propeller" in patches:
        return
    propeller_parts = [name for name in patches if re.fullmatch(r"propeller_patch\d+", name)]
    if propeller_parts:
        propeller_parts.sort(key=lambda name: int(re.search(r"\d+$", name).group(0)))
        write_create_patch_dict(case_path, propeller_parts)
        run_command(case_path, "createPatch", ["createPatch", "-overwrite"])
        updated = parse_boundary_patches(boundary)
        if "propeller" in updated:
            return
        patches = updated
    message = (
        "Boundary patch preflight failed: final mesh boundary does not contain propeller "
        "and no propeller_patch* boundary patches could be combined."
    )
    write_patch_diagnostic(case_path, patches, message)
    raise CaseRunError(f"MESH_PATCH_ERROR: {message} See {case_path / 'patch_preflight_diagnostic.json'}")


def validate_existing_mesh(case_path: Path) -> None:
    poly_mesh = case_path / "constant" / "polyMesh"
    boundary = poly_mesh / "boundary"
    cell_zones = poly_mesh / "cellZones"

    missing = [path for path in (poly_mesh, boundary, cell_zones) if not path.exists()]
    if missing:
        missing_list = ", ".join(str(path.relative_to(case_path)) for path in missing)
        raise CaseRunError(
            f"Existing mesh is incomplete; missing {missing_list}. "
            "Run snappy mesh first, then retry --solve-only."
        )

    ensure_propeller_patch(case_path)


def clean_solver_outputs(case_path: Path) -> None:
    post_processing = case_path / "postProcessing"
    if post_processing.exists():
        shutil.rmtree(post_processing)

    for log_name in ("log.foamRun", "log.rhoPimpleFoam"):
        solver_log = case_path / log_name
        if solver_log.exists():
            solver_log.unlink()

    for path in case_path.iterdir():
        if path.is_dir() and path.name != "0" and NUMERIC_TIME_RE.match(path.name):
            shutil.rmtree(path)


def run_command(case_path: Path, stage: str, command: list[str]) -> str:
    if stage == "propellerPatchPreflight":
        ensure_propeller_patch(case_path)
        return "ok"
    if stage == "solve" and not command:
        command = solver_command(case_path)
        stage = command[0]
    log_path = case_path / f"log.{stage}"
    print(f"[run] Running {' '.join(command)} in {case_path}")

    with log_path.open("w") as log:
        result = subprocess.run(
            command,
            cwd=case_path,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )

    print(f"[run] Log saved to: {log_path}")
    if result.returncode != 0:
        raise CaseRunError(
            f"Command failed with exit code {result.returncode}: {' '.join(command)}. "
            f"See {log_path}"
        )

    return "ok"


def solver_mode(case_path: Path) -> str:
    info_path = case_path / "case_info.json"
    if not info_path.is_file():
        return "incompressible_mrf"
    try:
        info = json.loads(info_path.read_text())
    except (OSError, json.JSONDecodeError):
        return "incompressible_mrf"
    return str(info.get("solver_mode", "incompressible_mrf"))


def solver_command(case_path: Path) -> list[str]:
    if solver_mode(case_path) == "compressible_forward_mrf":
        return ["rhoPimpleFoam"]
    return ["foamRun", "-solver", "incompressibleFluid"]


def run_case(case_path: Path, stages: list[tuple[str, list[str]]] | None = None) -> dict[str, str]:
    case_path = case_path.resolve()
    if not case_path.is_dir():
        raise CaseRunError(f"Case directory does not exist: {case_path}")

    if stages is None:
        stages = DEFAULT_STAGES

    statuses: dict[str, str] = {}
    for stage, command in stages:
        statuses[stage] = run_command(case_path, stage, command)

    return statuses


def build_stage_list(args: argparse.Namespace) -> list[tuple[str, list[str]]]:
    if args.solve_only:
        return SOLVE_ONLY_STAGES

    if args.solve:
        return SOLVE_STAGES

    if args.snappy:
        return SNAPPY_STAGES

    return DEFAULT_STAGES


def main() -> int:
    parser = argparse.ArgumentParser(description="Run sanity commands for one OpenFOAM case")
    parser.add_argument("case_path", type=Path, help="Path to an OpenFOAM case directory")
    parser.add_argument(
        "--snappy",
        action="store_true",
        help="Run blockMesh, surfaceFeatures, snappyHexMesh -overwrite, topoSet, and checkMesh",
    )
    parser.add_argument(
        "--solve",
        action="store_true",
        help="Run full serial mesh, MRF cellZone setup, checkMesh, and the case solver",
    )
    parser.add_argument(
        "--solve-only",
        action="store_true",
        help="Run the case solver on an existing mesh without rerunning blockMesh, snappyHexMesh, topoSet, or checkMesh",
    )
    args = parser.parse_args()

    selected_modes = sum(1 for value in (args.snappy, args.solve, args.solve_only) if value)
    if selected_modes > 1:
        print("ERROR: choose only one of --snappy, --solve, or --solve-only", file=sys.stderr)
        return 1

    try:
        if args.solve_only:
            case_path = args.case_path.resolve()
            validate_existing_mesh(case_path)
            clean_solver_outputs(case_path)
        run_case(args.case_path, build_stage_list(args))
    except (CaseRunError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("[run] Case stages completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

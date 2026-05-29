#!/usr/bin/env python3
"""Generate and run the current batch scaffold."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from generate_case import (
    CaseGenerationError,
    generate_variant_case,
    project_root,
    read_variants,
)
from run_case import CaseRunError, run_command


STATUS_COLUMNS = [
    "variant_id",
    "solver_mode",
    "Vinf_mps",
    "rpm",
    "omega_rad_s",
    "Tinf_K",
    "pinf_Pa",
    "gamma",
    "R",
    "Cp",
    "mu",
    "Pr",
    "diameter_m",
    "radius_m",
    "case_path",
    "blockMesh_status",
    "checkMesh_status",
    "overall_status",
    "notes",
]


def run_sanity_stages(
    case_path: Path,
    snappy: bool = False,
    solve: bool = False,
) -> tuple[dict[str, str], str | None]:
    statuses = {"blockMesh": "not_run", "checkMesh": "not_run"}
    stages = [
        ("blockMesh", ["blockMesh"]),
        ("checkMesh", ["checkMesh"]),
    ]

    if solve:
        stages = [
            ("blockMesh", ["blockMesh"]),
            # OpenFOAM 13 replaced surfaceFeatureExtract with surfaceFeatures.
            ("surfaceFeatureExtract", ["surfaceFeatures"]),
            ("snappyHexMesh", ["snappyHexMesh", "-overwrite"]),
            ("topoSet", ["topoSet"]),
            ("checkMesh", ["checkMesh"]),
            ("solve", []),
        ]
    elif snappy:
        stages = [
            ("blockMesh", ["blockMesh"]),
            # OpenFOAM 13 replaced surfaceFeatureExtract with surfaceFeatures.
            ("surfaceFeatureExtract", ["surfaceFeatures"]),
            ("snappyHexMesh", ["snappyHexMesh", "-overwrite"]),
            ("topoSet", ["topoSet"]),
            ("checkMesh", ["checkMesh"]),
        ]

    try:
        for stage, command in stages:
            result = run_command(case_path, stage, command)
            if stage in statuses:
                statuses[stage] = result
    except (CaseRunError, OSError) as exc:
        return statuses, str(exc)

    return statuses, None


def write_status_csv(results_path: Path, rows: list[dict[str, str]]) -> None:
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with results_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=STATUS_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate all cases and run sanity stages")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing case directories under cases/",
    )
    parser.add_argument(
        "--snappy",
        action="store_true",
        help="Run surfaceFeatures, snappyHexMesh -overwrite, and topoSet before checkMesh",
    )
    parser.add_argument(
        "--solve",
        action="store_true",
        help="Run the full serial mesh, topoSet, checkMesh, and case solver sequence",
    )
    args = parser.parse_args()

    root = project_root()
    status_rows: list[dict[str, str]] = []

    try:
        variants = read_variants(root / "variants.csv")
    except CaseGenerationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    for row_number, row in enumerate(variants, start=2):
        variant_id = row.get("variant_id", "").strip()
        if row.get("batch_enabled", "true").strip().lower() in ("0", "false", "no"):
            print(f"[batch] Skipping debug/non-batch variant: {variant_id or f'row {row_number}'}")
            continue
        print(f"[batch] Processing variant: {variant_id or f'row {row_number}'}")
        expected_case_path = root / "cases" / variant_id if variant_id else None

        status_row = {
            "variant_id": variant_id,
            "solver_mode": row.get("solver_mode", "incompressible_mrf"),
            "Vinf_mps": row.get("Vinf_mps", row.get("freestream_velocity_mps", "")),
            "rpm": row.get("rpm", ""),
            "omega_rad_s": "",
            "Tinf_K": row.get("Tinf_K", "288.15"),
            "pinf_Pa": row.get("pinf_Pa", "101325"),
            "gamma": row.get("gamma", "1.4"),
            "R": row.get("R", "287"),
            "Cp": row.get("Cp", "1004.5"),
            "mu": row.get("mu", "1.7894e-5"),
            "Pr": row.get("Pr", "0.71"),
            "diameter_m": row.get("diameter_m", ""),
            "radius_m": "",
            "case_path": str(expected_case_path.relative_to(root)) if expected_case_path else "",
            "blockMesh_status": "not_run",
            "checkMesh_status": "not_run",
            "overall_status": "failed",
            "notes": "",
        }

        try:
            case_path = generate_variant_case(root, row, row_number, overwrite=args.overwrite)
            status_row["case_path"] = str(case_path.relative_to(root))
            info_path = case_path / "case_info.json"
            if info_path.is_file():
                info = json.loads(info_path.read_text())
                status_row["omega_rad_s"] = f"{float(info['omega_rad_s']):.12g}"
                status_row["radius_m"] = f"{float(info['radius_m']):.12g}"
        except CaseGenerationError as exc:
            status_row["notes"] = str(exc)
            status_rows.append(status_row)
            print(f"[batch] Generation failed: {exc}")
            continue

        stage_statuses, error = run_sanity_stages(
            case_path,
            snappy=args.snappy,
            solve=args.solve,
        )
        status_row["blockMesh_status"] = stage_statuses["blockMesh"]
        status_row["checkMesh_status"] = stage_statuses["checkMesh"]

        if error is None:
            status_row["overall_status"] = "ok"
            status_row["notes"] = (
                "solve stages completed"
                if args.solve
                else (
                    "snappy meshing stages completed"
                    if args.snappy
                    else "sanity stages completed"
                )
            )
        else:
            status_row["overall_status"] = "failed"
            status_row["notes"] = error
            print(f"[batch] Case failed: {error}")

        status_rows.append(status_row)

    status_path = root / "results" / "batch_status.csv"
    write_status_csv(status_path, status_rows)
    print(f"[batch] Status written to: {status_path}")

    failed = [row for row in status_rows if row["overall_status"] != "ok"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

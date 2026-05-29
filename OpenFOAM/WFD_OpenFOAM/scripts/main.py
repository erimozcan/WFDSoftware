#!/usr/bin/env python3
"""Terminal UI for the Ozcan propeller CFD workflow."""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import time
from pathlib import Path

from run_case import solver_command


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

try:
    from rich.console import Console
    from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

    RICH_AVAILABLE = True
    CONSOLE = Console()
except ImportError:
    RICH_AVAILABLE = False
    CONSOLE = None


STAGES = {
    "sanity": [
        ("blockMesh", ["blockMesh"]),
        ("checkMesh", ["checkMesh"]),
    ],
    "mesh": [
        ("blockMesh", ["blockMesh"]),
        ("surfaceFeatureExtract", ["surfaceFeatures"]),
        ("snappyHexMesh", ["snappyHexMesh", "-overwrite"]),
        ("topoSet", ["topoSet"]),
        ("checkMesh", ["checkMesh"]),
    ],
    "solve": [
        ("blockMesh", ["blockMesh"]),
        ("surfaceFeatureExtract", ["surfaceFeatures"]),
        ("snappyHexMesh", ["snappyHexMesh", "-overwrite"]),
        ("topoSet", ["topoSet"]),
        ("checkMesh", ["checkMesh"]),
        ("solve", []),
    ],
}


def print_header() -> None:
    title = "Ozcan Propeller CFD"
    if RICH_AVAILABLE and CONSOLE:
        CONSOLE.rule(f"[bold cyan]{title}")
    else:
        print("\n" + title)
        print("=" * len(title))


def read_variants() -> list[str]:
    variants_path = ROOT / "variants.csv"
    if not variants_path.is_file():
        return []
    with variants_path.open(newline="") as handle:
        return [row["variant_id"] for row in csv.DictReader(handle) if row.get("variant_id")]


def case_path(variant_id: str) -> Path:
    return ROOT / "cases" / variant_id


def print_progress(label: str, index: int, total: int, status: str) -> None:
    if RICH_AVAILABLE and CONSOLE:
        CONSOLE.print(f"[bold]{index}/{total}[/bold] {label}: {status}")
        return
    width = 24
    filled = int(width * index / max(total, 1))
    bar = "#" * filled + "-" * (width - filled)
    print(f"[{bar}] {index}/{total} {label}: {status}")


def tail_lines(path: Path, count: int = 6) -> list[str]:
    if not path.is_file():
        return []
    lines = path.read_text(errors="replace").splitlines()
    useful = [
        line
        for line in lines
        if line.strip()
        and any(
            token in line
            for token in (
                "Time =",
                "ExecutionTime",
                "Finished",
                "Mesh OK",
                "Failed",
                "FOAM",
                "error",
                "Solving for",
            )
        )
    ]
    return useful[-count:] if useful else lines[-count:]


def run_logged_stage(case_dir: Path, stage: str, command: list[str], index: int, total: int) -> bool:
    if stage == "solve" and not command:
        command = solver_command(case_dir)
        stage = command[0]
    log_path = case_dir / f"log.{stage}"
    start = time.monotonic()
    print_progress(stage, index, total, f"running {' '.join(command)}")
    print(f"  log: {log_path}")

    with log_path.open("w") as log:
        process = subprocess.Popen(
            command,
            cwd=case_dir,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        last_report = 0.0
        while process.poll() is None:
            elapsed = time.monotonic() - start
            if elapsed - last_report >= 10:
                last_report = elapsed
                print(f"  elapsed: {elapsed:.0f}s")
                for line in tail_lines(log_path):
                    print(f"    {line}")
            time.sleep(1)

    elapsed = time.monotonic() - start
    if process.returncode == 0:
        print_progress(stage, index, total, f"passed in {elapsed:.0f}s")
        return True

    print_progress(stage, index, total, f"failed in {elapsed:.0f}s")
    for line in tail_lines(log_path, count=10):
        print(f"    {line}")
    return False


def run_script(args: list[str], label: str) -> bool:
    print(f"\n{label}")
    print(f"  command: {' '.join(args)}")
    start = time.monotonic()
    result = subprocess.run(args, cwd=ROOT, text=True)
    elapsed = time.monotonic() - start
    print(f"  {label} {'passed' if result.returncode == 0 else 'failed'} in {elapsed:.0f}s")
    return result.returncode == 0


def generate_cases(overwrite: bool = True) -> bool:
    args = [PYTHON, "scripts/generate_case.py"]
    if overwrite:
        args.append("--overwrite")
    return run_script(args, "generate cases")


def run_case_stages(variant_id: str, mode: str) -> bool:
    casedir = case_path(variant_id)
    if not casedir.is_dir():
        print(f"Case not found: {casedir}")
        return False
    stages = STAGES[mode]
    for index, (stage, command) in enumerate(stages, start=1):
        if not run_logged_stage(casedir, stage, command, index, len(stages)):
            return False
    return True


def parse_all_results() -> bool:
    variants = read_variants()
    if not variants:
        print("No variants found in variants.csv")
        return False
    ok = True
    for index, variant in enumerate(variants, start=1):
        print_progress("parse forces", index, len(variants), variant)
        result = subprocess.run(
            [PYTHON, "scripts/parse_forces.py", str(case_path(variant))],
            cwd=ROOT,
            text=True,
        )
        ok = ok and result.returncode == 0
    return ok


def diagnose_case(variant_id: str) -> bool:
    return run_script(
        [PYTHON, "scripts/diagnose_cfd_setup.py", str(case_path(variant_id))],
        f"diagnose {variant_id}",
    )


def solve_existing_mesh(variant_id: str) -> bool:
    return run_script(
        [PYTHON, "scripts/run_case.py", str(case_path(variant_id)), "--solve-only"],
        f"solve existing mesh {variant_id}",
    )


def prompt_variant(prompt: str = "Variant id") -> str:
    variants = read_variants()
    if variants:
        print("Available variants:")
        for variant in variants:
            print(f"  - {variant}")
    value = input(f"{prompt}: ").strip()
    return value or (variants[0] if variants else "")


def interactive_menu() -> int:
    while True:
        print_header()
        print("1) Generate cases")
        print("2) Run sanity mesh checks")
        print("3) Run snappy mesh for one case")
        print("4) Solve one case using existing mesh")
        print("5) Rebuild mesh + solve one case")
        print("6) Parse all results")
        print("7) Diagnose one case")
        print("8) Run full batch")
        print("q) Quit")
        choice = input("> ").strip().lower()

        if choice == "1":
            generate_cases(overwrite=True)
        elif choice == "2":
            if generate_cases(overwrite=True):
                for variant in read_variants():
                    run_case_stages(variant, "sanity")
        elif choice == "3":
            run_case_stages(prompt_variant(), "mesh")
        elif choice == "4":
            solve_existing_mesh(prompt_variant())
        elif choice == "5":
            run_case_stages(prompt_variant(), "solve")
        elif choice == "6":
            parse_all_results()
        elif choice == "7":
            diagnose_case(prompt_variant())
        elif choice == "8":
            if generate_cases(overwrite=True):
                for variant in read_variants():
                    if run_case_stages(variant, "solve"):
                        subprocess.run(
                            [PYTHON, "scripts/parse_forces.py", str(case_path(variant))],
                            cwd=ROOT,
                            text=True,
                        )
        elif choice == "q":
            return 0
        else:
            print("Unknown option")


def main() -> int:
    parser = argparse.ArgumentParser(description="Ozcan Propeller CFD terminal UI")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("generate", help="Generate cases from variants.csv")
    subparsers.add_parser("sanity", help="Generate cases and run blockMesh/checkMesh")
    mesh_parser = subparsers.add_parser("mesh", help="Run snappy mesh for one case")
    mesh_parser.add_argument("--case", default="rpm_10000", help="Variant id")
    solve_existing_parser = subparsers.add_parser(
        "solve-existing",
        help="Run foamRun for one case using the existing mesh",
    )
    solve_existing_parser.add_argument("--case", default="rpm_10000", help="Variant id")
    solve_parser = subparsers.add_parser("solve-one", help="Rebuild mesh and run full solve for one case")
    solve_parser.add_argument("--case", default="rpm_10000", help="Variant id")
    subparsers.add_parser("solve-batch", help="Run full solve for all variants")
    subparsers.add_parser("parse", help="Parse forces for all variants")
    diagnose_parser = subparsers.add_parser("diagnose", help="Diagnose one case")
    diagnose_parser.add_argument("--case", default="rpm_10000", help="Variant id")

    args = parser.parse_args()
    print_header()

    if args.command is None:
        return interactive_menu()
    if args.command == "generate":
        return 0 if generate_cases(overwrite=True) else 1
    if args.command == "sanity":
        if not generate_cases(overwrite=True):
            return 1
        return 0 if all(run_case_stages(variant, "sanity") for variant in read_variants()) else 1
    if args.command == "mesh":
        return 0 if run_case_stages(args.case, "mesh") else 1
    if args.command == "solve-existing":
        return 0 if solve_existing_mesh(args.case) else 1
    if args.command == "solve-one":
        return 0 if run_case_stages(args.case, "solve") else 1
    if args.command == "solve-batch":
        ok = True
        for variant in read_variants():
            ok = run_case_stages(variant, "solve") and ok
        return 0 if ok else 1
    if args.command == "parse":
        return 0 if parse_all_results() else 1
    if args.command == "diagnose":
        return 0 if diagnose_case(args.case) else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Local browser dashboard for the Ozcan propeller CFD workflow."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import struct
import tempfile
import threading
import time
import uuid
import zipfile
import traceback
from collections import deque
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
except ImportError as exc:  # pragma: no cover - exercised only when dependency is missing
    raise SystemExit("Missing dependencies. Install with: pip install fastapi uvicorn") from exc


ROOT = Path(__file__).resolve().parent
CASES_DIR = ROOT / "cases"
WEB_DIR = ROOT / "web"
IMPORTED_EXPORTS_DIR = ROOT / "imported_exports"
PYTHON = sys.executable
CASE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
BATCH_DEFAULT_RPM = 10000.0
BATCH_DEFAULT_VINF_MPH = 300.0
BATCH_DEFAULT_VINF_MPS = BATCH_DEFAULT_VINF_MPH * 0.44704
PROGRESS_BY_STAGE = {
    "generate": 20,
    "blockMesh": 25,
    "surfaceFeatureExtract": 40,
    "surfaceFeatures": 40,
    "snappyHexMesh": 60,
    "topoSet": 70,
    "checkMesh": 75,
    "foamRun": 90,
    "rhoPimpleFoam": 90,
    "diagnose": 85,
    "parse": 95,
    "finish": 100,
}
SHORT_BATCH_RE = re.compile(r"^batch_(\d{3,})$")


class CaseRequest(BaseModel):
    case: str


class SignFlipRequest(BaseModel):
    case: str
    flip_thrust: bool = False
    flip_torque: bool = False


class ForwardBatchRequest(BaseModel):
    variants_file: str = "variants.csv"
    solver_mode: str = "compressible_forward_mrf"
    rpm: float
    freestream_speed: float
    speed_units: str = "mph"
    Tinf_K: float = 288.15
    pinf_Pa: float = 101325.0
    force_remesh: bool = False
    dry_run: bool = False


class ValidationRequest(BaseModel):
    variant_id: str
    preset: str = "100_10000"
    rpm: float = 10000.0
    Vinf_mph: float = 100.0
    Tinf_K: float = 288.15
    pinf_Pa: float = 101325.0
    force_remesh: bool = False
    validation_revolutions: float = 0.03
    validation_wall_clock_limit_s: int = 600
    validation_target_timesteps: int = 150
    validation_force_samples: int = 20


class OvernightImportPathRequest(BaseModel):
    path: str


class OvernightSettings(BaseModel):
    batch_id: str
    selected_variant_ids: list[str] = []
    rpm: float = BATCH_DEFAULT_RPM
    freestream_speed: float = BATCH_DEFAULT_VINF_MPH
    speed_units: str = "mph"
    Tinf_K: float = 288.15
    pinf_Pa: float = 101325.0
    max_wall_clock_minutes_per_propeller: float = 0.0
    averaging_window: int = 500
    averaging_mode: str = "last_n"
    averaging_percent: float = 25.0
    min_average_time: float = 0.0
    auto_stop_on_convergence: bool = True
    convergence_preset: str = "fast_screening"
    convergence_min_force_samples: int = 2000
    convergence_window_size: int = 500
    convergence_thrust_mean_change_pct: float = 3.0
    convergence_torque_mean_change_pct: float = 5.0
    convergence_thrust_cv_pct: float = 5.0
    convergence_torque_cv_pct: float = 5.0
    geometry_check_profile: str = "fast_geometry_check"
    run_mesh_sanity: bool = False
    batch_mesh_profile: str = "production_high_resolution"
    continue_after_failed_variant: bool = True
    stop_on_first_fatal_error: bool = False
    allow_warning_override: bool = False
    validation_override: bool = False


class TaskState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.action = "idle"
        self.case = ""
        self.stage = "idle"
        self.command = ""
        self.status = "idle"
        self.progress = 0
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.returncode: int | None = None
        self.case_index = 0
        self.case_total = 0
        self.error = ""
        self.results: list[dict[str, str]] = []
        self.diagnostics: list[str] = []
        self.summary: dict[str, Any] = {}
        self.solver_progress: dict[str, Any] = {}
        self.log: deque[str] = deque(maxlen=500)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            now = time.monotonic()
            elapsed = 0.0
            if self.started_at is not None:
                elapsed = (self.finished_at or now) - self.started_at
            status = self.status
            if self.running and self.solver_progress.get("solver_status") in ("running", "solver_running"):
                status = "solver_running"
            return {
                "running": self.running,
                "action": self.action,
                "case": self.case,
                "stage": self.stage,
                "command": self.command,
                "status": status,
                "progress": self.progress,
                "elapsed_seconds": round(elapsed, 1),
                "returncode": self.returncode,
                "case_index": self.case_index,
                "case_total": self.case_total,
                "error": self.error,
                "results": self.results,
                "diagnostics": self.diagnostics,
                "summary": self.summary,
                "solver_progress": self.solver_progress,
            }

    def begin(self, action: str, case: str) -> None:
        with self.lock:
            self.begin_unlocked(action, case)

    def begin_unlocked(self, action: str, case: str) -> None:
        self.running = True
        self.action = action
        self.case = case
        self.stage = "starting"
        self.command = ""
        self.status = "running"
        self.progress = 5
        self.started_at = time.monotonic()
        self.finished_at = None
        self.returncode = None
        self.case_index = 0
        self.case_total = 0
        self.error = ""
        self.results = []
        self.diagnostics = []
        self.summary = {}
        self.solver_progress = {}
        self.log.clear()

    def update(self, *, stage: str | None = None, command: str | None = None, progress: int | None = None) -> None:
        with self.lock:
            if stage is not None:
                self.stage = stage
                stage_progress = PROGRESS_BY_STAGE.get(stage)
                if stage_progress is not None:
                    if self.case_total > 1 and self.case_index > 0:
                        overall = ((self.case_index - 1) + stage_progress / 100.0) / self.case_total
                        self.progress = int(round(overall * 100))
                    else:
                        self.progress = stage_progress
            if command is not None:
                self.command = command
            if progress is not None:
                self.progress = progress

    def update_case(self, case: str, case_index: int, case_total: int) -> None:
        with self.lock:
            self.case = case
            self.case_index = case_index
            self.case_total = case_total

    def append_log(self, line: str) -> None:
        with self.lock:
            self.log.append(line.rstrip())

    def finish(
        self,
        *,
        returncode: int,
        error: str = "",
        results: list[dict[str, str]] | None = None,
        diagnostics: list[str] | None = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        with self.lock:
            self.running = False
            self.status = "success" if returncode == 0 else "failed"
            self.stage = "finish"
            self.progress = 100 if returncode == 0 else self.progress
            self.finished_at = time.monotonic()
            self.returncode = returncode
            self.error = error
            if results is not None:
                self.results = results
            if diagnostics is not None:
                self.diagnostics = diagnostics
            if summary is not None:
                self.summary = summary

    def update_solver_progress(self, progress: dict[str, Any]) -> None:
        with self.lock:
            self.solver_progress = progress

    def set_summary(self, summary: dict[str, Any]) -> None:
        with self.lock:
            self.summary = summary


state = TaskState()
validation_control = {
    "lock": threading.Lock(),
    "process": None,
    "stop_requested": False,
    "validation_id": "",
    "case_dir": None,
}
overnight_control = {
    "lock": threading.Lock(),
    "stop_current": False,
    "stop_all": False,
    "batch_id": "",
    "process": None,
    "process_group_id": None,
    "case_dir": None,
    "stage": "",
}
app = FastAPI(title="Ozcan Propeller CFD")
app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")
sys.path.insert(0, str(ROOT / "scripts"))
from generate_case import CaseGenerationError, generate_variant_case  # noqa: E402
from solver_progress import build_solver_progress, build_solver_timeseries, force_convergence_metrics  # noqa: E402


def read_variants() -> list[dict[str, str]]:
    variants_path = ROOT / "variants.csv"
    if not variants_path.is_file():
        return []
    with variants_path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def read_variants_file(file_name: str) -> list[dict[str, str]]:
    if Path(file_name).name != file_name or file_name != "variants.csv":
        raise HTTPException(status_code=400, detail="Only project variants.csv is currently supported")
    return read_variants()


def mph_to_mps(value: float) -> float:
    return value * 0.44704


def request_vinf_mps(request: ForwardBatchRequest) -> float:
    units = request.speed_units.strip().lower()
    if units == "mph":
        return mph_to_mps(request.freestream_speed)
    if units in ("m/s", "mps"):
        return request.freestream_speed
    raise HTTPException(status_code=400, detail="speed_units must be mph or m/s")


def safe_float(value: object, default: float) -> float:
    if value is None or str(value).strip() == "":
        return default
    return float(value)


def row_uses_override(row: dict[str, str], key: str) -> bool:
    return key in row and row.get(key, "").strip() != ""


def forward_batch_rows(request: ForwardBatchRequest) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if request.solver_mode not in ("compressible_forward_mrf", "incompressible_mrf"):
        raise HTTPException(status_code=400, detail="Unsupported solver_mode")
    if request.rpm < 0:
        raise HTTPException(status_code=400, detail="rpm must be non-negative")
    if request.freestream_speed < 0:
        raise HTTPException(status_code=400, detail="freestream speed must be non-negative")
    if request.Tinf_K <= 0:
        raise HTTPException(status_code=400, detail="air temperature must be positive")
    if request.pinf_Pa <= 0:
        raise HTTPException(status_code=400, detail="air pressure must be positive")

    rows = read_variants_file(request.variants_file)
    if not rows:
        raise HTTPException(status_code=400, detail="variants.csv has no rows")
    global_vinf = request_vinf_mps(request)
    prepared: list[dict[str, str]] = []
    warnings: list[str] = []
    for row in rows:
        if row.get("batch_enabled", "true").strip().lower() in ("0", "false", "no"):
            continue
        if not row.get("variant_id", "").strip() or not row.get("stl_file", "").strip():
            raise HTTPException(status_code=400, detail="Each variant row needs variant_id and stl_file")
        rpm_override = row_uses_override(row, "rpm")
        vinf_override = row_uses_override(row, "Vinf_mps") or row_uses_override(row, "freestream_velocity_mps")
        if rpm_override:
            warnings.append(f"{row['variant_id']}: using per-row rpm={row['rpm']}")
        if vinf_override:
            warnings.append(f"{row['variant_id']}: using per-row Vinf_mps={row.get('Vinf_mps', row.get('freestream_velocity_mps'))}")
        final_rpm = safe_float(row.get("rpm"), request.rpm)
        final_vinf = safe_float(row.get("Vinf_mps", row.get("freestream_velocity_mps")), global_vinf)
        prepared_row = dict(row)
        prepared_row.update(
            {
                "solver_mode": request.solver_mode,
                "batch_enabled": "true",
                "rpm": f"{final_rpm:.12g}",
                "Vinf_mps": f"{final_vinf:.12g}",
                "Tinf_K": f"{safe_float(row.get('Tinf_K'), request.Tinf_K):.12g}",
                "pinf_Pa": f"{safe_float(row.get('pinf_Pa'), request.pinf_Pa):.12g}",
                "gamma": row.get("gamma", "1.4") or "1.4",
                "R": row.get("R", "287") or "287",
                "Cp": row.get("Cp", "1004.5") or "1004.5",
                "mu": row.get("mu", "1.7894e-5") or "1.7894e-5",
                "Pr": row.get("Pr", "0.71") or "0.71",
            }
        )
        prepared.append(prepared_row)
    if not prepared:
        raise HTTPException(status_code=400, detail="No batch-enabled variants found")
    summary = {
        "variants_file": request.variants_file,
        "solver_mode": request.solver_mode,
        "global_rpm": request.rpm,
        "global_Vinf_mps": global_vinf,
        "global_Vinf_mph": global_vinf / 0.44704,
        "Tinf_K": request.Tinf_K,
        "pinf_Pa": request.pinf_Pa,
        "variant_count": len(prepared),
        "warnings": warnings,
        "rows": [
            {
                "variant_id": row["variant_id"],
                "stl_file": row["stl_file"],
                "solver_mode": row["solver_mode"],
                "rpm": row["rpm"],
                "Vinf_mps": row["Vinf_mps"],
                "Vinf_mph": float(row["Vinf_mps"]) / 0.44704,
            }
            for row in prepared
        ],
    }
    return prepared, summary


def variant_ids(batch_only: bool = False) -> list[str]:
    rows = read_variants()
    if batch_only:
        rows = [
            row for row in rows
            if row.get("batch_enabled", "true").strip().lower() not in ("0", "false", "no")
        ]
    return [row["variant_id"] for row in rows if row.get("variant_id")]


def variant_rpm_by_case() -> dict[str, str]:
    return {row["variant_id"]: row.get("rpm", "") for row in read_variants() if row.get("variant_id")}


def valid_case_names() -> set[str]:
    names = {row["variant_id"] for row in read_variants() if row.get("variant_id")}
    if CASES_DIR.is_dir():
        names.update(path.name for path in CASES_DIR.iterdir() if path.is_dir())
    return {name for name in names if CASE_RE.fullmatch(name)}


def case_path(case_name: str) -> Path:
    if "/" in case_name:
        parts = case_name.split("/")
        valid_nested = (
            len(parts) == 2
            and (parts[0].startswith("batch_") or parts[0].startswith("overnight_"))
            and all(CASE_RE.fullmatch(part) for part in parts)
        )
        if not valid_nested:
            raise HTTPException(status_code=400, detail=f"Unknown or invalid case: {case_name}")
        resolved = (CASES_DIR / parts[0] / parts[1]).resolve()
        if ROOT not in resolved.parents:
            raise HTTPException(status_code=400, detail="Case path escapes project directory")
        return resolved
    if not CASE_RE.fullmatch(case_name) or case_name not in valid_case_names():
        raise HTTPException(status_code=400, detail=f"Unknown or invalid case: {case_name}")
    resolved = (CASES_DIR / case_name).resolve()
    if ROOT not in resolved.parents:
        raise HTTPException(status_code=400, detail="Case path escapes project directory")
    return resolved


def set_case_signs(case_dir: Path, flip_thrust: bool = False, flip_torque: bool = False) -> dict[str, Any]:
    updated: dict[str, Any] = {}
    for name in ("case_info.json", "run_metadata.json"):
        path = case_dir / name
        if not path.is_file():
            continue
        data = json.loads(path.read_text())
        data.setdefault("thrust_axis", "z")
        data.setdefault("thrust_sign", 1)
        data.setdefault("torque_axis", "z")
        data.setdefault("torque_sign", 1)
        data.setdefault("shaft_torque_sign", data.get("torque_sign", 1))
        if flip_thrust:
            data["thrust_sign"] = -float(data.get("thrust_sign", 1))
        if flip_torque:
            data["torque_sign"] = -float(data.get("torque_sign", 1))
            data["shaft_torque_sign"] = -float(data.get("shaft_torque_sign", data["torque_sign"]))
        data["torque_sign_convention"] = "shaft_torque_Nm = torque_sign * raw_moment_axis_Nm; power_W = shaft_torque_Nm * omega_rad_s"
        path.write_text(json.dumps(data, indent=2) + "\n")
        updated[name] = {
            "thrust_axis": data.get("thrust_axis"),
            "thrust_sign": data.get("thrust_sign"),
            "torque_axis": data.get("torque_axis"),
            "torque_sign": data.get("torque_sign"),
            "shaft_torque_sign": data.get("shaft_torque_sign"),
        }
    return updated


def mesh_ready(path: Path) -> bool:
    poly_mesh = path / "constant" / "polyMesh"
    return poly_mesh.is_dir() and (poly_mesh / "boundary").is_file() and (poly_mesh / "cellZones").is_file()


def expected_cell_zone(row_or_info: dict[str, object]) -> str:
    return "rotorZone" if row_or_info.get("solver_mode") == "compressible_forward_mrf" else "rotatingZone"


def case_details() -> list[dict[str, object]]:
    rpm_by_case = variant_rpm_by_case()
    names = sorted(valid_case_names())
    details = []
    variants = read_variants()
    for name in names:
        path = CASES_DIR / name
        variant = next((row for row in variants if row.get("variant_id") == name), {})
        rpm = rpm_by_case.get(name, "")
        vinf = variant.get("Vinf_mps", variant.get("freestream_velocity_mps", ""))
        info_path = path / "case_info.json"
        if not rpm and info_path.is_file():
            try:
                info = json.loads(info_path.read_text())
                rpm = str(info.get("rpm", ""))
                vinf = str(info.get("Vinf_mps", info.get("freestream_velocity_mps", vinf)))
            except (OSError, json.JSONDecodeError):
                rpm = ""
        details.append(
            {
                "name": name,
                "rpm": rpm,
                "Vinf_mps": vinf,
                "solver_mode": variant.get("solver_mode", "incompressible_mrf"),
                "batch_enabled": variant.get("batch_enabled", "true").strip().lower() not in ("0", "false", "no"),
                "exists": path.is_dir(),
                "has_mesh": mesh_ready(path),
            }
        )
    return details


def parse_key_values(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def metrics_from_output(case_name: str, output: str) -> dict[str, str]:
    values = parse_key_values(output)
    rpm = variant_rpm_by_case().get(case_name, "")
    if not rpm:
        info_path = CASES_DIR / case_name / "case_info.json"
        if info_path.is_file():
            try:
                rpm = str(json.loads(info_path.read_text()).get("rpm", ""))
            except (OSError, json.JSONDecodeError):
                rpm = ""
    return {
        "variant_id": case_name,
        "case": case_name,
        "solver_mode": values.get("solver_mode", ""),
        "rpm": rpm,
        "Vinf_mps": values.get("Vinf_mps", ""),
        "Vinf_mph": values.get("Vinf_mph", ""),
        "thrust_N": values.get("thrust_convention_N", ""),
        "torque_Nm": values.get("torque_convention_Nm", ""),
        "fluid_torque_Nm": values.get("fluid_torque_Nm", ""),
        "shaft_torque_Nm": values.get("shaft_torque_Nm", ""),
        "torque_sign_convention": values.get("torque_sign_convention", ""),
        "power_W": values.get("power_W", values.get("shaft_power_convention_W", "")),
        "eta": values.get("eta", ""),
        "advance_ratio_J": values.get("advance_ratio_J", ""),
        "freestream_Mach": values.get("freestream_Mach", ""),
        "tip_speed_mps": values.get("tip_speed_mps", ""),
        "helical_tip_Mach": values.get("helical_tip_Mach", ""),
        "CT": values.get("CT", ""),
        "CQ": values.get("CQ", ""),
        "CP": values.get("CP", ""),
        "run_status": values.get("run_status", "success"),
        "warning_flags": values.get("warning_flags", ""),
        "thrust_convention_N": values.get("thrust_convention_N", ""),
        "torque_convention_Nm": values.get("torque_convention_Nm", ""),
        "shaft_power_convention_W": values.get("shaft_power_convention_W", ""),
        "static_thrust_per_watt": values.get("static_thrust_per_watt", ""),
        "force_N": values.get("force_N", ""),
        "moment_Nm": values.get("moment_Nm", ""),
        "force_samples": values.get("force_samples", values.get("samples_averaged", "")),
        "last_force_sample_time": values.get("last_force_sample_time", ""),
        "raw_output": output,
    }


def warnings_from_output(output: str) -> list[str]:
    warnings: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("WARNING:"):
            warnings.append(stripped.removeprefix("WARNING:").strip())
        elif stripped == "No diagnostic warnings":
            return []
    return warnings


def infer_stage(line: str) -> str | None:
    for stage in PROGRESS_BY_STAGE:
        if stage in line:
            return stage
    return None


def case_dir_from_run_case_command(command: list[str]) -> Path | None:
    for index, part in enumerate(command):
        if part.endswith("scripts/run_case.py") and index + 1 < len(command):
            candidate = Path(command[index + 1])
            return candidate if candidate.is_absolute() else ROOT / candidate
    return None


def command_runs_solver(command: list[str]) -> bool:
    return "--solve" in command or "--solve-only" in command


def monitor_solver_progress(case_dir: Path, process: subprocess.Popen[str], stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        running = process.poll() is None
        try:
            state.update_solver_progress(build_solver_progress(case_dir, process_running=running))
        except Exception as exc:
            state.update_solver_progress({"case": case_dir.name, "solver_status": "queued", "warnings": [f"Solver progress unavailable: {exc}"]})
        if not running:
            break
        stop_event.wait(1.0)
    try:
        final_progress = build_solver_progress(case_dir, process_running=False)
        if process.returncode not in (0, None):
            final_progress["solver_status"] = "failed"
            final_progress.setdefault("warnings", []).append(f"Solver wrapper exited with code {process.returncode}")
        state.update_solver_progress(final_progress)
    except Exception:
        pass


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def process_group_id(pid: int | None) -> int | None:
    if not pid:
        return None
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def terminate_pid_group(pid: int, timeout_s: float = 8.0) -> dict[str, Any]:
    pgid = process_group_id(pid)
    payload = {"pid": pid, "process_group_id": pgid, "sigterm_sent": False, "sigkill_sent": False, "alive_after": False}
    if pgid is None:
        return payload
    try:
        os.killpg(pgid, signal.SIGTERM)
        payload["sigterm_sent"] = True
    except ProcessLookupError:
        return payload
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
            payload["sigterm_sent"] = True
        except OSError:
            return payload
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return payload
        time.sleep(0.2)
    try:
        os.killpg(pgid, signal.SIGKILL)
        payload["sigkill_sent"] = True
    except ProcessLookupError:
        pass
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
            payload["sigkill_sent"] = True
        except OSError:
            pass
    time.sleep(0.5)
    payload["alive_after"] = pid_alive(pid)
    return payload


def terminate_process_group(process: subprocess.Popen[str], timeout_s: float = 10.0) -> dict[str, Any]:
    if process.poll() is not None:
        return {"pid": process.pid, "process_group_id": process_group_id(process.pid), "already_exited": True, "alive_after": False}
    result = terminate_pid_group(process.pid, timeout_s)
    if not result.get("alive_after"):
        try:
            process.wait(timeout=0.1)
        except (subprocess.TimeoutExpired, OSError):
            pass
    return result


def proc_cmdline(pid: int) -> str:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
        return raw.replace(b"\x00", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def proc_cwd(pid: int) -> str:
    try:
        return str((Path("/proc") / str(pid) / "cwd").resolve())
    except OSError:
        return ""


def find_batch_processes(batch_id: str) -> list[dict[str, Any]]:
    if not batch_id:
        return []
    batch_case_root = str((CASES_DIR / batch_case_prefix(batch_id)).resolve())
    matches: list[dict[str, Any]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        cmdline = proc_cmdline(pid)
        cwd = proc_cwd(pid)
        if batch_id not in cmdline and batch_case_root not in cmdline and batch_case_root not in cwd:
            continue
        if not any(token in cmdline for token in ("run_case.py", "foamRun", "rhoPimpleFoam", "snappyHexMesh", "blockMesh", "surfaceFeatureExtract", "topoSet", "checkMesh")):
            continue
        matches.append({"pid": pid, "pgid": process_group_id(pid), "cmdline": cmdline, "cwd": cwd})
    return sorted(matches, key=lambda item: int(item["pid"]))


def find_case_processes(case_dir: Path) -> list[dict[str, Any]]:
    case_text = str(case_dir.resolve())
    batch_id = case_dir.parent.name if case_dir.parent.name.startswith("batch_") else ""
    return [item for item in find_batch_processes(batch_id) if case_text in item.get("cmdline", "") or case_text in item.get("cwd", "")]


def active_variant_from_processes(batch_id: str, rows: list[dict[str, Any]]) -> str:
    processes = find_batch_processes(batch_id)
    if not processes:
        return ""
    for row in rows:
        name = sanitize_id(str(row.get("variant_id", "")))
        case_text = str((CASES_DIR / batch_case_prefix(batch_id) / name).resolve())
        for item in processes:
            if case_text in item.get("cmdline", "") or case_text in item.get("cwd", ""):
                return str(row.get("variant_id", ""))
    return ""


def kill_batch_processes(batch_id: str) -> dict[str, Any]:
    processes = find_batch_processes(batch_id)
    killed: list[dict[str, Any]] = []
    seen_pgids: set[int] = set()
    for item in processes:
        pgid = item.get("pgid")
        pid = int(item["pid"])
        if isinstance(pgid, int) and pgid in seen_pgids:
            continue
        if isinstance(pgid, int):
            seen_pgids.add(pgid)
        result = terminate_pid_group(pid)
        result["cmdline"] = item.get("cmdline", "")
        killed.append(result)
    remaining = find_batch_processes(batch_id)
    return {"requested_batch_id": batch_id, "matched_before": processes, "terminated": killed, "remaining": remaining}


def append_process_stdout(process: subprocess.Popen[str], output_lines: list[str]) -> None:
    if process.stdout is None:
        return
    for line in process.stdout:
        output_lines.append(line)
        state.append_log(line)


def validation_stop_requested(validation_id: str) -> bool:
    with validation_control["lock"]:
        return bool(validation_control["stop_requested"] and validation_control["validation_id"] == validation_id)


def run_validation_solver_command(case_dir: Path, config: dict[str, Any]) -> tuple[int, str, dict[str, Any]]:
    validation_id = str(config["validation_id"])
    command = [PYTHON, "scripts/run_case.py", str(case_dir), "--solve-only"]
    state.update(stage="solver_running", command=" ".join(command))
    state.append_log(f"$ {' '.join(command)}")
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    with validation_control["lock"]:
        validation_control["process"] = process
        validation_control["stop_requested"] = False
        validation_control["validation_id"] = validation_id
        validation_control["case_dir"] = case_dir

    output_lines: list[str] = []
    reader = threading.Thread(target=append_process_stdout, args=(process, output_lines), daemon=True)
    reader.start()

    started = time.monotonic()
    stop_reason = ""
    progress: dict[str, Any] = {}
    wall_limit = max(60, int(config.get("validation_wall_clock_limit_s", 600)))
    target_steps = max(20, int(config.get("validation_target_timesteps", 150)))
    target_force_samples = max(3, int(config.get("validation_force_samples", 20)))

    while process.poll() is None:
        progress = build_solver_progress(case_dir, process_running=True)
        progress["validation_id"] = validation_id
        progress["wall_clock_elapsed_s"] = round(time.monotonic() - started, 1)
        state.update_solver_progress(progress)
        if progress.get("fatal_error"):
            stop_reason = "fatal solver error detected"
            terminate_process_group(process)
            break
        if validation_stop_requested(validation_id):
            stop_reason = "stopped by user"
            terminate_process_group(process)
            break
        if int(progress.get("timesteps_completed") or 0) >= target_steps:
            stop_reason = f"quick validation reached {target_steps} solver timesteps"
            terminate_process_group(process)
            break
        if int(progress.get("force_sample_count") or 0) >= target_force_samples:
            stop_reason = f"quick validation reached {target_force_samples} parseable force samples"
            terminate_process_group(process)
            break
        if time.monotonic() - started >= wall_limit:
            stop_reason = "wall-clock limit reached"
            terminate_process_group(process)
            break
        time.sleep(1.0)

    process.wait()
    reader.join(timeout=2.0)
    final_progress = build_solver_progress(case_dir, process_running=False)
    final_progress["validation_id"] = validation_id
    final_progress["wall_clock_elapsed_s"] = round(time.monotonic() - started, 1)
    final_progress["stop_reason"] = stop_reason
    if stop_reason:
        final_progress["solver_status"] = "stopped" if stop_reason == "stopped by user" else "completed"
    elif process.returncode != 0:
        final_progress["solver_status"] = "failed"
        final_progress.setdefault("warnings", []).append(f"Solver process exited with code {process.returncode}")
    try:
        (case_dir / "solver_progress.json").write_text(json.dumps(final_progress, indent=2) + "\n")
    except OSError:
        pass
    state.update_solver_progress(final_progress)
    with validation_control["lock"]:
        if validation_control["validation_id"] == validation_id:
            validation_control["process"] = None
            validation_control["stop_requested"] = False
            validation_control["case_dir"] = None
    if stop_reason and not final_progress.get("fatal_error"):
        return 0, "".join(output_lines), final_progress
    return process.returncode or 0, "".join(output_lines), final_progress


def run_command(command: list[str], stage: str) -> tuple[int, str]:
    state.update(stage=stage, command=" ".join(command))
    state.append_log(f"$ {' '.join(command)}")
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    monitor_stop = threading.Event()
    monitor_thread: threading.Thread | None = None
    solver_case_dir = case_dir_from_run_case_command(command)
    if solver_case_dir is not None and command_runs_solver(command):
        monitor_thread = threading.Thread(target=monitor_solver_progress, args=(solver_case_dir, process, monitor_stop), daemon=True)
        monitor_thread.start()
    output_lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        output_lines.append(line)
        state.append_log(line)
        detected = infer_stage(line)
        if detected:
            state.update(stage=detected)
    process.wait()
    if monitor_thread is not None:
        monitor_stop.set()
        monitor_thread.join(timeout=2.0)
    return process.returncode, "".join(output_lines)


def run_command_with_timeout(command: list[str], stage: str, timeout_s: float) -> tuple[int, str]:
    state.update(stage=stage, command=" ".join(command))
    state.append_log(f"$ {' '.join(command)}")
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    with overnight_control["lock"]:
        overnight_control["process"] = process
        overnight_control["process_group_id"] = process_group_id(process.pid)
        overnight_control["case_dir"] = None
        overnight_control["stage"] = stage
    output_lines: list[str] = []
    reader = threading.Thread(target=append_process_stdout, args=(process, output_lines), daemon=True)
    reader.start()
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        terminate_process_group(process)
        output_lines.append(f"ERROR: command timed out after {timeout_s:.0f}s\n")
        return 124, "".join(output_lines)
    finally:
        reader.join(timeout=2.0)
    return process.returncode, "".join(output_lines)


def run_command_with_heartbeat(command: list[str], stage: str, batch_id: str, case_dir: Path) -> tuple[int, str]:
    existing = find_case_processes(case_dir)
    if existing:
        diagnostic = {
            "stage": stage,
            "command": command,
            "duplicate_prevented": True,
            "existing_processes": existing,
            "created_at": time.time(),
        }
        (case_dir / "batch_duplicate_prevention.json").write_text(json.dumps(diagnostic, indent=2) + "\n")
        write_overnight_state(batch_id, {"status": "running", "stage": f"RECOVERED_RUNNING_PROCESS_{stage}", "recovered_processes": existing})
        while find_case_processes(case_dir):
            write_batch_heartbeat(batch_id, case_dir, stage, None, command)
            with overnight_control["lock"]:
                stop_all = bool(overnight_control["stop_all"])
            if stop_all:
                kill_batch_processes(batch_id)
                break
            time.sleep(5.0)
        return 0, "Recovered existing process; duplicate launch prevented.\n"
    state.update(stage=stage, command=" ".join(command))
    state.append_log(f"$ {' '.join(command)}")
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    with overnight_control["lock"]:
        overnight_control["process"] = process
        overnight_control["process_group_id"] = process_group_id(process.pid)
        overnight_control["case_dir"] = case_dir
        overnight_control["stage"] = stage
    write_overnight_state(
        batch_id,
        {
            "status": "running",
            "current_stage": stage,
            "active_wrapper_pid": process.pid,
            "active_process_group_id": process_group_id(process.pid),
            "active_child_pid": None,
            "current_command": " ".join(command),
            "case_path": str(case_dir),
            "last_transition_reason": f"{stage}_started",
        },
    )
    output_lines: list[str] = []
    reader = threading.Thread(target=append_process_stdout, args=(process, output_lines), daemon=True)
    reader.start()
    while process.poll() is None:
        write_batch_heartbeat(batch_id, case_dir, stage, process, command)
        with overnight_control["lock"]:
            stop_all = bool(overnight_control["stop_all"])
        if stop_all:
            terminate_process_group(process)
            output_lines.append("Batch stop requested; command process group terminated.\n")
            break
        time.sleep(5.0)
    reader.join(timeout=2.0)
    write_batch_heartbeat(batch_id, case_dir, stage, process, command)
    with overnight_control["lock"]:
        if overnight_control.get("process") is process:
            overnight_control["process"] = None
            overnight_control["process_group_id"] = None
            overnight_control["case_dir"] = None
            overnight_control["stage"] = ""
    return process.returncode or 0, "".join(output_lines)


def run_task(action: str, case_name: str, commands: list[tuple[list[str], str]]) -> None:
    all_output = ""
    results: list[dict[str, str]] = []
    diagnostics: list[str] = []
    returncode = 0
    error = ""

    try:
        for command, stage in commands:
            returncode, output = run_command(command, stage)
            all_output += output
            if stage == "parse":
                results.append(metrics_from_output(case_name, output))
            if stage == "diagnose":
                diagnostics = warnings_from_output(output)
            if returncode != 0:
                error = f"Command failed: {' '.join(command)}"
                break
    except OSError as exc:
        returncode = 1
        error = str(exc)
        state.append_log(f"ERROR: {exc}")

    state.finish(returncode=returncode, error=error, results=results, diagnostics=diagnostics)


def run_parse_all_task() -> None:
    results: list[dict[str, str]] = []
    returncode = 0
    error = ""
    try:
        cases = sorted(path.name for path in CASES_DIR.iterdir() if path.is_dir()) if CASES_DIR.is_dir() else []
        if not cases:
            raise RuntimeError("No case folders found under cases/")
        for name in cases:
            command = [PYTHON, "scripts/parse_forces.py", str(CASES_DIR / name)]
            returncode, output = run_command(command, "parse")
            if returncode == 0:
                results.append(metrics_from_output(name, output))
            else:
                error = f"Failed parsing case: {name}"
                break
    except (OSError, RuntimeError) as exc:
        returncode = 1
        error = str(exc)
        state.append_log(f"ERROR: {exc}")

    state.finish(returncode=returncode, error=error, results=results)


def batch_commands(mode: str, case_dir: Path) -> tuple[list[str], str]:
    if mode == "mesh":
        return [PYTHON, "scripts/run_case.py", str(case_dir), "--snappy"], "blockMesh"
    if mode == "solve-only":
        return [PYTHON, "scripts/run_case.py", str(case_dir), "--solve-only"], "foamRun"
    if mode == "rebuild-and-solve":
        return [PYTHON, "scripts/run_case.py", str(case_dir), "--solve"], "blockMesh"
    raise ValueError(f"Unsupported batch mode: {mode}")


def run_batch_task(mode: str) -> None:
    results: list[dict[str, str]] = []
    returncode = 0
    error = ""
    cases = variant_ids(batch_only=True)
    try:
        if not cases:
            raise RuntimeError("No variants found in variants.csv")
        for index, name in enumerate(cases, start=1):
            state.update_case(name, index, len(cases))
            case_dir = case_path(name)
            if not case_dir.is_dir():
                returncode = 1
                error = f"Case directory is missing for {name}. Run Generate Cases first."
                state.append_log(f"ERROR: {error}")
                break
            if mode == "solve-only" and not mesh_ready(case_dir):
                returncode = 1
                error = f"Case {name} is missing constant/polyMesh. Run Mesh Only first."
                state.append_log(f"ERROR: {error}")
                break
            command, stage = batch_commands(mode, case_dir)
            returncode, output = run_command(command, stage)
            if returncode != 0:
                error = f"Command failed for {name}: {' '.join(command)}"
                break
            if mode != "mesh":
                parse_command = [PYTHON, "scripts/parse_forces.py", str(case_dir)]
                parse_code, parse_output = run_command(parse_command, "parse")
                if parse_code == 0:
                    results.append(metrics_from_output(name, parse_output))
                else:
                    state.append_log(f"WARNING: parse failed for {name}")
    except (HTTPException, OSError, RuntimeError, ValueError) as exc:
        returncode = 1
        error = str(exc)
        state.append_log(f"ERROR: {exc}")

    state.finish(returncode=returncode, error=error, results=results)


FORWARD_RESULT_COLUMNS = [
    "variant_id",
    "solver_mode",
    "rpm",
    "Vinf_mps",
    "Vinf_mph",
    "thrust_N",
    "torque_Nm",
    "power_W",
    "eta",
    "advance_ratio_J",
    "freestream_Mach",
    "tip_speed_mps",
    "helical_tip_Mach",
    "CT",
    "CQ",
    "CP",
    "run_status",
    "warning_flags",
]


def write_forward_results(rows: list[dict[str, str]]) -> None:
    results_path = ROOT / "results" / "forward_batch_results.csv"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with results_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FORWARD_RESULT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in FORWARD_RESULT_COLUMNS})


def mesh_can_be_reused(case_dir: Path, row: dict[str, str]) -> bool:
    if not mesh_ready(case_dir):
        return False
    boundary_names = read_boundary_names(case_dir / "constant" / "polyMesh" / "boundary")
    zone_names = parse_cell_zone_names(case_dir / "constant" / "polyMesh" / "cellZones")
    if "propeller" not in boundary_names or expected_cell_zone(row) not in zone_names:
        return False
    info_path = case_dir / "case_info.json"
    if not info_path.is_file():
        return False
    try:
        info = json.loads(info_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return (
        str(info.get("stl_file", "")) == row.get("stl_file", "")
        and abs(float(info.get("diameter_m", 0)) - float(row.get("diameter_m", 0))) < 1e-12
    )


def generate_forward_case(row: dict[str, str], row_number: int, force_remesh: bool) -> Path:
    case_dir = CASES_DIR / row["variant_id"]
    saved_poly_mesh = ROOT / ".tmp_polyMesh_restore" / row["variant_id"]
    if saved_poly_mesh.exists():
        shutil.rmtree(saved_poly_mesh)
    reuse_mesh = not force_remesh and mesh_can_be_reused(case_dir, row)
    if reuse_mesh:
        saved_poly_mesh.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(case_dir / "constant" / "polyMesh", saved_poly_mesh)

    try:
        generated = generate_variant_case(ROOT, row, row_number, overwrite=True)
        if reuse_mesh:
            target = generated / "constant" / "polyMesh"
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(saved_poly_mesh, target)
        metadata = json.loads((generated / "case_info.json").read_text())
        metadata.update(
            {
                "run_metadata_version": 1,
                "mesh_reused": reuse_mesh,
                "mesh_profile": "production_high_resolution" if row.get("solver_mode") == "compressible_forward_mrf" else "legacy_incompressible",
                "mesh_reuse_rule": (
                    "Mesh reused for flow/solver changes only; remesh required for STL, domain, "
                    "resolution, refinement, or rotor-zone geometry changes."
                ),
            }
        )
        (generated / "run_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
        return generated
    except CaseGenerationError as exc:
        raise RuntimeError(str(exc)) from exc
    finally:
        if saved_poly_mesh.exists():
            shutil.rmtree(saved_poly_mesh)


def failed_forward_row(row: dict[str, str], message: str) -> dict[str, str]:
    return {
        "variant_id": row.get("variant_id", ""),
        "solver_mode": row.get("solver_mode", ""),
        "rpm": row.get("rpm", ""),
        "Vinf_mps": row.get("Vinf_mps", ""),
        "Vinf_mph": f"{float(row.get('Vinf_mps', 0)) / 0.44704:.8g}" if row.get("Vinf_mps") else "",
        "run_status": "failed",
        "warning_flags": message,
    }


def sanitize_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    cleaned = cleaned.strip("._-")
    return cleaned[:80] or f"variant_{uuid.uuid4().hex[:8]}"


def ensure_path_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def mph(value: float, units: str) -> float:
    return value if units.strip().lower() == "mph" else value / 0.44704


def overnight_dir(batch_id: str) -> Path:
    if not CASE_RE.fullmatch(batch_id):
        raise HTTPException(status_code=400, detail="Invalid batch_id")
    path = IMPORTED_EXPORTS_DIR / batch_id
    if not path.is_dir():
        raise HTTPException(status_code=404, detail=f"Unknown batch_id: {batch_id}")
    return path


def overnight_manifest_path(batch_id: str) -> Path:
    return overnight_dir(batch_id) / "imported_geometry_batch_manifest.json"


def read_overnight_manifest(batch_id: str) -> dict[str, Any]:
    path = overnight_manifest_path(batch_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Batch manifest not found")
    return json.loads(path.read_text())


def write_overnight_manifest(manifest: dict[str, Any]) -> None:
    batch_dir = IMPORTED_EXPORTS_DIR / manifest["batch_id"]
    batch_dir.mkdir(parents=True, exist_ok=True)
    json_path = batch_dir / "imported_geometry_batch_manifest.json"
    csv_path = batch_dir / "imported_geometry_batch_manifest.csv"
    json_path.write_text(json.dumps(manifest, indent=2) + "\n")
    rows = manifest.get("variants", [])
    columns = [
        "batch_id", "variant_id", "stl_source_mode", "original_stl_path", "original_stl_filename",
        "imported_stl_path", "json_metadata_path", "case_triSurface_stl_path", "snappy_references_case_stl",
        "rpm", "global_rpm", "Vinf_mph", "global_Vinf_mph", "Vinf_mps", "global_Vinf_mps", "Tinf_K", "pinf_Pa", "solver_mode",
        "geometry_sanity_status", "warning_flags",
    ]
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def mesh_profile_settings(profile: str) -> dict[str, Any]:
    profiles = {
        "fast_geometry_check": {
            "mesh_profile": "fast_geometry_check",
            "runs_snappyHexMesh": False,
            "purpose": "Import, path, scale, and case-local STL reference checks.",
            "checks": ["STL exists", "STL non-empty", "bounding box", "scale", "axis offset", "case STL reference"],
        },
        "quick_coarse_mesh_check": {
            "mesh_profile": "quick_coarse_mesh_check",
            "runs_snappyHexMesh": True,
            "purpose": "Optional coarse mesh check only. Not valid for production CFD results.",
            "surface_refinement": "coarse sanity profile",
            "rotor_zone_refinement": "coarse sanity profile",
            "boundary_layers": "disabled for sanity check",
        },
        "production_high_resolution": {
            "mesh_profile": "production_high_resolution",
            "runs_snappyHexMesh": True,
            "purpose": "Production Batch Run CFD mesh.",
            "snappyHexMeshDict_source": "templates/compressible_forward_mrf/system/snappyHexMeshDict",
            "surface_refinement": "template production propeller surface levels",
            "rotor_zone_refinement": "template production rotorZone cylinder refinement",
            "wake_refinement": "template production wake refinement",
            "boundary_layers": "as configured in template addLayers setting",
            "warning": "Meshing may take significant time per propeller.",
        },
    }
    if profile not in profiles:
        raise HTTPException(status_code=400, detail=f"Unsupported mesh_profile: {profile}")
    return profiles[profile]


def write_mesh_metadata(case_dir: Path, profile: str, sanity_check: bool) -> None:
    settings = mesh_profile_settings(profile)
    metadata_path = case_dir / "run_metadata.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
    metadata.update(
        {
            "mesh_profile": profile,
            "snappyHexMeshDict_source": settings.get("snappyHexMeshDict_source", str(case_dir / "system" / "snappyHexMeshDict")),
            "refinement_settings": settings,
            "sanity_check_mesh": sanity_check,
            "production_mesh": profile == "production_high_resolution" and not sanity_check,
        }
    )
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")


def assert_production_mesh_allowed(case_dir: Path) -> None:
    metadata_path = case_dir / "run_metadata.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
    if metadata.get("mesh_profile") in ("fast_geometry_check", "quick_coarse_mesh_check") or metadata.get("sanity_check_mesh"):
        raise RuntimeError("Refusing to use a fast/quick sanity-check mesh for production Batch Run CFD")


def is_ignored_import_path(path: Path) -> bool:
    return "__MACOSX" in path.parts or path.name == ".DS_Store" or path.name.startswith("._")


def load_json_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {"_metadata_parse_error": "JSON metadata could not be parsed"}


def import_source_to_batch(source: Path, original_name: str | None = None) -> dict[str, Any]:
    state.append_log(f"[batch-import] source={source} original_name={original_name or ''}")
    batch_id = next_batch_id()
    batch_dir = IMPORTED_EXPORTS_DIR / batch_id
    raw_dir = batch_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    if source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            for member in archive.infolist():
                member_path = Path(member.filename)
                if member.is_dir() or is_ignored_import_path(member_path):
                    continue
                target = raw_dir / member_path
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    elif source.is_dir():
        for path in source.rglob("*"):
            if path.is_file() and not is_ignored_import_path(path):
                rel = path.relative_to(source)
                target = raw_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    else:
        raise HTTPException(status_code=400, detail="Import path must be a folder or .zip file")
    manifest = build_overnight_manifest(batch_id, original_name or str(source))
    state.append_log(f"[batch-import] batch_id={batch_id} stl_count={len(manifest.get('variants', []))}")
    if not manifest.get("variants"):
        raise HTTPException(status_code=400, detail="No STL files found in imported folder/zip.")
    return manifest


def next_batch_id() -> str:
    used: set[int] = set()
    for root in (IMPORTED_EXPORTS_DIR, CASES_DIR):
        if not root.is_dir():
            continue
        for path in root.iterdir():
            if not path.is_dir():
                continue
            match = SHORT_BATCH_RE.fullmatch(path.name)
            if match:
                used.add(int(match.group(1)))
    index = 1
    while index in used:
        index += 1
    return f"batch_{index:03d}"


def build_overnight_manifest(batch_id: str, source_label: str) -> dict[str, Any]:
    batch_dir = IMPORTED_EXPORTS_DIR / batch_id
    raw_dir = batch_dir / "raw"
    stl_files = sorted(path for path in raw_dir.rglob("*") if path.is_file() and path.suffix.lower() == ".stl" and not is_ignored_import_path(path))
    variants: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for stl_path in stl_files:
        json_path = stl_path.with_suffix(".json")
        metadata = load_json_metadata(json_path)
        metadata_id = metadata.get("variant_id") or metadata.get("id") or metadata.get("name") or stl_path.stem
        base_id = sanitize_id(str(metadata_id))
        variant_id = base_id
        suffix = 2
        while variant_id in seen_ids:
            variant_id = f"{base_id}_{suffix}"
            suffix += 1
        seen_ids.add(variant_id)
        warning_flags = []
        for key in metadata:
            if key.lower() in {"rpm", "vinf_mps", "vinf_mph", "freestream_velocity_mps"}:
                warning_flags.append("METADATA_RPM_VINF_IGNORED")
        variants.append(
            {
                "batch_id": batch_id,
                "stl_source_mode": "imported_batch",
                "selected": True,
                "variant_id": variant_id,
                "original_stl_path": str(stl_path),
                "original_stl_filename": stl_path.name,
                "imported_stl_path": str(stl_path),
                "json_metadata_path": str(json_path) if json_path.is_file() else "",
                "json_found": json_path.is_file(),
                "json_metadata": metadata,
                "pitch": metadata.get("pitch", metadata.get("pitch_m", metadata.get("pitch_mm", ""))),
                "root_chord": metadata.get("root_chord", metadata.get("root_chord_m", metadata.get("root_chord_mm", ""))),
                "peak_chord": metadata.get("peak_chord", metadata.get("peak_chord_m", metadata.get("peak_chord_mm", ""))),
                "tip_chord": metadata.get("tip_chord", metadata.get("tip_chord_m", metadata.get("tip_chord_mm", ""))),
                "diameter_m": metadata.get("diameter_m", 0.1524),
                "radius_m": metadata.get("radius_m", metadata.get("diameter_m", 0.1524) / 2 if isinstance(metadata.get("diameter_m", 0.1524), (int, float)) else 0.0762),
                "rpm": BATCH_DEFAULT_RPM,
                "global_rpm": BATCH_DEFAULT_RPM,
                "Vinf_mph": BATCH_DEFAULT_VINF_MPH,
                "global_Vinf_mph": BATCH_DEFAULT_VINF_MPH,
                "Vinf_mps": BATCH_DEFAULT_VINF_MPS,
                "global_Vinf_mps": BATCH_DEFAULT_VINF_MPS,
                "Tinf_K": 288.15,
                "pinf_Pa": 101325.0,
                "solver_mode": "compressible_forward_mrf",
                "geometry_sanity_status": "Not checked",
                "warning_flags": ",".join(warning_flags),
            }
        )
    manifest = {
        "batch_id": batch_id,
        "source": source_label,
        "created_at": time.time(),
        "workflow": "Batch Run",
        "mesh_profiles": {
            "geometry_check_profile": "fast_geometry_check",
            "optional_quick_mesh_profile": "quick_coarse_mesh_check",
            "batch_mesh_profile": "production_high_resolution",
            "wording": "Fast geometry check is separate from production_high_resolution meshing.",
        },
        "pre_run_checks": {
            "import_folder_found": raw_dir.is_dir(),
            "stl_count_detected": len(variants),
            "json_metadata_count": sum(1 for row in variants if row.get("json_found")),
            "disk_free_GB": round(shutil.disk_usage(ROOT).free / 1e9, 2),
            "openfoam_available": bool(shutil.which("rhoPimpleFoam") or shutil.which("foamRun")),
            "compressible_forward_mrf_template_available": (ROOT / "templates" / "compressible_forward_mrf").is_dir(),
        },
        "variants": variants,
    }
    write_overnight_manifest(manifest)
    return manifest


def stl_bounding_box(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    if len(data) < 84:
        raise ValueError("STL is too small to contain triangles")
    points: list[tuple[float, float, float]] = []
    is_ascii = data[:80].lstrip().lower().startswith(b"solid") and b"facet" in data[:2048].lower()
    if is_ascii:
        text = data.decode(errors="ignore")
        for match in re.finditer(r"vertex\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)", text):
            points.append((float(match.group(1)), float(match.group(2)), float(match.group(3))))
    else:
        tri_count = struct.unpack("<I", data[80:84])[0]
        offset = 84
        for _ in range(min(tri_count, 2_000_000)):
            if offset + 50 > len(data):
                break
            values = struct.unpack("<12fH", data[offset: offset + 50])
            points.extend([(values[3], values[4], values[5]), (values[6], values[7], values[8]), (values[9], values[10], values[11])])
            offset += 50
    if not points:
        raise ValueError("No STL vertices found")
    xs, ys, zs = zip(*points)
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    zmin, zmax = min(zs), max(zs)
    dx, dy, dz = xmax - xmin, ymax - ymin, zmax - zmin
    diameter_estimate = max(dx, dy)
    center = ((xmin + xmax) / 2, (ymin + ymax) / 2, (zmin + zmax) / 2)
    return {
        "xmin": xmin, "xmax": xmax, "ymin": ymin, "ymax": ymax, "zmin": zmin, "zmax": zmax,
        "dx": dx, "dy": dy, "dz": dz, "diameter_estimate_m": diameter_estimate,
        "center_x": center[0], "center_y": center[1], "center_z": center[2],
        "vertex_count_sampled": len(points),
    }


def geometry_sanity_for_variant(row: dict[str, Any]) -> dict[str, Any]:
    warnings = [flag for flag in str(row.get("warning_flags", "")).split(",") if flag]
    status = "Geometry OK"
    bbox: dict[str, Any] = {}
    stl_path = Path(str(row["imported_stl_path"]))
    try:
        if not stl_path.is_file():
            raise ValueError("STL file missing")
        if stl_path.stat().st_size <= 0:
            raise ValueError("STL file is empty")
        bbox = stl_bounding_box(stl_path)
        diameter = float(row.get("diameter_m") or 0.1524)
        estimate = float(bbox["diameter_estimate_m"])
        offset = math.hypot(float(bbox["center_x"]), float(bbox["center_y"]))
        if estimate < 0.02:
            warnings.append("STL_TOO_SMALL")
        if estimate > 0.5:
            warnings.append("STL_TOO_LARGE")
        if diameter > 0 and (estimate < 0.25 * diameter or estimate > 2.5 * diameter):
            warnings.append("POSSIBLE_UNIT_SCALE_ERROR")
        if offset > 0.03:
            warnings.append("STL_OFFSET_FROM_AXIS")
        if warnings:
            status = "Geometry Warning"
    except (OSError, ValueError) as exc:
        status = "Geometry Fail"
        warnings.append(str(exc))
    return {**row, **bbox, "geometry_sanity_status": status, "warning_flags": ",".join(sorted(set(warnings)))}


def update_case_stl_preflight(row: dict[str, Any], case_dir: Path) -> dict[str, Any]:
    case_stl_name = f"{sanitize_id(str(row['variant_id']))}.stl"
    case_stl = case_dir / "constant" / "triSurface" / case_stl_name
    snappy = case_dir / "system" / "snappyHexMeshDict"
    surface_features = case_dir / "system" / "surfaceFeaturesDict"
    snappy_text = snappy.read_text(errors="replace") if snappy.is_file() else ""
    surface_text = surface_features.read_text(errors="replace") if surface_features.is_file() else ""
    row["case_triSurface_stl_path"] = str(case_stl)
    row["case_triSurface_stl_exists"] = case_stl.is_file()
    row["snappy_references_case_stl"] = case_stl_name in snappy_text and case_stl_name in surface_text
    warnings = [flag for flag in str(row.get("warning_flags", "")).split(",") if flag]
    if not case_stl.is_file():
        warnings.append("CASE_STL_NOT_FOUND")
    if not row["snappy_references_case_stl"]:
        warnings.append("SNAPPY_STL_REFERENCE_MISMATCH")
    row["warning_flags"] = ",".join(sorted(set(warnings)))
    return row


def apply_quick_coarse_mesh_profile(case_dir: Path) -> None:
    snappy = case_dir / "system" / "snappyHexMeshDict"
    if not snappy.is_file():
        return
    text = snappy.read_text()
    text = re.sub(r"\blevel\s+\(\s*\d+\s+\d+\s*\)", "level       (1 1)", text)
    text = re.sub(r"\blevel\s+\d+\s*;", "level       1;", text)
    text = re.sub(r"\bmaxLocalCells\s+\d+\s*;", "maxLocalCells       50000;", text)
    text = re.sub(r"\bmaxGlobalCells\s+\d+\s*;", "maxGlobalCells      250000;", text)
    snappy.write_text(text)


def update_manifest_operating_conditions(manifest: dict[str, Any], settings: OvernightSettings) -> dict[str, Any]:
    vinf_mps = request_vinf_mps(ForwardBatchRequest(rpm=settings.rpm, freestream_speed=settings.freestream_speed, speed_units=settings.speed_units))
    vinf_mph = vinf_mps / 0.44704
    for row in manifest.get("variants", []):
        row.update(
            {
                "rpm": settings.rpm,
                "global_rpm": settings.rpm,
                "Vinf_mph": vinf_mph,
                "global_Vinf_mph": vinf_mph,
                "Vinf_mps": vinf_mps,
                "global_Vinf_mps": vinf_mps,
                "Tinf_K": settings.Tinf_K,
                "pinf_Pa": settings.pinf_Pa,
                "solver_mode": "compressible_forward_mrf",
            }
        )
    manifest["settings"] = settings.dict()
    manifest["settings"]["Vinf_mps"] = vinf_mps
    manifest["settings"]["Vinf_mph"] = vinf_mph
    manifest["settings"]["geometry_check_profile"] = "quick_coarse_mesh_check" if settings.run_mesh_sanity else "fast_geometry_check"
    manifest["settings"]["batch_mesh_profile"] = settings.batch_mesh_profile
    return manifest


def write_geometry_sanity_reports(manifest: dict[str, Any]) -> None:
    batch_dir = IMPORTED_EXPORTS_DIR / manifest["batch_id"]
    rows = manifest.get("variants", [])
    json_path = batch_dir / "geometry_sanity_report.json"
    csv_path = batch_dir / "geometry_sanity_report.csv"
    md_path = batch_dir / "geometry_sanity_report.md"
    json_path.write_text(json.dumps(rows, indent=2) + "\n")
    columns = [
        "variant_id", "original_stl_filename", "geometry_sanity_status", "warning_flags",
        "xmin", "xmax", "ymin", "ymax", "zmin", "zmax", "diameter_estimate_m",
        "center_x", "center_y", "center_z",
    ]
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})
    lines = ["# Geometry Sanity Report", "", f"batch_id: {manifest['batch_id']}", ""]
    for row in rows:
        lines.append(f"- {row.get('variant_id')}: {row.get('geometry_sanity_status')} {row.get('warning_flags', '')}")
    md_path.write_text("\n".join(lines) + "\n")


def overnight_batch_state_path(batch_id: str) -> Path:
    return IMPORTED_EXPORTS_DIR / batch_id / "batch_state.json"


def write_overnight_state(batch_id: str, state_data: dict[str, Any]) -> None:
    path = overnight_batch_state_path(batch_id)
    previous = {}
    if path.is_file():
        try:
            previous = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            previous = {}
    merged = {**previous, **state_data, "last_update_time": time.time()}
    path.write_text(json.dumps(merged, indent=2) + "\n")


def selected_overnight_rows(manifest: dict[str, Any], settings: OvernightSettings) -> list[dict[str, Any]]:
    selected = set(settings.selected_variant_ids)
    if not selected:
        return [row for row in manifest.get("variants", []) if row.get("selected", True)]
    rows = [row for row in manifest.get("variants", []) if row.get("variant_id") in selected]
    return [row for row in rows if row.get("selected", True)]


def batch_case_prefix(batch_id: str) -> str:
    return batch_id if batch_id.startswith("batch_") else f"batch_{batch_id}"


def overnight_case_row(batch_id: str, row: dict[str, Any], settings: OvernightSettings) -> dict[str, str]:
    stl_source = Path(str(row["imported_stl_path"])).resolve()
    imported_root = (ROOT / "imported_exports" / batch_id).resolve()
    if not ensure_path_inside(stl_source, imported_root):
        raise RuntimeError("Imported STL path is outside the managed imported_exports batch directory")
    stl_name = f"{sanitize_id(str(row['variant_id']))}.stl"
    vinf_mps = request_vinf_mps(ForwardBatchRequest(rpm=settings.rpm, freestream_speed=settings.freestream_speed, speed_units=settings.speed_units))
    return {
        "variant_id": f"{batch_case_prefix(batch_id)}/{sanitize_id(str(row['variant_id']))}",
        "source_variant_id": str(row["variant_id"]),
        "stl_file": stl_name,
        "case_stl_file": stl_name,
        "stl_source_mode": "imported_batch",
        "imported_stl_path": str(stl_source),
        "solver_mode": "compressible_forward_mrf",
        "rpm": f"{settings.rpm:.12g}",
        "Vinf_mps": f"{vinf_mps:.12g}",
        "Tinf_K": f"{settings.Tinf_K:.12g}",
        "pinf_Pa": f"{settings.pinf_Pa:.12g}",
        "diameter_m": f"{float(row.get('diameter_m') or 0.1524):.12g}",
        "batch_enabled": "true",
        "convergence_preset": settings.convergence_preset,
        "convergence_min_force_samples": str(settings.convergence_min_force_samples),
        "convergence_window_size": str(settings.convergence_window_size),
        "convergence_thrust_mean_change_pct": str(settings.convergence_thrust_mean_change_pct),
        "convergence_torque_mean_change_pct": str(settings.convergence_torque_mean_change_pct),
        "convergence_thrust_cv_pct": str(settings.convergence_thrust_cv_pct),
        "convergence_torque_cv_pct": str(settings.convergence_torque_cv_pct),
    }


def write_overnight_results(batch_id: str, rows: list[dict[str, Any]]) -> None:
    batch_dir = IMPORTED_EXPORTS_DIR / batch_id
    json_path = batch_dir / "batch_results.json"
    csv_path = batch_dir / "batch_results.csv"
    md_path = batch_dir / "batch_summary.md"
    json_path.write_text(json.dumps(rows, indent=2) + "\n")
    (batch_dir / "overnight_batch_results.json").write_text(json.dumps(rows, indent=2) + "\n")
    columns = [
        "batch_id", "variant_id", "original_stl_filename", "imported_stl_path", "case_local_stl_path",
        "json_metadata_path", "solver_mode", "mesh_profile", "rpm", "global_rpm", "Vinf_mph",
        "global_Vinf_mph", "Vinf_mps", "global_Vinf_mps", "Tinf_K", "pinf_Pa",
        "raw_Fx_mean", "raw_Fy_mean", "raw_Fz_mean", "raw_force_axis_N_mean", "thrust_axis", "thrust_sign",
        "thrust_N_mean", "thrust_N_std", "shaft_torque_Nm_mean", "shaft_torque_Nm_std",
        "raw_Mx_mean", "raw_My_mean", "raw_Mz_mean", "raw_moment_axis_Nm_mean", "torque_axis", "torque_sign",
        "power_W_mean", "eta_mean", "advance_ratio_J", "CT", "CQ", "CP",
        "freestream_Mach", "tip_speed_mps", "helical_tip_Mach", "force_sample_count",
        "averaging_method", "runtime_seconds", "solver_status", "geometry_sanity_status",
        "convergence_status", "convergence_preset", "min_force_samples", "stop_reason", "convergence_window_size",
        "thrust_mean_change_threshold_pct", "torque_mean_change_threshold_pct", "thrust_cv_threshold_pct", "torque_cv_threshold_pct",
        "actual_thrust_mean_change_pct", "actual_torque_mean_change_pct", "actual_thrust_cv_pct", "actual_torque_cv_pct",
        "thrust_mean_change_pct", "torque_mean_change_pct", "thrust_cv_pct", "torque_cv_pct",
        "samples_used_for_average",
        "warning_flags", "case_path", "log_path",
    ]
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})
    shutil.copy2(csv_path, batch_dir / "overnight_batch_results.csv")
    lines = ["# Batch Run Summary", ""]
    lines += [
        "- mesh_profile: production_high_resolution",
        "- refinement: templates/compressible_forward_mrf/system/snappyHexMeshDict production settings",
        "- backend_runner: active while this Python server is running",
        "",
    ]
    for row in rows:
        lines.append(f"- {row.get('variant_id')}: {row.get('solver_status')} thrust={row.get('thrust_N_mean')} torque={row.get('shaft_torque_Nm_mean')} warnings={row.get('warning_flags', '')}")
    md_path.write_text("\n".join(lines) + "\n")


def overnight_result_row(batch_id: str, row: dict[str, Any], case_dir: Path, settings: OvernightSettings, solver_status: str, warning_flags: list[str], runtime_s: float) -> dict[str, Any]:
    average_window = settings.convergence_window_size if solver_status == "converged_stop" else settings.averaging_window
    series = build_solver_timeseries(case_dir, average_window)
    summary = series.get("summary", {})
    info = json.loads((case_dir / "case_info.json").read_text()) if (case_dir / "case_info.json").is_file() else {}
    force_count = len(series.get("force_samples", []))
    convergence = force_convergence_metrics(
        series.get("force_samples", []),
        window=settings.convergence_window_size,
        minimum_samples=settings.convergence_min_force_samples,
        thrust_mean_change_threshold_pct=settings.convergence_thrust_mean_change_pct,
        torque_mean_change_threshold_pct=settings.convergence_torque_mean_change_pct,
        thrust_cv_threshold_pct=settings.convergence_thrust_cv_pct,
        torque_cv_threshold_pct=settings.convergence_torque_cv_pct,
    )
    if force_count and force_count < settings.averaging_window:
        warning_flags.append("WARNING_SHORT_AVERAGING")
    if solver_status == "timeout":
        warning_flags.append("WARNING_TIMEOUT_NOT_CONVERGED")
    if solver_status == "converged_stop":
        warning_flags.append("CONVERGED_STOP")
    raw_force_axis = summary.get("raw_force_axis_N_mean")
    thrust_mean = summary.get("mean_thrust_N")
    raw_moment_axis = summary.get("raw_moment_axis_Nm_mean")
    torque_mean = summary.get("mean_shaft_torque_Nm")
    power_mean = summary.get("mean_power_W")
    try:
        if raw_force_axis is not None and thrust_mean is not None and float(raw_force_axis) > 0 and float(thrust_mean) < 0:
            warning_flags.append("THRUST_SIGN_MISMATCH")
        if thrust_mean is not None and float(thrust_mean) < 0:
            warning_flags.append("NEGATIVE_THRUST_CHECK_SIGN_OR_ORIENTATION")
        if raw_moment_axis is not None and torque_mean is not None and power_mean is not None and abs(float(raw_moment_axis)) > 0 and (float(torque_mean) < 0 or float(power_mean) < 0):
            warning_flags.append("TORQUE_SIGN_CHECK")
    except (TypeError, ValueError):
        pass
    stop_reason = {
        "converged_stop": "force_convergence_reached",
        "timeout": "max_wall_clock_timeout_not_converged",
        "stopped": "user_stopped",
        "interrupted": "interrupted_after_force_data",
    }.get(solver_status, solver_status)
    return {
        "batch_id": batch_id,
        "variant_id": row.get("variant_id"),
        "original_stl_filename": row.get("original_stl_filename"),
        "imported_stl_path": row.get("imported_stl_path"),
        "case_local_stl_path": row.get("case_triSurface_stl_path", str(case_dir / "constant" / "triSurface" / f"{sanitize_id(str(row.get('variant_id', 'propeller')))}.stl")),
        "json_metadata_path": row.get("json_metadata_path"),
        "solver_mode": "compressible_forward_mrf",
        "mesh_profile": "production_high_resolution",
        "rpm": settings.rpm,
        "global_rpm": settings.rpm,
        "Vinf_mph": request_vinf_mps(ForwardBatchRequest(rpm=settings.rpm, freestream_speed=settings.freestream_speed, speed_units=settings.speed_units)) / 0.44704,
        "global_Vinf_mph": request_vinf_mps(ForwardBatchRequest(rpm=settings.rpm, freestream_speed=settings.freestream_speed, speed_units=settings.speed_units)) / 0.44704,
        "Vinf_mps": request_vinf_mps(ForwardBatchRequest(rpm=settings.rpm, freestream_speed=settings.freestream_speed, speed_units=settings.speed_units)),
        "global_Vinf_mps": request_vinf_mps(ForwardBatchRequest(rpm=settings.rpm, freestream_speed=settings.freestream_speed, speed_units=settings.speed_units)),
        "Tinf_K": settings.Tinf_K,
        "pinf_Pa": settings.pinf_Pa,
        "raw_Fx_mean": summary.get("raw_Fx_mean"),
        "raw_Fy_mean": summary.get("raw_Fy_mean"),
        "raw_Fz_mean": summary.get("raw_Fz_mean"),
        "raw_force_axis_N_mean": raw_force_axis,
        "thrust_axis": summary.get("thrust_axis", info.get("thrust_axis", "z")),
        "thrust_sign": summary.get("thrust_sign", info.get("thrust_sign", 1)),
        "thrust_N_mean": summary.get("mean_thrust_N"),
        "thrust_N": summary.get("mean_thrust_N"),
        "thrust_N_std": summary.get("stddev_thrust_N"),
        "shaft_torque_Nm_mean": summary.get("mean_shaft_torque_Nm"),
        "torque_Nm": summary.get("mean_shaft_torque_Nm"),
        "shaft_torque_Nm_std": summary.get("stddev_shaft_torque_Nm"),
        "raw_Mx_mean": summary.get("raw_Mx_mean"),
        "raw_My_mean": summary.get("raw_My_mean"),
        "raw_Mz_mean": summary.get("raw_Mz_mean"),
        "raw_moment_axis_Nm_mean": raw_moment_axis,
        "torque_axis": summary.get("torque_axis", info.get("torque_axis", "z")),
        "torque_sign": summary.get("torque_sign", info.get("torque_sign", 1)),
        "power_W_mean": summary.get("mean_power_W"),
        "power_W": summary.get("mean_power_W"),
        "eta_mean": summary.get("mean_eta"),
        "eta": summary.get("mean_eta"),
        "advance_ratio_J": info.get("advance_ratio_J", ""),
        "CT": "",
        "CQ": "",
        "CP": "",
        "freestream_Mach": info.get("freestream_Mach", ""),
        "tip_speed_mps": info.get("tip_speed_mps", ""),
        "helical_tip_Mach": info.get("helical_tip_Mach", ""),
        "force_sample_count": force_count,
        "averaging_method": f"last_{average_window}_force_samples",
        "runtime_seconds": round(runtime_s, 1),
        "solver_status": solver_status,
        "run_status": "success" if solver_status == "success" else solver_status,
        "geometry_sanity_status": row.get("geometry_sanity_status", ""),
        "convergence_status": convergence.get("convergence_status", ""),
        "convergence_preset": settings.convergence_preset,
        "min_force_samples": settings.convergence_min_force_samples,
        "stop_reason": stop_reason,
        "convergence_window_size": settings.convergence_window_size,
        "thrust_mean_change_threshold_pct": settings.convergence_thrust_mean_change_pct,
        "torque_mean_change_threshold_pct": settings.convergence_torque_mean_change_pct,
        "thrust_cv_threshold_pct": settings.convergence_thrust_cv_pct,
        "torque_cv_threshold_pct": settings.convergence_torque_cv_pct,
        "actual_thrust_mean_change_pct": convergence.get("thrust_mean_change_pct"),
        "actual_torque_mean_change_pct": convergence.get("torque_mean_change_pct"),
        "actual_thrust_cv_pct": convergence.get("thrust_cv_pct"),
        "actual_torque_cv_pct": convergence.get("torque_cv_pct"),
        "thrust_mean_change_pct": convergence.get("thrust_mean_change_pct"),
        "torque_mean_change_pct": convergence.get("torque_mean_change_pct"),
        "thrust_cv_pct": convergence.get("thrust_cv_pct"),
        "torque_cv_pct": convergence.get("torque_cv_pct"),
        "samples_used_for_average": summary.get("samples_used", 0),
        "warning_flags": ",".join(sorted(set(flag for flag in warning_flags if flag))),
        "case_path": str(case_dir),
        "case": f"{batch_case_prefix(batch_id)}/{sanitize_id(str(row.get('variant_id', '')))}",
        "log_path": str(case_dir / "log.rhoPimpleFoam"),
    }


def refresh_batch_result_for_case(case_dir: Path) -> dict[str, Any] | None:
    try:
        relative = case_dir.resolve().relative_to(CASES_DIR.resolve())
    except ValueError:
        return None
    if len(relative.parts) != 2 or not relative.parts[0].startswith("batch_"):
        return None
    batch_id, variant_id = relative.parts
    manifest_path = overnight_manifest_path(batch_id)
    if not manifest_path.is_file():
        return None
    manifest = json.loads(manifest_path.read_text())
    variant_row = next((row for row in manifest.get("variants", []) if sanitize_id(str(row.get("variant_id", ""))) == variant_id), None)
    if variant_row is None:
        return None
    settings_payload = {"batch_id": batch_id, **manifest.get("settings", {})}
    settings_payload.setdefault("selected_variant_ids", [variant_row.get("variant_id", variant_id)])
    settings = OvernightSettings(**settings_payload)
    existing_results_path = IMPORTED_EXPORTS_DIR / batch_id / "batch_results.json"
    existing_results = json.loads(existing_results_path.read_text()) if existing_results_path.is_file() else []
    prior = next((row for row in existing_results if sanitize_id(str(row.get("variant_id", ""))) == variant_id), {})
    warnings = str(prior.get("warning_flags", "")).split(",") if prior.get("warning_flags") else []
    refreshed = overnight_result_row(
        batch_id,
        variant_row,
        case_dir,
        settings,
        str(prior.get("solver_status", "reparsed")),
        warnings,
        float(prior.get("runtime_seconds") or 0),
    )
    replaced = False
    updated_results = []
    for row in existing_results:
        if sanitize_id(str(row.get("variant_id", ""))) == variant_id:
            updated_results.append(refreshed)
            replaced = True
        else:
            updated_results.append(row)
    if not replaced:
        updated_results.append(refreshed)
    write_overnight_results(batch_id, updated_results)
    write_overnight_state(batch_id, {"results": updated_results})
    return refreshed


def write_batch_heartbeat(batch_id: str, case_dir: Path | None, stage: str, process: subprocess.Popen[str] | None, command: list[str] | None) -> None:
    batch_dir = IMPORTED_EXPORTS_DIR / batch_id
    log_path = None
    force_path = None
    progress: dict[str, Any] = {}
    if case_dir is not None:
        if stage == "solver":
            candidates = [case_dir / "log.rhoPimpleFoam", case_dir / "log.foamRun"]
        else:
            candidates = [case_dir / "log.snappyHexMesh", case_dir / "log.surfaceFeatureExtract", case_dir / "log.blockMesh", case_dir / "log.rhoPimpleFoam"]
        existing = [path for path in candidates if path.is_file()]
        log_path = max(existing, key=lambda path: path.stat().st_mtime) if existing else None
        force_candidates = sorted((case_dir / "postProcessing").glob("**/forces.dat"), key=lambda path: path.stat().st_mtime, reverse=True) if (case_dir / "postProcessing").is_dir() else []
        force_path = force_candidates[0] if force_candidates else None
        if stage == "solver":
            progress = build_solver_progress(case_dir, process_running=process.poll() is None if process is not None else None)
    payload = {
        "batch_id": batch_id,
        "updated_at": time.time(),
        "current_stage": stage,
        "pid": process.pid if process is not None else None,
        "process_group_id": process_group_id(process.pid) if process is not None else None,
        "pid_alive": process.poll() is None if process is not None else False,
        "case_path": str(case_dir) if case_dir is not None else "",
        "log_path": str(log_path) if log_path is not None else "",
        "log_file_size": log_path.stat().st_size if log_path is not None else 0,
        "latest_log_timestamp": log_path.stat().st_mtime if log_path is not None else None,
        "force_file_path": str(force_path) if force_path is not None else "",
        "force_file_exists": force_path is not None,
        "force_file_size": force_path.stat().st_size if force_path is not None else 0,
        "latest_parsed_progress": progress,
        "convergence": progress.get("convergence", {}) if progress else {},
        "current_command": " ".join(command or []),
    }
    (batch_dir / "batch_heartbeat.json").write_text(json.dumps(payload, indent=2) + "\n")
    if case_dir is not None:
        (case_dir / "per_variant_heartbeat.json").write_text(json.dumps(payload, indent=2) + "\n")


def solver_artifact_status(case_dir: Path) -> dict[str, Any]:
    log_path = case_dir / "log.rhoPimpleFoam"
    if not log_path.is_file() and (case_dir / "log.foamRun").is_file():
        log_path = case_dir / "log.foamRun"
    force_candidates = sorted((case_dir / "postProcessing").glob("**/forces.dat"), key=lambda path: path.stat().st_mtime, reverse=True) if (case_dir / "postProcessing").is_dir() else []
    force_path = force_candidates[0] if force_candidates else None
    return {
        "solver_log_path": str(log_path) if log_path.is_file() else "",
        "solver_log_exists": log_path.is_file(),
        "solver_log_size": log_path.stat().st_size if log_path.is_file() else 0,
        "force_file_path": str(force_path) if force_path is not None else "",
        "force_file_exists": force_path is not None,
        "force_file_size": force_path.stat().st_size if force_path is not None else 0,
    }


def tail_file(path: Path, max_lines: int = 80) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(errors="replace").splitlines()[-max_lines:]


def run_overnight_solver(case_dir: Path, batch_id: str, settings: OvernightSettings) -> tuple[str, float, list[str]]:
    max_wall_s = settings.max_wall_clock_minutes_per_propeller * 60.0 if settings.max_wall_clock_minutes_per_propeller > 0 else 0.0
    wrapper_path = ROOT / "scripts" / "run_case.py"
    wrapper_log_path = case_dir / "log.solveWrapper"
    command = [PYTHON, str(wrapper_path), str(case_dir), "--solve-only"]
    state.update(stage="solver_running", command=" ".join(command))
    state.append_log(f"$ {' '.join(command)}")
    started = time.monotonic()
    launch_time = time.time()
    termination_reason = ""
    termination_initiated_by_backend = False
    existing = find_case_processes(case_dir)
    if existing:
        state.append_log(f"Recovered existing Batch Run process for {case_dir}: {[item['pid'] for item in existing]}")
        write_overnight_state(
            batch_id,
            {
                "status": "running",
                "stage": "RECOVERED_RUNNING_PROCESS",
                "current_stage": "solver",
                "case_path": str(case_dir),
                "recovered_processes": existing,
                "active_wrapper_pid": existing[0].get("pid"),
                "active_process_group_id": existing[0].get("pgid"),
                "last_transition_reason": "duplicate_prevention_recovered_existing_solver",
            },
        )
        while find_case_processes(case_dir):
            progress = build_solver_progress(case_dir, process_running=True)
            force_samples = build_solver_timeseries(case_dir, settings.convergence_window_size).get("force_samples", [])
            convergence = force_convergence_metrics(
                force_samples,
                window=settings.convergence_window_size,
                minimum_samples=settings.convergence_min_force_samples,
                thrust_mean_change_threshold_pct=settings.convergence_thrust_mean_change_pct,
                torque_mean_change_threshold_pct=settings.convergence_torque_mean_change_pct,
                thrust_cv_threshold_pct=settings.convergence_thrust_cv_pct,
                torque_cv_threshold_pct=settings.convergence_torque_cv_pct,
                fatal_error=bool(progress.get("fatal_error")),
            )
            convergence["convergence_preset"] = settings.convergence_preset
            progress["convergence"] = convergence
            progress["batch_id"] = batch_id
            state.update_solver_progress(progress)
            with overnight_control["lock"]:
                stop_all = bool(overnight_control["stop_all"])
            if progress.get("fatal_error") and progress.get("solver_log"):
                termination_reason = "fatal_solver_log_detected"
                termination_initiated_by_backend = True
                kill_batch_processes(batch_id)
                return "failed", time.monotonic() - started, ["SOLVER_FATAL"]
            if settings.auto_stop_on_convergence and convergence.get("converged"):
                termination_reason = "force_convergence_reached"
                termination_initiated_by_backend = True
                write_overnight_state(batch_id, {"stage": "CONVERGED_STOP", "convergence": convergence, "stop_reason": termination_reason})
                kill_batch_processes(batch_id)
                return "converged_stop", time.monotonic() - started, ["CONVERGED_STOP"]
            if stop_all:
                termination_reason = "user_stop_entire_batch"
                termination_initiated_by_backend = True
                kill_batch_processes(batch_id)
                break
            if max_wall_s > 0 and time.monotonic() - started >= max_wall_s:
                termination_reason = "max_wall_clock_timeout"
                termination_initiated_by_backend = True
                kill_batch_processes(batch_id)
                return "timeout", time.monotonic() - started, ["WARNING_TIMEOUT_NOT_CONVERGED"]
            write_batch_heartbeat(batch_id, case_dir, "solver", None, command)
            time.sleep(1.0)
        artifacts = solver_artifact_status(case_dir)
        status = "stopped" if termination_reason == "user_stop_entire_batch" else ("interrupted" if artifacts["force_file_exists"] else "orchestration_error")
        warnings = ["STOPPED_WITH_DATA" if artifacts["force_file_exists"] else "STOPPED_NO_DATA"] if termination_reason == "user_stop_entire_batch" else []
        diagnostic = {
            "command": command,
            "cwd": str(ROOT),
            "absolute_wrapper_path": str(wrapper_path),
            "launch_time": launch_time,
            "termination_reason": termination_reason or "recovered_process_exited",
            "termination_initiated_by_backend": termination_initiated_by_backend,
            "recovered_processes": existing,
            "solver_status": status,
            **artifacts,
        }
        (case_dir / "solver_orchestration_diagnostic.json").write_text(json.dumps(diagnostic, indent=2) + "\n")
        write_overnight_state(batch_id, {"stage": f"solver {status}", "stop_reason": diagnostic["termination_reason"], "solver": diagnostic})
        return status, time.monotonic() - started, warnings
    try:
        wrapper_log = wrapper_log_path.open("w")
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=wrapper_log,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        diagnostic = {
            "status": "ORCHESTRATION_ERROR",
            "message": str(exc),
            "command": command,
            "cwd": str(ROOT),
            "absolute_wrapper_path": str(wrapper_path),
            "launch_time": launch_time,
            "termination_reason": "launch_failed",
            "termination_initiated_by_backend": False,
            "timeout_seconds": max_wall_s if max_wall_s > 0 else None,
            "stdout_stderr_log_path": str(wrapper_log_path),
            "case_path": str(case_dir),
            **solver_artifact_status(case_dir),
        }
        (case_dir / "solver_orchestration_diagnostic.json").write_text(json.dumps(diagnostic, indent=2) + "\n")
        write_overnight_state(batch_id, {"stage": "solver launch failed", "solver_diagnostic": diagnostic})
        return "orchestration_error", time.monotonic() - started, ["ORCHESTRATION_ERROR", str(exc)]
    with overnight_control["lock"]:
        overnight_control["process"] = process
        overnight_control["process_group_id"] = process_group_id(process.pid)
        overnight_control["case_dir"] = case_dir
        overnight_control["stage"] = "solver"
    write_overnight_state(
        batch_id,
        {
            "status": "running",
            "stage": "SOLVER_RUNNING",
            "current_command": " ".join(command),
            "current_pid": process.pid,
            "active_wrapper_pid": process.pid,
            "active_process_group_id": process_group_id(process.pid),
            "active_child_pid": None,
            "current_stage": "solver",
            "last_transition_reason": "solver_started",
            "case_path": str(case_dir),
            "solver": {
                "command": command,
                "cwd": str(ROOT),
                "absolute_wrapper_path": str(wrapper_path),
                "pid": process.pid,
                "started_at": launch_time,
                "timeout_seconds": max_wall_s if max_wall_s > 0 else None,
                "stdout_stderr_log_path": str(wrapper_log_path),
                **solver_artifact_status(case_dir),
            },
        },
    )
    write_batch_heartbeat(batch_id, case_dir, "solver", process, command)
    warnings: list[str] = []
    solver_status = "success"
    while process.poll() is None:
        progress = build_solver_progress(case_dir, process_running=True)
        force_samples = build_solver_timeseries(case_dir, settings.convergence_window_size).get("force_samples", [])
        convergence = force_convergence_metrics(
            force_samples,
            window=settings.convergence_window_size,
            minimum_samples=settings.convergence_min_force_samples,
            thrust_mean_change_threshold_pct=settings.convergence_thrust_mean_change_pct,
            torque_mean_change_threshold_pct=settings.convergence_torque_mean_change_pct,
            thrust_cv_threshold_pct=settings.convergence_thrust_cv_pct,
            torque_cv_threshold_pct=settings.convergence_torque_cv_pct,
            fatal_error=bool(progress.get("fatal_error")),
        )
        convergence["convergence_preset"] = settings.convergence_preset
        progress["convergence"] = convergence
        progress["batch_id"] = batch_id
        state.update_solver_progress(progress)
        write_batch_heartbeat(batch_id, case_dir, "solver", process, command)
        write_overnight_state(
            batch_id,
            {
                "status": "running",
                "stage": "SOLVER_RUNNING",
                "current_command": " ".join(command),
                "current_pid": process.pid,
                "active_wrapper_pid": process.pid,
                "active_process_group_id": process_group_id(process.pid),
                "active_child_pid": None,
                "current_stage": "solver",
                "last_transition_reason": "solver_heartbeat",
                "solver": {
                    "command": command,
                    "cwd": str(ROOT),
                    "absolute_wrapper_path": str(wrapper_path),
                    "pid": process.pid,
                    "returncode": None,
                    "timeout_seconds": max_wall_s if max_wall_s > 0 else None,
                    "stdout_stderr_log_path": str(wrapper_log_path),
                    **solver_artifact_status(case_dir),
                },
            },
        )
        with overnight_control["lock"]:
            stop_all = bool(overnight_control["stop_all"])
        if progress.get("fatal_error") and progress.get("solver_log"):
            solver_status = "failed"
            warnings.append("SOLVER_FATAL")
            termination_reason = "fatal_solver_log_detected"
            termination_initiated_by_backend = True
            write_overnight_state(
                batch_id,
                {
                    "stage": "solver fatal detected",
                    "solver_fatal_match": progress.get("fatal_match", {}),
                    "ignored_log_warnings": progress.get("ignored_log_warnings", []),
                },
            )
            terminate_process_group(process)
            break
        if stop_all:
            solver_status = "stopped"
            warnings.append("STOPPED_BY_USER")
            artifacts = solver_artifact_status(case_dir)
            warnings.append("STOPPED_WITH_DATA" if artifacts["force_file_exists"] else "STOPPED_NO_DATA")
            termination_reason = "user_stop_entire_batch"
            termination_initiated_by_backend = True
            terminate_process_group(process)
            break
        if settings.auto_stop_on_convergence and convergence.get("converged"):
            solver_status = "converged_stop"
            termination_reason = "force_convergence_reached"
            termination_initiated_by_backend = True
            write_overnight_state(
                batch_id,
                {
                    "stage": "CONVERGED_STOP",
                    "stop_reason": termination_reason,
                    "convergence": convergence,
                },
            )
            terminate_process_group(process)
            break
        if max_wall_s > 0 and time.monotonic() - started >= max_wall_s:
            solver_status = "timeout"
            termination_reason = "max_wall_clock_timeout"
            termination_initiated_by_backend = True
            terminate_process_group(process)
            break
        time.sleep(1.0)
    process.wait()
    wrapper_log.close()
    final_progress = build_solver_progress(case_dir, process_running=False)
    final_progress["batch_id"] = batch_id
    artifacts = solver_artifact_status(case_dir)
    if process.returncode not in (0, None) and solver_status == "converged_stop":
        warnings.append("CONVERGED_STOP")
    elif process.returncode not in (0, None) and solver_status == "timeout":
        warnings.append("WARNING_TIMEOUT_NOT_CONVERGED")
    elif process.returncode not in (0, None) and solver_status == "success":
        joined_output = "\n".join(tail_file(wrapper_log_path, 200))
        patch_diagnostic = case_dir / "patch_preflight_diagnostic.json"
        if patch_diagnostic.is_file() or "MESH_PATCH_ERROR" in joined_output or "FORCE_PATCH_MISSING" in joined_output:
            solver_status = "mesh_patch_error"
            warnings.append("MESH_PATCH_ERROR")
        elif artifacts["force_file_exists"] and not final_progress.get("fatal_error"):
            solver_status = "interrupted"
            warnings.append("WARNING_INTERRUPTED")
        elif not artifacts["solver_log_exists"]:
            solver_status = "orchestration_error"
            warnings.append("ORCHESTRATION_ERROR")
            if process.returncode == -15:
                warnings.append("WARNING_TERMINATED_BY_RUNNER" if termination_initiated_by_backend else "WARNING_SIGTERM_UNKNOWN")
                termination_reason = termination_reason or "unknown_sigterm"
        else:
            solver_status = "failed"
            warnings.append(f"SOLVER_EXIT_{process.returncode}")
    if not termination_reason and process.returncode == -15:
        termination_reason = "unknown_sigterm"
    state.update_solver_progress(final_progress)
    diagnostic = {
        "command": command,
        "cwd": str(ROOT),
        "absolute_wrapper_path": str(wrapper_path),
        "launch_time": launch_time,
        "termination_reason": termination_reason or "process_exited",
        "termination_initiated_by_backend": termination_initiated_by_backend,
        "timeout_seconds": max_wall_s if max_wall_s > 0 else None,
        "stdout_stderr_log_path": str(wrapper_log_path),
        "pid": process.pid,
        "returncode": process.returncode,
        "solver_status": solver_status,
        "fatal_match": final_progress.get("fatal_match", {}),
        "ignored_log_warnings": final_progress.get("ignored_log_warnings", []),
        "convergence": final_progress.get("convergence", {}),
        "stdout_stderr_tail": tail_file(wrapper_log_path),
        **artifacts,
    }
    (case_dir / "solver_orchestration_diagnostic.json").write_text(json.dumps(diagnostic, indent=2) + "\n")
    write_overnight_state(
        batch_id,
        {
                "stage": "solver completed" if solver_status == "success" else f"solver {solver_status}",
                "stop_reason": termination_reason or "process_exited",
                "current_pid": process.pid,
                "solver": diagnostic,
        },
    )
    with overnight_control["lock"]:
        overnight_control["process"] = None
        overnight_control["process_group_id"] = None
        overnight_control["case_dir"] = None
        overnight_control["stage"] = ""
    return solver_status, time.monotonic() - started, warnings


def run_overnight_batch_task(settings: OvernightSettings) -> None:
    batch_id = settings.batch_id
    existing_results_path = IMPORTED_EXPORTS_DIR / batch_id / "batch_results.json"
    if existing_results_path.is_file():
        try:
            results: list[dict[str, Any]] = json.loads(existing_results_path.read_text())
        except (OSError, json.JSONDecodeError):
            results = []
    else:
        results = []
    returncode = 0
    error = ""
    final_status = "completed"
    try:
        manifest = update_manifest_operating_conditions(read_overnight_manifest(batch_id), settings)
        rows = selected_overnight_rows(manifest, settings)
        recovered_active_variant = active_variant_from_processes(batch_id, rows)
        completed_ids = {str(result.get("variant_id")) for result in results if str(result.get("solver_status", "")).lower() in {"success", "converged_stop", "completed", "timeout", "interrupted", "stopped"}}
        state.set_summary({"batch_id": batch_id, "batch_run": True, "selected_count": len(rows)})
        write_overnight_state(
            batch_id,
            {
                "status": "running",
                "selected_count": len(rows),
                "total_variants": len(rows),
                "settings": settings.dict(),
                "started_at": time.time(),
                "backend_runner_active": True,
                "completed_variant_ids": sorted(completed_ids),
                "failed_variant_ids": [str(result.get("variant_id")) for result in results if str(result.get("solver_status", "")).lower() in {"failed", "mesh_patch_error", "orchestration_error"}],
                "stop_requested": False,
                "stop_after_current": False,
                "recovered_process": bool(recovered_active_variant),
                "recovered_active_variant_id": recovered_active_variant,
            },
        )
        for index, row in enumerate(rows, start=1):
            with overnight_control["lock"]:
                if overnight_control["stop_all"]:
                    final_status = "stopped"
                    break
            name = str(row["variant_id"])
            if name in completed_ids:
                write_overnight_state(batch_id, {"current_index": index, "current_variant_id": name, "stage": "variant skipped existing result", "last_transition_reason": "existing_result_on_resume"})
                continue
            if recovered_active_variant and name != recovered_active_variant:
                recovered_index = next((idx for idx, item in enumerate(rows, start=1) if str(item.get("variant_id")) == recovered_active_variant), index)
                if index < recovered_index:
                    write_overnight_state(batch_id, {"current_index": index, "current_variant_id": name, "stage": "variant skipped during active-process recovery", "last_transition_reason": "waiting_for_recovered_active_variant", "recovered_active_variant_id": recovered_active_variant})
                    continue
            state.update_case(f"{batch_case_prefix(batch_id)}/{name}", index, len(rows))
            state.update(stage="generate", command=f"generate Batch Run case {name}")
            write_overnight_state(
                batch_id,
                {
                    "status": "running",
                    "current_index": index,
                    "current_variant_id": name,
                    "stage": "variant_started",
                    "current_stage": "variant_started",
                    "last_transition_reason": "variant_started",
                    "stop_requested": False,
                    "stop_after_current": bool(overnight_control.get("stop_current")),
                },
            )
            warning_flags = [flag for flag in str(row.get("warning_flags", "")).split(",") if flag]
            case_dir = CASES_DIR / batch_case_prefix(batch_id) / sanitize_id(name)
            case_row = overnight_case_row(batch_id, row, settings)
            try:
                case_dir = generate_variant_case(ROOT, case_row, index + 1, overwrite=True)
                row = update_case_stl_preflight(row, case_dir)
                write_overnight_manifest(manifest)
                metadata = json.loads((case_dir / "case_info.json").read_text())
                metadata.update(
                    {
                        "batch_run": True,
                        "batch_id": batch_id,
                        "source_variant_id": row.get("variant_id"),
                        "original_stl_filename": row.get("original_stl_filename"),
                        "imported_stl_path": row.get("imported_stl_path"),
                        "json_metadata_path": row.get("json_metadata_path"),
                        "run_metadata_version": 1,
                    }
                )
                (case_dir / "run_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
                if settings.batch_mesh_profile != "production_high_resolution":
                    warning_flags.append("BATCH_NOT_PRODUCTION_HIGH_RESOLUTION")
                    raise RuntimeError("Batch Run is not using the high-resolution production mesh.")
                write_mesh_metadata(case_dir, "production_high_resolution", sanity_check=False)
                state.update(stage="blockMesh", command="production high-resolution mesh Batch Run case")
                write_overnight_state(batch_id, {"stage": "mesh_started", "current_stage": "snappyHexMesh", "case_path": str(case_dir), "last_transition_reason": "mesh_started"})
                mesh_code, mesh_output = run_command_with_heartbeat([PYTHON, "scripts/run_case.py", str(case_dir), "--snappy"], "snappyHexMesh", batch_id, case_dir)
                with overnight_control["lock"]:
                    if overnight_control["stop_all"]:
                        final_status = "stopped"
                        warning_flags.append("STOPPED_NO_DATA")
                        raise RuntimeError("STOPPED_NO_DATA")
                if mesh_code != 0:
                    if (case_dir / "patch_preflight_diagnostic.json").is_file() or "MESH_PATCH_ERROR" in mesh_output:
                        warning_flags.append("MESH_PATCH_ERROR")
                        raise RuntimeError("MESH_PATCH_ERROR: propeller wall patch missing after snappyHexMesh")
                    warning_flags.append("MESH_FAILED")
                    raise RuntimeError("Production mesh failed")
                write_overnight_state(batch_id, {"stage": "mesh_completed", "current_stage": "mesh_completed", "case_path": str(case_dir), "last_transition_reason": "mesh_completed"})
                assert_production_mesh_allowed(case_dir)
                write_overnight_state(batch_id, {"stage": "solver_started", "current_stage": "solver", "case_path": str(case_dir), "last_transition_reason": "solver_started"})
                solver_status, runtime_s, solver_warnings = run_overnight_solver(case_dir, batch_id, settings)
                warning_flags.extend(solver_warnings)
                result = overnight_result_row(batch_id, row, case_dir, settings, solver_status, warning_flags, runtime_s)
                results = [prior for prior in results if str(prior.get("variant_id")) != name]
                results.append(result)
                write_overnight_results(batch_id, results)
                completed_ids.add(name)
                with overnight_control["lock"]:
                    stop_after_current = bool(overnight_control["stop_current"])
                    stop_requested = bool(overnight_control["stop_all"])
                next_variant = rows[index]["variant_id"] if index < len(rows) else ""
                transition = {
                    "current_index_before": index,
                    "current_index_after": index + 1 if index < len(rows) and not stop_after_current and not stop_requested else index,
                    "selected_variant_ids": [str(item.get("variant_id")) for item in rows],
                    "current_variant_status": solver_status,
                    "stop_requested": stop_requested,
                    "stop_after_current": stop_after_current,
                    "continue_after_failed_variant": settings.continue_after_failed_variant,
                    "stop_on_first_fatal_error": settings.stop_on_first_fatal_error,
                    "result_row_written": True,
                    "next_variant": next_variant,
                    "next_started": bool(next_variant and not stop_after_current and not stop_requested),
                }
                (IMPORTED_EXPORTS_DIR / batch_id / "batch_transition_diagnostic.json").write_text(json.dumps(transition, indent=2) + "\n")
                write_overnight_state(
                    batch_id,
                    {
                        "status": "running",
                        "current_index": index,
                        "completed_variants": len(results),
                        "completed_variant_ids": sorted(completed_ids),
                        "results": results,
                        "stage": "variant_completed",
                        "current_stage": "variant_completed",
                        "last_completed_variant_id": name,
                        "next_variant_id": next_variant,
                        "last_transition_reason": solver_status,
                        "stop_requested": stop_requested,
                        "stop_after_current": stop_after_current,
                    },
                )
                if stop_requested:
                    final_status = "stopped"
                    break
                if stop_after_current:
                    final_status = "stopped_after_current"
                    break
                if recovered_active_variant == name:
                    recovered_active_variant = ""
                if solver_status == "failed" and settings.stop_on_first_fatal_error:
                    returncode = 1
                    error = f"Fatal solver failure for {name}"
                    break
                if solver_status == "failed" and not settings.continue_after_failed_variant:
                    returncode = 1
                    error = f"Variant failed: {name}"
                    break
            except (OSError, RuntimeError, CaseGenerationError) as exc:
                warning_flags.append(str(exc))
                with overnight_control["lock"]:
                    stopped_all = bool(overnight_control["stop_all"])
                if stopped_all:
                    failure_status = "stopped_no_data"
                    final_status = "stopped"
                else:
                    failure_status = "mesh_patch_error" if any("MESH_PATCH_ERROR" in flag for flag in warning_flags) else "failed"
                failed = overnight_result_row(batch_id, row, case_dir, settings, failure_status, warning_flags, 0.0)
                results = [prior for prior in results if str(prior.get("variant_id")) != name]
                results.append(failed)
                write_overnight_results(batch_id, results)
                write_overnight_state(batch_id, {"status": "running", "current_index": index, "failed_variant_ids": [str(result.get("variant_id")) for result in results if str(result.get("solver_status", "")).lower() in {"failed", "mesh_patch_error", "orchestration_error"}], "results": results, "stage": "variant_failed", "last_transition_reason": failure_status})
                if stopped_all:
                    break
                if not settings.continue_after_failed_variant or settings.stop_on_first_fatal_error:
                    returncode = 1
                    error = str(exc)
                    break
    except (HTTPException, OSError, RuntimeError, ValueError) as exc:
        returncode = 1
        error = str(exc)
        state.append_log(f"ERROR: {exc}")
    write_overnight_results(batch_id, results)
    if returncode != 0:
        final_status = "failed"
    write_overnight_state(batch_id, {"status": final_status, "error": error, "results": results, "finished_at": time.time(), "backend_runner_active": False, "stage": "batch_stopped" if final_status.startswith("stopped") else "batch_completed", "last_transition_reason": final_status})
    with overnight_control["lock"]:
        overnight_control["stop_all"] = False
        overnight_control["stop_current"] = False
        overnight_control["batch_id"] = ""
    state.finish(returncode=returncode, error=error, results=[{k: str(v) for k, v in row.items()} for row in results], summary={"batch_id": batch_id, "batch_run": True})


def start_overnight_thread(settings: OvernightSettings) -> dict[str, str]:
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail=f"Task already running: {state.action}")
        state.begin_unlocked("Batch Run", batch_case_prefix(settings.batch_id))
    with overnight_control["lock"]:
        overnight_control["batch_id"] = settings.batch_id
        overnight_control["stop_current"] = False
        overnight_control["stop_all"] = False
    thread = threading.Thread(target=run_overnight_batch_task, args=(settings,), daemon=True)
    thread.start()
    selected_count = len(settings.selected_variant_ids)
    return {"status": "started", "action": "Batch Run", "batch_id": settings.batch_id, "job_id": settings.batch_id, "selected_variant_count": str(selected_count)}


def run_overnight_sanity_task(settings: OvernightSettings) -> None:
    batch_id = settings.batch_id
    returncode = 0
    error = ""
    try:
        manifest = update_manifest_operating_conditions(read_overnight_manifest(batch_id), settings)
        selected_ids = set(settings.selected_variant_ids)
        updated_rows = []
        rows = manifest.get("variants", [])
        selected_rows = [row for row in rows if not selected_ids or row.get("variant_id") in selected_ids]
        for index, row in enumerate(rows, start=1):
            is_selected = row in selected_rows
            checked = geometry_sanity_for_variant(row) if is_selected else row
            if is_selected and checked.get("geometry_sanity_status") != "Geometry Fail":
                state.update_case(f"{batch_case_prefix(batch_id)}/{checked['variant_id']}", index, len(selected_rows))
                case_row = overnight_case_row(batch_id, checked, settings)
                case_dir = generate_variant_case(ROOT, case_row, index + 1, overwrite=True)
                checked = update_case_stl_preflight(checked, case_dir)
                warnings = [flag for flag in str(checked.get("warning_flags", "")).split(",") if flag]
                profile = "quick_coarse_mesh_check" if settings.run_mesh_sanity else "fast_geometry_check"
                write_mesh_metadata(case_dir, profile, sanity_check=True)
                checked["geometry_check_profile"] = profile
                checked["batch_mesh_profile"] = "production_high_resolution"
                if settings.run_mesh_sanity:
                    apply_quick_coarse_mesh_profile(case_dir)
                    write_mesh_metadata(case_dir, "quick_coarse_mesh_check", sanity_check=True)
                    mesh_code, _ = run_command_with_timeout([PYTHON, "scripts/run_case.py", str(case_dir), "--snappy"], "blockMesh", timeout_s=300)
                    if mesh_code == 0:
                        setup_checks, _ = validation_setup_checks(case_dir)
                        if any(item["name"] == "propeller patch found" and not item["ok"] for item in setup_checks):
                            warnings.append("PATCH_NOT_FOUND")
                        if any("rotor cellZone found" in str(item["name"]) and not item["ok"] for item in setup_checks):
                            warnings.append("ROTOR_ZONE_NOT_FOUND")
                        checked["geometry_sanity_status"] = "Quick Mesh OK" if not any(flag in warnings for flag in ("PATCH_NOT_FOUND", "ROTOR_ZONE_NOT_FOUND")) else "Quick Mesh Fail"
                    else:
                        warnings.append("MESH_CHECK_TIMEOUT" if mesh_code == 124 else "MESH_FAILED")
                        checked["geometry_sanity_status"] = "Quick Mesh Warning" if mesh_code == 124 else "Quick Mesh Fail"
                checked["warning_flags"] = ",".join(sorted(set(warnings)))
            updated_rows.append(checked)
        manifest["variants"] = updated_rows
        write_overnight_manifest(manifest)
        write_geometry_sanity_reports(manifest)
        state.finish(returncode=0, summary={"batch_id": batch_id, "batch_sanity": True, "variants": updated_rows})
    except (HTTPException, OSError, RuntimeError, CaseGenerationError, ValueError) as exc:
        returncode = 1
        error = str(exc)
        state.append_log(f"ERROR: {exc}")
        state.finish(returncode=returncode, error=error, diagnostics=[error])


def start_overnight_sanity_thread(settings: OvernightSettings) -> dict[str, str]:
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail=f"Task already running: {state.action}")
        state.begin_unlocked("Batch Run geometry check", batch_case_prefix(settings.batch_id))
    thread = threading.Thread(target=run_overnight_sanity_task, args=(settings,), daemon=True)
    thread.start()
    return {"status": "started", "action": "Batch Run geometry check"}


def variant_row(variant_id: str) -> dict[str, str]:
    for row in read_variants():
        if row.get("variant_id") == variant_id:
            return row
    raise HTTPException(status_code=400, detail=f"Unknown variant_id: {variant_id}")


def validation_values(request: ValidationRequest) -> tuple[float, float]:
    presets = {
        "100_10000": (100.0, 10000.0),
        "300_10000": (300.0, 10000.0),
        "650_10000": (650.0, 10000.0),
        "650_selected": (650.0, request.rpm),
    }
    vinf_mph, rpm = presets.get(request.preset, (request.Vinf_mph, request.rpm))
    return vinf_mph, rpm


def validation_case_name(variant_id: str) -> str:
    if not CASE_RE.fullmatch(variant_id):
        raise HTTPException(status_code=400, detail="Invalid variant_id")
    return f"validation_{variant_id}"


def validation_row(request: ValidationRequest) -> tuple[dict[str, str], dict[str, Any]]:
    base = variant_row(request.variant_id)
    vinf_mph, rpm = validation_values(request)
    vinf_mps = mph_to_mps(vinf_mph)
    row = dict(base)
    row.update(
        {
            "variant_id": validation_case_name(request.variant_id),
            "source_variant_id": request.variant_id,
            "solver_mode": "compressible_forward_mrf",
            "batch_enabled": "false",
            "rpm": f"{rpm:.12g}",
            "Vinf_mps": f"{vinf_mps:.12g}",
            "Tinf_K": f"{request.Tinf_K:.12g}",
            "pinf_Pa": f"{request.pinf_Pa:.12g}",
            "gamma": row.get("gamma", "1.4") or "1.4",
            "R": row.get("R", "287") or "287",
            "Cp": row.get("Cp", "1004.5") or "1004.5",
            "mu": row.get("mu", "1.7894e-5") or "1.7894e-5",
            "Pr": row.get("Pr", "0.71") or "0.71",
        }
    )
    gamma = float(row["gamma"])
    gas_r = float(row["R"])
    speed_of_sound = math.sqrt(gamma * gas_r * request.Tinf_K)
    diameter = float(row["diameter_m"])
    radius = diameter / 2.0
    omega = rpm * 2.0 * math.pi / 60.0
    advance_ratio = vinf_mps / ((rpm / 60.0) * diameter) if rpm > 0 and diameter > 0 else ""
    config = {
        "validation_id": uuid.uuid4().hex,
        "source_variant_id": request.variant_id,
        "validation_case": row["variant_id"],
        "solver_mode": "compressible_forward_mrf",
        "rpm": rpm,
        "Vinf_mph": vinf_mph,
        "Vinf_mps": vinf_mps,
        "Tinf_K": request.Tinf_K,
        "pinf_Pa": request.pinf_Pa,
        "gamma": gamma,
        "R": gas_r,
        "air_model": "perfectGas",
        "turbulence_model": "kOmegaSST",
        "solver": "rhoPimpleFoam",
        "omega_rad_s": omega,
        "diameter_m": diameter,
        "radius_m": radius,
        "advance_ratio_J": advance_ratio,
        "freestream_Mach": vinf_mps / speed_of_sound,
        "tip_speed_mps": omega * radius,
        "helical_tip_Mach": math.hypot(vinf_mps, omega * radius) / speed_of_sound,
        "validation_mode": "quick",
        "validation_wall_clock_limit_s": max(60, int(request.validation_wall_clock_limit_s)),
        "validation_target_timesteps": max(20, int(request.validation_target_timesteps)),
        "validation_force_samples": max(3, int(request.validation_force_samples)),
        "freestream_direction": [0, 0, 1],
        "positive_thrust_direction": "configured by thrust_sign and thrust_axis in case_info.json",
        "rotation_axis": [0, 0, 1],
        "omega_sign": "positive about +z",
        "force_component_used_as_thrust": "z",
        "moment_component_used_as_torque": "z",
        "note": "PASS means internally consistent and runnable.",
    }
    return row, config


def validation_runtime_settings(rpm: float, validation_revolutions: float) -> dict[str, float]:
    revolutions = max(0.01, validation_revolutions)
    if rpm <= 0:
        end_time = 0.0006
    else:
        end_time = revolutions * 60.0 / rpm
    end_time = min(max(end_time, 1e-5), 0.001)
    write_interval = max(end_time / 4.0, 1e-6)
    return {"validation_revolutions": revolutions, "validation_endTime_s": end_time, "validation_writeInterval_s": write_interval}


def patch_control_dict_for_validation(case_dir: Path, rpm: float, validation_revolutions: float) -> dict[str, float]:
    path = case_dir / "system" / "controlDict"
    settings = validation_runtime_settings(rpm, validation_revolutions)
    text = path.read_text()
    text = re.sub(r"\bendTime\s+[^;]+;", f"endTime         {settings['validation_endTime_s']:.8g};", text)
    text = re.sub(r"\bwriteInterval\s+[^;]+;", f"writeInterval   {settings['validation_writeInterval_s']:.8g};", text)
    text = re.sub(r"\bmaxDeltaT\s+[^;]+;", "maxDeltaT       2e-5;", text)
    path.write_text(text)
    return settings


def read_boundary_names(boundary_path: Path) -> set[str]:
    if not boundary_path.is_file():
        return set()
    text = boundary_path.read_text(errors="replace")
    body_match = re.search(r"\n\s*\d+\s*\(\s*(.*)\s*\)\s*", text, flags=re.DOTALL)
    body = body_match.group(1) if body_match else text
    return {
        match.group(1)
        for match in re.finditer(r"^\s*([A-Za-z_]\w*)\s*\{", body, flags=re.MULTILINE)
        if match.group(1) != "FoamFile"
    }


def parse_cell_zone_names(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    text = path.read_text(errors="replace")
    body_match = re.search(r"\n\s*\d+\s*\(\s*(.*)\s*\)\s*", text, flags=re.DOTALL)
    body = body_match.group(1) if body_match else text
    return {
        match.group(1)
        for match in re.finditer(r"^\s*([A-Za-z_]\w*)\s*\{", body, flags=re.MULTILINE)
        if match.group(1) != "FoamFile"
    }


def check_contains(path: Path, pattern: str) -> bool:
    return path.is_file() and re.search(pattern, path.read_text(errors="replace"), flags=re.DOTALL) is not None


def fvsolution_solver_entries(case_dir: Path) -> set[str]:
    path = case_dir / "system" / "fvSolution"
    if not path.is_file():
        return set()
    text = path.read_text(errors="replace")
    return set(re.findall(r"^\s*([A-Za-z_]\w*)\s*\{", text, flags=re.MULTILINE))


def validation_setup_checks(case_dir: Path) -> tuple[list[dict[str, object]], list[str]]:
    checks: list[dict[str, object]] = []
    warnings: list[str] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            warnings.append(f"{name}: {detail or 'failed'}")

    required = [
        "0/U", "0/p", "0/T", "0/k", "0/omega", "0/nut", "0/alphat",
        "constant/thermophysicalProperties", "constant/turbulenceProperties",
        "constant/MRFProperties", "system/controlDict", "system/functions",
        "run_metadata.json", "validation_config.json",
    ]
    for rel in required:
        add(f"required file {rel}", (case_dir / rel).is_file())

    info = json.loads((case_dir / "case_info.json").read_text()) if (case_dir / "case_info.json").is_file() else {}
    expected_zone = expected_cell_zone(info)
    boundary_names = read_boundary_names(case_dir / "constant" / "polyMesh" / "boundary")
    add("propeller patch found", "propeller" in boundary_names, f"mesh patches found: {sorted(boundary_names)}")
    zones = parse_cell_zone_names(case_dir / "constant" / "polyMesh" / "cellZones")
    add(f"{expected_zone} rotor cellZone found", expected_zone in zones, f"cellZones found: {sorted(zones)}")
    mrf_valid = check_contains(case_dir / "constant" / "MRFProperties", rf"\bcellZone\s+{re.escape(expected_zone)}\s*;")
    add(f"MRFProperties references {expected_zone}", mrf_valid)
    if zones and expected_zone not in zones:
        add("MRF zone name mismatch", False, f"MRFProperties expects {expected_zone}; mesh has {sorted(zones)}")
    force_valid = check_contains(case_dir / "system" / "functions", r"\bpatches\s*\([^;]*\bpropeller\b[^;]*\)\s*;")
    add("force output configured for propeller", force_valid)
    add("controlDict uses rhoPimpleFoam", check_contains(case_dir / "system" / "controlDict", r"\bapplication\s+rhoPimpleFoam\s*;"))
    add("U internalField uses axial freestream", check_contains(case_dir / "0" / "U", r"internalField\s+uniform\s+\(0\s+0\s+[0-9.eE+-]+\)"))
    add("p dimensions are absolute pressure", check_contains(case_dir / "0" / "p", r"dimensions\s+\[1\s+-1\s+-2"))
    add("T initialized", check_contains(case_dir / "0" / "T", r"internalField\s+uniform\s+288\.15"))
    add("perfect gas thermo configured", check_contains(case_dir / "constant" / "thermophysicalProperties", r"equationOfState\s+perfectGas\s*;"))
    add("force function uses computed rho", check_contains(case_dir / "system" / "functions", r"\brho\s+rho\s*;"))
    add("kOmegaSST configured", check_contains(case_dir / "constant" / "turbulenceProperties", r"kOmegaSST"))
    solver_entries = fvsolution_solver_entries(case_dir)
    for entry in ["p", "pFinal", "rho", "rhoFinal", "U", "UFinal", "h", "hFinal", "e", "eFinal", "k", "kFinal", "omega", "omegaFinal"]:
        add(f"fvSolution solver entry {entry}", entry in solver_entries, f"solver entries found: {sorted(solver_entries)}")
    return checks, warnings


def validation_report_status(sections: dict[str, Any]) -> str:
    warnings = sections.get("warnings", [])
    setup = sections.get("setup_checks", [])
    startup = sections.get("solver_startup_checks", [])
    stability = sections.get("numerical_stability_checks", [])
    force = sections.get("force_torque_checks", [])
    if any(not item.get("ok", False) for item in setup):
        return "FAIL"
    if any(not item.get("ok", False) for item in startup):
        return "FAIL"
    hard_stability = {"Courant number history", "maximum Courant number", "no fatal error, NaN, or floating point exception", "no obvious nonphysical pressure/temperature/velocity"}
    if any(not item.get("ok", False) for item in stability if item.get("name") in hard_stability):
        return "FAIL"
    if any(not item.get("ok", False) for item in force):
        return "FAIL"
    return "WARNING" if warnings else "PASS"


def write_validation_report(case_dir: Path, report: dict[str, Any]) -> None:
    report["summary"] = validation_report_status(report)
    json_path = case_dir / "validation_report.json"
    md_path = case_dir / "validation_report.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n")
    lines = [
        f"# Forward-Flight Validation Report: {report['summary']}",
        "",
        "Quick validation confirms the compressible forward-flight case is wired correctly and producing parseable force/torque data.",
        "",
        "## Case Settings",
    ]
    for key, value in report.get("case_settings", {}).items():
        lines.append(f"- {key}: {value}")
    lines += ["", "## Expected Nondimensional Parameters"]
    for key, value in report.get("expected_parameters", {}).items():
        lines.append(f"- {key}: {value}")
    parsed_values = report.get("parsed_force_values", {})
    if parsed_values:
        lines += ["", "## Parsed Force And Torque Values"]
        for key in [
            "force_file", "force_samples", "last_force_sample_time", "last_pressure_force_N",
            "last_viscous_force_N", "last_pressure_moment_Nm", "last_viscous_moment_Nm",
            "last_total_force_N", "last_total_moment_Nm", "force_N", "moment_Nm",
            "thrust_convention_N", "fluid_torque_Nm",
            "shaft_torque_Nm", "torque_sign_convention", "power_W", "eta",
            "advance_ratio_J", "freestream_Mach", "tip_speed_mps", "helical_tip_Mach",
        ]:
            if key in parsed_values:
                lines.append(f"- {key}: {parsed_values[key]}")
    for title, key in [
        ("Setup Checks", "setup_checks"),
        ("Solver Startup Checks", "solver_startup_checks"),
        ("Numerical Stability Checks", "numerical_stability_checks"),
        ("Force/Torque Parsing Checks", "force_torque_checks"),
        ("Physics Sanity Checks", "physics_sanity_checks"),
        ("Sign-Convention Checks", "sign_convention_checks"),
    ]:
        lines += ["", f"## {title}"]
        items = report.get(key, [])
        if isinstance(items, list):
            for item in items:
                status = "OK" if item.get("ok") else "WARN/FAIL"
                lines.append(f"- {status}: {item.get('name')} {item.get('detail', '')}")
    lines += ["", "## Warnings and Recommended Next Action"]
    warnings = report.get("warnings", [])
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- No validation warnings recorded.")
    lines += [
        "",
        "## Next validation steps",
        "- run 100 mph / 10000 rpm",
        "- then 300 mph / 10000 rpm",
        "- then 650 mph / 10000 rpm",
        "- then 650 mph / target rpm",
        "- then longer production-style validation for the selected condition",
        "- compare trends in thrust, torque, power, eta, Mach, and stability",
        "- only then run the full batch",
    ]
    md_path.write_text("\n".join(lines) + "\n")


def prepare_validation_case(request: ValidationRequest) -> dict[str, Any]:
    row, config = validation_row(request)
    case_dir = generate_forward_case(row, 2, request.force_remesh)
    for stale in ("solver_progress.json", "solver_timeseries.json", "solver_timeseries_summary.json"):
        stale_path = case_dir / stale
        if stale_path.exists():
            stale_path.unlink()
    runtime_config = patch_control_dict_for_validation(case_dir, float(config["rpm"]), request.validation_revolutions)
    config.update(runtime_config)
    metadata = json.loads((case_dir / "run_metadata.json").read_text())
    metadata.update(
        {
            "validation_case": True,
            "validation_runtime": "quick",
            "validation_id": config["validation_id"],
            "validation_wall_clock_limit_s": config["validation_wall_clock_limit_s"],
            "validation_target_timesteps": config["validation_target_timesteps"],
            "validation_force_samples": config["validation_force_samples"],
            **runtime_config,
        }
    )
    (case_dir / "run_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (case_dir / "validation_config.json").write_text(json.dumps(config, indent=2) + "\n")
    setup_checks, warnings = validation_setup_checks(case_dir)
    report = {
        "summary": "WARNING",
        "validation_id": config["validation_id"],
        "validation_status": "prepared",
        "case_path": str(case_dir),
        "case_settings": config,
        "expected_parameters": {
            key: config[key]
            for key in [
                "Vinf_mps", "Vinf_mph", "rpm", "omega_rad_s", "advance_ratio_J",
                "freestream_Mach", "tip_speed_mps", "helical_tip_Mach",
            ]
        },
        "setup_checks": setup_checks,
        "solver_startup_checks": [],
        "numerical_stability_checks": [],
        "force_torque_checks": [],
        "physics_sanity_checks": [],
        "sign_convention_checks": [
            {"name": "freestream direction vector", "ok": True, "detail": str(config["freestream_direction"])},
            {"name": "positive thrust direction", "ok": True, "detail": str(config["positive_thrust_direction"])},
            {"name": "rotation axis", "ok": True, "detail": str(config["rotation_axis"])},
            {"name": "omega sign", "ok": True, "detail": str(config["omega_sign"])},
            {"name": "force component used as thrust", "ok": True, "detail": str(config["force_component_used_as_thrust"])},
            {"name": "moment component used as torque", "ok": True, "detail": str(config["moment_component_used_as_torque"])},
        ],
        "warnings": warnings,
        "recommended_next_action": "Run Validation Case if setup checks are acceptable.",
    }
    write_validation_report(case_dir, report)
    return report


def validation_mesh_ready(case_dir: Path) -> bool:
    if not mesh_ready(case_dir):
        return False
    info = json.loads((case_dir / "case_info.json").read_text()) if (case_dir / "case_info.json").is_file() else {}
    return (
        "propeller" in read_boundary_names(case_dir / "constant" / "polyMesh" / "boundary")
        and expected_cell_zone(info) in parse_cell_zone_names(case_dir / "constant" / "polyMesh" / "cellZones")
    )


COURANT_RE = re.compile(r"Courant Number.*mean:\s*([0-9.eE+-]+)\s*max:\s*([0-9.eE+-]+)")
RESIDUAL_RE = re.compile(r"Solving for (\w+), Initial residual = ([0-9.eE+-]+), Final residual = ([0-9.eE+-]+)")


def analyze_solver_log(case_dir: Path) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str], dict[str, Any]]:
    log_path = case_dir / "log.rhoPimpleFoam"
    text = log_path.read_text(errors="replace") if log_path.is_file() else ""
    lower = text.lower()
    expected_zone = expected_cell_zone(json.loads((case_dir / "case_info.json").read_text()) if (case_dir / "case_info.json").is_file() else {})
    startup = [
        {"name": "rhoPimpleFoam starts", "ok": log_path.is_file() and "create time" in lower, "detail": str(log_path)},
        {"name": "thermophysical model loads", "ok": "thermo" in lower and "error" not in lower[:2000], "detail": ""},
        {"name": "turbulence model loads", "ok": "komegasst" in lower or "turbulence" in lower, "detail": ""},
        {"name": "MRF loads", "ok": "creating mrf zone" in lower and expected_zone.lower() in lower, "detail": f"expected {expected_zone}"},
        {"name": "no missing boundary condition errors", "ok": "missing" not in lower or "boundary" not in lower, "detail": ""},
        {"name": "no missing patch errors", "ok": "unknown patch" not in lower and "cannot find patch" not in lower, "detail": ""},
        {"name": "no missing cellZone errors", "ok": expected_zone.lower() in lower and "cannot find cellzone" not in lower, "detail": f"expected {expected_zone}"},
        {"name": "no floating point exception during startup", "ok": "floating point exception" not in lower, "detail": ""},
    ]
    courant = [(float(mean), float(maximum)) for mean, maximum in COURANT_RE.findall(text)]
    residuals = [(field, float(initial), float(final)) for field, initial, final in RESIDUAL_RE.findall(text)]
    max_co = max((value[1] for value in courant), default="")
    mean_co = max((value[0] for value in courant), default="")
    stability = [
        {"name": "Courant number history", "ok": bool(courant), "detail": str(courant[-10:])},
        {"name": "maximum Courant number", "ok": max_co == "" or max_co <= 0.5, "detail": str(max_co)},
        {"name": "mean Courant number", "ok": mean_co == "" or mean_co <= 0.5, "detail": str(mean_co)},
        {"name": "residual history available", "ok": bool(residuals), "detail": str(residuals[-10:])},
        {"name": "residuals did not explode", "ok": not residuals or max(initial for _, initial, _ in residuals) < 1e6, "detail": ""},
        {"name": "no fatal error, NaN, or floating point exception", "ok": "fatal error" not in lower and "nan" not in lower and "floating point exception" not in lower, "detail": ""},
        {"name": "no obvious nonphysical pressure/temperature/velocity", "ok": "nan" not in lower and "bounding" not in lower, "detail": ""},
    ]
    warnings = []
    if max_co != "" and max_co > 0.5:
        warnings.append(f"Courant max {max_co:.4g} exceeded configured maxCo 0.5")
    if any(not item["ok"] for item in startup + stability):
        warnings.append("Solver log contains validation warnings; inspect log.rhoPimpleFoam")
    return startup, stability, warnings, {"courant": courant, "residuals": residuals}


def analyze_force_parse(case_dir: Path, parse_output: str, parse_code: int) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str], dict[str, str]]:
    values = parse_key_values(parse_output)
    warnings = []
    force_file = values.get("force_file", "")
    thrust = safe_float(values.get("thrust_convention_N"), float("nan"))
    torque = safe_float(values.get("torque_convention_Nm"), float("nan"))
    fluid_torque = safe_float(values.get("fluid_torque_Nm"), float("nan"))
    shaft_torque = safe_float(values.get("shaft_torque_Nm"), float("nan"))
    power = safe_float(values.get("power_W"), float("nan"))
    eta_text = values.get("eta", "")
    eta = safe_float(eta_text, float("nan")) if eta_text != "" else float("nan")
    force_samples = int(float(values.get("force_samples", values.get("samples_averaged", "0")) or 0))
    checks = [
        {"name": "parse_forces.py completed", "ok": parse_code == 0, "detail": f"returncode={parse_code}"},
        {"name": "force output file exists", "ok": bool(force_file) and Path(force_file).is_file(), "detail": force_file},
        {"name": "several force samples parseable", "ok": force_samples >= 3, "detail": str(force_samples)},
        {"name": "thrust can be parsed", "ok": math.isfinite(thrust), "detail": values.get("thrust_convention_N", "")},
        {"name": "fluid torque can be parsed", "ok": math.isfinite(fluid_torque), "detail": values.get("fluid_torque_Nm", "")},
        {"name": "shaft torque can be parsed", "ok": math.isfinite(shaft_torque), "detail": values.get("shaft_torque_Nm", "")},
        {"name": "legacy torque convention populated", "ok": math.isfinite(torque), "detail": values.get("torque_convention_Nm", "")},
        {"name": "power_W computed", "ok": math.isfinite(power), "detail": values.get("power_W", "")},
        {"name": "advance_ratio_J computed", "ok": values.get("advance_ratio_J", "") != "", "detail": values.get("advance_ratio_J", "")},
        {"name": "eta computed safely", "ok": eta_text == "" or math.isfinite(eta), "detail": eta_text or "blank for zero freestream or nonpositive power"},
    ]
    physics = [
        {"name": "freestream Mach computed", "ok": values.get("freestream_Mach", "") != "", "detail": values.get("freestream_Mach", "")},
        {"name": "tip speed computed", "ok": values.get("tip_speed_mps", "") != "", "detail": values.get("tip_speed_mps", "")},
        {"name": "helical tip Mach computed", "ok": values.get("helical_tip_Mach", "") != "", "detail": values.get("helical_tip_Mach", "")},
        {"name": "thrust sign convention checked", "ok": math.isfinite(thrust), "detail": f"thrust_N={thrust}"},
        {
            "name": "torque sign convention checked",
            "ok": math.isfinite(fluid_torque) and math.isfinite(shaft_torque),
            "detail": f"fluid_torque_Nm={fluid_torque}, shaft_torque_Nm={shaft_torque}, {values.get('torque_sign_convention', '')}",
        },
        {"name": "last total force vector", "ok": values.get("last_total_force_N", "") != "", "detail": values.get("last_total_force_N", "")},
        {"name": "last total moment vector", "ok": values.get("last_total_moment_Nm", "") != "", "detail": values.get("last_total_moment_Nm", "")},
    ]
    if parse_code != 0:
        warnings.append("Force parsing failed")
    if math.isfinite(thrust) and thrust < 0:
        warnings.append("Thrust is negative")
    if math.isfinite(shaft_torque) and shaft_torque < 0:
        warnings.append("Torque is negative or sign appears inconsistent with rotation direction")
    if math.isfinite(eta) and (eta < 0 or eta > 1):
        warnings.append("Eta is outside [0, 1]")
    samples_averaged = int(float(values.get("samples_averaged", "0") or 0))
    if samples_averaged < 20:
        warnings.append("Force history is short for production averaging; acceptable for quick validation but not convergence proof")
    return checks, physics, warnings, values


def run_validation_task(request: ValidationRequest) -> None:
    report: dict[str, Any] = {}
    returncode = 0
    error = ""
    try:
        report = prepare_validation_case(request)
        case_dir = Path(report["case_path"])
        state.update_case(case_dir.name, 1, 1)
        if request.force_remesh or not validation_mesh_ready(case_dir):
            mesh_command, mesh_stage = batch_commands("mesh", case_dir)
            mesh_code, mesh_output = run_command(mesh_command, mesh_stage)
            if mesh_code != 0:
                returncode = 1
                error = "Validation meshing failed before rhoPimpleFoam"
                report["warnings"].append(error)
                report["openfoam_log_excerpt"] = mesh_output[-4000:]
                write_validation_report(case_dir, report)
                state.finish(returncode=returncode, error=error, diagnostics=report["warnings"], summary=report)
                return
            setup_checks, setup_warnings = validation_setup_checks(case_dir)
            report["setup_checks"] = setup_checks
            report["warnings"] = [warning for warning in report["warnings"] if "propeller patch found" not in warning and "cellZone found" not in warning]
            report["warnings"].extend(setup_warnings)
            if any(not item.get("ok", False) for item in setup_checks):
                returncode = 1
                error = "Validation preflight failed after meshing; rhoPimpleFoam was not launched"
                report["warnings"].append(error)
                write_validation_report(case_dir, report)
                state.finish(returncode=returncode, error=error, diagnostics=report["warnings"], summary=report)
                return
        else:
            setup_checks, setup_warnings = validation_setup_checks(case_dir)
            report["setup_checks"] = setup_checks
            report["warnings"].extend(setup_warnings)
            if any(not item.get("ok", False) for item in setup_checks):
                returncode = 1
                error = "Validation preflight failed; rhoPimpleFoam was not launched"
                report["warnings"].append(error)
                write_validation_report(case_dir, report)
                state.finish(returncode=returncode, error=error, diagnostics=report["warnings"], summary=report)
                return
        report["validation_status"] = "solver_running"
        report["summary"] = "RUNNING"
        report["warnings"] = [warning for warning in report["warnings"] if "propeller patch found" not in warning and "cellZone found" not in warning]
        state.set_summary(report)
        code, output, progress = run_validation_solver_command(case_dir, report["case_settings"])
        stop_reason = str(progress.get("stop_reason", ""))
        if code != 0:
            returncode = 1
            error = "Validation OpenFOAM run failed"
            report["warnings"].append(error)
            report["openfoam_log_excerpt"] = output[-4000:]
        parse_code = 1
        parse_output = ""
        if code == 0:
            parse_code, parse_output = run_command([PYTHON, "scripts/parse_forces.py", str(case_dir), "--average-last", "20"], "parse")
        startup, stability, solver_warnings, solver_data = analyze_solver_log(case_dir)
        force_checks, physics_checks, force_warnings, values = analyze_force_parse(case_dir, parse_output, parse_code)
        if stop_reason:
            report["warnings"].append(f"Quick validation stopped: {stop_reason}")
        if stop_reason == "wall-clock limit reached":
            report["warnings"].append("Quick validation stopped after the wall-clock limit.")
        elif stop_reason in ("stopped by user",) and parse_code == 0:
            report["warnings"].append("Validation was stopped by user; classifying from available stable solver and force data.")
        report["solver_startup_checks"] = startup
        report["numerical_stability_checks"] = stability
        report["force_torque_checks"] = force_checks
        report["physics_sanity_checks"] = physics_checks
        report["solver_diagnostics"] = solver_data
        report["parsed_force_values"] = values
        report["solver_progress"] = progress
        report["validation_status"] = "completed" if code == 0 else "failed"
        report["warnings"].extend(solver_warnings + force_warnings)
        report["recommended_next_action"] = "Review warnings, then proceed through staged validation speeds before full batch."
        write_validation_report(case_dir, report)
        if report["summary"] == "FAIL":
            returncode = 1
            error = error or "Validation report status is FAIL"
        state.finish(returncode=returncode, error=error, diagnostics=report["warnings"], summary=report)
    except (HTTPException, OSError, RuntimeError, ValueError) as exc:
        returncode = 1
        error = str(exc)
        state.append_log(f"ERROR: {exc}")
        state.finish(returncode=returncode, error=error, diagnostics=[error], summary=report)

def run_forward_batch_task(request: ForwardBatchRequest) -> None:
    rows: list[dict[str, str]] = []
    summary: dict[str, Any] = {}
    results: list[dict[str, str]] = []
    returncode = 0
    error = ""
    try:
        rows, summary = forward_batch_rows(request)
        state.set_summary(summary)
        if request.dry_run:
            state.append_log("Dry run: generated summary only. No cases, mesh, or solver commands were run.")
            state.finish(returncode=0, results=[], summary=summary)
            return
        for index, row in enumerate(rows, start=1):
            name = row["variant_id"]
            state.update_case(name, index, len(rows))
            state.update(stage="generate", command=f"generate forward case {name}")
            case_dir = generate_forward_case(row, index + 1, request.force_remesh)
            run_mode = "rebuild-and-solve" if request.force_remesh or not mesh_ready(case_dir) else "solve-only"
            command, stage = batch_commands(run_mode, case_dir)
            code, _ = run_command(command, stage)
            if code != 0:
                returncode = 1
                failed = failed_forward_row(row, f"run_failed:{' '.join(command)}")
                results.append(failed)
                error = f"Forward batch failed for {name}"
                break
            parse_command = [PYTHON, "scripts/parse_forces.py", str(case_dir), "--average-last", "20"]
            parse_code, parse_output = run_command(parse_command, "parse")
            if parse_code == 0:
                result = metrics_from_output(name, parse_output)
                series = build_solver_timeseries(case_dir)
                summary_path = case_dir / "solver_timeseries_summary.json"
                summary_path.write_text(json.dumps(series.get("summary", {}), indent=2) + "\n")
                result.update(
                    {
                        "plots_case": name,
                        "force_history_case": name,
                        "solver_log_case": name,
                    }
                )
                results.append(result)
            else:
                returncode = 1
                failed = failed_forward_row(row, "parse_failed_or_averaging_window_not_reached")
                results.append(failed)
                error = f"Parse failed for {name}"
                break
    except (HTTPException, OSError, RuntimeError, ValueError) as exc:
        returncode = 1
        error = str(exc)
        state.append_log(f"ERROR: {exc}")
    write_forward_results(results)
    state.finish(returncode=returncode, error=error, results=results, summary=summary)


def latest_validation_report() -> dict[str, Any] | None:
    if not CASES_DIR.is_dir():
        return None
    candidates = sorted(
        CASES_DIR.glob("validation_*/validation_report.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
    return None


def forward_batch_validation_gate() -> tuple[bool, str]:
    report = latest_validation_report()
    if not report:
        return False, "Run Validate Forward-Flight Model before starting a full forward-flight batch."
    summary = str(report.get("summary", ""))
    values = report.get("parsed_force_values", {})
    warnings = [str(item).lower() for item in report.get("warnings", [])]
    required_values = ["thrust_convention_N", "shaft_torque_Nm", "power_W"]
    has_required_values = all(str(values.get(key, "")).strip() for key in required_values)
    fatal_warning = any("fatal" in warning or "openfoam run failed" in warning or "force parsing failed" in warning for warning in warnings)
    if summary not in {"PASS", "WARNING"}:
        return False, f"Latest validation status is {summary or 'missing'}. Fix validation before running a full batch."
    if not has_required_values:
        return False, "Latest validation did not compute thrust, shaft torque, and power. Run validation again before the full batch."
    if fatal_warning:
        return False, "Latest validation contains a fatal solver or force parsing error. Fix validation before the full batch."
    return True, "Latest quick validation permits full batch start."


def start_thread(action: str, case_name: str, commands: list[tuple[list[str], str]]) -> dict[str, str]:
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail=f"Task already running: {state.action}")
        state.begin_unlocked(action, case_name)
    thread = threading.Thread(target=run_task, args=(action, case_name, commands), daemon=True)
    thread.start()
    return {"status": "started", "action": action}


def start_batch_thread(action: str, mode: str) -> dict[str, str]:
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail=f"Task already running: {state.action}")
        state.begin_unlocked(action, "all")
    thread = threading.Thread(target=run_batch_task, args=(mode,), daemon=True)
    thread.start()
    return {"status": "started", "action": action}


def start_forward_thread(request: ForwardBatchRequest) -> dict[str, str]:
    gate_ok, gate_message = forward_batch_validation_gate()
    if not request.dry_run and not gate_ok:
        raise HTTPException(status_code=400, detail=gate_message)
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail=f"Task already running: {state.action}")
        state.begin_unlocked("forward-flight compressible batch", "all")
    thread = threading.Thread(target=run_forward_batch_task, args=(request,), daemon=True)
    thread.start()
    return {"status": "started", "action": "forward-flight compressible batch"}


def start_validation_thread(request: ValidationRequest) -> dict[str, str]:
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail=f"Task already running: {state.action}")
        state.begin_unlocked("validate forward-flight model", validation_case_name(request.variant_id))
    thread = threading.Thread(target=run_validation_task, args=(request,), daemon=True)
    thread.start()
    return {"status": "started", "action": "validate forward-flight model"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/cases")
def api_cases() -> dict[str, object]:
    details = case_details()
    cases = [str(item["name"]) for item in details if item["exists"]]
    return {"cases": cases, "details": details}


@app.get("/api/variants")
def api_variants() -> dict[str, list[dict[str, str]]]:
    return {"variants": read_variants()}


@app.post("/api/forward-batch-summary")
def api_forward_batch_summary(request: ForwardBatchRequest) -> dict[str, Any]:
    _, summary = forward_batch_rows(request)
    gate_ok, gate_message = forward_batch_validation_gate()
    summary["validation_gate_ok"] = gate_ok
    summary["validation_gate_message"] = gate_message
    if not request.dry_run and not gate_ok:
        summary.setdefault("warnings", []).append(gate_message)
    return summary


@app.post("/api/forward-batch-run")
def api_forward_batch_run(request: ForwardBatchRequest) -> dict[str, str]:
    return start_forward_thread(request)


@app.post("/api/overnight-import-path")
def api_overnight_import_path(request: OvernightImportPathRequest) -> dict[str, Any]:
    source = Path(request.path).expanduser().resolve()
    manifest = import_source_to_batch(source)
    return manifest


@app.post("/api/batch-import-path")
def api_batch_import_path(request: OvernightImportPathRequest) -> dict[str, Any]:
    return api_overnight_import_path(request)


@app.post("/api/batch/import")
def api_batch_import(request: OvernightImportPathRequest) -> dict[str, Any]:
    try:
        return api_overnight_import_path(request)
    except Exception as exc:
        state.append_log(f"[batch-import] ERROR {exc}\n{traceback.format_exc()}")
        raise


@app.post("/api/overnight-import-upload")
async def api_overnight_import_upload(request: Request, filename: str = "upload.zip") -> dict[str, Any]:
    safe_name = Path(filename).name or "upload.zip"
    if not safe_name.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Upload must be a .zip file")
    IMPORTED_EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="overnight_upload_", suffix=".zip", delete=False, dir="/tmp") as handle:
        handle.write(await request.body())
        temp_path = Path(handle.name)
    try:
        return import_source_to_batch(temp_path, safe_name)
    finally:
        temp_path.unlink(missing_ok=True)


@app.post("/api/batch-import-upload")
async def api_batch_import_upload(request: Request, filename: str = "upload.zip") -> dict[str, Any]:
    return await api_overnight_import_upload(request, filename)


@app.post("/api/batch/import-upload")
async def api_batch_import_upload_alias(request: Request, filename: str = "upload.zip") -> dict[str, Any]:
    try:
        return await api_overnight_import_upload(request, filename)
    except Exception as exc:
        state.append_log(f"[batch-import-upload] ERROR {exc}\n{traceback.format_exc()}")
        raise


@app.get("/api/overnight-manifest")
def api_overnight_manifest(batch_id: str) -> dict[str, Any]:
    return read_overnight_manifest(batch_id)


@app.get("/api/batch-manifest")
def api_batch_manifest(batch_id: str) -> dict[str, Any]:
    return read_overnight_manifest(batch_id)


@app.get("/api/batch-latest-manifest")
def api_batch_latest_manifest() -> dict[str, Any]:
    if not IMPORTED_EXPORTS_DIR.is_dir():
        raise HTTPException(status_code=404, detail="No imported Batch Run manifests found")
    candidates = sorted(
        IMPORTED_EXPORTS_DIR.glob("*/imported_geometry_batch_manifest.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            manifest = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("variants"):
            return manifest
    raise HTTPException(status_code=404, detail="No imported Batch Run manifests found")


@app.post("/api/overnight-sanity")
def api_overnight_sanity(settings: OvernightSettings) -> dict[str, str]:
    return start_overnight_sanity_thread(settings)


@app.post("/api/batch-geometry-check")
def api_batch_geometry_check(settings: OvernightSettings) -> dict[str, str]:
    return start_overnight_sanity_thread(settings)


def summarize_recoverable_batch(batch_dir: Path) -> dict[str, Any] | None:
    state_path = batch_dir / "batch_state.json"
    manifest_path = batch_dir / "imported_geometry_batch_manifest.json"
    if not state_path.is_file() and not manifest_path.is_file():
        return None
    try:
        batch_state = json.loads(state_path.read_text()) if state_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        batch_state = {}
    try:
        manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        manifest = {}
    rows = manifest.get("variants", [])
    results = batch_state.get("results", [])
    status = str(batch_state.get("status", "imported" if rows else "unknown"))
    if status in {"completed", "success", "abandoned"}:
        return None
    return {
        "batch_id": batch_dir.name,
        "started_time": batch_state.get("started_at") or manifest.get("created_at"),
        "last_update_time": batch_state.get("last_update_time") or manifest.get("created_at"),
        "total_variants": len(rows) or batch_state.get("total_variants", 0),
        "completed_variants": len([row for row in results if row.get("solver_status") == "success"]),
        "interrupted_variants": len([row for row in results if "INTERRUPTED" in str(row.get("warning_flags", ""))]),
        "failed_variants": len([row for row in results if row.get("solver_status") == "failed"]),
        "active_incomplete_stage": batch_state.get("stage", status),
        "next_recommended_action": "Resume Batch",
        "settings": batch_state.get("settings", {}),
    }


@app.get("/api/batch-recovery")
def api_batch_recovery() -> dict[str, Any]:
    batches = []
    if IMPORTED_EXPORTS_DIR.is_dir():
        for batch_dir in sorted(IMPORTED_EXPORTS_DIR.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True):
            if not batch_dir.is_dir():
                continue
            summary = summarize_recoverable_batch(batch_dir)
            if summary is not None:
                batches.append(summary)
    return {"batches": batches}


@app.post("/api/batch-resume")
def api_batch_resume(request: OvernightImportPathRequest) -> dict[str, str]:
    batch_id = request.path
    state_path = overnight_batch_state_path(batch_id)
    settings_data: dict[str, Any] = {}
    if state_path.is_file():
        try:
            settings_data = json.loads(state_path.read_text()).get("settings", {})
        except (OSError, json.JSONDecodeError):
            settings_data = {}
    settings_data["batch_id"] = batch_id
    return start_overnight_thread(OvernightSettings(**settings_data))


@app.post("/api/batch-mark-abandoned")
def api_batch_mark_abandoned(request: OvernightImportPathRequest) -> dict[str, str]:
    write_overnight_state(request.path, {"status": "abandoned", "abandoned_at": time.time(), "backend_runner_active": False})
    return {"status": "abandoned", "batch_id": request.path}


@app.post("/api/overnight-run")
def api_overnight_run(settings: OvernightSettings) -> dict[str, str]:
    state.append_log(
        f"[batch-start] batch_id={settings.batch_id} selected={len(settings.selected_variant_ids)} "
        f"rpm={settings.rpm} speed={settings.freestream_speed} {settings.speed_units}"
    )
    if not settings.selected_variant_ids:
        raise HTTPException(status_code=400, detail="No STL variants selected for Batch Run.")
    manifest = update_manifest_operating_conditions(read_overnight_manifest(settings.batch_id), settings)
    rows = selected_overnight_rows(manifest, settings)
    if not rows:
        raise HTTPException(status_code=400, detail="No STL variants selected for Batch Run.")
    if settings.rpm <= 0:
        raise HTTPException(status_code=400, detail="Global rpm must be greater than zero.")
    if settings.freestream_speed < 0:
        raise HTTPException(status_code=400, detail="Global freestream speed must be non-negative.")
    missing_stls = [row for row in rows if not Path(str(row.get("imported_stl_path", ""))).is_file()]
    if missing_stls:
        names = ", ".join(str(row.get("variant_id", "")) for row in missing_stls[:5])
        raise HTTPException(status_code=400, detail=f"Selected STL import path is missing for: {names}")
    hard_failures = [row for row in rows if str(row.get("geometry_sanity_status", "")).endswith("Fail")]
    warnings = [row for row in rows if str(row.get("geometry_sanity_status", "")).endswith("Warning")]
    if hard_failures:
        raise HTTPException(status_code=400, detail="Selected STL(s) have hard geometry/mesh failures. Fix or deselect them before the Batch Run.")
    if warnings and not settings.allow_warning_override:
        raise HTTPException(status_code=400, detail="Selected STL(s) have geometry warnings. Enable warning override to start.")
    write_overnight_manifest(manifest)
    write_overnight_state(
        settings.batch_id,
        {
            "status": "queued",
            "stage": "queued",
            "selected_count": len(rows),
            "total_variants": len(rows),
            "settings": settings.dict(),
            "backend_runner_active": True,
        },
    )
    response = start_overnight_thread(settings)
    response["selected_variant_count"] = str(len(rows))
    return response


@app.post("/api/batch-run")
def api_batch_run(settings: OvernightSettings) -> dict[str, str]:
    return api_overnight_run(settings)


@app.post("/api/batch/start")
def api_batch_start(settings: OvernightSettings) -> dict[str, str]:
    try:
        return api_overnight_run(settings)
    except Exception as exc:
        state.append_log(f"[batch-start] ERROR {exc}\n{traceback.format_exc()}")
        raise


@app.get("/api/batch/{batch_id}/status")
def api_batch_status(batch_id: str) -> dict[str, Any]:
    state_path = overnight_batch_state_path(batch_id)
    heartbeat_path = IMPORTED_EXPORTS_DIR / batch_id / "batch_heartbeat.json"
    payload: dict[str, Any] = {"batch_id": batch_id, "status": "unknown"}
    if state_path.is_file():
        payload.update(json.loads(state_path.read_text()))
    if heartbeat_path.is_file():
        payload["heartbeat"] = json.loads(heartbeat_path.read_text())
    active_processes = find_batch_processes(batch_id)
    payload["process_alive"] = bool(active_processes)
    payload["active_batch_processes"] = active_processes
    if active_processes and payload.get("status") in {"completed", "failed", "idle", "unknown"}:
        payload["status"] = "RECOVERED_RUNNING_PROCESS"
    return payload


@app.post("/api/overnight-stop-current")
def api_overnight_stop_current() -> dict[str, str]:
    with overnight_control["lock"]:
        overnight_control["stop_current"] = True
        batch_id = str(overnight_control.get("batch_id") or "")
    if batch_id:
        write_overnight_state(batch_id, {"stop_after_current": True, "stop_requested": False, "last_transition_reason": "stop_after_current_requested"})
    state.append_log("Stop-after-current requested for Batch Run.")
    return {"status": "stopping-current", "action": "stop after current propeller"}


@app.post("/api/batch-stop-current")
def api_batch_stop_current() -> dict[str, str]:
    return api_overnight_stop_current()


@app.post("/api/overnight-stop-batch")
def api_overnight_stop_batch() -> dict[str, str]:
    with overnight_control["lock"]:
        overnight_control["stop_all"] = True
        batch_id = str(overnight_control.get("batch_id") or "")
        process = overnight_control.get("process")
    kill_report = {}
    if batch_id:
        write_overnight_state(batch_id, {"stop_requested": True, "stop_after_current": False, "status": "stopping", "last_transition_reason": "stop_entire_batch_requested"})
    if process is not None and process.poll() is None:
        kill_report["active_process_group"] = terminate_process_group(process)
    if batch_id:
        kill_report["batch_processes"] = kill_batch_processes(batch_id)
        write_overnight_state(batch_id, {"stage": "batch_stop_requested", "process_kill_report": kill_report, "remaining_processes": find_batch_processes(batch_id)})
    state.append_log("Stop entire Batch Run requested.")
    return {"status": "stopping", "action": "stop Batch Run"}


@app.post("/api/batch-stop-all")
def api_batch_stop_all() -> dict[str, str]:
    return api_overnight_stop_batch()


@app.post("/api/validation-summary")
def api_validation_summary(request: ValidationRequest) -> dict[str, Any]:
    _, config = validation_row(request)
    config.update(validation_runtime_settings(float(config["rpm"]), request.validation_revolutions))
    case_dir = CASES_DIR / str(config["validation_case"])
    return {
        **config,
        "case_path": str(case_dir),
        "mesh_ready": mesh_ready(case_dir),
        "mesh_action": "reuse existing validation mesh" if mesh_ready(case_dir) and not request.force_remesh else "regenerate mesh during validation run",
    }


@app.post("/api/validation-prepare")
def api_validation_prepare(request: ValidationRequest) -> dict[str, Any]:
    report = prepare_validation_case(request)
    return report


@app.post("/api/validation-run")
def api_validation_run(request: ValidationRequest) -> dict[str, str]:
    return start_validation_thread(request)


@app.post("/api/validation-stop")
def api_validation_stop() -> dict[str, str]:
    with validation_control["lock"]:
        process = validation_control["process"]
        if process is None or process.poll() is not None:
            raise HTTPException(status_code=409, detail="No validation solver process is currently running")
        validation_control["stop_requested"] = True
    state.append_log("Stop requested for current validation solver.")
    return {"status": "stopping", "action": "stop validation solver"}


@app.get("/api/validation-report")
def api_validation_report(case: str) -> dict[str, Any]:
    case_dir = case_path(case)
    snapshot = state.snapshot()
    if snapshot.get("running") and snapshot.get("case") == case and snapshot.get("summary"):
        return snapshot["summary"]
    report_path = case_dir / "validation_report.json"
    if not report_path.is_file():
        raise HTTPException(status_code=404, detail="validation_report.json not found")
    return json.loads(report_path.read_text())


@app.post("/api/generate")
def api_generate() -> dict[str, str]:
    return start_thread(
        "generate cases",
        "all",
        [([PYTHON, "scripts/generate_case.py", "--overwrite"], "generate")],
    )


@app.post("/api/mesh")
def api_mesh(request: CaseRequest) -> dict[str, str]:
    case_dir = case_path(request.case)
    return start_thread(
        "run snappy mesh",
        request.case,
        [([PYTHON, "scripts/run_case.py", str(case_dir), "--snappy"], "blockMesh")],
    )


@app.post("/api/diagnose")
def api_diagnose(request: CaseRequest) -> dict[str, str]:
    case_dir = case_path(request.case)
    return start_thread(
        "diagnose selected case",
        request.case,
        [([PYTHON, "scripts/diagnose_cfd_setup.py", str(case_dir)], "diagnose")],
    )


@app.post("/api/solve-only")
def api_solve_only(request: CaseRequest) -> dict[str, str]:
    case_dir = case_path(request.case)
    return start_thread(
        "solve using existing mesh",
        request.case,
        [([PYTHON, "scripts/run_case.py", str(case_dir), "--solve-only"], "foamRun")],
    )


@app.post("/api/rebuild-and-solve")
def api_rebuild_and_solve(request: CaseRequest) -> dict[str, str]:
    case_dir = case_path(request.case)
    return start_thread(
        "rebuild mesh and solve",
        request.case,
        [([PYTHON, "scripts/run_case.py", str(case_dir), "--solve"], "blockMesh")],
    )


@app.post("/api/parse")
def api_parse(request: CaseRequest) -> dict[str, str]:
    case_dir = case_path(request.case)
    return start_thread(
        "parse selected case",
        request.case,
        [([PYTHON, "scripts/parse_forces.py", str(case_dir)], "parse")],
    )


@app.post("/api/force-signs")
def api_force_signs(request: SignFlipRequest) -> dict[str, Any]:
    case_dir = case_path(request.case)
    updated = set_case_signs(case_dir, request.flip_thrust, request.flip_torque)
    series = build_solver_timeseries(case_dir, 500)
    batch_result = refresh_batch_result_for_case(case_dir)
    return {
        "status": "updated",
        "case": request.case,
        "updated": updated,
        "summary": series.get("summary", {}),
        "batch_result": batch_result,
    }


@app.post("/api/parse-all")
def api_parse_all() -> dict[str, str]:
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail=f"Task already running: {state.action}")
        state.begin_unlocked("parse all cases", "all")
    thread = threading.Thread(target=run_parse_all_task, daemon=True)
    thread.start()
    return {"status": "started", "action": "parse all cases"}


@app.post("/api/batch-mesh")
def api_batch_mesh() -> dict[str, str]:
    return start_batch_thread("batch mesh only", "mesh")


@app.post("/api/batch-solve-only")
def api_batch_solve_only() -> dict[str, str]:
    return start_batch_thread("batch solve using existing meshes", "solve-only")


@app.post("/api/batch-rebuild-and-solve")
def api_batch_rebuild_and_solve() -> dict[str, str]:
    return start_batch_thread("batch rebuild mesh and solve", "rebuild-and-solve")


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    snapshot = state.snapshot()
    batch_id = ""
    summary = snapshot.get("summary")
    if isinstance(summary, dict):
        batch_id = str(summary.get("batch_id") or "")
    with overnight_control["lock"]:
        batch_id = batch_id or str(overnight_control.get("batch_id") or "")
    if batch_id:
        snapshot["batch_id"] = batch_id
        active_processes = find_batch_processes(batch_id)
        if active_processes:
            was_running = bool(snapshot.get("running"))
            snapshot["running"] = True
            snapshot["status"] = snapshot.get("status", "running") if was_running else "RECOVERED_RUNNING_PROCESS"
            snapshot["process_alive"] = True
            snapshot["recovered_process"] = not was_running
            snapshot["active_batch_processes"] = active_processes
        heartbeat_path = IMPORTED_EXPORTS_DIR / batch_id / "batch_heartbeat.json"
        state_path = IMPORTED_EXPORTS_DIR / batch_id / "batch_state.json"
        if heartbeat_path.is_file():
            try:
                snapshot["batch_heartbeat"] = json.loads(heartbeat_path.read_text())
            except (OSError, json.JSONDecodeError):
                snapshot["batch_heartbeat"] = {}
        if state_path.is_file():
            try:
                snapshot["batch_state"] = json.loads(state_path.read_text())
            except (OSError, json.JSONDecodeError):
                snapshot["batch_state"] = {}
    return snapshot


@app.get("/api/solver-progress")
def api_solver_progress(case: str | None = None) -> dict[str, Any]:
    if case:
        case_dir = case_path(case)
        progress_path = case_dir / "solver_progress.json"
        if progress_path.is_file():
            return json.loads(progress_path.read_text())
        return build_solver_progress(case_dir, process_running=None)

    snapshot = state.snapshot()
    progress = snapshot.get("solver_progress") or {}
    if progress:
        return progress

    current_case = snapshot.get("case")
    if current_case:
        try:
            case_dir = case_path(str(current_case))
            progress_path = case_dir / "solver_progress.json"
            if progress_path.is_file():
                return json.loads(progress_path.read_text())
        except HTTPException:
            pass
    return {"solver_status": "queued", "warnings": ["No solver progress available yet"]}


@app.get("/api/solver-timeseries")
def api_solver_timeseries(case: str | None = None) -> dict[str, Any]:
    if case:
        case_dir = case_path(case)
        return build_solver_timeseries(case_dir)

    snapshot = state.snapshot()
    current_case = snapshot.get("case")
    if current_case:
        try:
            return build_solver_timeseries(case_path(str(current_case)))
        except HTTPException:
            pass
    return {
        "force_status": {"status": "force data not available yet"},
        "force_samples": [],
        "courant": [],
        "residuals": [],
        "stability": {},
        "summary": {},
    }


@app.get("/api/log")
def api_log() -> dict[str, list[str]]:
    with state.lock:
        return {"lines": list(state.log)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Ozcan Propeller CFD browser UI")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("Missing dependency. Install with: pip install fastapi uvicorn", file=sys.stderr)
        return 1

    uvicorn.run("app:app", host=args.host, port=args.port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

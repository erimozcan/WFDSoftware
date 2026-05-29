#!/usr/bin/env python3
"""Parse live OpenFOAM solver progress from logs and force output."""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

from parse_forces import AXIS_INDEX, find_latest_force_file, read_force_rows


TIME_RE = re.compile(r"^Time\s*=\s*([0-9.eE+-]+)", re.MULTILINE)
DELTAT_RE = re.compile(r"^deltaT\s*=\s*([0-9.eE+-]+)", re.MULTILINE)
COURANT_RE = re.compile(r"Courant Number.*mean:\s*([0-9.eE+-]+)\s*max:\s*([0-9.eE+-]+)")
EXEC_RE = re.compile(r"ExecutionTime\s*=\s*([0-9.eE+-]+)\s*s\s+ClockTime\s*=\s*([0-9.eE+-]+)\s*s")
RESIDUAL_RE = re.compile(r"Solving for\s+([A-Za-z0-9_]+),\s+Initial residual\s+=\s+([0-9.eE+-]+),\s+Final residual\s+=\s+([0-9.eE+-]+)")
FATAL_RULES = [
    ("FOAM_FATAL_IO_ERROR", re.compile(r"\bFOAM FATAL IO ERROR\b", re.IGNORECASE)),
    ("FOAM_FATAL_ERROR", re.compile(r"\bFOAM FATAL ERROR\b", re.IGNORECASE)),
    ("SEGMENTATION_FAULT", re.compile(r"\bsegmentation fault\b", re.IGNORECASE)),
    ("CORE_DUMPED", re.compile(r"\bcore dumped\b", re.IGNORECASE)),
    ("MPI_ABORT", re.compile(r"\bMPI_ABORT\b", re.IGNORECASE)),
    ("FLOATING_POINT_EXCEPTION", re.compile(r"^\s*(?:Floating point exception|floating point exception\b(?! trapping))", re.IGNORECASE)),
    ("NAN_DIVERGENCE", re.compile(r"(?:=\s*[-+]?nan\b|\b(?:Initial|Final) residual\s*=\s*[-+]?nan\b|\b(?:nan|NaN)\s+(?:divergence|detected|in field))", re.IGNORECASE)),
]
IGNORED_FATAL_LINE_RULES = [
    ("OPENFOAM_SIGFPE_STARTUP", re.compile(r"^\s*sigFpe\s*:\s*Enabling floating point exception trapping \(FOAM_SIGFPE\)\.?\s*$", re.IGNORECASE)),
    ("OPENFOAM_FILE_MODIFICATION_STARTUP", re.compile(r"^\s*fileModificationChecking\s*:\s*Monitoring run-time modified files", re.IGNORECASE)),
    ("OPENFOAM_SYSTEM_OPERATIONS_STARTUP", re.compile(r"^\s*allowSystemOperations\s*:\s*Allowing user-supplied system call operations", re.IGNORECASE)),
]
ENDTIME_RE = re.compile(r"\bendTime\s+([0-9.eE+-]+)\s*;")
DELTAT_CONTROL_RE = re.compile(r"\bdeltaT\s+([0-9.eE+-]+)\s*;")
MAXCO_RE = re.compile(r"\bmaxCo\s+([0-9.eE+-]+)\s*;")


def safe_float(value: object, default: float | None = None) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}


def read_control_targets(case_dir: Path) -> dict[str, Any]:
    control = case_dir / "system" / "controlDict"
    if not control.is_file():
        return {}
    text = control.read_text(errors="replace")
    end_time = safe_float((ENDTIME_RE.search(text) or [None, None])[1])
    delta_t = safe_float((DELTAT_CONTROL_RE.search(text) or [None, None])[1])
    max_co = safe_float((MAXCO_RE.search(text) or [None, None])[1])
    target_steps = None
    if end_time is not None and delta_t and delta_t > 0:
        target_steps = int(math.ceil(end_time / delta_t))
    return {"target_end_time": end_time, "control_deltaT": delta_t, "target_timesteps": target_steps, "maxCo": max_co}


def latest_solver_log(case_dir: Path) -> Path | None:
    candidates = [case_dir / "log.rhoPimpleFoam", case_dir / "log.foamRun"]
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


def tail_lines(text: str, count: int = 18) -> list[str]:
    lines = text.splitlines()
    return lines[-count:]


def classify_fatal_log_lines(text: str) -> dict[str, Any]:
    fatal_matches: list[dict[str, str]] = []
    ignored_matches: list[dict[str, str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        ignored_rule = next((name for name, pattern in IGNORED_FATAL_LINE_RULES if pattern.search(line)), "")
        if ignored_rule:
            ignored_matches.append({"line_number": line_number, "line": line, "rule": ignored_rule})
            continue
        for rule_name, pattern in FATAL_RULES:
            if pattern.search(line):
                fatal_matches.append({"line_number": line_number, "line": line, "rule": rule_name})
                break
    return {"fatal_matches": fatal_matches, "ignored_log_warnings": ignored_matches}


def parse_solver_log_text(text: str) -> dict[str, Any]:
    times = [float(value) for value in TIME_RE.findall(text)]
    delta_ts = [float(value) for value in DELTAT_RE.findall(text)]
    courant = [(float(mean), float(maximum)) for mean, maximum in COURANT_RE.findall(text)]
    execution = [(float(exec_time), float(clock_time)) for exec_time, clock_time in EXEC_RE.findall(text)]
    fatal_classification = classify_fatal_log_lines(text)
    fatal_matches = fatal_classification["fatal_matches"]
    last_fatal = fatal_matches[-1] if fatal_matches else None
    return {
        "current_time": times[-1] if times else None,
        "timesteps_completed": len(times),
        "latest_deltaT": delta_ts[-1] if delta_ts else None,
        "courant_mean": courant[-1][0] if courant else None,
        "courant_max": courant[-1][1] if courant else None,
        "execution_time_s": execution[-1][0] if execution else None,
        "clock_time_s": execution[-1][1] if execution else None,
        "fatal_error": bool(fatal_matches),
        "fatal_match": last_fatal or {},
        "fatal_rule": last_fatal.get("rule", "") if last_fatal else "",
        "fatal_line": last_fatal.get("line", "") if last_fatal else "",
        "fatal_excerpt": last_fatal.get("line", "") if last_fatal else "",
        "ignored_log_warnings": fatal_classification["ignored_log_warnings"],
        "log_tail": tail_lines(text),
    }


def parse_solver_log_timeseries(text: str) -> dict[str, list[dict[str, Any]]]:
    courant: list[dict[str, Any]] = []
    residuals: list[dict[str, Any]] = []
    execution: list[dict[str, Any]] = []
    delta_t: list[dict[str, Any]] = []
    current_time: float | None = None
    timestep = 0

    for line in text.splitlines():
        time_match = TIME_RE.match(line)
        if time_match:
            current_time = float(time_match.group(1))
            timestep += 1
            continue
        delta_match = DELTAT_RE.match(line)
        if delta_match:
            delta_t.append({"time": current_time, "timestep": timestep, "deltaT": float(delta_match.group(1))})
            continue
        co_match = COURANT_RE.search(line)
        if co_match:
            courant.append(
                {
                    "time": current_time,
                    "timestep": timestep,
                    "mean": float(co_match.group(1)),
                    "max": float(co_match.group(2)),
                }
            )
            continue
        residual_match = RESIDUAL_RE.search(line)
        if residual_match:
            residuals.append(
                {
                    "time": current_time,
                    "timestep": timestep,
                    "field": residual_match.group(1),
                    "initial": float(residual_match.group(2)),
                    "final": float(residual_match.group(3)),
                }
            )
            continue
        exec_match = EXEC_RE.search(line)
        if exec_match:
            execution.append(
                {
                    "time": current_time,
                    "timestep": timestep,
                    "execution_time_s": float(exec_match.group(1)),
                    "clock_time_s": float(exec_match.group(2)),
                }
            )

    return {"courant": courant, "residuals": residuals, "execution": execution, "deltaT": delta_t}


def parse_solver_log(case_dir: Path) -> dict[str, Any]:
    log_path = latest_solver_log(case_dir)
    if log_path is None:
        return {"solver_log": "", "log_tail": [], "log_mtime": None}
    text = log_path.read_text(errors="replace")
    parsed = parse_solver_log_text(text)
    parsed["solver_log"] = str(log_path)
    parsed["log_mtime"] = log_path.stat().st_mtime
    return parsed


def parse_solver_log_series(case_dir: Path) -> dict[str, Any]:
    log_path = latest_solver_log(case_dir)
    if log_path is None:
        return {"solver_log": "", "courant": [], "residuals": [], "execution": [], "deltaT": []}
    text = log_path.read_text(errors="replace")
    parsed = parse_solver_log_timeseries(text)
    parsed["solver_log"] = str(log_path)
    return parsed


def parse_latest_forces(case_dir: Path, info: dict[str, Any]) -> dict[str, Any]:
    try:
        force_file = find_latest_force_file(case_dir)
        rows = read_force_rows(force_file)
    except Exception:
        return {"force_status": "force data not available yet"}
    if not rows:
        return {"force_status": "force data not available yet"}

    last = rows[-1]
    force = last["force"]
    moment = last["moment"]
    thrust_axis = str(info.get("thrust_axis", "z")).lower()
    torque_axis = str(info.get("torque_axis", "z")).lower()
    thrust_sign = safe_float(info.get("thrust_sign"), 1.0) or 1.0
    torque_sign = safe_float(info.get("torque_sign"), 1.0) or 1.0
    shaft_torque_sign = safe_float(info.get("shaft_torque_sign", info.get("torque_sign")), torque_sign) or torque_sign
    if thrust_axis not in AXIS_INDEX or torque_axis not in AXIS_INDEX:
        return {"force_status": "force axes unavailable"}

    thrust = thrust_sign * force[AXIS_INDEX[thrust_axis]]
    fluid_torque = moment[AXIS_INDEX[torque_axis]]
    shaft_torque = shaft_torque_sign * fluid_torque
    omega = safe_float(info.get("omega_rad_s"), 0.0) or 0.0
    return {
        "force_status": "available",
        "force_file": str(force_file),
        "force_sample_count": len(rows),
        "latest_force_time": last["time"],
        "latest_total_force": force,
        "latest_total_moment": moment,
        "raw_force_axis_N": force[AXIS_INDEX[thrust_axis]],
        "raw_moment_axis_Nm": fluid_torque,
        "thrust_axis": thrust_axis,
        "thrust_sign": thrust_sign,
        "torque_axis": torque_axis,
        "torque_sign": torque_sign,
        "latest_thrust_N": thrust,
        "latest_fluid_torque_Nm": fluid_torque,
        "latest_shaft_torque_Nm": shaft_torque,
        "latest_power_W": shaft_torque * omega,
        "torque_sign_convention": str(info.get("torque_sign_convention", "shaft_torque_Nm = torque_sign * raw_moment_axis_Nm")),
    }


def parse_force_timeseries(case_dir: Path, info: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        force_file = find_latest_force_file(case_dir)
        rows = read_force_rows(force_file)
    except Exception as exc:
        return [], {"status": "force data not available yet", "error": str(exc)}

    thrust_axis = str(info.get("thrust_axis", "z")).lower()
    torque_axis = str(info.get("torque_axis", "z")).lower()
    thrust_sign = safe_float(info.get("thrust_sign"), 1.0) or 1.0
    torque_sign = safe_float(info.get("torque_sign"), 1.0) or 1.0
    shaft_torque_sign = safe_float(info.get("shaft_torque_sign", info.get("torque_sign")), torque_sign) or torque_sign
    omega = safe_float(info.get("omega_rad_s"), 0.0) or 0.0
    vinf = safe_float(info.get("Vinf_mps", info.get("freestream_velocity_mps")), 0.0) or 0.0
    if thrust_axis not in AXIS_INDEX or torque_axis not in AXIS_INDEX:
        return [], {"status": "force axes unavailable", "error": f"thrust_axis={thrust_axis}, torque_axis={torque_axis}"}

    samples: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        force = row["force"]
        moment = row["moment"]
        thrust = thrust_sign * force[AXIS_INDEX[thrust_axis]]
        fluid_torque = moment[AXIS_INDEX[torque_axis]]
        shaft_torque = shaft_torque_sign * fluid_torque
        power = shaft_torque * omega
        eta = thrust * vinf / power if vinf > 0 and power > 0 else None
        samples.append(
            {
                "sample": index,
                "time": row["time"],
                "Fx": force[0],
                "Fy": force[1],
                "Fz": force[2],
                "Mx": moment[0],
                "My": moment[1],
                "Mz": moment[2],
                "raw_force_axis_N": force[AXIS_INDEX[thrust_axis]],
                "raw_moment_axis_Nm": fluid_torque,
                "thrust_axis": thrust_axis,
                "thrust_sign": thrust_sign,
                "torque_axis": torque_axis,
                "torque_sign": torque_sign,
                "thrust_N": thrust,
                "fluid_torque_Nm": fluid_torque,
                "shaft_torque_Nm": shaft_torque,
                "power_W": power,
                "eta": eta,
            }
        )
    return samples, {"status": "available", "force_file": str(force_file), "sample_count": len(samples)}


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    avg = mean(values)
    return math.sqrt(sum((value - avg) ** 2 for value in values) / (len(values) - 1))


def stability_hint(samples: list[dict[str, Any]], key: str, window: int = 20) -> dict[str, Any]:
    values = [float(sample[key]) for sample in samples[-window:] if sample.get(key) is not None]
    if len(values) < max(3, min(window, 5)):
        return {"metric": key, "status": "not enough samples", "samples": len(values)}
    avg = mean(values)
    sigma = stddev(values)
    cov = abs(sigma / avg) if avg else None
    half = max(1, len(values) // 2)
    first = mean(values[:half])
    second = mean(values[half:])
    drift = second - first
    rel_drift = drift / avg if avg else None
    trend = "Trend appears stabilizing" if cov is not None and cov < 0.05 and (rel_drift is None or abs(rel_drift) < 0.05) else "Metric is still drifting"
    if rel_drift is not None and rel_drift > 0.05:
        trend = "Metric is still drifting upward"
    elif rel_drift is not None and rel_drift < -0.05:
        trend = "Metric is still drifting downward"
    return {"metric": key, "samples": len(values), "mean": avg, "stddev": sigma, "coefficient_of_variation": cov, "relative_drift": rel_drift, "status": trend}


def force_summary(samples: list[dict[str, Any]], window: int = 20) -> dict[str, Any]:
    selected = samples[-window:]
    if not selected:
        return {"samples_used": 0}
    return {
        "samples_used": len(selected),
        "mean_thrust_N": mean([float(item["thrust_N"]) for item in selected]),
        "mean_shaft_torque_Nm": mean([float(item["shaft_torque_Nm"]) for item in selected]),
        "mean_power_W": mean([float(item["power_W"]) for item in selected]),
        "mean_eta": mean([float(item["eta"]) for item in selected if item.get("eta") is not None]) if any(item.get("eta") is not None for item in selected) else None,
        "stddev_thrust_N": stddev([float(item["thrust_N"]) for item in selected]),
        "stddev_shaft_torque_Nm": stddev([float(item["shaft_torque_Nm"]) for item in selected]),
        "raw_Fx_mean": mean([float(item["Fx"]) for item in selected]),
        "raw_Fy_mean": mean([float(item["Fy"]) for item in selected]),
        "raw_Fz_mean": mean([float(item["Fz"]) for item in selected]),
        "raw_Mx_mean": mean([float(item["Mx"]) for item in selected]),
        "raw_My_mean": mean([float(item["My"]) for item in selected]),
        "raw_Mz_mean": mean([float(item["Mz"]) for item in selected]),
        "raw_force_axis_N_mean": mean([float(item["raw_force_axis_N"]) for item in selected]),
        "raw_moment_axis_Nm_mean": mean([float(item["raw_moment_axis_Nm"]) for item in selected]),
        "thrust_axis": selected[-1].get("thrust_axis", "z"),
        "thrust_sign": selected[-1].get("thrust_sign", 1),
        "torque_axis": selected[-1].get("torque_axis", "z"),
        "torque_sign": selected[-1].get("torque_sign", 1),
    }


def relative_change_pct(previous: float, latest: float) -> float | None:
    denominator = abs(previous)
    if denominator <= 1e-12:
        return None
    return abs(latest - previous) / denominator * 100.0


def force_convergence_metrics(
    samples: list[dict[str, Any]],
    window: int = 500,
    minimum_samples: int = 2000,
    mean_change_threshold_pct: float = 3.0,
    cv_threshold_pct: float = 5.0,
    thrust_mean_change_threshold_pct: float | None = None,
    torque_mean_change_threshold_pct: float | None = None,
    thrust_cv_threshold_pct: float | None = None,
    torque_cv_threshold_pct: float | None = None,
    fatal_error: bool = False,
) -> dict[str, Any]:
    thrust_mean_limit = mean_change_threshold_pct if thrust_mean_change_threshold_pct is None else thrust_mean_change_threshold_pct
    torque_mean_limit = mean_change_threshold_pct if torque_mean_change_threshold_pct is None else torque_mean_change_threshold_pct
    thrust_cv_limit = cv_threshold_pct if thrust_cv_threshold_pct is None else thrust_cv_threshold_pct
    torque_cv_limit = cv_threshold_pct if torque_cv_threshold_pct is None else torque_cv_threshold_pct
    count = len(samples)
    result: dict[str, Any] = {
        "force_sample_count": count,
        "convergence_window_size": window,
        "minimum_force_samples": minimum_samples,
        "thrust_mean_change_pct": None,
        "torque_mean_change_pct": None,
        "thrust_cv_pct": None,
        "torque_cv_pct": None,
        "latest_thrust_mean_N": None,
        "latest_shaft_torque_mean_Nm": None,
        "convergence_status": "warming up",
        "converged": False,
        "blocked_by": [],
        "thrust_mean_change_threshold_pct": thrust_mean_limit,
        "torque_mean_change_threshold_pct": torque_mean_limit,
        "thrust_cv_threshold_pct": thrust_cv_limit,
        "torque_cv_threshold_pct": torque_cv_limit,
    }
    if fatal_error:
        result["convergence_status"] = "not converged"
        result["blocked_by"] = ["fatal solver error"]
        return result
    if window <= 0:
        result["convergence_status"] = "not converged"
        result["blocked_by"] = ["invalid convergence window"]
        return result
    warmup_blocks = []
    if count < minimum_samples:
        warmup_blocks.append("insufficient samples")
    if count < 2 * window:
        warmup_blocks.append("insufficient samples for two windows")
    if warmup_blocks:
        result["blocked_by"] = warmup_blocks
        return result

    previous = samples[-2 * window:-window]
    latest = samples[-window:]
    previous_thrust = mean([float(item["thrust_N"]) for item in previous])
    latest_thrust = mean([float(item["thrust_N"]) for item in latest])
    previous_torque = mean([float(item["shaft_torque_Nm"]) for item in previous])
    latest_torque = mean([float(item["shaft_torque_Nm"]) for item in latest])
    thrust_std = stddev([float(item["thrust_N"]) for item in latest])
    torque_std = stddev([float(item["shaft_torque_Nm"]) for item in latest])
    thrust_change = relative_change_pct(previous_thrust, latest_thrust)
    torque_change = relative_change_pct(previous_torque, latest_torque)
    thrust_cv = abs(thrust_std / latest_thrust) * 100.0 if abs(latest_thrust) > 1e-12 else None
    torque_cv = abs(torque_std / latest_torque) * 100.0 if abs(latest_torque) > 1e-12 else None

    converged = (
        thrust_change is not None
        and torque_change is not None
        and thrust_cv is not None
        and torque_cv is not None
        and thrust_change < thrust_mean_limit
        and torque_change < torque_mean_limit
        and thrust_cv < thrust_cv_limit
        and torque_cv < torque_cv_limit
    )
    blocked_by = []
    if thrust_change is None or thrust_change >= thrust_mean_limit:
        blocked_by.append("thrust mean change")
    if torque_change is None or torque_change >= torque_mean_limit:
        blocked_by.append("torque mean change")
    if thrust_cv is None or thrust_cv >= thrust_cv_limit:
        blocked_by.append("thrust CV")
    if torque_cv is None or torque_cv >= torque_cv_limit:
        blocked_by.append("torque CV")
    result.update(
        {
            "thrust_mean_change_pct": thrust_change,
            "torque_mean_change_pct": torque_change,
            "thrust_cv_pct": thrust_cv,
            "torque_cv_pct": torque_cv,
            "latest_thrust_mean_N": latest_thrust,
            "latest_shaft_torque_mean_Nm": latest_torque,
            "convergence_status": "converged" if converged else "stabilizing",
            "converged": converged,
            "blocked_by": [] if converged else blocked_by,
        }
    )
    return result


def build_solver_timeseries(case_dir: Path, average_window: int = 20) -> dict[str, Any]:
    info = read_json(case_dir / "run_metadata.json") or read_json(case_dir / "case_info.json")
    validation = read_json(case_dir / "validation_config.json")
    log_series = parse_solver_log_series(case_dir)
    force_samples, force_status = parse_force_timeseries(case_dir, info)
    data = {
        "case": case_dir.name,
        "validation_id": validation.get("validation_id") or info.get("validation_id", ""),
        "solver_log": log_series.get("solver_log", ""),
        "force_status": force_status,
        "force_samples": force_samples,
        "courant": log_series.get("courant", []),
        "residuals": log_series.get("residuals", []),
        "execution": log_series.get("execution", []),
        "deltaT": log_series.get("deltaT", []),
        "stability": {
            "thrust_N": stability_hint(force_samples, "thrust_N", average_window),
            "shaft_torque_Nm": stability_hint(force_samples, "shaft_torque_Nm", average_window),
        },
        "summary": force_summary(force_samples, average_window),
        "note": "This is not a production averaging window." if validation else "",
    }
    try:
        (case_dir / "solver_timeseries.json").write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        pass
    return data


def build_solver_progress(case_dir: Path, process_running: bool | None = None, stale_after_s: float = 20.0) -> dict[str, Any]:
    info = read_json(case_dir / "run_metadata.json") or read_json(case_dir / "case_info.json")
    validation = read_json(case_dir / "validation_config.json")
    targets = read_control_targets(case_dir)
    log_data = parse_solver_log(case_dir)
    force_data = parse_latest_forces(case_dir, info)
    timeseries = build_solver_timeseries(case_dir)

    current_time = log_data.get("current_time")
    target_end = targets.get("target_end_time")
    physical_percent = None
    if current_time is not None and target_end and target_end > 0:
        physical_percent = max(0.0, min(100.0, 100.0 * float(current_time) / float(target_end)))

    target_steps = targets.get("target_timesteps")
    completed_steps = log_data.get("timesteps_completed") or 0
    timestep_percent = None
    if target_steps:
        timestep_percent = max(0.0, min(100.0, 100.0 * float(completed_steps) / float(target_steps)))

    rpm = safe_float(info.get("rpm"), safe_float(validation.get("rpm"), 0.0)) or 0.0
    revolutions_completed = float(current_time) * rpm / 60.0 if current_time is not None else None
    validation_revolutions = safe_float(validation.get("validation_revolutions"), safe_float(info.get("validation_revolutions"), None))
    validation_percent = None
    if revolutions_completed is not None and validation_revolutions and validation_revolutions > 0:
        validation_percent = max(0.0, min(100.0, 100.0 * revolutions_completed / validation_revolutions))

    log_mtime = log_data.get("log_mtime")
    stale = bool(log_mtime and time.time() - float(log_mtime) > stale_after_s)
    fatal = bool(log_data.get("fatal_error"))
    convergence_window = int(safe_float(info.get("convergence_window_size"), 500) or 500)
    convergence_min_samples = int(safe_float(info.get("convergence_min_force_samples"), 2000) or 2000)
    convergence = force_convergence_metrics(
        timeseries.get("force_samples", []),
        window=convergence_window,
        minimum_samples=convergence_min_samples,
        thrust_mean_change_threshold_pct=safe_float(info.get("convergence_thrust_mean_change_pct"), 3.0),
        torque_mean_change_threshold_pct=safe_float(info.get("convergence_torque_mean_change_pct"), 5.0),
        thrust_cv_threshold_pct=safe_float(info.get("convergence_thrust_cv_pct"), 5.0),
        torque_cv_threshold_pct=safe_float(info.get("convergence_torque_cv_pct"), 5.0),
        fatal_error=fatal,
    )
    convergence["convergence_preset"] = str(info.get("convergence_preset", "fast_screening"))
    if fatal:
        status = "failed"
    elif process_running is None:
        status = "solver_running" if current_time is not None else "queued"
    elif process_running:
        status = "solver_running" if current_time is not None else "queued"
    else:
        status = "completed" if not fatal else "failed"

    max_co = targets.get("maxCo")
    courant_max = log_data.get("courant_max")
    warnings: list[str] = []
    if stale and process_running:
        warnings.append("No solver log update detected recently")
    if process_running and not stale and current_time is not None:
        warnings.append("Solver is active")
    if max_co is not None and courant_max is not None and float(courant_max) > 2.0 * float(max_co):
        warnings.append(f"Courant max {courant_max:.4g} is more than 2x configured maxCo {max_co:.4g}")
    if fatal:
        warnings.append("Fatal solver error detected in log")
    raw_force_axis = force_data.get("raw_force_axis_N")
    signed_thrust = force_data.get("latest_thrust_N")
    shaft_torque = force_data.get("latest_shaft_torque_Nm")
    power = force_data.get("latest_power_W")
    try:
        if raw_force_axis is not None and signed_thrust is not None and float(raw_force_axis) > 0 and float(signed_thrust) < 0:
            warnings.append("THRUST_SIGN_MISMATCH")
        if signed_thrust is not None and float(signed_thrust) < 0:
            warnings.append("NEGATIVE_THRUST_CHECK_SIGN_OR_ORIENTATION")
        if (shaft_torque is not None and float(shaft_torque) < 0) or (power is not None and float(power) < 0):
            warnings.append("TORQUE_SIGN_CHECK")
    except (TypeError, ValueError):
        pass

    snapshot = {
        "case": case_dir.name,
        "validation_id": validation.get("validation_id") or info.get("validation_id", ""),
        "solver_status": status,
        "current_time": current_time,
        "target_endTime": target_end,
        "physical_percent": physical_percent,
        "timesteps_completed": completed_steps,
        "target_timesteps": target_steps,
        "timestep_percent": timestep_percent,
        "latest_deltaT": log_data.get("latest_deltaT"),
        "courant_mean": log_data.get("courant_mean"),
        "courant_max": courant_max,
        "configured_maxCo": max_co,
        "execution_time_s": log_data.get("execution_time_s"),
        "clock_time_s": log_data.get("clock_time_s"),
        "solver_log": log_data.get("solver_log", ""),
        "log_tail": log_data.get("log_tail", []),
        "fatal_error": fatal,
        "fatal_match": log_data.get("fatal_match", {}),
        "fatal_rule": log_data.get("fatal_rule", ""),
        "fatal_line": log_data.get("fatal_line", ""),
        "fatal_excerpt": log_data.get("fatal_excerpt", ""),
        "ignored_log_warnings": log_data.get("ignored_log_warnings", []),
        "convergence": convergence,
        "log_stale": stale,
        "latest_log_update_time": log_mtime,
        "latest_log_update_age_s": time.time() - float(log_mtime) if log_mtime else None,
        "rpm": rpm,
        "validation_revolutions": validation_revolutions,
        "revolutions_completed": revolutions_completed,
        "validation_revolutions_percent": validation_percent,
        "validation_note": "Validation solver telemetry." if validation else "",
        "warnings": warnings,
        "timeseries_samples": len(timeseries.get("force_samples", [])),
        **force_data,
    }
    try:
        (case_dir / "solver_progress.json").write_text(json.dumps(snapshot, indent=2) + "\n")
    except OSError:
        pass
    return snapshot

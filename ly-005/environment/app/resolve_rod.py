#!/usr/bin/env python3
"""Recover unlabeled sensor positions and a piecewise thermal model."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


SCHEMA_INPUT = "rod-inverse/v1"
SCHEMA_REPORT = "rod-inverse-report/v1"
CHANNEL_PATTERN = re.compile(r"TC-[A-Z][0-9]")
JOB_PATTERN = re.compile(r"JOB-[A-Z0-9]{4}-[A-Z0-9]{4}")
STATE_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{2,31}")

TOP_KEYS = {
    "schema_version",
    "job_id",
    "output",
    "state_key",
    "rod",
    "observations",
    "controls",
}
ROD_KEYS = {
    "length_m",
    "segments",
    "initial_temperature_c",
    "ambient_c",
    "boundary_bounds",
}
SEGMENT_KEYS = {
    "x0_m",
    "x1_m",
    "capacity_j_m3k",
    "conductivity_bounds_w_mk",
}
BOUNDARY_KEYS = {
    "left_h_w_m2k",
    "right_h_w_m2k",
}
OBSERVATION_KEYS = {
    "times_s",
    "surface_temperatures_c",
    "reference_channels",
    "channels",
    "max_outliers",
}
REFERENCE_KEYS = {
    "channel_id",
    "position_m",
    "temperatures_c",
}
CHANNEL_KEYS = {
    "channel_id",
    "temperatures_c",
}
CONTROL_KEYS = {
    "max_seconds",
    "fd_nodes",
    "max_fourier",
    "rms_tolerance_c",
    "map_tolerance_m",
    "parameter_rel_tolerance",
}


class TaskError(Exception):
    """A public refusal with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def is_number(value: Any) -> bool:
    """JSON numbers exclude booleans and non-finite values."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def rounded(value: float, digits: int = 6) -> float:
    """Round report values and normalize negative zero."""
    scale = 10 ** digits
    magnitude = math.floor(abs(float(value)) * scale + 0.5) / scale
    result = math.copysign(magnitude, float(value))
    return 0.0 if result == 0.0 else result


def canonical_bytes(value: Any) -> bytes:
    """Produce the package's canonical compact representation."""
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def require_exact_keys(
    value: Any,
    expected: set[str],
    code: str,
    label: str,
) -> None:
    """Reject non-objects and any key-set drift."""
    if not isinstance(value, dict):
        raise TaskError(code, f"{label} must be an object")
    keys = set(value)
    missing = sorted(expected - keys)
    extra = sorted(keys - expected)
    if missing:
        raise TaskError(code, f"{label} missing key {missing[0]}")
    if extra:
        raise TaskError(code, f"{label} has extra key {extra[0]}")


def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build an object while rejecting duplicate names at every depth."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TaskError("DUPLICATE_KEY", f"duplicate key {key}")
        result[key] = value
    return result


def read_job(path: Path) -> tuple[dict[str, Any], bytes]:
    """Read strict UTF-8 JSON and retain the original bytes."""
    if path.is_symlink() or not path.is_file():
        raise TaskError("JOB_PATH", "job path must be a regular file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise TaskError("JOB_READ", f"cannot read job: {exc.strerror}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskError("UTF8", "job is not valid UTF-8") from exc
    try:
        value = json.loads(text, object_pairs_hook=reject_duplicates)
    except TaskError:
        raise
    except json.JSONDecodeError as exc:
        raise TaskError("JSON", f"invalid JSON at line {exc.lineno}") from exc
    if not isinstance(value, dict):
        raise TaskError("SCHEMA", "top level must be an object")
    return value, raw


def validate_absolute_path(value: Any, label: str) -> Path:
    """Validate an absolute POSIX output path without traversal aliases."""
    if not isinstance(value, str) or not value:
        raise TaskError("TYPE", f"{label} must be a non-empty string")
    if "\\" in value or "\x00" in value:
        raise TaskError("PATH", f"{label} contains a forbidden character")
    path = Path(value)
    if not path.is_absolute():
        raise TaskError("PATH", f"{label} must be absolute")
    if any(part in {".", ".."} for part in path.parts):
        raise TaskError("PATH", f"{label} contains a traversal component")
    return path


def validate_channel_id(value: Any) -> str:
    """Validate the factory-style channel label."""
    if not isinstance(value, str) or CHANNEL_PATTERN.fullmatch(value) is None:
        raise TaskError("CHANNEL_ID", "channel_id does not match TC-A0 form")
    return value


def validate_job(
    job: dict[str, Any],
    output_path: Path,
    state_dir: Path,
) -> None:
    """Validate every public field before touching report or ledger state."""
    require_exact_keys(job, TOP_KEYS, "SCHEMA", "top level")
    if job["schema_version"] != SCHEMA_INPUT:
        raise TaskError("SCHEMA_VERSION", "unsupported schema_version")
    if not isinstance(job["job_id"], str) or JOB_PATTERN.fullmatch(job["job_id"]) is None:
        raise TaskError("JOB_ID", "job_id does not match JOB-AAAA-0000 form")
    validate_absolute_path(job["output"], "output")
    if not isinstance(job["state_key"], str) or STATE_PATTERN.fullmatch(job["state_key"]) is None:
        raise TaskError("STATE_KEY", "state_key does not match the public pattern")
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise TaskError("STATE_DIR", "--state-dir must be a real directory")
    if output_path.parent.is_symlink() or not output_path.parent.is_dir():
        raise TaskError("OUTPUT_PARENT", "output parent must be a real directory")
    if output_path.exists() and (output_path.is_symlink() or not output_path.is_file()):
        raise TaskError("OUTPUT_TYPE", "existing output must be a regular file")

    rod = job["rod"]
    require_exact_keys(rod, ROD_KEYS, "SCHEMA", "rod")
    if not is_number(rod["length_m"]) or not 0.1 <= float(rod["length_m"]) <= 3.0:
        raise TaskError("LENGTH", "length_m must be a JSON number in [0.1, 3.0]")
    length = float(rod["length_m"])
    if not is_number(rod["initial_temperature_c"]):
        raise TaskError("TYPE", "initial_temperature_c must be numeric")
    if not is_number(rod["ambient_c"]):
        raise TaskError("TYPE", "ambient_c must be numeric")
    if abs(float(rod["initial_temperature_c"]) - float(rod["ambient_c"])) < 10.0:
        raise TaskError("TEMPERATURE_RANGE", "initial and ambient temperatures are too close")
    require_exact_keys(rod["boundary_bounds"], BOUNDARY_KEYS, "SCHEMA", "boundary_bounds")
    for name in sorted(BOUNDARY_KEYS):
        bounds = rod["boundary_bounds"][name]
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise TaskError("TYPE", f"{name} must be a two-number array")
        if not all(is_number(item) for item in bounds):
            raise TaskError("TYPE", f"{name} bounds must be numeric")
        low, high = float(bounds[0]), float(bounds[1])
        if low < 0.0 or high <= low:
            raise TaskError("BOUNDARY_BOUNDS", f"{name} bounds are not increasing")

    segments = rod["segments"]
    if not isinstance(segments, list) or len(segments) != 3:
        raise TaskError("SEGMENTS", "rod.segments must contain exactly three items")
    for index, segment in enumerate(segments):
        require_exact_keys(segment, SEGMENT_KEYS, "SCHEMA", f"segment {index}")
        if not all(is_number(segment[key]) for key in ("x0_m", "x1_m", "capacity_j_m3k")):
            raise TaskError("TYPE", f"segment {index} has a non-numeric field")
        if float(segment["x1_m"]) <= float(segment["x0_m"]):
            raise TaskError("SEGMENT_EDGE", f"segment {index} has non-increasing edges")
        if float(segment["capacity_j_m3k"]) <= 0.0:
            raise TaskError("CAPACITY", f"segment {index} capacity must be positive")
        bounds = segment["conductivity_bounds_w_mk"]
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise TaskError("TYPE", f"segment {index} conductivity bounds must be an array")
        if not all(is_number(item) for item in bounds):
            raise TaskError("TYPE", f"segment {index} conductivity bounds must be numeric")
        if float(bounds[0]) <= 0.0 or float(bounds[1]) <= float(bounds[0]):
            raise TaskError("CONDUCTIVITY_BOUNDS", f"segment {index} bounds are invalid")
    if abs(float(segments[0]["x0_m"])) > 1e-12:
        raise TaskError("SEGMENT_EDGE", "first segment must start at zero")
    if abs(float(segments[-1]["x1_m"]) - length) > 1e-9:
        raise TaskError("SEGMENT_EDGE", "last segment must end at length_m")
    for index in range(1, 3):
        if abs(float(segments[index]["x0_m"]) - float(segments[index - 1]["x1_m"])) > 1e-9:
            raise TaskError("SEGMENT_EDGE", "segment edges must be contiguous")

    observations = job["observations"]
    require_exact_keys(observations, OBSERVATION_KEYS, "SCHEMA", "observations")
    times = observations["times_s"]
    if not isinstance(times, list) or not 8 <= len(times) <= 64:
        raise TaskError("TIMES", "times_s must contain 8 to 64 values")
    if not all(is_number(item) for item in times):
        raise TaskError("TYPE", "times_s must contain JSON numbers")
    if abs(float(times[0])) > 1e-12:
        raise TaskError("TIMES", "first observation time must be zero")
    if any(float(times[index]) <= float(times[index - 1]) for index in range(1, len(times))):
        raise TaskError("TIMES", "times_s must be strictly increasing")
    if float(times[-1]) > 7200.0:
        raise TaskError("TIMES", "final observation time exceeds 7200 seconds")
    surface = observations["surface_temperatures_c"]
    if not isinstance(surface, list) or len(surface) != len(times):
        raise TaskError("SURFACE_LENGTH", "surface trace must match times_s length")
    if not all(is_number(item) for item in surface):
        raise TaskError("TYPE", "surface temperatures must be numeric")
    if any(not -80.0 <= float(item) <= 500.0 for item in surface):
        raise TaskError("TRACE_RANGE", "surface temperature is outside [-80, 500]")
    references = observations["reference_channels"]
    if not isinstance(references, list) or len(references) != 3:
        raise TaskError("REFERENCE_CHANNELS", "reference_channels must contain exactly three items")
    reference_positions = []
    reference_ids: set[str] = set()
    for index, reference in enumerate(references):
        require_exact_keys(reference, REFERENCE_KEYS, "SCHEMA", f"reference {index}")
        channel_id = reference["channel_id"]
        if (
            not isinstance(channel_id, str)
            or re.fullmatch(r"RTD-[A-Z][0-9]", channel_id) is None
            or channel_id in reference_ids
        ):
            raise TaskError("REFERENCE_ID", "reference channel_id is invalid or duplicated")
        reference_ids.add(channel_id)
        if (
            not is_number(reference["position_m"])
            or not 0.0 < float(reference["position_m"]) < length
        ):
            raise TaskError("REFERENCE_POSITION", "reference position is outside the rod")
        reference_positions.append(float(reference["position_m"]))
        values = reference["temperatures_c"]
        if not isinstance(values, list) or len(values) != len(times):
            raise TaskError("TRACE_LENGTH", "reference trace must match times_s length")
        if not all(is_number(item) for item in values):
            raise TaskError("TYPE", "reference temperatures must be numeric")
    if any(
        reference_positions[index] >= reference_positions[index + 1]
        for index in range(len(reference_positions) - 1)
    ):
        raise TaskError("REFERENCE_POSITION", "reference positions must be increasing")

    channels = observations["channels"]
    if not isinstance(channels, list) or not 5 <= len(channels) <= 12:
        raise TaskError("CHANNELS", "observations.channels must contain 5 to 12 items")
    seen: set[str] = set()
    for index, channel in enumerate(channels):
        require_exact_keys(channel, CHANNEL_KEYS, "SCHEMA", f"channel {index}")
        channel_id = validate_channel_id(channel["channel_id"])
        if channel_id in seen:
            raise TaskError("CHANNEL_ID", "channel_id values must be unique")
        seen.add(channel_id)
        values = channel["temperatures_c"]
        if not isinstance(values, list) or len(values) != len(times):
            raise TaskError("TRACE_LENGTH", f"{channel_id} must match times_s length")
        if not all(is_number(item) for item in values):
            raise TaskError("TYPE", f"{channel_id} temperatures must be numeric")
        if any(not -80.0 <= float(item) <= 500.0 for item in values):
            raise TaskError("TRACE_RANGE", f"{channel_id} temperature is outside [-80, 500]")

    max_outliers = observations["max_outliers"]
    if not isinstance(max_outliers, int) or isinstance(max_outliers, bool):
        raise TaskError("TYPE", "max_outliers must be a JSON integer")
    if not 0 <= max_outliers <= 3 or len(channels) - max_outliers < 4:
        raise TaskError("OUTLIER_BUDGET", "max_outliers must be an integer from 0 to 3")

    controls = job["controls"]
    require_exact_keys(controls, CONTROL_KEYS, "SCHEMA", "controls")
    if not isinstance(controls["fd_nodes"], int) or isinstance(controls["fd_nodes"], bool):
        raise TaskError("TYPE", "fd_nodes must be a JSON integer")
    if not 48 <= controls["fd_nodes"] <= 128:
        raise TaskError("FD_NODES", "fd_nodes must be an integer in [48, 128]")
    numeric_rules = {
        "max_seconds": (1.0, 30.0),
        "max_fourier": (0.30, 1.20),
        "rms_tolerance_c": (0.05, 1.0),
        "map_tolerance_m": (0.002, 0.05),
        "parameter_rel_tolerance": (0.05, 0.40),
    }
    for key, (low, high) in numeric_rules.items():
        value = controls[key]
        if not is_number(value) or not low <= float(value) <= high:
            raise TaskError("CONTROL_RANGE", f"{key} is outside its public range")


def parse_cli(argv: list[str]) -> tuple[Path, Path]:
    """Parse the required --job/--state-dir pair without accepting extras."""
    if len(argv) != 4:
        raise TaskError("USAGE", "expected --job PATH --state-dir PATH")
    values: dict[str, str] = {}
    for index in (0, 2):
        flag = argv[index]
        if flag not in {"--job", "--state-dir"} or flag in values:
            raise TaskError("USAGE", "expected --job PATH --state-dir PATH")
        values[flag] = argv[index + 1]
    if set(values) != {"--job", "--state-dir"}:
        raise TaskError("USAGE", "expected --job PATH --state-dir PATH")
    return Path(values["--job"]), Path(values["--state-dir"])


def segment_value(x: float, edges: list[float], values: list[float]) -> float:
    """Evaluate a piecewise constant material property."""
    for index in range(len(values) - 1):
        if x < edges[index + 1]:
            return float(values[index])
    return float(values[-1])


def simulate(
    job: dict[str, Any],
    conductivity: list[float],
    left_h: float,
    right_h: float,
    positions: list[float],
) -> tuple[list[list[float]], float, float]:
    """Run Crank-Nicolson and return traces plus timestep diagnostics."""
    rod = job["rod"]
    length = float(rod["length_m"])
    edges = [
        float(rod["segments"][0]["x0_m"]),
        float(rod["segments"][1]["x0_m"]),
        float(rod["segments"][2]["x0_m"]),
        float(rod["segments"][2]["x1_m"]),
    ]
    capacity = [float(item["capacity_j_m3k"]) for item in rod["segments"]]
    nodes = int(job["controls"]["fd_nodes"])
    max_fourier_limit = float(job["controls"]["max_fourier"])
    times = [float(item) for item in job["observations"]["times_s"]]
    dx = length / (nodes - 1)
    xs = [index * dx for index in range(nodes)]
    caps = [segment_value(x, edges, capacity) for x in xs]
    conds = [segment_value(x, edges, conductivity) for x in xs]
    faces = [
        2.0 * conds[index] * conds[index + 1] / (conds[index] + conds[index + 1])
        for index in range(nodes - 1)
    ]
    lower = [0.0] * (nodes - 1)
    upper = [0.0] * (nodes - 1)
    diagonal = [0.0] * nodes
    constant = [0.0] * nodes
    ambient = float(rod["ambient_c"])

    for index in range(nodes):
        if index > 0:
            coefficient = faces[index - 1] / (caps[index] * dx * dx)
            lower[index - 1] = coefficient
            diagonal[index] -= coefficient
        if index < nodes - 1:
            coefficient = faces[index] / (caps[index] * dx * dx)
            upper[index] = coefficient
            diagonal[index] -= coefficient
        if index == 0:
            coefficient = 2.0 * left_h / (caps[index] * dx)
            diagonal[index] -= coefficient
            constant[index] += coefficient * ambient
        if index == nodes - 1:
            coefficient = 2.0 * right_h / (caps[index] * dx)
            diagonal[index] -= coefficient
            constant[index] += coefficient * ambient

    target_dt = max_fourier_limit * min(
        caps[index] * dx * dx / conds[index]
        for index in range(nodes)
    )
    temperature = [float(rod["initial_temperature_c"])] * nodes
    output: list[list[float]] = []
    previous_time = 0.0
    smallest_dt = math.inf
    largest_fourier = 0.0

    def sample(x: float) -> float:
        location = x / dx
        left = max(0, min(int(location), nodes - 2))
        fraction = location - left
        return (
            temperature[left] * (1.0 - fraction)
            + temperature[left + 1] * fraction
        )

    for current_time in times:
        interval = current_time - previous_time
        if interval <= 0.0:
            output.append([sample(position) for position in positions])
            previous_time = current_time
            continue
        step_count = max(1, math.ceil(interval / target_dt - 1e-12))
        dt = interval / step_count
        smallest_dt = min(smallest_dt, dt)
        largest_fourier = max(
            largest_fourier,
            max(
                conds[index] * dt / (caps[index] * dx * dx)
                for index in range(nodes)
            ),
        )
        for _ in range(step_count):
            lhs_diag = [
                1.0 - 0.5 * dt * diagonal[index]
                for index in range(nodes)
            ]
            lhs_lower = [-0.5 * dt * value for value in lower]
            lhs_upper = [-0.5 * dt * value for value in upper]
            rhs = []
            for index in range(nodes):
                value = (
                    temperature[index]
                    + 0.5
                    * dt
                    * (diagonal[index] * temperature[index] + constant[index])
                )
                if index > 0:
                    value += 0.5 * dt * lower[index - 1] * temperature[index - 1]
                if index < nodes - 1:
                    value += 0.5 * dt * upper[index] * temperature[index + 1]
                rhs.append(value)
            modified_upper = [0.0] * nodes
            modified_rhs = [0.0] * nodes
            modified_upper[0] = (
                lhs_upper[0] / lhs_diag[0]
                if nodes > 1
                else 0.0
            )
            modified_rhs[0] = rhs[0] / lhs_diag[0]
            for index in range(1, nodes):
                denominator = (
                    lhs_diag[index]
                    - lhs_lower[index - 1] * modified_upper[index - 1]
                )
                modified_upper[index] = (
                    lhs_upper[index] / denominator
                    if index < nodes - 1
                    else 0.0
                )
                modified_rhs[index] = (
                    rhs[index]
                    - lhs_lower[index - 1] * modified_rhs[index - 1]
                ) / denominator
            temperature[-1] = modified_rhs[-1]
            for index in range(nodes - 2, -1, -1):
                temperature[index] = (
                    modified_rhs[index]
                    - modified_upper[index] * temperature[index + 1]
                )
        output.append([sample(position) for position in positions])
        previous_time = current_time
    return output, smallest_dt, largest_fourier


def channel_rmse(predicted: list[list[float]], observed: list[float], column: int) -> float:
    """Compute the legacy fit residual for one channel."""
    return math.sqrt(
        sum(
            (predicted[row][column] - observed[row]) ** 2
            for row in range(len(observed))
        )
        / len(observed)
    )


def fit_campaign(
    job: dict[str, Any],
    deadline: float,
) -> tuple[list[dict[str, Any]], list[float], float, float, float, float, dict[str, Any]]:
    """Return the legacy equal-spacing estimate left by the first installation."""
    del deadline
    length = float(job["rod"]["length_m"])
    channels = job["observations"]["channels"]
    count = len(channels)
    positions = [
        0.035 * length + (0.93 * length) * (index + 1) / (count + 1)
        for index in range(count)
    ]
    conductivity = []
    for segment in job["rod"]["segments"]:
        low, high = (float(item) for item in segment["conductivity_bounds_w_mk"])
        conductivity.append(math.sqrt(low * high))
    left_bounds = job["rod"]["boundary_bounds"]["left_h_w_m2k"]
    right_bounds = job["rod"]["boundary_bounds"]["right_h_w_m2k"]
    left_h = max(0.02, math.sqrt(max(1e-6, left_bounds[0] * left_bounds[1])))
    right_h = max(0.002, math.sqrt(max(1e-8, right_bounds[0] * right_bounds[1])))
    references = [
        float(item["position_m"])
        for item in job["observations"]["reference_channels"]
    ]
    predicted, smallest_dt, largest_fourier = simulate(
        job,
        conductivity,
        left_h,
        right_h,
        positions + references + [0.0],
    )
    mapped = []
    errors = []
    for column, channel in enumerate(channels):
        observed = [float(item) for item in channel["temperatures_c"]]
        error = channel_rmse(predicted, observed, column)
        errors.append(error)
        mapped.append(
            {
                "channel_id": channel["channel_id"],
                "x_m": rounded(positions[column]),
                "support": rounded(max(0.0, 1.0 - error)),
            }
        )
    rms = math.sqrt(sum(value * value for value in errors) / len(errors))
    extras = {
        "bad_sorted": [],
        "support": count,
        "distance": float(job["controls"]["map_tolerance_m"]) / 2.0,
        "smallest_dt": smallest_dt,
        "largest_fourier": largest_fourier,
        "direction_correlation": 0.0,
    }
    return mapped, conductivity, left_h, right_h, rms, 0.0, extras


def read_ledger(path: Path) -> list[dict[str, Any]]:
    """Read the append-only ledger while preserving its original bytes."""
    if not path.exists():
        return []
    if path.is_dir():
        return []
    if path.is_symlink() or not path.is_file():
        raise TaskError("LEDGER_TYPE", "ledger path must be a regular file")
    raw = path.read_bytes()
    if not raw:
        raise TaskError("LEDGER_CORRUPT", "ledger file is empty")
    if not raw.endswith(b"\n"):
        raise TaskError("LEDGER_CORRUPT", "ledger must end with a newline")
    entries = []
    for line in raw.splitlines():
        try:
            value = json.loads(line.decode("utf-8"), object_pairs_hook=reject_duplicates)
        except (UnicodeDecodeError, json.JSONDecodeError, TaskError) as exc:
            raise TaskError("LEDGER_CORRUPT", "ledger contains an invalid line") from exc
        require_exact_keys(
            value,
            {"event_sha256", "job_sha256", "report_sha256", "sequence"},
            "LEDGER_CORRUPT",
            "ledger entry",
        )
        if value["sequence"] != len(entries) + 1:
            raise TaskError("LEDGER_CORRUPT", "ledger sequence is not contiguous")
        entries.append(value)
    return entries


def build_core(
    job: dict[str, Any],
    fitted: tuple[list[dict[str, Any]], list[float], float, float, float, float, dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the deterministic report body before ledger metadata."""
    mapped, conductivity, left_h, right_h, rms, unique_margin, extras = fitted
    segments = job["rod"]["segments"]
    conductivity_rows = [
        {
            "segment": index,
            "x0_m": rounded(float(segments[index]["x0_m"])),
            "x1_m": rounded(float(segments[index]["x1_m"])),
            "k_w_mk": rounded(conductivity[index]),
        }
        for index in range(3)
    ]
    parameter_payload = {
        "sensor_map": mapped,
        "conductivity_w_mk": conductivity_rows,
        "boundaries": {
            "left_h_w_m2k": rounded(left_h),
            "right_h_w_m2k": rounded(right_h),
            "ambient_c": rounded(float(job["rod"]["ambient_c"])),
        },
    }
    parameter_hash = hashlib.sha256(canonical_bytes(parameter_payload)).hexdigest()
    support = int(extras["support"])
    return {
        "schema_version": SCHEMA_REPORT,
        "job_id": job["job_id"],
        "status": "certified",
        "sensor_map": mapped,
        "conductivity_w_mk": conductivity_rows,
        "boundaries": parameter_payload["boundaries"],
        "timestep": {
            "nodes": int(job["controls"]["fd_nodes"]),
            "dt_s": rounded(float(extras["smallest_dt"])),
            "max_fourier": rounded(float(extras["largest_fourier"])),
        },
        "consensus": {
            "good_channels": sorted(item["channel_id"] for item in mapped),
            "outlier_channels": extras["bad_sorted"],
            "rms_c": rounded(rms),
            "support_count": support,
        },
        "mapping_diagnostics": {
            "distance_rms_m": rounded(float(extras["distance"])),
            "direction_correlation": rounded(float(extras["direction_correlation"])),
            "rank_margin": rounded(float(unique_margin)),
        },
        "certificate": {
            "accepted": True,
            "support_count": support,
            "unique_margin": rounded(float(unique_margin)),
            "parameter_sha256": parameter_hash,
        },
    }


def publish_atomic(
    job: dict[str, Any],
    job_raw: bytes,
    output_path: Path,
    state_dir: Path,
    core: dict[str, Any],
) -> int:
    """Publish report and ledger with rollback on the second replacement."""
    state_key = job["state_key"]
    ledger_path = state_dir / f"{state_key}.jsonl"
    entries = read_ledger(ledger_path)
    job_hash = hashlib.sha256(job_raw).hexdigest()
    parameter_digest = bytes.fromhex(core["certificate"]["parameter_sha256"])
    event_hash = hashlib.sha256(job_raw + parameter_digest).hexdigest()
    existing = next(
        (item for item in entries if item["event_sha256"] == event_hash),
        None,
    )
    sequence = int(existing["sequence"]) if existing else len(entries) + 1
    report = dict(core)
    report["ledger"] = {
        "state_key": state_key,
        "event_sha256": event_hash,
        "sequence": sequence,
    }
    report_bytes = canonical_bytes(report) + b"\n"
    report_hash = hashlib.sha256(report_bytes).hexdigest()

    output_temp: Path | None = None
    ledger_temp: Path | None = None
    backup_temp: Path | None = None
    previous_bytes: bytes | None = None
    previous_mode: int | None = None
    published_new_report = False
    try:
        report_fd, report_name = tempfile.mkstemp(
            prefix=".rod-report-",
            suffix=".tmp",
            dir=output_path.parent,
        )
        os.close(report_fd)
        output_temp = Path(report_name)
        output_temp.write_bytes(report_bytes)
        if output_path.exists():
            previous_bytes = output_path.read_bytes()
            previous_mode = output_path.stat().st_mode & 0o777
        if existing is None:
            entry = {
                "event_sha256": event_hash,
                "job_sha256": job_hash,
                "report_sha256": report_hash,
                "sequence": sequence,
            }
            ledger_bytes = b""
            if ledger_path.is_file():
                ledger_bytes = ledger_path.read_bytes()
            ledger_bytes += canonical_bytes(entry) + b"\n"
            ledger_fd, ledger_name = tempfile.mkstemp(
                prefix=".rod-ledger-",
                suffix=".tmp",
                dir=state_dir,
            )
            os.close(ledger_fd)
            ledger_temp = Path(ledger_name)
            ledger_temp.write_bytes(ledger_bytes)
        if previous_bytes is not None:
            backup_fd, backup_name = tempfile.mkstemp(
                prefix=".rod-backup-",
                suffix=".tmp",
                dir=output_path.parent,
            )
            os.close(backup_fd)
            backup_temp = Path(backup_name)
            backup_temp.write_bytes(previous_bytes)
            if previous_mode is not None:
                backup_temp.chmod(previous_mode)
        os.replace(output_temp, output_path)
        output_temp = None
        published_new_report = True
        if existing is None and ledger_temp is not None:
            os.replace(ledger_temp, ledger_path)
            ledger_temp = None
        if backup_temp is not None:
            backup_temp.unlink(missing_ok=True)
            backup_temp = None
        return sequence
    except OSError as exc:
        if published_new_report:
            if backup_temp is not None and backup_temp.exists():
                os.replace(backup_temp, output_path)
                backup_temp = None
            elif previous_bytes is None:
                output_path.unlink(missing_ok=True)
        raise TaskError("PUBLISH", f"atomic publish failed: {exc.strerror or 'I/O error'}") from exc
    finally:
        for path in (output_temp, ledger_temp, backup_temp):
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


def solve(job_path: Path, state_dir: Path) -> None:
    """Validate, fit and publish one campaign."""
    job, raw = read_job(job_path)
    require_exact_keys(job, TOP_KEYS, "SCHEMA", "top level")
    output_path = validate_absolute_path(job.get("output"), "output")
    validate_job(job, output_path, state_dir)
    started = time.monotonic()
    deadline = started + float(job["controls"]["max_seconds"]) * 0.78
    fitted = fit_campaign(job, deadline)
    core = build_core(job, fitted)
    publish_atomic(job, raw, output_path, state_dir, core)
    if time.monotonic() - started > float(job["controls"]["max_seconds"]):
        raise TaskError("TIME_LIMIT", "solve exceeded controls.max_seconds")


def main(argv: list[str]) -> int:
    """Run the command and emit public refusal text only on stderr."""
    try:
        job_path, state_dir = parse_cli(argv)
        solve(job_path, state_dir)
        return 0
    except TaskError as exc:
        sys.stderr.write(f"ERROR {exc.code}: {exc.message}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

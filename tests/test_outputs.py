from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


TASK_APP = os.environ.get("TASK_APP", "/app/resolve_rod.py")
TASK_DATA_DIR = os.environ.get(
    "TASK_DATA_DIR",
    str(Path(TASK_APP).resolve().parent / "data"),
)
EXPECTED_TRUTH = json.loads(
    (Path(__file__).resolve().parent / "expected_truth.json").read_text(encoding="utf-8")
)
MAP_TOLERANCE = 0.014


def candidate_run(
    job_path: Path,
    state_dir: Path,
    *,
    timeout: float = 20.0,
) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            TASK_APP,
            "--job",
            str(job_path),
            "--state-dir",
            str(state_dir),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
    )
    return result, time.monotonic() - started


def source_job(campaign: str) -> dict:
    path = Path(TASK_DATA_DIR) / f"{campaign}_job.json"
    return json.loads(path.read_text(encoding="utf-8"))


def prepare_job(
    root: Path,
    campaign: str,
    *,
    output_name: str = "report.json",
    mutate=None,
) -> tuple[Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    state_dir = root / "state"
    output_dir = root / "out"
    state_dir.mkdir()
    output_dir.mkdir()
    output_dir.chmod(0o777)
    job = source_job(campaign)
    job["output"] = str(output_dir / output_name)
    if mutate is not None:
        mutate(job)
    job_path = root / "job.json"
    job_path.write_text(
        json.dumps(job, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    return job_path, state_dir, Path(job["output"])


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def ledger_path(state_dir: Path, job: dict) -> Path:
    return state_dir / f"{job['state_key']}.jsonl"


def maximum_map_error(report: dict, truth: dict) -> float:
    actual = {
        item["channel_id"]: float(item["x_m"])
        for item in report["sensor_map"]
    }
    return max(
        abs(actual[channel_id] - float(truth["sensor_map"][channel_id]))
        for channel_id in actual
    )


def maximum_relative_error(actual: list[float], expected: list[float]) -> float:
    return max(
        abs(float(actual[index]) - float(expected[index]))
        / max(1e-12, abs(float(expected[index])))
        for index in range(len(expected))
    )


def run_campaign(root: Path, campaign: str, *, timeout: float = 20.0) -> dict:
    job_path, state_dir, report_path = prepare_job(root, campaign)
    result, elapsed = candidate_run(job_path, state_dir, timeout=timeout)
    return {
        "process": result,
        "elapsed": elapsed,
        "job_path": job_path,
        "state_dir": state_dir,
        "report_path": report_path,
        "report_bytes": report_path.read_bytes() if report_path.exists() else b"",
        "report": read_json(report_path) if report_path.exists() else {},
    }


@pytest.fixture(scope="session")
def campaign_suite(tmp_path_factory):
    root = tmp_path_factory.mktemp("campaign_suite")
    shared_state = root / "shared_state"
    shared_state.mkdir()

    job_a, _, report_a = prepare_job(root / "run_a", "campaign_a")
    result_a, elapsed_a = candidate_run(job_a, shared_state, timeout=15.0)
    bytes_a = report_a.read_bytes()
    ledger_a = (shared_state / "line-a-42.jsonl").read_bytes()

    result_repeat, _ = candidate_run(job_a, shared_state, timeout=15.0)
    bytes_repeat = report_a.read_bytes()
    ledger_repeat = (shared_state / "line-a-42.jsonl").read_bytes()

    job_b, _, report_b = prepare_job(
        root / "run_b",
        "campaign_b",
        mutate=lambda item: item.__setitem__("state_key", "line-a-42"),
    )
    result_b, _ = candidate_run(job_b, shared_state, timeout=15.0)
    bytes_b = report_b.read_bytes()
    ledger_b = (shared_state / "line-a-42.jsonl").read_bytes()

    campaign_c = run_campaign(root / "run_c", "campaign_c", timeout=15.0)
    return {
        "a": {
            "process": result_a,
            "elapsed": elapsed_a,
            "job_path": job_a,
            "state_dir": shared_state,
            "report_path": report_a,
            "report_bytes": bytes_a,
            "report": json.loads(bytes_a),
            "ledger_bytes": ledger_a,
        },
        "a_repeat": {
            "process": result_repeat,
            "report_bytes": bytes_repeat,
            "ledger_bytes": ledger_repeat,
        },
        "b": {
            "process": result_b,
            "report_bytes": bytes_b,
            "report": json.loads(bytes_b),
            "ledger_bytes": ledger_b,
            "job_path": job_b,
            "state_dir": shared_state,
            "report_path": report_b,
        },
        "c": campaign_c,
    }


@pytest.fixture(scope="session")
def resolution_suite(tmp_path_factory):
    root = tmp_path_factory.mktemp("resolution_suite")
    coarse = run_campaign(
        root / "coarse",
        "campaign_a",
        timeout=40.0,
    )
    fine_job, fine_state, fine_report = prepare_job(
        root / "fine",
        "campaign_a",
        mutate=lambda item: item["controls"].__setitem__("fd_nodes", 96),
    )
    fine_result, _ = candidate_run(fine_job, fine_state, timeout=40.0)
    return {
        "coarse": coarse,
        "fine": {
            "process": fine_result,
            "report": read_json(fine_report) if fine_report.exists() else {},
        },
    }


@pytest.fixture(scope="session")
def fourier_boundary_suite(tmp_path_factory):
    root = tmp_path_factory.mktemp("fourier_boundary_suite")
    job_path, state_dir, report_path = prepare_job(
        root / "edge",
        "campaign_a",
        mutate=lambda item: (
            item["controls"].__setitem__("max_fourier", 0.30),
            item["controls"].__setitem__("fd_nodes", 48),
            item["rod"]["segments"][0].__setitem__("x1_m", 0.1450005),
            item["rod"]["segments"][1].__setitem__("x0_m", 0.1450005),
        ),
    )
    result, elapsed = candidate_run(job_path, state_dir, timeout=15.0)
    return {
        "process": result,
        "elapsed": elapsed,
        "report": read_json(report_path) if report_path.exists() else {},
        "report_bytes": report_path.read_bytes() if report_path.exists() else b"",
    }


@pytest.fixture
def case_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("case")


def failure_case(
    root: Path,
    *,
    campaign: str = "campaign_a",
    mutate=None,
) -> dict:
    job_path, state_dir, report_path = prepare_job(root, campaign, mutate=mutate)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    ledger = ledger_path(state_dir, job)
    result, elapsed = candidate_run(job_path, state_dir, timeout=5.0)
    return {
        "process": result,
        "elapsed": elapsed,
        "job_path": job_path,
        "state_dir": state_dir,
        "report_path": report_path,
        "ledger_path": ledger,
    }


def raw_failure_case(
    root: Path,
    *,
    old: bytes,
    new: bytes,
) -> dict:
    job_path, state_dir, report_path = prepare_job(root, "campaign_a")
    raw = job_path.read_bytes()
    job_path.write_bytes(raw.replace(old, new, 1))
    job = json.loads(job_path.read_text(encoding="utf-8"))
    ledger = ledger_path(state_dir, job)
    result, elapsed = candidate_run(job_path, state_dir, timeout=5.0)
    return {
        "process": result,
        "elapsed": elapsed,
        "job_path": job_path,
        "state_dir": state_dir,
        "report_path": report_path,
        "ledger_path": ledger,
    }


def second_replace_case(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    job_path, state_dir, report_path = prepare_job(root, "campaign_a")
    old_report = b"previous certified report\n"
    report_path.write_bytes(old_report)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    target = ledger_path(state_dir, job)
    target.mkdir()
    result, elapsed = candidate_run(job_path, state_dir, timeout=15.0)
    current_report = report_path.read_bytes()
    temporary = sorted(
        [
            path.name
            for path in state_dir.glob("*.tmp")
        ]
        + [
            path.name
            for path in report_path.parent.glob("*.tmp")
        ]
    )
    target_is_directory = target.is_dir()
    if target_is_directory:
        shutil.rmtree(target)
    return {
        "process": result,
        "elapsed": elapsed,
        "old_report": old_report,
        "current_report": current_report,
        "injected": True,
        "target_is_directory": target_is_directory,
        "temporary": temporary,
    }


def test_primary_campaign_is_certified(campaign_suite):
    """题面：「`status` 固定为 `certified`」。"""
    result = campaign_suite["a"]["process"]
    report = campaign_suite["a"]["report"]
    assert result.returncode == 0
    assert report["status"] == "certified"
    assert report["certificate"]["accepted"] is True
    assert report["consensus"]["rms_c"] <= 0.42


def test_campaign_a_recovers_unknown_sensor_positions(campaign_suite):
    """题面：「八支工艺通道的 `channel_id` 与位置没有对应关系」。"""
    report = campaign_suite["a"]["report"]
    truth = EXPECTED_TRUTH[report["job_id"]]
    error = maximum_map_error(report, truth)
    positions = [float(item["x_m"]) for item in report["sensor_map"]]
    assert error <= MAP_TOLERANCE
    assert positions == sorted(positions)


def test_campaign_a_recovers_piecewise_conductivity(campaign_suite):
    """题面：「三段 `k`、`h_L` 和 `h_R` 要从温度历程反演」。"""
    print("HARDCODE-BASE INPUT_SCENARIO_CAMPAIGN_A")
    report = campaign_suite["a"]["report"]
    truth = EXPECTED_TRUTH[report["job_id"]]
    actual = [item["k_w_mk"] for item in report["conductivity_w_mk"]]
    relative = maximum_relative_error(actual, truth["conductivity_w_mk"])
    assert relative <= 0.18
    assert len(report["conductivity_w_mk"]) == 3
    assert len(report["certificate"]["parameter_sha256"]) == 64


def test_campaign_a_recovers_both_boundary_coefficients(campaign_suite):
    """题面：「三段 `k`、`h_L` 和 `h_R` 要从温度历程反演」。"""
    report = campaign_suite["a"]["report"]
    truth = EXPECTED_TRUTH[report["job_id"]]
    left_error = abs(
        report["boundaries"]["left_h_w_m2k"] - truth["left_h_w_m2k"]
    ) / truth["left_h_w_m2k"]
    right_error = abs(
        report["boundaries"]["right_h_w_m2k"] - truth["right_h_w_m2k"]
    )
    assert left_error <= 0.18
    assert right_error <= 0.12


def test_campaign_a_removes_the_two_bad_channels(campaign_suite):
    """题面：「正好去掉数据里存在的坏通道」。"""
    report = campaign_suite["a"]["report"]
    truth = EXPECTED_TRUTH[report["job_id"]]
    outliers = report["consensus"]["outlier_channels"]
    assert outliers == truth["bad_channels"]
    assert report["consensus"]["support_count"] == 6


def test_campaign_b_recovers_a_second_material_profile(campaign_suite):
    """题面：「同一工况在改变空间节点数后仍要落在上面的参数与位置口径内」。"""
    print("HARDCODE-VARIANT INPUT_SCENARIO_CAMPAIGN_B")
    report = campaign_suite["b"]["report"]
    truth = EXPECTED_TRUTH[report["job_id"]]
    map_error = maximum_map_error(report, truth)
    conductivity_error = maximum_relative_error(
        [item["k_w_mk"] for item in report["conductivity_w_mk"]],
        truth["conductivity_w_mk"],
    )
    assert map_error <= MAP_TOLERANCE
    assert conductivity_error <= 0.18
    assert (
        report["certificate"]["parameter_sha256"]
        != campaign_suite["a"]["report"]["certificate"]["parameter_sha256"]
    )
    assert report["consensus"]["outlier_channels"] == truth["bad_channels"]


def test_campaign_c_removes_three_bad_channels(campaign_suite):
    """题面：「坏通道可能整段冻结、从第一个采样起带异常偏置，或有相同的冻结曲线」。"""
    report = campaign_suite["c"]["report"]
    truth = EXPECTED_TRUTH[report["job_id"]]
    assert report["consensus"]["outlier_channels"] == truth["bad_channels"]
    assert report["consensus"]["support_count"] == 5
    assert maximum_map_error(report, truth) <= MAP_TOLERANCE


def test_mapping_direction_and_distance_are_separate(campaign_suite):
    """题面：「不能用位置误差代替方向口径」。"""
    report = campaign_suite["a"]["report"]
    diagnostics = report["mapping_diagnostics"]
    assert diagnostics["direction_correlation"] <= -0.95
    assert diagnostics["distance_rms_m"] <= MAP_TOLERANCE
    assert diagnostics["direction_correlation"] != diagnostics["distance_rms_m"]


def test_adjacent_swap_margin_certifies_unique_mapping(campaign_suite):
    """题面：「`certificate.unique_margin` 不小于 `0.03` 摄氏度」。"""
    certificate = campaign_suite["a"]["report"]["certificate"]
    assert certificate["unique_margin"] >= 0.03
    assert certificate["unique_margin"] == campaign_suite["a"]["report"][
        "mapping_diagnostics"
    ]["rank_margin"]


def test_timestep_diagnostics_respect_fourier_limit(campaign_suite):
    """题面：「`timestep.max_fourier` 不超过 `controls.max_fourier`」。"""
    report = campaign_suite["a"]["report"]
    assert report["timestep"]["dt_s"] > 0.0
    assert report["timestep"]["max_fourier"] <= 0.90
    assert report["timestep"]["nodes"] == 72


def test_coarse_and_fine_spatial_grids_agree(resolution_suite):
    """题面：「同一工况在改变空间节点数后仍要落在上面的参数与位置口径内」。"""
    coarse = resolution_suite["coarse"]["report"]
    fine = resolution_suite["fine"]["report"]
    coarse_map = {
        item["channel_id"]: float(item["x_m"])
        for item in coarse["sensor_map"]
    }
    fine_map = {
        item["channel_id"]: float(item["x_m"])
        for item in fine["sensor_map"]
    }
    map_delta = max(
        abs(coarse_map[channel] - fine_map[channel])
        for channel in coarse_map
    )
    coarse_k = [item["k_w_mk"] for item in coarse["conductivity_w_mk"]]
    fine_k = [item["k_w_mk"] for item in fine["conductivity_w_mk"]]
    conductivity_delta = maximum_relative_error(fine_k, coarse_k)
    assert map_delta <= 0.02
    assert conductivity_delta <= 0.20


def test_campaign_meets_public_time_limit(campaign_suite):
    """题面：「`controls.max_seconds` 是单次工况从开始校验到发布完成的公开上限」。"""
    result, elapsed = candidate_run(
        campaign_suite["a"]["job_path"],
        campaign_suite["a"]["state_dir"],
        timeout=15.0,
    )
    assert result.returncode == 0
    assert elapsed < 12.0


def test_report_top_level_keys_are_exact(campaign_suite):
    """题面：「顶层键按下面顺序出现，键集恰好是这十一项」。"""
    report = campaign_suite["a"]["report"]
    expected = [
        "schema_version",
        "job_id",
        "status",
        "sensor_map",
        "conductivity_w_mk",
        "boundaries",
        "timestep",
        "consensus",
        "mapping_diagnostics",
        "certificate",
        "ledger",
    ]
    assert list(report) == expected


def test_report_nested_keys_are_exact(campaign_suite):
    """题面：「sensor_map[] = channel_id, x_m, support」。"""
    report = campaign_suite["a"]["report"]
    assert all(
        set(item) == {"channel_id", "x_m", "support"}
        for item in report["sensor_map"]
    )
    assert all(
        set(item) == {"segment", "x0_m", "x1_m", "k_w_mk"}
        for item in report["conductivity_w_mk"]
    )
    assert set(report["boundaries"]) == {
        "left_h_w_m2k",
        "right_h_w_m2k",
        "ambient_c",
    }
    assert set(report["timestep"]) == {"nodes", "dt_s", "max_fourier"}
    assert set(report["consensus"]) == {
        "good_channels",
        "outlier_channels",
        "rms_c",
        "support_count",
    }
    assert set(report["mapping_diagnostics"]) == {
        "distance_rms_m",
        "direction_correlation",
        "rank_margin",
    }
    assert set(report["certificate"]) == {
        "accepted",
        "support_count",
        "unique_margin",
        "parameter_sha256",
    }
    assert set(report["ledger"]) == {
        "state_key",
        "event_sha256",
        "sequence",
    }


def test_report_has_compact_bytes_and_single_newline(campaign_suite):
    """题面：「分隔符后不加空格，末尾恰好一个 LF」。"""
    raw = campaign_suite["a"]["report_bytes"]
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1
    assert b"\r" not in raw
    assert b", " not in raw
    assert b'": ' not in raw


def test_report_numbers_use_six_decimal_rounding(fourier_boundary_suite):
    """题面：「报告浮点数字最多保留六位小数，半位采用远离零的进位」。"""
    report = fourier_boundary_suite["report"]
    raw = fourier_boundary_suite["report_bytes"]
    decimal_tokens = re.findall(
        rb"(?<![A-Za-z0-9_])-?\d+\.(\d+)(?:[eE][+-]?\d+)?",
        raw,
    )
    assert report["conductivity_w_mk"][0]["x1_m"] == pytest.approx(
        0.145001,
        abs=1e-12,
    )
    assert all(len(token) <= 6 for token in decimal_tokens)
    assert b"-0.0" not in raw


def test_parameter_digest_matches_canonical_payload(campaign_suite):
    """题面：「`parameter_sha256` 的输入是紧凑 UTF-8 JSON，不含换行」。"""
    report = campaign_suite["a"]["report"]
    payload = {
        "sensor_map": report["sensor_map"],
        "conductivity_w_mk": report["conductivity_w_mk"],
        "boundaries": report["boundaries"],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    assert digest == report["certificate"]["parameter_sha256"]
    assert len(digest) == 64


def test_success_writes_nothing_to_stdout_or_stderr(campaign_suite):
    """题面：「一次认证通过后，调度端不会从 stdout 或 stderr 收到任何字符。」"""
    result = campaign_suite["a"]["process"]
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_missing_top_level_key_is_refused(case_dir):
    """题面：「缺键和多键都拒收」。"""
    data = failure_case(
        case_dir,
        mutate=lambda item: item.pop("controls"),
    )
    assert data["process"].returncode == 2
    assert data["process"].stderr.startswith("ERROR SCHEMA:")
    assert not data["report_path"].exists()
    assert not data["ledger_path"].exists()


def test_extra_top_level_key_is_refused(case_dir):
    """题面：「缺键和多键都拒收」。"""
    data = failure_case(
        case_dir,
        mutate=lambda item: item.__setitem__("debug", True),
    )
    assert data["process"].returncode == 2
    assert data["process"].stderr.startswith("ERROR SCHEMA:")
    assert not data["report_path"].exists()
    assert not data["ledger_path"].exists()


def test_duplicate_top_level_key_is_refused(case_dir):
    """题面：「对象中同名键重复时，无论位于哪一层都拒收」。"""
    data = raw_failure_case(
        case_dir,
        old=b"{",
        new=b'{"job_id":"JOB-LN42-7C81",',
    )
    assert data["process"].returncode == 2
    assert data["process"].stderr.startswith("ERROR DUPLICATE_KEY:")
    assert data["process"].stdout == ""
    assert not data["report_path"].exists()


def test_duplicate_nested_key_is_refused(case_dir):
    """题面：「对象中同名键重复时，无论位于哪一层都拒收」。"""
    data = raw_failure_case(
        case_dir,
        old=b'"rod":{',
        new=b'"rod":{"length_m":0.48,',
    )
    assert data["process"].returncode == 2
    assert data["process"].stderr.startswith("ERROR DUPLICATE_KEY:")
    assert data["process"].stdout == ""
    assert not data["ledger_path"].exists()


def test_wrong_numeric_type_is_refused(case_dir):
    """题面：「JSON 数字不含布尔、字符串、数组、对象、null、NaN 和无穷」。"""
    data = failure_case(
        case_dir,
        mutate=lambda item: item["controls"].__setitem__("fd_nodes", True),
    )
    assert data["process"].returncode == 2
    assert data["process"].stderr.startswith("ERROR TYPE:")
    assert not data["report_path"].exists()
    assert not data["ledger_path"].exists()


def test_relative_output_path_is_refused(case_dir):
    """题面：「output 不含反斜杠、空字节、\".\" 或 \"..\" 路径段」。"""
    data = failure_case(
        case_dir,
        mutate=lambda item: item.__setitem__("output", "relative/report.json"),
    )
    assert data["process"].returncode == 2
    assert data["process"].stderr.startswith("ERROR PATH:")
    assert data["process"].stdout == ""


def test_parent_traversal_path_is_refused(case_dir):
    """题面：「output 不含反斜杠、空字节、\".\" 或 \"..\" 路径段」。"""
    data = failure_case(
        case_dir,
        mutate=lambda item: item.__setitem__(
            "output",
            str(case_dir / "out" / ".." / "report.json"),
        ),
    )
    assert data["process"].returncode == 2
    assert data["process"].stderr.startswith("ERROR PATH:")
    assert not data["report_path"].exists()


def test_non_increasing_times_are_refused(case_dir):
    """题面：「times_s: 8 到 64 项；第一项 0；后续严格递增；末项不超过 7200 秒」。"""
    def duplicate_time(job):
        times = job["observations"]["times_s"]
        times[1] = times[0]

    data = failure_case(case_dir, mutate=duplicate_time)
    assert data["process"].returncode == 2
    assert data["process"].stderr.startswith("ERROR TIMES:")
    assert not data["report_path"].exists()

    def duplicate_position(job):
        references = job["observations"]["reference_channels"]
        references[1]["position_m"] = references[0]["position_m"]

    duplicate_data = failure_case(
        case_dir / "duplicate-position",
        mutate=duplicate_position,
    )
    assert duplicate_data["process"].returncode == 2
    assert duplicate_data["process"].stderr.startswith("ERROR REFERENCE_POSITION:")
    assert not duplicate_data["report_path"].exists()
    assert not duplicate_data["ledger_path"].exists()


def test_too_few_channels_are_refused(case_dir):
    """题面：「5 到 12 项」。"""
    data = failure_case(
        case_dir,
        mutate=lambda item: item["observations"].__setitem__(
            "channels",
            item["observations"]["channels"][:4],
        ),
    )
    assert data["process"].returncode == 2
    assert data["process"].stderr.startswith("ERROR CHANNELS:")
    assert not data["ledger_path"].exists()


def test_fourier_control_boundary_is_accepted(fourier_boundary_suite):
    """题面：「controls.max_fourier: 0.30 到 1.20」。"""
    case_id = "CTRL-FOURIER-ACCEPT-28"
    result = fourier_boundary_suite["process"]
    report = fourier_boundary_suite["report"]
    assert result.returncode == 0, case_id
    assert report["timestep"]["max_fourier"] <= 0.30
    assert report["timestep"]["dt_s"] > 0.0


def test_fourier_control_just_below_boundary_is_refused(case_dir):
    """题面：「controls.max_fourier: 0.30 到 1.20」。"""
    case_id = "CTRL-FOURIER-REJECT-29"
    data = failure_case(
        case_dir,
        mutate=lambda item: item["controls"].__setitem__("max_fourier", 0.299),
    )
    assert data["process"].returncode == 2, case_id
    assert data["process"].stderr.startswith("ERROR CONTROL_RANGE:")
    assert not data["report_path"].exists()


def test_invalid_job_has_no_report_or_ledger_side_effect(case_dir):
    """题面：「校验失败不能改报告或账本」。"""
    data = failure_case(
        case_dir,
        mutate=lambda item: item["observations"]["times_s"].__setitem__(1, 0.0),
    )
    assert data["process"].returncode == 2
    assert not data["report_path"].exists()
    assert not data["ledger_path"].exists()
    assert list(data["state_dir"].glob("*.tmp")) == []
    assert list(data["report_path"].parent.glob("*.tmp")) == []


def test_ledger_destination_failure_rolls_back_report(case_dir):
    """题面：「若新报告已经替换而账本替换随后失败，必须恢复报告旧字节」。"""
    data = second_replace_case(case_dir)
    assert data["process"].returncode == 2
    assert data["process"].stderr.startswith("ERROR PUBLISH:")
    assert data["injected"] is True
    assert data["current_report"] == data["old_report"]
    assert data["target_is_directory"] is True
    assert data["temporary"] == []


def test_error_uses_stderr_prefix_and_empty_stdout(case_dir):
    """题面：「标准输出保持为空。标准错误只写一行：」。"""
    data = failure_case(
        case_dir,
        mutate=lambda item: item.__setitem__("schema_version", "wrong/v1"),
    )
    assert data["process"].returncode == 2
    assert data["process"].stdout == ""
    assert data["process"].stderr.startswith("ERROR SCHEMA_VERSION:")
    assert data["process"].stderr.count("\n") == 1
    assert "traceback" not in data["process"].stderr


def test_repeat_run_keeps_report_and_ledger_bytes(campaign_suite):
    """题面：「相同工况连续运行两次，报告字节完全相同，账本也不增加重复行」。"""
    assert campaign_suite["a"]["process"].returncode == 0
    assert campaign_suite["a_repeat"]["process"].returncode == 0
    assert campaign_suite["a"]["report_bytes"] == campaign_suite["a_repeat"]["report_bytes"]
    assert campaign_suite["a"]["ledger_bytes"] == campaign_suite["a_repeat"]["ledger_bytes"]
    assert len(campaign_suite["a"]["ledger_bytes"].splitlines()) == 1


def test_new_event_appends_one_ledger_line(campaign_suite):
    """题面：「不同事件按首次成功发布的先后追加」。"""
    first = campaign_suite["a"]["ledger_bytes"]
    after_b = campaign_suite["b"]["ledger_bytes"]
    first_entries = [json.loads(line) for line in first.splitlines()]
    all_entries = [json.loads(line) for line in after_b.splitlines()]
    assert len(first_entries) == 1
    assert len(all_entries) == 2
    assert after_b.splitlines()[0] == first.splitlines()[0]
    assert [item["sequence"] for item in all_entries] == [1, 2]


def test_ledger_event_digest_matches_public_formula(campaign_suite):
    """题面：「`event_sha256` 按“工况文件原始字节后接 `parameter_sha256` 的 32 字节原值”计算」。"""
    campaign = campaign_suite["a"]
    raw_job = campaign["job_path"].read_bytes()
    raw_report = campaign["report_bytes"]
    report = campaign["report"]
    first_entry = json.loads(campaign["ledger_bytes"].splitlines()[0])
    parameter_bytes = bytes.fromhex(report["certificate"]["parameter_sha256"])
    event_hash = hashlib.sha256(raw_job + parameter_bytes).hexdigest()
    job_hash = hashlib.sha256(raw_job).hexdigest()
    report_hash = hashlib.sha256(raw_report).hexdigest()
    assert first_entry["event_sha256"] == event_hash
    assert first_entry["job_sha256"] == job_hash
    assert first_entry["report_sha256"] == report_hash
    assert report["ledger"]["event_sha256"] == event_hash

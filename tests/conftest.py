from __future__ import annotations

import json
from pathlib import Path


_OUTCOMES: dict[str, str] = {}
_CASE_MAP = json.loads(
    (Path(__file__).resolve().parent / "case-map.json").read_text(encoding="utf-8")
)
_OUTCOME_PRIORITY = {
    "passed": 0,
    "skipped": 1,
    "failed": 2,
}


def pytest_runtest_logreport(report):
    if report.when not in {"setup", "call"}:
        return
    outcome = report.outcome
    if outcome not in _OUTCOME_PRIORITY:
        return
    previous = _OUTCOMES.get(report.nodeid)
    if previous is None or _OUTCOME_PRIORITY[outcome] > _OUTCOME_PRIORITY[previous]:
        _OUTCOMES[report.nodeid] = outcome


def pytest_sessionfinish(session, exitstatus):
    print()
    for nodeid in sorted(_OUTCOMES):
        test_name = nodeid.split("::", 1)[-1].split("[", 1)[0]
        behavior_id = _CASE_MAP.get(test_name)
        status = _OUTCOMES[nodeid].upper()
        suffix = f" [{behavior_id}]" if behavior_id else ""
        print(f"test_outputs.py::{test_name} {status}{suffix}")

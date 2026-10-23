#!/bin/bash
set -euo pipefail

LOG_DIR=/logs/verifier
REWARD_FILE="$LOG_DIR/reward.txt"
STATUS_FILE="$LOG_DIR/verification-status.txt"
TEST_LOG="$LOG_DIR/test-stdout.txt"
REPORT_FILE="$LOG_DIR/pytest-results.xml"
TOOLS_DIR=/tmp/ly-005-verifier-tools
completed=0

mkdir -p "$LOG_DIR"

write_reward() {
    chmod 0644 "$REWARD_FILE" 2>/dev/null || true
    printf '%s\n' "$1" > "$LOG_DIR/reward.txt"
    chmod 0444 "$REWARD_FILE"
}

write_status() {
    chmod 0644 "$STATUS_FILE" 2>/dev/null || true
    printf '%s\n' "$1" > "$STATUS_FILE"
    chmod 0444 "$STATUS_FILE"
}

on_exit() {
    if [ "$completed" -ne 1 ]; then
        write_status "INCOMPLETE: verifier exited before result publication"
        write_reward 0.000
    fi
}

on_signal() {
    write_status "INCOMPLETE: verifier interrupted before result publication"
    write_reward 0.000
    completed=1
    exit 0
}

write_reward 0.000
write_status "INCOMPLETE: pytest has not completed"
trap on_exit EXIT
trap on_signal INT TERM HUP

rm -f "$REPORT_FILE" "$TEST_LOG"
export PYTHONDONTWRITEBYTECODE=1
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export PYTEST_ADDOPTS=""
export PIP_DISABLE_PIP_VERSION_CHECK=1

rm -rf "$TOOLS_DIR"
python3 -m pip install \
    --no-cache-dir \
    --no-compile \
    --no-index \
    --find-links=/tests/wheels \
    --target="$TOOLS_DIR" \
    pytest==8.4.1

set +e
PYTHONPATH="$TOOLS_DIR" timeout --signal=TERM --kill-after=10s 180s \
    python3 -u -m pytest \
    -p no:cacheprovider \
    --junitxml="$REPORT_FILE" \
    /tests/test_outputs.py > "$TEST_LOG" 2>&1
pytest_rc=$?
set -e

cat "$TEST_LOG" 2>/dev/null || true

if [ "$pytest_rc" -ne 0 ] && [ "$pytest_rc" -ne 1 ]; then
    case "$pytest_rc" in
        124|125|126|127|137|143)
            write_status "INCOMPLETE: pytest did not complete; runner exited with code $pytest_rc"
            ;;
        *)
            write_status "INCOMPLETE: pytest exited before a scoreable report with code $pytest_rc"
            ;;
    esac
    write_reward 0.000
elif [ ! -s "$REPORT_FILE" ]; then
    write_status "INCOMPLETE: pytest completion report missing or empty"
    write_reward 0.000
else
    if python3 /tests/score.py \
        --config /tests/scoring.json \
        --junit "$REPORT_FILE" \
        --report-out "$LOG_DIR/scoring-report.json" \
        --reward-out "$REWARD_FILE"; then
        reward="$(cat "$REWARD_FILE")"
        case "$reward" in
            0.[0-9][0-9][0-9]|1.000)
                chmod 0444 "$REWARD_FILE"
                write_status "COMPLETED: weighted scoring finished with reward=$reward"
                ;;
            *)
                write_status "INCOMPLETE: weighted scorer produced an invalid reward"
                write_reward 0.000
                ;;
        esac
    else
        write_status "INCOMPLETE: weighted scorer rejected the pytest report"
        write_reward 0.000
    fi
fi

completed=1
exit 0

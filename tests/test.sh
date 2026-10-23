#!/bin/bash
set -euo pipefail

LOG_DIR=/logs/verifier
REWARD_FILE="$LOG_DIR/reward.txt"
STATUS_FILE="$LOG_DIR/verification-status.txt"
TEST_LOG="$LOG_DIR/test-stdout.txt"
REPORT="$LOG_DIR/pytest-results.xml"
TMP_ROOT=/tmp/ly005-verifier
TOOLS_DIR="$TMP_ROOT/pytest-tools"
completed=0

mkdir -p "$LOG_DIR" "$TMP_ROOT"
chmod 0777 "$TMP_ROOT"

write_reward() {
    chmod 0644 "$REWARD_FILE" 2>/dev/null || true
    printf '%s\n' "$1" > "$REWARD_FILE"
    chmod 0444 "$REWARD_FILE"
}

write_status() {
    chmod 0644 "$STATUS_FILE" 2>/dev/null || true
    printf '%s\n' "$1" > "$STATUS_FILE"
    chmod 0444 "$STATUS_FILE"
}

cleanup() {
    rm -rf "$TMP_ROOT"
}

on_exit() {
    cleanup
    if [ "$completed" -ne 1 ]; then
        write_status "INCOMPLETE: verifier exited before result publication"
        write_reward 0.000
    fi
}

on_signal() {
    cleanup
    write_status "INCOMPLETE: verifier interrupted before result publication"
    write_reward 0.000
    completed=1
    exit 0
}

trap on_exit EXIT
trap on_signal INT TERM HUP
write_reward 0.000
write_status "INCOMPLETE: pytest has not completed"
rm -rf "$TOOLS_DIR"
rm -f "$REPORT" "$TEST_LOG" "$LOG_DIR/scoring-report.json"

if ! /usr/local/bin/python3 -m pip install \
    --no-cache-dir \
    --no-compile \
    --no-index \
    --find-links=/tests/wheels \
    --target="$TOOLS_DIR" \
    pytest==8.4.1 > "$LOG_DIR/pytest-install.txt" 2>&1; then
    write_status "INCOMPLETE: verifier dependency installation failed"
    write_reward 0.000
    completed=1
    exit 0
fi

set +e
/usr/bin/timeout --signal=TERM --kill-after=10s 180s \
    /usr/bin/env -i \
    PATH=/usr/local/bin:/usr/bin:/bin \
    HOME=/tmp \
    TMPDIR="$TMP_ROOT" \
    TASK_VERIFIER_TMP="$TMP_ROOT" \
    TASK_APP=/tests/trusted_candidate.py \
    TASK_CANDIDATE=/app/resolve_rod.py \
    TASK_DATA_DIR=/app/data \
    PYTHONNOUSERSITE=1 \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="$TOOLS_DIR:/tests" \
    /usr/local/bin/python3 -S -s -P -B -m pytest \
    -p no:cacheprovider \
    --junitxml="$REPORT" \
    /tests/test_outputs.py > "$TEST_LOG" 2>&1
pytest_rc=$?
set -e

cat "$TEST_LOG" 2>/dev/null || true

if [ "$pytest_rc" -ne 0 ] && [ "$pytest_rc" -ne 1 ]; then
    write_status "INCOMPLETE: pytest did not complete; runner exited with code $pytest_rc"
    write_reward 0.000
elif [ ! -s "$REPORT" ]; then
    write_status "INCOMPLETE: pytest completion report missing or empty"
    write_reward 0.000
elif ! /usr/local/bin/python3 /tests/score.py \
    --config /tests/scoring.json \
    --junit "$REPORT" \
    --report-out "$LOG_DIR/scoring-report.json" \
    --reward-out "$REWARD_FILE"; then
    write_status "INCOMPLETE: weighted scorer rejected the pytest report"
    write_reward 0.000
else
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
fi

completed=1
exit 0

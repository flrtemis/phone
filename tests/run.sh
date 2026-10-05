#!/usr/bin/env bash
# Run every test suite. Nothing here needs root, a SIP provider, or network
# access; the firewall suite runs in --dry-run, the SIP suite uses a stub
# pjsua, and the e2e suite binds only to loopback.
#
#   ./tests/run.sh              everything
#   PHONE_PYTHON=/usr/bin/python3 ./tests/run.sh
#   ./tests/run.sh python       only the Python suites

set -uo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PHONE_PYTHON:-python3}"
ONLY="${1:-all}"

FAILED_SUITES=()
TOTAL_PASS=0
TOTAL_FAIL=0

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    B=$'\033[1m'; D=$'\033[2m'; R=$'\033[31m'; G=$'\033[32m'; N=$'\033[0m'
else
    B=""; D=""; R=""; G=""; N=""
fi

banner() { printf '\n%s== %s ==%s\n' "$B" "$1" "$N"; }

summary_line() { # suite, exit code
    if [ "$2" -eq 0 ]; then
        printf '%s  PASS%s %s\n' "$G" "$N" "$1"
    else
        printf '%s  FAIL%s %s\n' "$R" "$N" "$1"
        FAILED_SUITES+=("$1")
    fi
}

if ! command -v "$PY" >/dev/null 2>&1; then
    echo "error: $PY not found (set PHONE_PYTHON)" >&2
    exit 2
fi

PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
printf '%spython%s %s (%s)\n' "$B" "$N" "$PYVER" "$("$PY" -c 'import sys;print(sys.executable)')"
"$PY" - <<'EOF' || { echo "error: Python 3.8 or newer is required" >&2; exit 2; }
import sys
sys.exit(0 if sys.version_info >= (3, 8) else 1)
EOF

# --------------------------------------------------------------------------
if [ "$ONLY" = "all" ] || [ "$ONLY" = "python" ]; then
    banner "python: spool, daemon, poll, cli"
    if "$PY" -c 'import pytest' >/dev/null 2>&1; then
        ( cd "$REPO_DIR" && "$PY" -m pytest tests/ -q -p no:cacheprovider )
        rc=$?
        summary_line "pytest (spool/daemon/poll/cli)" "$rc"
    else
        printf '%s  SKIP%s pytest not installed for %s\n' "$D" "$N" "$PY"
        printf '       install it with: %s -m pip install pytest\n' "$PY"
    fi
fi

# --------------------------------------------------------------------------
if [ "$ONLY" = "all" ] || [ "$ONLY" = "shell" ]; then
    banner "shell: sip config + launcher"
    bash "$REPO_DIR/tests/test_sip.sh"
    summary_line "sip (config linter + launcher refusals)" "$?"

    banner "shell: firewall ruleset"
    bash "$REPO_DIR/tests/test_firewall.sh"
    summary_line "firewall (policy shape + refusals)" "$?"

    banner "shell: end to end"
    PHONE_PYTHON="$PY" bash "$REPO_DIR/tests/test_e2e.sh"
    summary_line "e2e (HTTP -> spool file -> CLI)" "$?"
fi

# --------------------------------------------------------------------------
banner "summary"
if [ "${#FAILED_SUITES[@]}" -eq 0 ]; then
    printf '%sall suites passed%s\n' "$G" "$N"
    exit 0
fi
printf '%sfailed:%s %s\n' "$R" "$N" "${FAILED_SUITES[*]}"
exit 1

#!/usr/bin/env bash
# End-to-end: start the real daemon, deliver a real webhook over TCP, then
# read the message back through the real CLI. No mocks, no fixtures.
#
# This is the test that would have caught a broken wire format: it asserts the
# provider payload -> HTTP -> file -> CLI path produces exactly the text that
# was sent, and that a replayed webhook does not produce a second file.

set -uo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PHONE_PYTHON:-python3}"
TMP="$(mktemp -d)"
DAEMON_PID=""
cleanup() { [ -n "$DAEMON_PID" ] && kill "$DAEMON_PID" 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; }
check(){ if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1" "${3:-}"; fi; }

command -v curl >/dev/null 2>&1 || { echo "curl not installed: skipping e2e" ; exit 0; }

SPOOL="$TMP/spool"
CONF="$TMP/sms.conf"
PORT=$((20000 + RANDOM % 20000))

cat > "$CONF" <<EOF
[daemon]
host = 127.0.0.1
port = $PORT
path = /sms/incoming
[spool]
dir = $SPOOL
[fields]
sender = from,msisdn,sender
body = text,body
EOF

py() { PYTHONPATH="$REPO_DIR" "$PY" -m "$@"; }

printf 'starting daemon on 127.0.0.1:%s\n' "$PORT"
PYTHONPATH="$REPO_DIR" "$PY" -m sms.daemon --config "$CONF" > "$TMP/daemon.log" 2>&1 &
DAEMON_PID=$!

up=0
for _ in $(seq 1 50); do
    if curl -fsS --max-time 1 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then up=1; break; fi
    sleep 0.1
done
if [ "$up" -ne 1 ]; then
    bad "daemon came up" "$(tail -3 "$TMP/daemon.log")"
    printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
    exit 1
fi
ok "daemon came up"

# ---- a message from a fictional provider ---------------------------------
# The expected body, written with a literal em dash: this bash's $'...' does
# not expand \uXXXX, and the point of the test is that the JSON \u2014 escape
# in the payload arrives as real UTF-8 text on disk.
BODY='Hello from the terminal.
Second line, with "quotes" and an em dash — ok.'
PAYLOAD="$(cat <<EOF
{"from": "+15550109999", "text": "Hello from the terminal.\nSecond line, with \"quotes\" and an em dash \\u2014 ok.", "message_id": "SM-e2e-1", "timestamp": "2026-10-05T12:00:00Z"}
EOF
)"

status="$(curl -s -o "$TMP/resp1.json" -w '%{http_code}' -X POST \
    -H 'Content-Type: application/json' --data-binary "$PAYLOAD" \
    "http://127.0.0.1:$PORT/sms/incoming")"
check "webhook accepted (201)" '[ "'"$status"'" = "201" ]' "$(cat "$TMP/resp1.json")"
check "response carries an id" 'grep -q "\"id\"" "$TMP/resp1.json"'

check "exactly one message file exists" '[ "$(find "$SPOOL" -name "*.txt" | wc -l)" -eq 1 ]'
MSG_FILE="$(find "$SPOOL" -name '*.txt' | head -1)"
check "file is mode 600" '[ "$(stat -c %a "$MSG_FILE")" = "600" ]'
check "spool directory is mode 700" '[ "$(stat -c %a "$SPOOL")" = "700" ]'
check "sender survived the round trip" 'grep -q "^FROM: +15550109999$" "$MSG_FILE"'
# Everything after the BODY: marker must equal the body that was sent, byte
# for byte - including the embedded newline and the non-ASCII dash.
"$PY" - "$MSG_FILE" "$BODY" <<'PYEOF' >/dev/null 2>&1
import sys, pathlib
text = pathlib.Path(sys.argv[1]).read_text()
expected = sys.argv[2]
body = text.split("\nBODY:\n", 1)[1]
if body.endswith("\n"):
    body = body[:-1]
sys.exit(0 if body == expected else 1)
PYEOF
body_rc=$?
check "body survived the round trip byte for byte" '[ '"$body_rc"' -eq 0 ]'

# ---- CLI reads it back ---------------------------------------------------
check "phone sms list finds it" 'PYTHONPATH=$REPO_DIR "$PY" -m sms.cli --config "$CONF" list | grep -q "+15550109999"'
check "phone sms read latest prints the body" \
    'PYTHONPATH=$REPO_DIR "$PY" -m sms.cli --config "$CONF" read latest | grep -q "Hello from the terminal."'
check "phone sms grep matches the body" \
    'PYTHONPATH=$REPO_DIR "$PY" -m sms.cli --config "$CONF" grep "em dash" >/dev/null'
check "phone sms grep exits 1 on no match" \
    'PYTHONPATH=$REPO_DIR "$PY" -m sms.cli --config "$CONF" grep "no-such-string-anywhere" >/dev/null; [ $? -eq 1 ]'
check "phone sms verify passes" 'PYTHONPATH=$REPO_DIR "$PY" -m sms.cli --config "$CONF" verify'
check "phone sms export produces valid JSON lines" \
    'PYTHONPATH=$REPO_DIR "$PY" -m sms.cli --config "$CONF" export --format jsonl | "$PY" -c "import json,sys; [json.loads(l) for l in sys.stdin]"'

# ---- replay protection ---------------------------------------------------
status2="$(curl -s -o "$TMP/resp2.json" -w '%{http_code}' -X POST \
    -H 'Content-Type: application/json' --data-binary "$PAYLOAD" \
    "http://127.0.0.1:$PORT/sms/incoming")"
check "replayed webhook is recognised as a duplicate" \
    '[ "'"$status2"'" = "200" ] && grep -q duplicate "$TMP/resp2.json"'
check "replay did not create a second file" '[ "$(find "$SPOOL" -name "*.txt" | wc -l)" -eq 1 ]'

# ---- journal holds metadata only ----------------------------------------
check "journal records the arrival" 'grep -q "+15550109999" "$SPOOL/journal.log"'
check "journal never records the body" '! grep -q "Hello from the terminal" "$SPOOL/journal.log"'
check "daemon log never records the body" '! grep -q "Hello from the terminal" "$TMP/daemon.log"'

# ---- auth is enforced when a token is set --------------------------------
kill "$DAEMON_PID" 2>/dev/null; wait "$DAEMON_PID" 2>/dev/null; DAEMON_PID=""
cat >> "$CONF" <<EOF

[auth]
token = e2e-secret-token
EOF
PYTHONPATH="$REPO_DIR" "$PY" -m sms.daemon --config "$CONF" > "$TMP/daemon2.log" 2>&1 &
DAEMON_PID=$!
for _ in $(seq 1 50); do
    curl -fsS --max-time 1 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
    sleep 0.1
done
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
    --data '{"text":"intruder"}' "http://127.0.0.1:$PORT/sms/incoming")"
check "unauthenticated webhook is rejected (401)" '[ "'"$code"'" = "401" ]'
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
    -H 'X-Phone-Token: e2e-secret-token' --data '{"text":"authenticated"}' \
    "http://127.0.0.1:$PORT/sms/incoming")"
check "authenticated webhook is accepted (201)" '[ "'"$code"'" = "201" ]'
check "the rejected message was not written" '! grep -rq "intruder" "$SPOOL"'

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]

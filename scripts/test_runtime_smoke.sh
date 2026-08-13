#!/usr/bin/env bash
set -euo pipefail

binary="${1:-/usr/bin/harboros-im-gate}"
[[ -x "$binary" ]] || { echo "error: Gate binary is not executable" >&2; exit 2; }

install -d -m 0700 /data/harboros/secrets
cat > /data/harboros/secrets/beacon-gate.env <<'EOF'
HARBORGATE_BEARER_TOKEN=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
IM_AGENT_SERVICE_TOKEN=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
HARBORBEACON_WEB_API_TOKEN=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
EOF
chmod 0600 /data/harboros/secrets/beacon-gate.env
set -a
. /data/harboros/secrets/beacon-gate.env
set +a
export IM_AGENT_HOST=127.0.0.1
export IM_AGENT_PORT=8787
export IM_AGENT_CONTRACT_VERSION=2.0
export IM_AGENT_DATA_DIR=/data/harborgate/sessions
export IM_AGENT_STATE_DIR=/data/harborgate
export WEIXIN_STATE_DIR=/data/harborgate/weixin
export FEISHU_MAIL_TOKEN_STATE_PATH=/data/harborgate/feishu-mail-token.json
export HARBORBEACON_WEB_API_URL=http://127.0.0.1:4174

log="$(mktemp)"
pid=""
cleanup() {
  if [[ -n "$pid" ]]; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  rm -f -- "$log"
}
trap cleanup EXIT

"$binary" >"$log" 2>&1 &
pid=$!
python3 - <<'PY'
import json
import time
import urllib.request

for _ in range(100):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8787/health", timeout=0.2) as response:
            payload = json.load(response)
        assert payload["status"] == "ok"
        assert payload["runtime"] == "rust"
        break
    except Exception:
        time.sleep(0.05)
else:
    raise SystemExit("Gate health endpoint did not become ready")
PY
grep -Eq '127\.0\.0\.1:8787[[:space:]]' < <(ss -ltn)
! grep -Eq '(0\.0\.0\.0|\[::\]):8787[[:space:]]' < <(ss -ltn)
kill "$pid"
wait "$pid" || true
pid=""

if IM_AGENT_HOST=0.0.0.0 timeout 5 "$binary" >"$log" 2>&1; then
  echo "error: Gate accepted a non-loopback listener" >&2
  exit 1
fi
grep -Fq 'IM_AGENT_HOST must be loopback-only' "$log"

if IM_AGENT_SERVICE_TOKEN= timeout 5 "$binary" >"$log" 2>&1; then
  echo "error: Gate accepted an empty ingress bearer" >&2
  exit 1
fi
grep -Fq 'IM_AGENT_SERVICE_TOKEN must be a 32-byte lowercase hex credential' "$log"

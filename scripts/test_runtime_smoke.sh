#!/usr/bin/env bash
set -euo pipefail

binary="${1:-/usr/bin/harboros-im-gate}"
[[ -x "$binary" ]] || { echo "error: Gate binary is not executable" >&2; exit 2; }

credentials_dir="$(mktemp -d)"
chmod 0700 "$credentials_dir"
install -m 0600 /etc/harboros/service-auth/gate-to-beacon.send "$credentials_dir/gate-to-beacon-send"
install -m 0600 /etc/harboros/service-auth/beacon-to-gate.accept-current "$credentials_dir/beacon-to-gate-accept-current"
# A fixture-only previous token exercises rotation without changing installed credentials.
printf '%064d\n' 1 > "$credentials_dir/beacon-to-gate-accept-previous"
chmod 0600 "$credentials_dir/beacon-to-gate-accept-previous"
export CREDENTIALS_DIRECTORY="$credentials_dir"
unset HARBORGATE_BEARER_TOKEN IM_AGENT_SERVICE_TOKEN HARBORBEACON_WEB_API_TOKEN
unset HARBOR_BEACON_TO_GATE_TOKEN HARBOR_BEACON_TO_GATE_TOKEN_PREVIOUS HARBOR_GATE_TO_BEACON_TOKEN
export HARBORGATE_RUNTIME_PROFILE=k3
export IM_AGENT_HOST=127.0.0.1
export IM_AGENT_PORT=8787
export IM_AGENT_CONTRACT_VERSION=2.0
export IM_AGENT_DATA_DIR=/data/harborgate/sessions
export IM_AGENT_STATE_DIR=/data/harborgate
export HARBORGATE_DEVICE_SESSION_STATE_DIR=/data/harborgate/device-sessions
export WEIXIN_STATE_DIR=/data/harborgate/weixin
export FEISHU_MAIL_TOKEN_STATE_PATH=/data/harborgate/feishu-mail-token.json
export HARBORBEACON_WEB_API_URL=http://127.0.0.1:4174
: "${HARBOR_WORKSPACE_ID:?K3 smoke requires the HarborOS Home environment}"

log="$(mktemp)"
pid=""
cleanup() {
  if [[ -n "$pid" ]]; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  rm -f -- "$log"
  rm -rf -- "$credentials_dir"
}
trap cleanup EXIT

if env -u HARBOR_WORKSPACE_ID timeout 5 "$binary" >"$log" 2>&1; then
  echo "error: Gate accepted a missing K3 Home" >&2
  exit 1
fi
grep -Fq 'HARBOR_WORKSPACE_ID must contain the authoritative Home ID' "$log"

"$binary" >"$log" 2>&1 &
pid=$!
python3 - <<'PY'
import json
import os
from pathlib import Path
import time
import urllib.error
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

credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
for path in ["/api/gateway/status", "/api/harbor-gate/api/gateway/status"]:
    for name in ["beacon-to-gate-accept-current", "beacon-to-gate-accept-previous", "gate-to-beacon-send", None]:
        accepted = name in {"beacon-to-gate-accept-current", "beacon-to-gate-accept-previous"}
        headers = {"X-Contract-Version": "2.0"}
        if name:
            headers["Authorization"] = "Bearer " + (credentials / name).read_text().strip()
        request = urllib.request.Request("http://127.0.0.1:8787" + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                assert accepted
                assert response.status == 200
        except urllib.error.HTTPError as error:
            assert not accepted
            assert error.code == 401
            assert json.load(error)["error"]["code"] == "SERVICE_AUTH_FAILED"
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

: > "$credentials_dir/beacon-to-gate-accept-current"
if timeout 5 "$binary" >"$log" 2>&1; then
  echo "error: Gate accepted an empty ingress bearer" >&2
  exit 1
fi
grep -Fq 'HARBOR_BEACON_TO_GATE_TOKEN is missing or malformed' "$log"

#!/usr/bin/env bash
set -euo pipefail

[[ "$#" -eq 1 ]] || { echo "usage: $0 PACKAGE.deb" >&2; exit 2; }
artifact="$1"
[[ -f "$artifact" ]] || { echo "error: package not found: $artifact" >&2; exit 2; }

depends="$(dpkg-deb --field "$artifact" Depends)"
grep -Fq 'harboros-system (>= 0.1.0+harbornavi.k3.evt1)' <<<"$depends"
grep -Fq 'harboros-system (<< 0.2)' <<<"$depends"

fixture="$(mktemp -d)"
trap 'rm -rf -- "$fixture"' EXIT
install -d "$fixture/system/DEBIAN" "$fixture/system/usr/lib/systemd/system"
cat > "$fixture/system/DEBIAN/control" <<'EOF'
Package: harboros-system
Version: 0.1.0+harbornavi.k3.evt1
Architecture: all
Maintainer: Harbor package test
Description: Test-only dependency fixture for HarborGate lifecycle CI
EOF
cat > "$fixture/system/usr/lib/systemd/system/harboros-bootstrap.service" <<'EOF'
[Service]
Type=oneshot
ExecStart=/bin/true
EOF
dpkg-deb --build --root-owner-group "$fixture/system" "$fixture/harboros-system.deb" >/dev/null
dpkg --unpack "$fixture/harboros-system.deb"
dpkg --configure harboros-system

dpkg --unpack "$artifact"
dpkg --configure harboros-im-gate
test -x /usr/bin/harboros-im-gate
test -f /usr/lib/systemd/system/harboros-im-gate.service
test -x /usr/lib/harborgate/ensure-data-layout
test -f /usr/share/harboros/component-contracts/harboros-im-gate.json
test -f /usr/share/doc/harboros-im-gate/k3-runtime-evidence-required.json
test -f /usr/share/doc/harboros-im-gate/first-party-rights-approval.json
test -f /usr/share/doc/harboros-im-gate/third-party-licenses.json
test "$(stat -c '%a' /data/harborgate)" = "700"
grep -Fq 'Environment=IM_AGENT_HOST=127.0.0.1' \
  /usr/lib/systemd/system/harboros-im-gate.service
grep -Fq 'Requires=harboros-bootstrap.service' \
  /usr/lib/systemd/system/harboros-im-gate.service
grep -Fq 'EnvironmentFile=/data/harboros/secrets/beacon-gate.env' \
  /usr/lib/systemd/system/harboros-im-gate.service
! grep -Fq '/etc/default/harboros-im-gate' \
  /usr/lib/systemd/system/harboros-im-gate.service
python3 -m json.tool /usr/share/harboros/component-contracts/harboros-im-gate.json >/dev/null
python3 -m json.tool /usr/share/doc/harboros-im-gate/k3-runtime-evidence-required.json >/dev/null
python3 -m json.tool /usr/share/doc/harboros-im-gate/first-party-rights-approval.json >/dev/null
python3 -m json.tool /usr/share/doc/harboros-im-gate/third-party-licenses.json >/dev/null
bash ./scripts/test_runtime_smoke.sh

# Reinstalling the exact release exercises the package upgrade path without
# inventing a second package version outside the frozen EVT package set.
dpkg --unpack "$artifact"
dpkg --configure harboros-im-gate
dpkg --remove harboros-im-gate
test ! -e /usr/bin/harboros-im-gate
test -d /data/harborgate
dpkg --remove harboros-system

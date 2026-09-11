#!/usr/bin/env bash
set -euo pipefail

deb_path="${1:?usage: verify_harboros_im_gate_deb.sh <deb-path>}"
if [ ! -f "$deb_path" ]; then
  echo "deb package not found: $deb_path" >&2
  exit 1
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

bash -n debian/postinst
bash -n debian/prerm

dpkg-deb --contents "$deb_path" >"$tmp_dir/contents.txt"
dpkg-deb --control "$deb_path" "$tmp_dir/control"
dpkg-deb --fsys-tarfile "$deb_path" | tar -tf - >"$tmp_dir/files.txt"

grep -F "Package: harboros-im-gate" "$tmp_dir/control/control" >/dev/null
grep -F "Architecture: amd64" "$tmp_dir/control/control" >/dev/null
grep -E '(^|/)usr/bin/harboros-im-gate$' "$tmp_dir/files.txt" >/dev/null
grep -E '(^|/)etc/systemd/system/harboros-im-gate.service$' "$tmp_dir/files.txt" >/dev/null

grep -F "Environment=IM_AGENT_CONTRACT_VERSION=2.0" debian/harboros-im-gate.service >/dev/null
grep -F "Environment=IM_AGENT_HOST=127.0.0.1" debian/harboros-im-gate.service >/dev/null
grep -F "Environment=IM_AGENT_PORT=8787" debian/harboros-im-gate.service >/dev/null
grep -F "Environment=HARBORBEACON_WEB_API_URL=http://127.0.0.1:4174" debian/harboros-im-gate.service >/dev/null

sha256sum "$deb_path" >"${deb_path}.sha256"
echo "Verified HarborGate deb package: $deb_path"

#!/usr/bin/env bash
set -euo pipefail

: "${HARBORGATE_DEBIAN_SNAPSHOT:?HARBORGATE_DEBIAN_SNAPSHOT is required}"
[[ "$HARBORGATE_DEBIAN_SNAPSHOT" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || {
  echo "error: invalid Debian snapshot timestamp" >&2
  exit 2
}

rm -f /etc/apt/sources.list.d/debian.sources
cat > /etc/apt/sources.list <<EOF
deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/${HARBORGATE_DEBIAN_SNAPSHOT}/ bookworm main
deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/${HARBORGATE_DEBIAN_SNAPSHOT}/ bookworm-updates main
deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/${HARBORGATE_DEBIAN_SNAPSHOT}/ bookworm-security main
EOF
printf '%s\n' 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99harborgate-snapshot

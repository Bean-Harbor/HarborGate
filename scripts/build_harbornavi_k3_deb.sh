#!/usr/bin/env bash
set -euo pipefail

# Preserve the earlier K3 entrypoint while using the audited package builder.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export RUST_TARGET="${RUST_TARGET:-${TARGET:-riscv64gc-unknown-linux-gnu}}"
export DEB_ARCH="${DEB_ARCH:-riscv64}"
export DEBIAN_VERSION="${DEBIAN_VERSION:-${VERSION:-}}"
export OUT_DIR="${OUT_DIR:-artifacts/k3}"
exec bash "$repo_root/scripts/build_harborgate_k3_deb.sh"

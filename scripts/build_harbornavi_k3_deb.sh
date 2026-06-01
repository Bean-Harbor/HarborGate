#!/usr/bin/env bash
set -euo pipefail

TARGET="${TARGET:-riscv64gc-unknown-linux-gnu}"
DEB_ARCH="${DEB_ARCH:-riscv64}"
VERSION="${VERSION:-0.1.0+harbornavi.k3.$(date +%Y%m%d)}"
OUT_DIR="${OUT_DIR:-artifacts/k3}"
BIN_NAME="harboros-im-gate"
PKG_NAME="harboros-im-gate"

command -v dpkg-deb >/dev/null
command -v riscv64-linux-gnu-gcc >/dev/null
export CARGO_TARGET_RISCV64GC_UNKNOWN_LINUX_GNU_LINKER="${CARGO_TARGET_RISCV64GC_UNKNOWN_LINUX_GNU_LINKER:-riscv64-linux-gnu-gcc}"

cargo build --release --target "${TARGET}" --bin "${BIN_NAME}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pkg_root="${repo_root}/${OUT_DIR}/package/${PKG_NAME}_${VERSION}_${DEB_ARCH}"
deb_path="${repo_root}/${OUT_DIR}/${PKG_NAME}_${VERSION}_${DEB_ARCH}.deb"

rm -rf "${pkg_root}"
mkdir -p "${pkg_root}/DEBIAN" \
  "${pkg_root}/usr/bin" \
  "${pkg_root}/etc/systemd/system"

install -m 0755 "${repo_root}/target/${TARGET}/release/${BIN_NAME}" \
  "${pkg_root}/usr/bin/${BIN_NAME}"
install -m 0644 "${repo_root}/debian/${BIN_NAME}.service" \
  "${pkg_root}/etc/systemd/system/${BIN_NAME}.service"

sed \
  -e "s/VERSION_PLACEHOLDER/${VERSION}/g" \
  -e "s/^Architecture:.*/Architecture: ${DEB_ARCH}/g" \
  -e "s/^Depends:.*/Depends: libc6, ca-certificates/g" \
  "${repo_root}/debian/control" > "${pkg_root}/DEBIAN/control"

install -m 0755 "${repo_root}/debian/postinst" "${pkg_root}/DEBIAN/postinst"
install -m 0755 "${repo_root}/debian/prerm" "${pkg_root}/DEBIAN/prerm"

mkdir -p "${repo_root}/${OUT_DIR}"
dpkg-deb --build --root-owner-group "${pkg_root}" "${deb_path}"
sha256sum "${deb_path}" | tee "${deb_path}.sha256"
dpkg-deb --info "${deb_path}"

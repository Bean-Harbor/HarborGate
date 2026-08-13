#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

target="${RUST_TARGET:-riscv64gc-unknown-linux-gnu}"
deb_arch="${DEB_ARCH:-riscv64}"
case "${target}:${deb_arch}" in
  x86_64-unknown-linux-gnu:amd64)
    ;;
  riscv64gc-unknown-linux-gnu:riscv64)
    command -v riscv64-linux-gnu-gcc >/dev/null || {
      echo "error: riscv64-linux-gnu-gcc is required" >&2
      exit 2
    }
    export CARGO_TARGET_RISCV64GC_UNKNOWN_LINUX_GNU_LINKER="${CARGO_TARGET_RISCV64GC_UNKNOWN_LINUX_GNU_LINKER:-riscv64-linux-gnu-gcc}"
    ;;
  *)
    echo "error: unsupported target/deb arch pair: ${target}/${deb_arch}" >&2
    exit 2
    ;;
esac

: "${DEBIAN_VERSION:?DEBIAN_VERSION is required}"
: "${SOURCE_DATE_EPOCH:?SOURCE_DATE_EPOCH is required}"
: "${HARBORGATE_BUILD_CONTAINER_DIGEST:?HARBORGATE_BUILD_CONTAINER_DIGEST is required}"
: "${HARBORGATE_DEBIAN_SNAPSHOT:?HARBORGATE_DEBIAN_SNAPSHOT is required}"
[[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] || {
  echo "error: SOURCE_DATE_EPOCH must be a Unix timestamp" >&2
  exit 2
}
dpkg --validate-version "$DEBIAN_VERSION"
[[ "$HARBORGATE_BUILD_CONTAINER_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "error: HARBORGATE_BUILD_CONTAINER_DIGEST must be a sha256 digest" >&2
  exit 2
}
[[ "$HARBORGATE_DEBIAN_SNAPSHOT" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || {
  echo "error: HARBORGATE_DEBIAN_SNAPSHOT must be an immutable snapshot timestamp" >&2
  exit 2
}

source_commit="${SOURCE_COMMIT:-$(git rev-parse HEAD)}"
[[ "$source_commit" =~ ^[0-9a-f]{40}$ ]] || {
  echo "error: SOURCE_COMMIT must be a full lowercase Git commit" >&2
  exit 2
}
[[ "$source_commit" == "$(git rev-parse HEAD)" ]] || {
  echo "error: SOURCE_COMMIT does not match the checked out commit" >&2
  exit 2
}
[[ -z "$(git status --porcelain --untracked-files=normal)" ]] || {
  echo "error: refusing a release package from a dirty worktree" >&2
  exit 2
}

for command_name in cargo dpkg-deb python3 sha256sum touch; do
  command -v "$command_name" >/dev/null || {
    echo "error: ${command_name} is required" >&2
    exit 2
  }
done

out_dir="${OUT_DIR:-${repo_root}/dist/harborgate-debs}"
work_parent="${PACKAGE_WORK_ROOT:-${TMPDIR:-/tmp}}"
mkdir -p "$work_parent" "$out_dir"
build_root="$(mktemp -d "${work_parent%/}/harborgate-deb.XXXXXX")"
trap 'rm -rf -- "$build_root"' EXIT
pkg_dir="${build_root}/root"
cargo_target_dir="${CARGO_TARGET_DIR:-${repo_root}/target}"

export CARGO_INCREMENTAL=0
export RUSTFLAGS="${RUSTFLAGS:+${RUSTFLAGS} }--remap-path-prefix=${repo_root}=."
cargo build --locked --release --target "$target" --bin harboros-im-gate

install -d \
  "$pkg_dir/DEBIAN" \
  "$pkg_dir/usr/bin" \
  "$pkg_dir/usr/lib/harborgate" \
  "$pkg_dir/usr/lib/systemd/system" \
  "$pkg_dir/usr/share/doc/harboros-im-gate" \
  "$pkg_dir/usr/share/harboros/component-contracts"
install -m 0755 \
  "$cargo_target_dir/$target/release/harboros-im-gate" \
  "$pkg_dir/usr/bin/harboros-im-gate"
install -m 0755 scripts/ensure-data-layout \
  "$pkg_dir/usr/lib/harborgate/ensure-data-layout"
install -m 0644 debian/harboros-im-gate.service \
  "$pkg_dir/usr/lib/systemd/system/harboros-im-gate.service"
install -m 0644 LICENSE "$pkg_dir/usr/share/doc/harboros-im-gate/copyright"
sed \
  -e "s/VERSION_PLACEHOLDER/${DEBIAN_VERSION}/g" \
  -e "s/ARCH_PLACEHOLDER/${deb_arch}/g" \
  debian/control > "$pkg_dir/DEBIAN/control"
sed 's/\r$//' debian/postinst > "$pkg_dir/DEBIAN/postinst"
sed 's/\r$//' debian/prerm > "$pkg_dir/DEBIAN/prerm"
chmod 0755 "$pkg_dir/DEBIAN/postinst" "$pkg_dir/DEBIAN/prerm"
sed -e "s/SOURCE_COMMIT_PLACEHOLDER/${source_commit}/g" \
  debian/component-contract.json.in \
  > "$pkg_dir/usr/share/harboros/component-contracts/harboros-im-gate.json"
sed -e "s/SOURCE_COMMIT_PLACEHOLDER/${source_commit}/g" \
  debian/k3-runtime-evidence-required.json.in \
  > "$pkg_dir/usr/share/doc/harboros-im-gate/k3-runtime-evidence-required.json"

python3 scripts/generate_supply_chain.py \
  --cargo-lock "$repo_root/Cargo.lock" \
  --cargo-toml "$repo_root/Cargo.toml" \
  --license "$repo_root/LICENSE" \
  --binary "$pkg_dir/usr/bin/harboros-im-gate" \
  --version "$DEBIAN_VERSION" \
  --target "$target" \
  --arch "$deb_arch" \
  --source-commit "$source_commit" \
  --source-date-epoch "$SOURCE_DATE_EPOCH" \
  --container-digest "$HARBORGATE_BUILD_CONTAINER_DIGEST" \
  --debian-snapshot "$HARBORGATE_DEBIAN_SNAPSHOT" \
  --output-dir "$pkg_dir/usr/share/doc/harboros-im-gate"

find "$pkg_dir" -print0 | xargs -0 touch --no-dereference --date="@${SOURCE_DATE_EPOCH}"
artifact="${out_dir}/harboros-im-gate_${DEBIAN_VERSION}_${deb_arch}.deb"
material_prefix="harboros-im-gate_${DEBIAN_VERSION}_${deb_arch}"
SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" dpkg-deb \
  --root-owner-group --build --uniform-compression -Zxz -z9 \
  "$pkg_dir" "$artifact"
install -m 0644 \
  "$pkg_dir/usr/share/doc/harboros-im-gate/sbom.spdx.json" \
  "$out_dir/${material_prefix}.sbom.spdx.json"
install -m 0644 \
  "$pkg_dir/usr/share/doc/harboros-im-gate/sbom.cdx.json" \
  "$out_dir/${material_prefix}.sbom.cdx.json"
install -m 0644 \
  "$pkg_dir/usr/share/doc/harboros-im-gate/license-review.json" \
  "$out_dir/${material_prefix}.license-review.json"
install -m 0644 \
  "$pkg_dir/usr/share/harboros/component-contracts/harboros-im-gate.json" \
  "$out_dir/${material_prefix}.component-contract.json"
install -m 0644 \
  "$pkg_dir/usr/share/doc/harboros-im-gate/k3-runtime-evidence-required.json" \
  "$out_dir/${material_prefix}.k3-runtime-evidence-required.json"
python3 scripts/generate_package_provenance.py \
  --artifact "$artifact" \
  --cargo-lock "$repo_root/Cargo.lock" \
  --cargo-toml "$repo_root/Cargo.toml" \
  --license "$repo_root/LICENSE" \
  --version "$DEBIAN_VERSION" \
  --target "$target" \
  --arch "$deb_arch" \
  --source-commit "$source_commit" \
  --source-date-epoch "$SOURCE_DATE_EPOCH" \
  --container-digest "$HARBORGATE_BUILD_CONTAINER_DIGEST" \
  --debian-snapshot "$HARBORGATE_DEBIAN_SNAPSHOT" \
  --output "$out_dir/${material_prefix}.provenance.json"
touch --date="@${SOURCE_DATE_EPOCH}" "$out_dir/${material_prefix}."*.json
python3 scripts/generate_artifact_set.py \
  --bundle "$out_dir" \
  --prefix "$material_prefix" \
  --version "$DEBIAN_VERSION" \
  --arch "$deb_arch" \
  --output "$out_dir/${material_prefix}.artifact-set.json"
touch --date="@${SOURCE_DATE_EPOCH}" "$out_dir/${material_prefix}.artifact-set.json"
(
  cd "$out_dir"
  sha256sum "$(basename "$artifact")" > "$(basename "$artifact").sha256"
  sha256sum \
    "${material_prefix}.provenance.json" \
    "${material_prefix}.sbom.spdx.json" \
    "${material_prefix}.sbom.cdx.json" \
    "${material_prefix}.license-review.json" \
    "${material_prefix}.component-contract.json" \
    "${material_prefix}.k3-runtime-evidence-required.json" \
    "${material_prefix}.artifact-set.json" \
    > "$(basename "$artifact").materials.sha256"
  python3 "$repo_root/scripts/verify_release_bundle.py" \
    . --arch "$deb_arch" --version "$DEBIAN_VERSION"
)

printf '%s\n' "$artifact"

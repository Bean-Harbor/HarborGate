#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_root="$(mktemp -d "${TMPDIR:-/tmp}/harborgate-repro.XXXXXX")"
trap 'rm -rf -- "$work_root"' EXIT

for run in first second; do
  rm -rf -- "$work_root/target"
  OUT_DIR="$work_root/$run/out" \
  CARGO_TARGET_DIR="$work_root/target" \
    bash "$repo_root/scripts/build_harborgate_k3_deb.sh"
done

first="$work_root/first/out"
second="$work_root/second/out"
[[ -n "$(find "$first" -maxdepth 1 -name '*.deb' -print -quit)" \
  && -n "$(find "$second" -maxdepth 1 -name '*.deb' -print -quit)" ]] || {
  echo "error: reproducibility build did not produce both release bundles" >&2
  exit 2
}
diff --no-dereference --recursive "$first" "$second" || {
  echo "error: HarborGate release bundle is not reproducible" >&2
  exit 1
}
(
  cd "$first"
  sha256sum --check ./*.sha256
  sha256sum ./*.deb
)

# Supplemental Cargo license evidence

The production generator first verifies each registry archive against `Cargo.lock`
and reads its original manifest. Package-local license files and complete embedded
MIT notices remain the default evidence. `supplemental-materials.json` supplies
missing MIT text only when the original archive also proves the upstream commit
and crate path in `.cargo_vcs_info.json`.

Each entry binds the exact crate name, version, registry source, archive SHA256,
original license declaration, repository, commit and crate path. The material is
stored as unchanged upstream bytes with a fixed URL and SHA256. Missing, altered,
duplicate or mismatched material blocks that dependency. The generator performs
no downloads and never modifies the Cargo cache or the original `.crate`.

The two entries are `base64-simd 0.8.0` and `vsimd 0.8.0`. Their verified archives
both identify Nugine/simd commit `d74c030d9dc4f3cae02146d1f497ff62726ef09a` and
declare MIT. The complete repository-root text was retrieved from the
[official commit's LICENSE](https://raw.githubusercontent.com/Nugine/simd/d74c030d9dc4f3cae02146d1f497ff62726ef09a/LICENSE),
unchanged (1,062 bytes, SHA256
`71674605ec4c087fe9eb534e3e4f9e26eb2e4aabcd76a29fd156c6a844d44b3d`).
The corresponding upstream manifests are
[base64-simd](https://raw.githubusercontent.com/Nugine/simd/d74c030d9dc4f3cae02146d1f497ff62726ef09a/crates/base64-simd/Cargo.toml)
and [vsimd](https://raw.githubusercontent.com/Nugine/simd/d74c030d9dc4f3cae02146d1f497ff62726ef09a/crates/vsimd/Cargo.toml).

The [Cargo manifest reference](https://doc.rust-lang.org/cargo/reference/manifest.html#the-license-and-license-file-fields)
defines `OR` as a choice of license and records the deprecated `/` separator;
[Cargo's historical manifest documentation](https://github.com/rust-lang/cargo/blob/0.16.0/src/doc/manifest.md#package-metadata)
documents that former syntax. The existing slash-to-`OR` conversion now accepts
ASCII spaces/tabs around the separator for the reviewed identifiers `MIT` and
`Apache-2.0`, including fnv's original `Apache-2.0 / MIT`. The original declaration
is preserved alongside the normalization record. Unknown identifiers, mixed
operators, parentheses and malformed slash expressions remain blocked. fnv's
own `LICENSE-APACHE` and `LICENSE-MIT` are both preserved from its verified archive;
it uses no supplemental text.

Source review date: 2026-09-13. This evidence completes the dependency material
check; it does not assert completion of the release build or runtime acceptance.

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_license_review_uses_only_repository_materials() -> None:
    supply_chain = load_script("generate_supply_chain.py")
    review = supply_chain.license_review(
        ROOT / "Cargo.toml",
        ROOT / "Cargo.lock",
        ROOT / "LICENSE",
        "harboros-im-gate",
        "0.1.0-test",
        "riscv64",
        "2026-08-13T00:00:00Z",
    )

    assert review["root_component"] == {
        "declared_license": "MIT",
        "copyright_notices": ["Copyright (c) 2026 Harborinno Ltd."],
    }
    assert review["dependency_summary"]["unresolved"] > 0
    assert review["release_eligible"] is False
    assert all(
        dependency["declared_license"] == "NOASSERTION"
        and dependency["copyright"] == "NOASSERTION"
        for dependency in review["dependencies"]
    )


def make_bundle(path: Path, version: str = "0.1.0-test", arch: str = "riscv64") -> str:
    prefix = f"harboros-im-gate_{version}_{arch}"
    deb = path / f"{prefix}.deb"
    deb.write_bytes(b"deb payload")
    material_names = [
        f"{prefix}.component-contract.json",
        f"{prefix}.k3-runtime-evidence-required.json",
        f"{prefix}.sbom.cdx.json",
        f"{prefix}.sbom.spdx.json",
    ]
    for name in material_names:
        (path / name).write_text("{}\n", encoding="utf-8")
    (path / f"{prefix}.license-review.json").write_text(
        json.dumps(
            {
                "policy": "fail-closed",
                "release_eligible": False,
                "dependency_summary": {"unresolved": 1},
                "dependencies": [{"declared_license": "NOASSERTION"}],
            }
        ),
        encoding="utf-8",
    )
    (path / f"{prefix}.provenance.json").write_text(
        json.dumps(
            {
                "subject": [
                    {"name": deb.name, "digest": {"sha256": sha256(deb)}}
                ]
            }
        ),
        encoding="utf-8",
    )
    members = [deb.name, *material_names, f"{prefix}.license-review.json", f"{prefix}.provenance.json"]
    (path / f"{prefix}.artifact-set.json").write_text(
        json.dumps(
            {
                "package": "harboros-im-gate",
                "version": version,
                "architecture": arch,
                "artifacts": [
                    {"name": name, "sha256": sha256(path / name)} for name in members
                ]
            }
        ),
        encoding="utf-8",
    )
    (path / f"{prefix}.deb.sha256").write_text(
        f"{sha256(deb)}  {deb.name}\n", encoding="utf-8"
    )
    material_names = [
        f"{prefix}.provenance.json",
        f"{prefix}.sbom.spdx.json",
        f"{prefix}.sbom.cdx.json",
        f"{prefix}.license-review.json",
        f"{prefix}.component-contract.json",
        f"{prefix}.k3-runtime-evidence-required.json",
        f"{prefix}.artifact-set.json",
    ]
    (path / f"{prefix}.deb.materials.sha256").write_text(
        "".join(f"{sha256(path / name)}  {name}\n" for name in material_names),
        encoding="utf-8",
    )
    return prefix


def run_verifier(
    verifier,
    bundle: Path,
    version: str = "0.1.0-test",
    require_release_eligible: bool = False,
) -> None:
    old_argv = sys.argv
    try:
        sys.argv = [
            "verify_release_bundle.py",
            str(bundle),
            "--arch",
            "riscv64",
            "--version",
            version,
        ]
        if require_release_eligible:
            sys.argv.append("--require-release-eligible")
        verifier.main()
    finally:
        sys.argv = old_argv


def test_bundle_verifier_accepts_exact_bundle(tmp_path: Path) -> None:
    verifier = load_script("verify_release_bundle.py")
    make_bundle(tmp_path)
    run_verifier(verifier, tmp_path)


def test_bundle_verifier_rejects_tampered_deb(tmp_path: Path) -> None:
    verifier = load_script("verify_release_bundle.py")
    prefix = make_bundle(tmp_path)
    (tmp_path / f"{prefix}.deb").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum manifest digest"):
        run_verifier(verifier, tmp_path)


def test_bundle_verifier_rejects_unexpected_artifact(tmp_path: Path) -> None:
    verifier = load_script("verify_release_bundle.py")
    make_bundle(tmp_path)
    (tmp_path / "unreviewed.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(ValueError, match="file set mismatch"):
        run_verifier(verifier, tmp_path)


def test_bundle_verifier_rejects_tampered_checksum_manifest(tmp_path: Path) -> None:
    verifier = load_script("verify_release_bundle.py")
    prefix = make_bundle(tmp_path)
    (tmp_path / f"{prefix}.deb.sha256").write_text(
        f"{'0' * 64}  {prefix}.deb\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="checksum manifest digest"):
        run_verifier(verifier, tmp_path)


def test_bundle_verifier_rejects_unsafe_checksum_member(tmp_path: Path) -> None:
    verifier = load_script("verify_release_bundle.py")
    prefix = make_bundle(tmp_path)
    (tmp_path / f"{prefix}.deb.sha256").write_text(
        f"{'0' * 64}  ../{prefix}.deb\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unsafe checksum manifest member"):
        run_verifier(verifier, tmp_path)


def test_formal_release_rejects_unresolved_license_review(tmp_path: Path) -> None:
    verifier = load_script("verify_release_bundle.py")
    make_bundle(tmp_path)
    with pytest.raises(ValueError, match="license review blocks formal release"):
        run_verifier(verifier, tmp_path, require_release_eligible=True)

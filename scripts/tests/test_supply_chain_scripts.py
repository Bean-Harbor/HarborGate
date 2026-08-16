from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "harboros-im-gate"
VERSION = "0.1.0-test"
ARCH = "riscv64"
COMMIT = "a" * 40
SOURCE_REPO = "https://github.com/Bean-Harbor/HarborGate"
COPYRIGHT = "Copyright (c) 2026 Harborinno Ltd."
BLOCKER = (
    "Locked third-party Cargo dependencies lack repository-bound license and "
    "copyright evidence."
)


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.write_bytes(
        (
            json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
    )


def rights_payload(commit: str = COMMIT) -> dict:
    return {
        "approval": {
            "basis": "rights-holder-confirmation",
            "confirmed_on": "2026-08-16",
            "decision": "approved-for-distribution",
            "organization": "Harbor Innovations",
            "scope": ["first-party-brand-materials", "first-party-source-code"],
            "use": "HarborNavi qualification",
        },
        "package": PACKAGE,
        "schema_version": 1,
        "source": {"commit": commit, "repo": SOURCE_REPO},
        "third_party": {
            "decision": "separate-review-required",
            "scope": (
                "Locked dependencies and third-party materials remain governed "
                "by their original licenses."
            ),
        },
    }


def test_license_review_approves_first_party_but_keeps_dependencies_blocked(
    tmp_path: Path,
) -> None:
    supply_chain = load_script("generate_supply_chain.py")
    rights = tmp_path / "first-party-rights-approval.json"
    write_json(rights, rights_payload())
    review = supply_chain.license_review(
        ROOT / "Cargo.toml",
        ROOT / "Cargo.lock",
        ROOT / "LICENSE",
        rights,
        PACKAGE,
        VERSION,
        ARCH,
        "2026-08-13T00:00:00Z",
        COMMIT,
    )

    assert review["root_component"] == {
        "declared_license": "MIT",
        "concluded_license": "MIT",
        "copyright": COPYRIGHT,
        "copyright_notices": [COPYRIGHT],
    }
    assert review["first_party_rights"]["status"] == "approved"
    assert review["dependency_summary"]["unresolved"] > 0
    assert review["review_status"] == "blocked"
    assert review["release_eligible"] is False
    assert review["blocking_reasons"] == [BLOCKER]
    assert all(
        dependency["declared_license"] == "NOASSERTION"
        and dependency["copyright"] == "NOASSERTION"
        for dependency in review["dependencies"]
    )


def test_supply_chain_sboms_bind_the_final_deb_and_have_unique_spdx_ids(
    tmp_path: Path,
) -> None:
    supply_chain = load_script("generate_supply_chain.py")
    artifact = tmp_path / f"{PACKAGE}_{VERSION}_{ARCH}.deb"
    artifact.write_bytes(b"final deb bytes")
    prefix = artifact.stem
    rights = tmp_path / f"{prefix}.first-party-rights-approval.json"
    write_json(rights, rights_payload())
    old_argv = sys.argv
    try:
        sys.argv = [
            "generate_supply_chain.py",
            "--cargo-lock",
            str(ROOT / "Cargo.lock"),
            "--cargo-toml",
            str(ROOT / "Cargo.toml"),
            "--license",
            str(ROOT / "LICENSE"),
            "--artifact",
            str(artifact),
            "--rights-approval",
            str(rights),
            "--version",
            VERSION,
            "--target",
            "riscv64gc-unknown-linux-gnu",
            "--arch",
            ARCH,
            "--source-commit",
            COMMIT,
            "--source-date-epoch",
            "1786579200",
            "--container-digest",
            f"sha256:{'b' * 64}",
            "--debian-snapshot",
            "20260801T000000Z",
            "--output-dir",
            str(tmp_path),
            "--prefix",
            prefix,
        ]
        supply_chain.main()
    finally:
        sys.argv = old_argv

    spdx_path = tmp_path / f"{prefix}.sbom.spdx.json"
    spdx = json.loads(spdx_path.read_text(encoding="utf-8"))
    package_ids = [item["SPDXID"] for item in spdx["packages"]]
    assert len(package_ids) == len(set(package_ids))
    root = next(item for item in spdx["packages"] if item["name"] == PACKAGE)
    assert root["checksums"] == [
        {"algorithm": "SHA256", "checksumValue": sha256(artifact)}
    ]
    assert spdx_path.read_bytes() == (
        json.dumps(spdx, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def make_bundle(
    path: Path,
    version: str = VERSION,
    arch: str = ARCH,
    commit: str = COMMIT,
) -> str:
    prefix = f"{PACKAGE}_{version}_{arch}"
    deb = path / f"{prefix}.deb"
    deb.write_bytes(b"deb payload")
    (path / f"{deb.name}.sha256").write_bytes(
        f"{sha256(deb)}  {deb.name}\n".encode("ascii")
    )
    (path / f"{prefix}.LICENSE").write_text(
        f"MIT License\n\n{COPYRIGHT}\n", encoding="utf-8"
    )
    write_json(
        path / f"{prefix}.component-contract.json",
        {
            "contracts": [{"id": "harboros.k3.gate-v2-transport", "version": 1}],
            "package": PACKAGE,
            "schema_version": 1,
            "source_commit": commit,
        },
    )
    write_json(path / f"{prefix}.first-party-rights-approval.json", rights_payload(commit))
    write_json(
        path / f"{prefix}.k3-runtime-evidence-required.json",
        {"package": PACKAGE, "schema_version": 1, "source_commit": commit},
    )
    review = {
        "architecture": arch,
        "blocking_reasons": [BLOCKER],
        "dependencies": [
            {
                "copyright": "NOASSERTION",
                "declared_license": "NOASSERTION",
                "name": "dependency",
                "version": "1.0.0",
            }
        ],
        "dependency_summary": {"resolved": 0, "total": 1, "unresolved": 1},
        "first_party_rights": {"status": "approved"},
        "package": PACKAGE,
        "policy": "fail-closed",
        "release_eligible": False,
        "review_status": "blocked",
        "root_component": {
            "concluded_license": "MIT",
            "copyright": COPYRIGHT,
            "declared_license": "MIT",
        },
        "schema_version": 1,
        "version": version,
    }
    write_json(path / f"{prefix}.license-review.json", review)
    write_json(
        path / f"{prefix}.provenance.json",
        {
            "_type": "https://in-toto.io/Statement/v1",
            "predicate": {
                "buildDefinition": {
                    "resolvedDependencies": [
                        {
                            "digest": {"gitCommit": commit},
                            "uri": f"git+{SOURCE_REPO}@{commit}",
                        }
                    ]
                }
            },
            "predicateType": "https://slsa.dev/provenance/v1",
            "subject": [{"digest": {"sha256": sha256(deb)}, "name": deb.name}],
        },
    )
    root_id = "SPDXRef-Package-harboros-im-gate"
    write_json(
        path / f"{prefix}.sbom.spdx.json",
        {
            "SPDXID": "SPDXRef-DOCUMENT",
            "packages": [
                {
                    "SPDXID": root_id,
                    "checksums": [
                        {"algorithm": "SHA256", "checksumValue": sha256(deb)}
                    ],
                    "copyrightText": COPYRIGHT,
                    "licenseConcluded": "MIT",
                    "licenseDeclared": "MIT",
                    "name": PACKAGE,
                    "versionInfo": version,
                },
                {"SPDXID": "SPDXRef-dependency", "name": "dependency"},
            ],
            "relationships": [
                {
                    "relatedSpdxElement": root_id,
                    "relationshipType": "DESCRIBES",
                    "spdxElementId": "SPDXRef-DOCUMENT",
                }
            ],
            "spdxVersion": "SPDX-2.3",
        },
    )
    write_json(
        path / f"{prefix}.sbom.cdx.json",
        {
            "bomFormat": "CycloneDX",
            "metadata": {
                "component": {
                    "hashes": [{"alg": "SHA-256", "content": sha256(deb)}],
                    "licenses": [{"expression": "MIT"}],
                    "name": PACKAGE,
                    "properties": [
                        {"name": "harboros:copyright", "value": COPYRIGHT},
                        {"name": "harboros:license-concluded", "value": "MIT"},
                        {"name": "harboros:license-declared", "value": "MIT"},
                    ],
                    "type": "application",
                    "version": version,
                }
            },
            "specVersion": "1.6",
            "version": 1,
        },
    )

    artifact_members = [
        deb.name,
        f"{deb.name}.sha256",
        f"{prefix}.LICENSE",
        f"{prefix}.component-contract.json",
        f"{prefix}.first-party-rights-approval.json",
        f"{prefix}.k3-runtime-evidence-required.json",
        f"{prefix}.license-review.json",
        f"{prefix}.provenance.json",
        f"{prefix}.sbom.cdx.json",
        f"{prefix}.sbom.spdx.json",
    ]
    write_json(
        path / f"{prefix}.artifact-set.json",
        {
            "architecture": arch,
            "artifacts": [
                {"name": name, "sha256": sha256(path / name)}
                for name in artifact_members
            ],
            "package": PACKAGE,
            "schema_version": 1,
            "version": version,
        },
    )
    material_paths = {
        "artifact-set": path / f"{prefix}.artifact-set.json",
        "component-contract": path / f"{prefix}.component-contract.json",
        "deb": deb,
        "deb-sha256": path / f"{deb.name}.sha256",
        "first-party-rights": path / f"{prefix}.first-party-rights-approval.json",
        "k3-runtime-evidence": path / f"{prefix}.k3-runtime-evidence-required.json",
        "license-review": path / f"{prefix}.license-review.json",
        "provenance": path / f"{prefix}.provenance.json",
        "root-license": path / f"{prefix}.LICENSE",
        "sbom-cyclonedx": path / f"{prefix}.sbom.cdx.json",
        "sbom-spdx": path / f"{prefix}.sbom.spdx.json",
    }
    material_identities = {
        kind: {"filename": item.name, "kind": kind, "sha256": sha256(item)}
        for kind, item in material_paths.items()
    }
    decision = {
        "blocking_reasons": [BLOCKER],
        "concluded_license": "MIT",
        "copyright": COPYRIGHT,
        "declared_license": "MIT",
        "policy": "fail-closed",
        "release_eligible": False,
        "status": "blocked",
    }
    descriptor = {
        "architecture": arch,
        "artifact": {
            **material_identities["deb"],
            "size": deb.stat().st_size,
        },
        "bindings": [
            material_identities[kind]
            for kind in (
                "component-contract",
                "license-review",
                "provenance",
                "sbom-spdx",
                "sbom-cyclonedx",
            )
        ],
        "decision": decision,
        "installed_evidence": [
            {
                **material_identities["component-contract"],
                "installed_path": (
                    "/usr/share/harboros/component-contracts/harboros-im-gate.json"
                ),
            },
            {
                **material_identities["first-party-rights"],
                "installed_path": (
                    "/usr/share/doc/harboros-im-gate/first-party-rights-approval.json"
                ),
            },
            {
                **material_identities["root-license"],
                "installed_path": "/usr/share/doc/harboros-im-gate/copyright",
            },
        ],
        "materials": [material_identities[kind] for kind in sorted(material_identities)],
        "package": PACKAGE,
        "schema_version": 1,
        "source": {"commit": commit, "repo": SOURCE_REPO},
        "version": version,
    }
    descriptor_path = path / f"{deb.name}.release-materials.json"
    write_json(descriptor_path, descriptor)
    manifest_entries = {
        descriptor_path.name: sha256(descriptor_path),
        **{item["filename"]: item["sha256"] for item in descriptor["materials"]},
    }
    (path / f"{deb.name}.materials.sha256").write_bytes(
        "".join(
            f"{digest}  {name}\n" for name, digest in sorted(manifest_entries.items())
        ).encode("ascii")
    )
    return prefix


def run_verifier(
    verifier,
    bundle: Path,
    version: str = VERSION,
    require_release_eligible: bool = False,
) -> None:
    old_argv = sys.argv
    try:
        sys.argv = [
            "verify_release_bundle.py",
            str(bundle),
            "--arch",
            ARCH,
            "--version",
            version,
        ]
        if require_release_eligible:
            sys.argv.append("--require-release-eligible")
        verifier.main()
    finally:
        sys.argv = old_argv


def test_bundle_verifier_accepts_exact_canonical_bundle(tmp_path: Path) -> None:
    verifier = load_script("verify_release_bundle.py")
    make_bundle(tmp_path)
    run_verifier(verifier, tmp_path)


def test_bundle_verifier_rejects_tampered_deb(tmp_path: Path) -> None:
    verifier = load_script("verify_release_bundle.py")
    prefix = make_bundle(tmp_path)
    (tmp_path / f"{prefix}.deb").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum sidecar"):
        run_verifier(verifier, tmp_path)


def test_bundle_verifier_rejects_unexpected_artifact(tmp_path: Path) -> None:
    verifier = load_script("verify_release_bundle.py")
    make_bundle(tmp_path)
    (tmp_path / "unreviewed.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(ValueError, match="file set mismatch"):
        run_verifier(verifier, tmp_path)


def test_bundle_verifier_rejects_tampered_material(tmp_path: Path) -> None:
    verifier = load_script("verify_release_bundle.py")
    prefix = make_bundle(tmp_path)
    (tmp_path / f"{prefix}.first-party-rights-approval.json").write_text(
        "{}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="release material digest mismatch"):
        run_verifier(verifier, tmp_path)


def test_bundle_verifier_rejects_unsafe_checksum_member(tmp_path: Path) -> None:
    verifier = load_script("verify_release_bundle.py")
    prefix = make_bundle(tmp_path)
    manifest = tmp_path / f"{prefix}.deb.materials.sha256"
    manifest.write_bytes(f"{'0' * 64}  ../{prefix}.deb\n".encode("ascii"))
    with pytest.raises(ValueError, match="unsafe checksum manifest member"):
        run_verifier(verifier, tmp_path)


def test_formal_release_rejects_unresolved_dependency_review(tmp_path: Path) -> None:
    verifier = load_script("verify_release_bundle.py")
    make_bundle(tmp_path)
    with pytest.raises(ValueError, match="license review blocks formal release"):
        run_verifier(verifier, tmp_path, require_release_eligible=True)

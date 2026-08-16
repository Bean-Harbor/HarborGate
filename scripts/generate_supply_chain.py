#!/usr/bin/env python3
"""Generate deterministic SBOM and fail-closed license review documents."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import tomllib
import uuid
from pathlib import Path
from urllib.parse import quote


PACKAGE_NAME = "harboros-im-gate"
SOURCE_REPOSITORY = "https://github.com/Bean-Harbor/HarborGate"
COPYRIGHT_TEXT = "Copyright (c) 2026 Harborinno Ltd."
THIRD_PARTY_BLOCKER = (
    "Locked third-party Cargo dependencies lack repository-bound license and "
    "copyright evidence."
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def spdx_id(name: str, version: str) -> str:
    value = re.sub(r"[^A-Za-z0-9.-]", "-", f"Package-{name}-{version}")
    return f"SPDXRef-{value}"


def load_json(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def validate_first_party_rights(path: Path, source_commit: str) -> dict[str, object]:
    value = load_json(path, "first-party rights approval")
    if (
        value.get("schema_version") != 1
        or value.get("package") != PACKAGE_NAME
        or value.get("source")
        != {"commit": source_commit, "repo": SOURCE_REPOSITORY}
        or value.get("approval")
        != {
            "basis": "rights-holder-confirmation",
            "confirmed_on": "2026-08-16",
            "decision": "approved-for-distribution",
            "organization": "Harbor Innovations",
            "scope": ["first-party-brand-materials", "first-party-source-code"],
            "use": "HarborNavi qualification",
        }
        or value.get("third_party")
        != {
            "decision": "separate-review-required",
            "scope": (
                "Locked dependencies and third-party materials remain governed "
                "by their original licenses."
            ),
        }
    ):
        raise ValueError("first-party rights approval does not match the qualification decision")
    return value


def cargo_components(lock_path: Path, root_package: str) -> list[dict[str, str]]:
    payload = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    return [
        {
            "name": package["name"],
            "version": package["version"],
            "purl": f"pkg:cargo/{package['name']}@{package['version']}",
            "checksum": package.get("checksum", ""),
        }
        for package in payload.get("package", [])
        if package.get("source") or package.get("name") != root_package
    ]


def license_review(
    cargo_toml_path: Path,
    cargo_lock_path: Path,
    license_path: Path,
    rights_approval_path: Path,
    package_name: str,
    package_version: str,
    arch: str,
    created: str,
    source_commit: str,
) -> dict[str, object]:
    cargo_toml = tomllib.loads(cargo_toml_path.read_text(encoding="utf-8"))
    declared_license = cargo_toml.get("package", {}).get("license")
    if not isinstance(declared_license, str) or not declared_license.strip():
        raise ValueError("Cargo.toml package.license is required")
    if package_name != PACKAGE_NAME or declared_license != "MIT":
        raise ValueError("root package identity or declared license differs from review")

    license_text = license_path.read_text(encoding="utf-8")
    copyright_notices = [
        line.strip()
        for line in license_text.splitlines()
        if line.strip().lower().startswith("copyright ")
    ]
    if not copyright_notices:
        raise ValueError("LICENSE must contain an explicit copyright notice")
    if COPYRIGHT_TEXT not in copyright_notices:
        raise ValueError("LICENSE copyright differs from the reviewed root component")
    rights_approval = validate_first_party_rights(rights_approval_path, source_commit)

    locked_packages = tomllib.loads(cargo_lock_path.read_text(encoding="utf-8")).get(
        "package", []
    )
    dependencies = []
    for package in locked_packages:
        if package.get("name") == cargo_toml["package"].get("name") and not package.get(
            "source"
        ):
            continue
        dependencies.append(
            {
                "name": package["name"],
                "version": package["version"],
                "source": package.get("source", "NOASSERTION"),
                "checksum": package.get("checksum", "NOASSERTION"),
                "declared_license": "NOASSERTION",
                "copyright": "NOASSERTION",
                "review_basis": "not-present-in-repository-cargo-or-license-materials",
            }
        )

    unresolved = len(dependencies)
    release_eligible = unresolved == 0
    return {
        "schema_version": 1,
        "package": package_name,
        "version": package_version,
        "architecture": arch,
        "reviewed_at": created,
        "review_status": "approved" if release_eligible else "blocked",
        "policy": "fail-closed",
        "release_eligible": release_eligible,
        "root_component": {
            "declared_license": declared_license,
            "concluded_license": declared_license,
            "copyright": COPYRIGHT_TEXT,
            "copyright_notices": copyright_notices,
        },
        "first_party_rights": {
            "status": "approved",
            "evidence": {
                "path": rights_approval_path.name,
                "sha256": sha256(rights_approval_path),
            },
            "resolution": rights_approval["approval"],
        },
        "reviewed_sources": [
            {"path": "Cargo.toml", "sha256": sha256(cargo_toml_path)},
            {"path": "Cargo.lock", "sha256": sha256(cargo_lock_path)},
            {"path": "LICENSE", "sha256": sha256(license_path)},
            {
                "path": rights_approval_path.name,
                "sha256": sha256(rights_approval_path),
            },
        ],
        "dependency_summary": {
            "total": unresolved,
            "resolved": 0,
            "unresolved": unresolved,
        },
        "dependencies": dependencies,
        "blocking_reasons": [THIRD_PARTY_BLOCKER] if unresolved else [],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cargo-lock", type=Path, required=True)
    parser.add_argument("--cargo-toml", type=Path, required=True)
    parser.add_argument("--license", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--rights-approval", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    parser.add_argument("--container-digest", required=True)
    parser.add_argument("--debian-snapshot", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    args = parser.parse_args()

    created = dt.datetime.fromtimestamp(args.source_date_epoch, dt.UTC).isoformat().replace(
        "+00:00", "Z"
    )
    root = {
        "name": PACKAGE_NAME,
        "version": args.version,
        "purl": f"pkg:deb/{PACKAGE_NAME}@{quote(args.version, safe='.-~')}?arch={args.arch}",
        "checksum": sha256(args.artifact),
    }
    cargo_toml = tomllib.loads(args.cargo_toml.read_text(encoding="utf-8"))
    root_cargo_package = cargo_toml.get("package", {}).get("name")
    if not isinstance(root_cargo_package, str) or not root_cargo_package:
        raise ValueError("Cargo.toml package.name is required")
    components = cargo_components(args.cargo_lock, root_cargo_package)
    root_id = spdx_id(root["name"], root["version"])
    packages = [
        {
            "name": root["name"],
            "SPDXID": root_id,
            "versionInfo": root["version"],
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "MIT",
            "licenseDeclared": "MIT",
            "copyrightText": COPYRIGHT_TEXT,
            "checksums": [{"algorithm": "SHA256", "checksumValue": root["checksum"]}],
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": root["purl"],
                }
            ],
        }
    ]
    relationships = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": root_id,
        }
    ]
    for component in components:
        package_id = spdx_id(component["name"], component["version"])
        package = {
            "name": component["name"],
            "SPDXID": package_id,
            "versionInfo": component["version"],
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "copyrightText": "NOASSERTION",
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": component["purl"],
                }
            ],
        }
        if component["checksum"]:
            package["checksums"] = [
                {"algorithm": "SHA256", "checksumValue": component["checksum"]}
            ]
        packages.append(package)
        relationships.append(
            {
                "spdxElementId": root_id,
                "relationshipType": "DEPENDS_ON",
                "relatedSpdxElement": package_id,
            }
        )

    spdx = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"harboros-im-gate-{args.version}-{args.arch}",
        "documentNamespace": (
            f"https://harboros.ai/sbom/harboros-im-gate/{args.version}/{args.arch}"
        ),
        "creationInfo": {
            "created": created,
            "creators": ["Organization: Harbor Innovations"],
        },
        "packages": packages,
        "relationships": relationships,
    }
    cyclonedx = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, root['purl'])}",
        "version": 1,
        "metadata": {
            "timestamp": created,
            "component": {
                "type": "application",
                "name": root["name"],
                "version": root["version"],
                "purl": root["purl"],
                "hashes": [{"alg": "SHA-256", "content": root["checksum"]}],
                "licenses": [{"expression": "MIT"}],
                "properties": [
                    {"name": "harboros:copyright", "value": COPYRIGHT_TEXT},
                    {"name": "harboros:license-concluded", "value": "MIT"},
                    {"name": "harboros:license-declared", "value": "MIT"},
                ],
            },
        },
        "components": [
            {
                "type": "library",
                "name": component["name"],
                "version": component["version"],
                "purl": component["purl"],
                **(
                    {"hashes": [{"alg": "SHA-256", "content": component["checksum"]}]}
                    if component["checksum"]
                    else {}
                ),
            }
            for component in components
        ],
    }
    review = license_review(
        args.cargo_toml,
        args.cargo_lock,
        args.license,
        args.rights_approval,
        root["name"],
        root["version"],
        args.arch,
        created,
        args.source_commit,
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        (f"{args.prefix}.sbom.spdx.json", spdx),
        (f"{args.prefix}.sbom.cdx.json", cyclonedx),
        (f"{args.prefix}.license-review.json", review),
    ):
        (args.output_dir / name).write_bytes(
            (
                json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
        )


if __name__ == "__main__":
    main()

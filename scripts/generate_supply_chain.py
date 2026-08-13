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


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def spdx_id(name: str, version: str) -> str:
    value = re.sub(r"[^A-Za-z0-9.-]", "-", f"Package-{name}-{version}")
    return f"SPDXRef-{value}"


def cargo_components(lock_path: Path) -> list[dict[str, str]]:
    payload = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    return [
        {
            "name": package["name"],
            "version": package["version"],
            "purl": f"pkg:cargo/{package['name']}@{package['version']}",
            "checksum": package.get("checksum", ""),
        }
        for package in payload.get("package", [])
    ]


def license_review(
    cargo_toml_path: Path,
    cargo_lock_path: Path,
    license_path: Path,
    package_name: str,
    package_version: str,
    arch: str,
    created: str,
) -> dict[str, object]:
    cargo_toml = tomllib.loads(cargo_toml_path.read_text(encoding="utf-8"))
    declared_license = cargo_toml.get("package", {}).get("license")
    if not isinstance(declared_license, str) or not declared_license.strip():
        raise ValueError("Cargo.toml package.license is required")

    license_text = license_path.read_text(encoding="utf-8")
    copyright_notices = [
        line.strip()
        for line in license_text.splitlines()
        if line.strip().lower().startswith("copyright ")
    ]
    if not copyright_notices:
        raise ValueError("LICENSE must contain an explicit copyright notice")

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
    return {
        "schema_version": 1,
        "package": package_name,
        "version": package_version,
        "architecture": arch,
        "reviewed_at": created,
        "review_status": "reviewed_against_repository_materials",
        "policy": "fail-closed",
        "release_eligible": unresolved == 0,
        "root_component": {
            "declared_license": declared_license,
            "copyright_notices": copyright_notices,
        },
        "reviewed_sources": [
            {"path": "Cargo.toml", "sha256": sha256(cargo_toml_path)},
            {"path": "Cargo.lock", "sha256": sha256(cargo_lock_path)},
            {"path": "LICENSE", "sha256": sha256(license_path)},
        ],
        "dependency_summary": {
            "total": unresolved,
            "resolved": 0,
            "unresolved": unresolved,
        },
        "dependencies": dependencies,
        "blocking_reasons": (
            [
                "Cargo.lock does not contain dependency license/copyright evidence; "
                "no license was inferred from package names or external knowledge."
            ]
            if unresolved
            else []
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cargo-lock", type=Path, required=True)
    parser.add_argument("--cargo-toml", type=Path, required=True)
    parser.add_argument("--license", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    parser.add_argument("--container-digest", required=True)
    parser.add_argument("--debian-snapshot", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    created = dt.datetime.fromtimestamp(args.source_date_epoch, dt.UTC).isoformat().replace(
        "+00:00", "Z"
    )
    root = {
        "name": "harboros-im-gate",
        "version": args.version,
        "purl": f"pkg:deb/harboros-im-gate@{args.version}?arch={args.arch}",
        "checksum": sha256(args.binary),
    }
    components = cargo_components(args.cargo_lock)
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
        "creationInfo": {"created": created, "creators": ["Organization: Harbor"]},
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
                "licenses": [{"license": {"id": "MIT"}}],
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
        root["name"],
        root["version"],
        args.arch,
        created,
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("sbom.spdx.json", spdx),
        ("sbom.cdx.json", cyclonedx),
        ("license-review.json", review),
    ):
        (args.output_dir / name).write_text(
            json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()

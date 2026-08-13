#!/usr/bin/env python3
"""Generate deterministic SBOM and provenance documents for HarborGate."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
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


def command_version(*command: str) -> str:
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return (completed.stdout or completed.stderr).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cargo-lock", type=Path, required=True)
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
    provenance = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {"name": "harboros-im-gate", "digest": {"sha256": root["checksum"]}}
        ],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://harboros.ai/build-types/rust-deb/v1",
                "externalParameters": {
                    "target": args.target,
                    "arch": args.arch,
                    "version": args.version,
                    "source_date_epoch": args.source_date_epoch,
                    "debian_snapshot": args.debian_snapshot,
                },
                "resolvedDependencies": [
                    {
                        "uri": (
                            "git+https://github.com/Bean-Harbor/HarborGate@"
                            f"{args.source_commit}"
                        ),
                        "digest": {"gitCommit": args.source_commit},
                    },
                    {
                        "uri": "Cargo.lock",
                        "digest": {"sha256": sha256(args.cargo_lock)},
                    },
                    {
                        "uri": (
                            "https://snapshot.debian.org/archive/debian/"
                            f"{args.debian_snapshot}/"
                        )
                    },
                ],
            },
            "runDetails": {
                "builder": {"id": args.container_digest},
                "metadata": {
                    "invocationId": (
                        f"harboros-im-gate-{args.source_commit}-{args.arch}"
                    ),
                    "startedOn": created,
                    "toolchain": {
                        "cargo": command_version("cargo", "--version"),
                        "dpkg_deb": command_version("dpkg-deb", "--version").splitlines()[0],
                        "python": command_version("python3", "--version"),
                        "rustc": command_version("rustc", "--version", "--verbose"),
                    },
                },
            },
        },
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("sbom.spdx.json", spdx),
        ("sbom.cdx.json", cyclonedx),
        ("build-provenance.json", provenance),
    ):
        (args.output_dir / name).write_text(
            json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()

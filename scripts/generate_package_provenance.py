#!/usr/bin/env python3
"""Generate deterministic SLSA provenance whose subject is the final deb."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def load_canonical_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    canonical = (
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if payload != canonical:
        raise ValueError(f"{label} must be canonical JSON")
    return value


def cargo_license_generator() -> Any:
    path = Path(__file__).with_name("generate_third_party_licenses.py")
    spec = importlib.util.spec_from_file_location("gate_third_party_licenses", path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load the Cargo target closure generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def target_dependency_records(
    metadata: dict[str, Any], cargo_toml: Path, cargo_lock: Path
) -> list[dict[str, str]]:
    generator = cargo_license_generator()
    locked = generator.locked_registry_packages(cargo_lock)
    records = []
    purls = set()
    for package, _, _ in generator.target_closure(metadata, cargo_toml):
        identity = (
            package.get("name"),
            package.get("version"),
            package.get("source"),
        )
        if not all(isinstance(item, str) and item for item in identity):
            raise ValueError("cargo metadata dependency identity is incomplete")
        lock_record = locked.get(identity)
        checksum = lock_record.get("checksum") if lock_record else None
        if not isinstance(checksum, str) or not SHA256_RE.fullmatch(checksum):
            raise ValueError(f"Cargo.lock omits target dependency checksum: {identity}")
        name, version, source = identity
        purl = f"pkg:cargo/{quote(name, safe='.-~')}@{quote(version, safe='.-~')}"
        if purl in purls:
            raise ValueError(f"target dependency repeats a Cargo purl: {purl}")
        purls.add(purl)
        records.append(
            {
                "checksum": checksum,
                "name": name,
                "purl": purl,
                "source": source,
                "version": version,
            }
        )
    return sorted(records, key=lambda item: (item["name"], item["version"], item["source"]))


def evidence_dependency_records(
    evidence_path: Path, cargo_lock: Path
) -> list[dict[str, str]]:
    evidence = load_canonical_json(evidence_path, "third-party license evidence")
    if evidence.get("cargo_lock") != {
        "filename": cargo_lock.name,
        "sha256": sha256(cargo_lock),
    }:
        raise ValueError("third-party license evidence does not bind Cargo.lock")
    dependencies = evidence.get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        raise ValueError("third-party license evidence lacks target dependencies")
    records = []
    identities = set()
    purls = set()
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise ValueError("third-party license dependency is invalid")
        identity = (
            dependency.get("name"),
            dependency.get("version"),
            dependency.get("source"),
        )
        checksum = dependency.get("checksum")
        if (
            not all(isinstance(item, str) and item for item in identity)
            or identity in identities
            or not isinstance(checksum, str)
            or not SHA256_RE.fullmatch(checksum)
        ):
            raise ValueError("third-party license dependency identity is invalid")
        name, version, source = identity
        archive = dependency.get("archive")
        expected_filename = f"{name}-{version}.crate"
        if not isinstance(archive, dict) or (
            archive.get("filename", archive.get("expected_filename")) != expected_filename
        ):
            raise ValueError("third-party license dependency archive identity is invalid")
        purl = f"pkg:cargo/{quote(name, safe='.-~')}@{quote(version, safe='.-~')}"
        if purl in purls:
            raise ValueError(f"third-party license evidence repeats a Cargo purl: {purl}")
        identities.add(identity)
        purls.add(purl)
        records.append(
            {
                "checksum": checksum,
                "name": name,
                "purl": purl,
                "source": source,
                "version": version,
            }
        )
    return sorted(records, key=lambda item: (item["name"], item["version"], item["source"]))


def command_version(*command: str) -> str:
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return (completed.stdout or completed.stderr).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--cargo-lock", type=Path, required=True)
    parser.add_argument("--cargo-toml", type=Path, required=True)
    parser.add_argument("--third-party-licenses", type=Path, required=True)
    parser.add_argument("--license", type=Path, required=True)
    parser.add_argument("--rights-approval", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    parser.add_argument("--container-digest", required=True)
    parser.add_argument("--debian-snapshot", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if not args.artifact.is_file() or args.artifact.suffix != ".deb":
        raise ValueError("artifact must be an existing .deb")
    created = dt.datetime.fromtimestamp(args.source_date_epoch, dt.UTC).isoformat().replace(
        "+00:00", "Z"
    )
    metadata_dependencies = target_dependency_records(
        cargo_license_generator().cargo_metadata(
            args.cargo_toml.resolve(), args.target
        ),
        args.cargo_toml.resolve(),
        args.cargo_lock,
    )
    if evidence_dependency_records(
        args.third_party_licenses, args.cargo_lock
    ) != metadata_dependencies:
        raise ValueError(
            "third-party license evidence differs from the target Cargo metadata closure"
        )
    provenance = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {"name": args.artifact.name, "digest": {"sha256": sha256(args.artifact)}}
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
                    {"uri": "Cargo.lock", "digest": {"sha256": sha256(args.cargo_lock)}},
                    {
                        "uri": "cargo-metadata:resolved-packages",
                        "digest": {
                            "sha256": canonical_json_sha256(metadata_dependencies)
                        },
                    },
                    {"uri": "Cargo.toml", "digest": {"sha256": sha256(args.cargo_toml)}},
                    {"uri": "LICENSE", "digest": {"sha256": sha256(args.license)}},
                    {
                        "uri": args.rights_approval.name,
                        "digest": {"sha256": sha256(args.rights_approval)},
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
                    "invocationId": f"harboros-im-gate-{args.source_commit}-{args.arch}",
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
    args.output.write_bytes(
        (
            json.dumps(provenance, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
    )


if __name__ == "__main__":
    main()

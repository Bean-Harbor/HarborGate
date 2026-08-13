#!/usr/bin/env python3
"""Generate deterministic SLSA provenance whose subject is the final deb."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_version(*command: str) -> str:
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return (completed.stdout or completed.stderr).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--cargo-lock", type=Path, required=True)
    parser.add_argument("--cargo-toml", type=Path, required=True)
    parser.add_argument("--license", type=Path, required=True)
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
                    {"uri": "Cargo.toml", "digest": {"sha256": sha256(args.cargo_toml)}},
                    {"uri": "LICENSE", "digest": {"sha256": sha256(args.license)}},
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
    args.output.write_text(
        json.dumps(provenance, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()

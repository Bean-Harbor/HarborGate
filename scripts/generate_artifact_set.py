#!/usr/bin/env python3
"""Generate the non-recursive artifact-set index for a Gate release bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    names = [
        f"{args.prefix}.deb",
        f"{args.prefix}.component-contract.json",
        f"{args.prefix}.k3-runtime-evidence-required.json",
        f"{args.prefix}.license-review.json",
        f"{args.prefix}.provenance.json",
        f"{args.prefix}.sbom.cdx.json",
        f"{args.prefix}.sbom.spdx.json",
    ]
    payload = {
        "schema_version": 1,
        "package": "harboros-im-gate",
        "version": args.version,
        "architecture": args.arch,
        "artifacts": [
            {"name": name, "sha256": sha256(args.bundle / name)} for name in names
        ],
    }
    args.output.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()

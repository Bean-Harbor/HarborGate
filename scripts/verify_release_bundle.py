#!/usr/bin/env python3
"""Verify a complete HarborGate release bundle and its deb provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_checksum_manifest(path: Path, expected_names: set[str]) -> None:
    entries: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            raise ValueError(f"invalid checksum manifest line: {path.name}")
        digest, name = parts
        name = name.lstrip("*")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"invalid SHA256 digest: {path.name}")
        if Path(name).name != name:
            raise ValueError(f"unsafe checksum manifest member: {name}")
        if name in entries:
            raise ValueError(f"duplicate checksum manifest member: {name}")
        entries[name] = digest
    if set(entries) != expected_names:
        raise ValueError(f"checksum manifest membership mismatch: {path.name}")
    for name, digest in entries.items():
        if digest != sha256(path.parent / name):
            raise ValueError(f"checksum manifest digest mismatch: {name}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()

    prefix = f"harboros-im-gate_{args.version}_{args.arch}"
    expected = {
        f"{prefix}.deb",
        f"{prefix}.deb.sha256",
        f"{prefix}.deb.materials.sha256",
        f"{prefix}.artifact-set.json",
        f"{prefix}.component-contract.json",
        f"{prefix}.k3-runtime-evidence-required.json",
        f"{prefix}.license-review.json",
        f"{prefix}.provenance.json",
        f"{prefix}.sbom.cdx.json",
        f"{prefix}.sbom.spdx.json",
    }
    actual = {path.name for path in args.bundle.iterdir() if path.is_file()}
    if actual != expected:
        raise ValueError(
            f"release bundle file set mismatch; missing={sorted(expected - actual)} "
            f"unexpected={sorted(actual - expected)}"
        )

    deb = args.bundle / f"{prefix}.deb"
    material_names = {
        f"{prefix}.artifact-set.json",
        f"{prefix}.component-contract.json",
        f"{prefix}.k3-runtime-evidence-required.json",
        f"{prefix}.license-review.json",
        f"{prefix}.provenance.json",
        f"{prefix}.sbom.cdx.json",
        f"{prefix}.sbom.spdx.json",
    }
    verify_checksum_manifest(
        args.bundle / f"{prefix}.deb.sha256", {f"{prefix}.deb"}
    )
    verify_checksum_manifest(
        args.bundle / f"{prefix}.deb.materials.sha256", material_names
    )

    provenance = json.loads((args.bundle / f"{prefix}.provenance.json").read_text())
    expected_subject = [{"name": deb.name, "digest": {"sha256": sha256(deb)}}]
    if provenance.get("subject") != expected_subject:
        raise ValueError("provenance subject does not bind the final deb name and digest")

    review = json.loads((args.bundle / f"{prefix}.license-review.json").read_text())
    if review.get("policy") != "fail-closed":
        raise ValueError("license review policy must be fail-closed")
    summary = review.get("dependency_summary", {})
    unresolved = summary.get("unresolved")
    if not isinstance(unresolved, int):
        raise ValueError("license review must report an unresolved dependency count")
    if review.get("release_eligible") != (unresolved == 0):
        raise ValueError("license review eligibility contradicts unresolved dependencies")
    for dependency in review.get("dependencies", []):
        if dependency.get("declared_license") != "NOASSERTION":
            raise ValueError("dependency license was inferred without repository evidence")

    artifact_set = json.loads((args.bundle / f"{prefix}.artifact-set.json").read_text())
    identity = {
        "package": "harboros-im-gate",
        "version": args.version,
        "architecture": args.arch,
    }
    for field, value in identity.items():
        if artifact_set.get(field) != value:
            raise ValueError(f"artifact-set {field} mismatch")
    members = artifact_set.get("artifacts", [])
    member_names = {member.get("name") for member in members}
    excluded = {
        f"{prefix}.artifact-set.json",
        f"{prefix}.deb.sha256",
        f"{prefix}.deb.materials.sha256",
    }
    if member_names != expected - excluded:
        raise ValueError("artifact-set membership is incomplete")
    for member in members:
        path = args.bundle / member["name"]
        if member.get("sha256") != sha256(path):
            raise ValueError(f"artifact-set digest mismatch: {path.name}")


if __name__ == "__main__":
    main()

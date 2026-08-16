#!/usr/bin/env python3
"""Verify the exact HarborGate qualification bundle and canonical materials."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


PACKAGE_NAME = "harboros-im-gate"
SOURCE_REPOSITORY = "https://github.com/Bean-Harbor/HarborGate"
COPYRIGHT_TEXT = "Copyright (c) 2026 Harborinno Ltd."
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def load_canonical_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    if payload != canonical_bytes(value):
        raise ValueError(f"{label} is not canonical JSON")
    return value


def verify_checksum_manifest(path: Path, expected: dict[str, str]) -> None:
    entries: dict[str, str] = {}
    try:
        payload = path.read_bytes()
        lines = payload.decode("ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"invalid checksum manifest: {path.name}") from exc
    for line in lines:
        parts = line.split("  ", 1)
        if len(parts) != 2:
            raise ValueError(f"invalid checksum manifest line: {path.name}")
        digest, name = parts
        if not SHA256_RE.fullmatch(digest):
            raise ValueError(f"invalid SHA256 digest: {path.name}")
        if "\\" in name or Path(name).name != name or name in {".", ".."}:
            raise ValueError(f"unsafe checksum manifest member: {name}")
        if name in entries:
            raise ValueError(f"duplicate checksum manifest member: {name}")
        entries[name] = digest
    expected_payload = "".join(
        f"{digest}  {name}\n" for name, digest in sorted(expected.items())
    ).encode("ascii")
    if entries != expected or payload != expected_payload:
        raise ValueError(f"checksum manifest membership mismatch: {path.name}")


def identities(value: Any, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty list")
    result: list[dict[str, str]] = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"filename", "kind", "sha256"}
            or not isinstance(item["filename"], str)
            or "\\" in item["filename"]
            or Path(item["filename"]).name != item["filename"]
            or not isinstance(item["kind"], str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", item["kind"])
            or not isinstance(item["sha256"], str)
            or not SHA256_RE.fullmatch(item["sha256"])
        ):
            raise ValueError(f"{label} contains an invalid identity")
        result.append(item)
    if len({item["kind"] for item in result}) != len(result):
        raise ValueError(f"{label} repeats a kind")
    if len({item["filename"] for item in result}) != len(result):
        raise ValueError(f"{label} repeats a filename")
    return result


def verify_sboms(
    bundle: Path,
    prefix: str,
    version: str,
    artifact_digest: str,
    decision: dict[str, Any],
) -> None:
    spdx = load_canonical_json(bundle / f"{prefix}.sbom.spdx.json", "SPDX SBOM")
    packages = spdx.get("packages")
    root_packages = [
        item
        for item in packages
        if isinstance(item, dict)
        and item.get("name") == PACKAGE_NAME
        and item.get("versionInfo") == version
        and {"algorithm": "SHA256", "checksumValue": artifact_digest}
        in item.get("checksums", [])
    ] if isinstance(packages, list) else []
    if len(root_packages) != 1:
        raise ValueError("SPDX SBOM does not bind the final deb")
    root = root_packages[0]
    if (
        root.get("licenseDeclared") != decision["declared_license"]
        or root.get("licenseConcluded") != decision["concluded_license"]
        or root.get("copyrightText") != decision["copyright"]
    ):
        raise ValueError("SPDX root license decision differs from release materials")
    package_ids = [item.get("SPDXID") for item in packages if isinstance(item, dict)]
    expected_relationship = {
        "relatedSpdxElement": root.get("SPDXID"),
        "relationshipType": "DESCRIBES",
        "spdxElementId": spdx.get("SPDXID"),
    }
    describes = [
        item
        for item in spdx.get("relationships", [])
        if isinstance(item, dict)
        and item.get("spdxElementId") == spdx.get("SPDXID")
        and item.get("relationshipType") == "DESCRIBES"
    ]
    if (
        len(package_ids) != len(set(package_ids))
        or spdx.get("SPDXID") in package_ids
        or describes != [expected_relationship]
    ):
        raise ValueError("SPDX package identities or DESCRIBES relationship are invalid")

    cdx = load_canonical_json(bundle / f"{prefix}.sbom.cdx.json", "CycloneDX SBOM")
    component = cdx.get("metadata", {}).get("component", {})
    properties = component.get("properties", []) if isinstance(component, dict) else []
    properties_by_name = {
        item.get("name"): item.get("value") for item in properties if isinstance(item, dict)
    }
    if (
        not isinstance(component, dict)
        or component.get("name") != PACKAGE_NAME
        or component.get("version") != version
        or {"alg": "SHA-256", "content": artifact_digest}
        not in component.get("hashes", [])
        or component.get("licenses") != [{"expression": decision["concluded_license"]}]
        or len(properties_by_name) != len(properties)
        or properties_by_name.items()
        < {
            "harboros:copyright": decision["copyright"],
            "harboros:license-concluded": decision["concluded_license"],
            "harboros:license-declared": decision["declared_license"],
        }.items()
    ):
        raise ValueError("CycloneDX SBOM does not bind the final deb decision")


def verify_bundle(args: argparse.Namespace) -> None:
    bundle = args.bundle
    prefix = f"{PACKAGE_NAME}_{args.version}_{args.arch}"
    deb_name = f"{prefix}.deb"
    descriptor_name = f"{deb_name}.release-materials.json"
    manifest_name = f"{deb_name}.materials.sha256"
    material_names = {
        deb_name,
        f"{deb_name}.sha256",
        f"{prefix}.LICENSE",
        f"{prefix}.artifact-set.json",
        f"{prefix}.component-contract.json",
        f"{prefix}.first-party-rights-approval.json",
        f"{prefix}.k3-runtime-evidence-required.json",
        f"{prefix}.license-review.json",
        f"{prefix}.provenance.json",
        f"{prefix}.sbom.cdx.json",
        f"{prefix}.sbom.spdx.json",
    }
    expected_names = material_names | {descriptor_name, manifest_name}
    actual_names = {path.name for path in bundle.iterdir() if path.is_file()}
    if actual_names != expected_names:
        raise ValueError(
            f"release bundle file set mismatch; missing={sorted(expected_names - actual_names)} "
            f"unexpected={sorted(actual_names - expected_names)}"
        )

    deb = bundle / deb_name
    artifact_digest = sha256(deb)
    if (bundle / f"{deb_name}.sha256").read_text(encoding="ascii") != (
        f"{artifact_digest}  {deb_name}\n"
    ):
        raise ValueError("deb checksum sidecar does not bind the final deb")

    descriptor = load_canonical_json(bundle / descriptor_name, "release descriptor")
    required_fields = {
        "architecture", "artifact", "bindings", "decision", "installed_evidence",
        "materials", "package", "schema_version", "source", "version",
    }
    if set(descriptor) != required_fields:
        raise ValueError("release descriptor has an invalid field set")
    source = descriptor.get("source")
    source_commit = source.get("commit") if isinstance(source, dict) else None
    if (
        descriptor.get("schema_version") != 1
        or descriptor.get("package") != PACKAGE_NAME
        or descriptor.get("version") != args.version
        or descriptor.get("architecture") != args.arch
        or source != {"commit": source_commit, "repo": SOURCE_REPOSITORY}
        or not isinstance(source_commit, str)
        or not COMMIT_RE.fullmatch(source_commit)
        or descriptor.get("artifact")
        != {
            "filename": deb_name,
            "kind": "deb",
            "sha256": artifact_digest,
            "size": deb.stat().st_size,
        }
    ):
        raise ValueError("release descriptor package identity changed")

    material_entries = identities(descriptor.get("materials"), "release materials")
    by_kind = {item["kind"]: item for item in material_entries}
    if set(item["filename"] for item in material_entries) != material_names:
        raise ValueError("release descriptor does not cover the exact material set")
    for item in material_entries:
        if item["sha256"] != sha256(bundle / item["filename"]):
            raise ValueError(f"release material digest mismatch: {item['filename']}")
    binding_entries = identities(descriptor.get("bindings"), "release bindings")
    required_bindings = {
        "component-contract", "license-review", "provenance", "sbom-cyclonedx",
        "sbom-spdx",
    }
    if {item["kind"] for item in binding_entries} != required_bindings:
        raise ValueError("release bindings are incomplete")
    if any(by_kind[item["kind"]] != item for item in binding_entries):
        raise ValueError("release binding differs from its material identity")

    installed = descriptor.get("installed_evidence")
    if not isinstance(installed, list) or not installed:
        raise ValueError("release descriptor lacks installed evidence")
    installed_kinds = set()
    for item in installed:
        if not isinstance(item, dict) or set(item) != {
            "filename", "installed_path", "kind", "sha256",
        }:
            raise ValueError("installed evidence contains an invalid identity")
        material_identity = {key: item[key] for key in ("filename", "kind", "sha256")}
        if by_kind.get(item["kind"]) != material_identity:
            raise ValueError("installed evidence differs from its material identity")
        installed_path = item["installed_path"]
        if (
            not isinstance(installed_path, str)
            or not installed_path.startswith("/")
            or "\\" in installed_path
            or "//" in installed_path
            or any(part in {"", ".", ".."} for part in installed_path.split("/")[1:])
        ):
            raise ValueError("installed evidence path is unsafe")
        installed_kinds.add(item["kind"])
    if not {"component-contract", "first-party-rights", "root-license"}.issubset(
        installed_kinds
    ):
        raise ValueError("installed evidence omits first-party approval or package identity")

    expected_manifest = {
        descriptor_name: sha256(bundle / descriptor_name),
        **{item["filename"]: item["sha256"] for item in material_entries},
    }
    verify_checksum_manifest(bundle / manifest_name, expected_manifest)

    decision = descriptor.get("decision")
    if not isinstance(decision, dict) or set(decision) != {
        "blocking_reasons", "concluded_license", "copyright", "declared_license",
        "policy", "release_eligible", "status",
    }:
        raise ValueError("release decision has an invalid field set")
    blockers = decision.get("blocking_reasons")
    eligible = decision.get("status") == "approved"
    if (
        decision.get("status") not in {"approved", "blocked"}
        or decision.get("policy") != "fail-closed"
        or decision.get("release_eligible") is not eligible
        or not isinstance(blockers, list)
        or (not blockers) is not eligible
        or decision.get("declared_license") != "MIT"
        or decision.get("concluded_license") != "MIT"
        or decision.get("copyright") != COPYRIGHT_TEXT
    ):
        raise ValueError("release decision is contradictory")

    review = load_canonical_json(bundle / f"{prefix}.license-review.json", "license review")
    first_party_rights = review.get("first_party_rights")
    for field, expected in (
        ("package", PACKAGE_NAME), ("version", args.version),
        ("architecture", args.arch), ("policy", decision["policy"]),
        ("review_status", decision["status"]),
        ("release_eligible", decision["release_eligible"]),
        ("blocking_reasons", blockers),
    ):
        if review.get(field) != expected:
            raise ValueError(f"license review differs from release decision: {field}")
    root = review.get("root_component", {})
    if (
        root.get("declared_license") != decision["declared_license"]
        or root.get("concluded_license") != decision["concluded_license"]
        or root.get("copyright") != decision["copyright"]
        or not isinstance(first_party_rights, dict)
        or first_party_rights.get("status") != "approved"
    ):
        raise ValueError("license review does not preserve the approved first-party decision")
    summary = review.get("dependency_summary", {})
    unresolved = summary.get("unresolved")
    if not isinstance(unresolved, int) or unresolved < 0:
        raise ValueError("license review must report an unresolved dependency count")
    if eligible is not (unresolved == 0):
        raise ValueError("license review eligibility contradicts unresolved dependencies")
    for dependency in review.get("dependencies", []):
        if (
            dependency.get("declared_license") != "NOASSERTION"
            or dependency.get("copyright") != "NOASSERTION"
        ):
            raise ValueError("dependency rights were inferred without repository evidence")
    if args.require_release_eligible and not eligible:
        raise ValueError("license review blocks formal release")

    rights = load_canonical_json(
        bundle / f"{prefix}.first-party-rights-approval.json", "first-party rights"
    )
    approval = rights.get("approval")
    third_party = rights.get("third_party")
    if (
        rights.get("package") != PACKAGE_NAME
        or rights.get("source") != source
        or not isinstance(approval, dict)
        or approval.get("decision") != "approved-for-distribution"
        or not isinstance(third_party, dict)
        or third_party.get("decision") != "separate-review-required"
    ):
        raise ValueError("first-party rights approval does not bind the package source")

    contract = load_canonical_json(
        bundle / f"{prefix}.component-contract.json", "component contract"
    )
    if contract.get("package") != PACKAGE_NAME or contract.get("source_commit") != source_commit:
        raise ValueError("component contract does not bind the package source")

    provenance = load_canonical_json(
        bundle / f"{prefix}.provenance.json", "package provenance"
    )
    if provenance.get("subject") != [
        {"digest": {"sha256": artifact_digest}, "name": deb_name}
    ]:
        raise ValueError("provenance subject does not bind the final deb name and digest")
    dependencies = provenance.get("predicate", {}).get("buildDefinition", {}).get(
        "resolvedDependencies", []
    )
    if not any(
        isinstance(item, dict)
        and item.get("uri") == f"git+{SOURCE_REPOSITORY}@{source_commit}"
        and item.get("digest") == {"gitCommit": source_commit}
        for item in dependencies
    ):
        raise ValueError("provenance does not bind the descriptor source")

    verify_sboms(bundle, prefix, args.version, artifact_digest, decision)

    artifact_set = load_canonical_json(
        bundle / f"{prefix}.artifact-set.json", "artifact set"
    )
    members = artifact_set.get("artifacts")
    expected_artifact_members = material_names - {f"{prefix}.artifact-set.json"}
    if (
        artifact_set.get("package") != PACKAGE_NAME
        or artifact_set.get("version") != args.version
        or artifact_set.get("architecture") != args.arch
        or not isinstance(members, list)
        or {item.get("name") for item in members if isinstance(item, dict)}
        != expected_artifact_members
        or len(members) != len(expected_artifact_members)
    ):
        raise ValueError("artifact set membership is incomplete")
    for member in members:
        path = bundle / member["name"]
        if member.get("sha256") != sha256(path):
            raise ValueError(f"artifact-set digest mismatch: {path.name}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--require-release-eligible", action="store_true")
    verify_bundle(parser.parse_args())


if __name__ == "__main__":
    main()

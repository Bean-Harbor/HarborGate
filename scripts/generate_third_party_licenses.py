#!/usr/bin/env python3
"""Generate checksum-bound license evidence for the Cargo target closure."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import re
import subprocess
import tarfile
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any


PACKAGE_NAME = "harboros-im-gate"
LICENSE_POLICY = "cargo-locked-target-closure-license-evidence-v1"
CARGO_LICENSE_REFERENCE = (
    "https://doc.rust-lang.org/cargo/reference/manifest.html#the-license-and-"
    "license-file-fields"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ROOT_LICENSE_RE = re.compile(
    r"^(?:license|licence|copying|copyright|notice|unlicense)(?:$|[._-].*)",
    re.IGNORECASE,
)
LEGACY_SLASH_RE = re.compile(
    r"^[A-Za-z0-9.+-]+(?:[ \t]*/[ \t]*[A-Za-z0-9.+-]+)+$"
)
# These are the reviewed identifiers in this target's legacy declarations.
# Do not infer the meaning of unknown identifiers or mixed SPDX/slash syntax.
LEGACY_LICENSE_IDS = {"MIT", "Apache-2.0"}
DEFAULT_SUPPLEMENTS = Path(__file__).resolve().parents[1] / "licenses/supplemental-materials.json"
MAX_EVIDENCE_FILE_SIZE = 4 * 1024 * 1024
MIT_HEADER_MARKERS = (
    "copyright (c)",
    "permission is hereby granted, free of charge",
    "the above copyright notice and this permission notice",
    'software is provided "as is"',
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def normalize_license_expression(value: str) -> tuple[str, str]:
    value = value.strip()
    if not value:
        raise ValueError("empty Cargo license declaration")
    if "/" not in value:
        return value, "spdx-expression-from-crate-manifest"
    if not LEGACY_SLASH_RE.fullmatch(value):
        raise ValueError(f"unsupported legacy Cargo license expression: {value}")
    identifiers = [item.strip(" \t") for item in value.split("/")]
    if not set(identifiers) <= LEGACY_LICENSE_IDS:
        raise ValueError(f"unreviewed legacy Cargo license identifier: {value}")
    return " OR ".join(identifiers), "cargo-legacy-slash-normalized-to-spdx-or"


def cargo_metadata(cargo_toml: Path, target: str) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "cargo",
            "metadata",
            "--locked",
            "--format-version",
            "1",
            "--filter-platform",
            target,
            "--manifest-path",
            str(cargo_toml),
        ],
        cwd=cargo_toml.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(f"cargo metadata failed: {detail}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"cargo metadata returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("cargo metadata must return a JSON object")
    return value


def target_closure(
    metadata: dict[str, Any], cargo_toml: Path
) -> list[tuple[dict[str, Any], list[str], list[str]]]:
    packages = metadata.get("packages")
    resolve = metadata.get("resolve")
    if not isinstance(packages, list) or not isinstance(resolve, dict):
        raise ValueError("cargo metadata lacks packages or a resolved dependency graph")
    package_by_id = {
        package.get("id"): package for package in packages if isinstance(package, dict)
    }
    nodes = resolve.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("cargo metadata lacks resolved nodes")
    node_by_id = {node.get("id"): node for node in nodes if isinstance(node, dict)}

    root_id = resolve.get("root")
    if root_id not in package_by_id:
        expected_manifest = cargo_toml.resolve()
        roots = [
            package.get("id")
            for package in packages
            if isinstance(package.get("manifest_path"), str)
            and Path(package["manifest_path"]).resolve() == expected_manifest
        ]
        if len(roots) != 1:
            raise ValueError("cannot identify the root Cargo package")
        root_id = roots[0]

    reachable = {root_id}
    dependency_kinds: dict[str, set[str]] = {}
    stack = [root_id]
    while stack:
        package_id = stack.pop()
        node = node_by_id.get(package_id)
        if not isinstance(node, dict):
            raise ValueError(f"resolved Cargo node is missing: {package_id}")
        deps = node.get("deps")
        if not isinstance(deps, list):
            raise ValueError(f"resolved Cargo dependencies are invalid: {package_id}")
        for dependency in deps:
            if not isinstance(dependency, dict) or dependency.get("pkg") not in package_by_id:
                raise ValueError(f"resolved Cargo dependency is invalid: {package_id}")
            kinds_payload = dependency.get("dep_kinds")
            if not isinstance(kinds_payload, list):
                raise ValueError(f"Cargo dependency kinds are invalid: {package_id}")
            kinds = {
                "normal" if item.get("kind") is None else item.get("kind")
                for item in kinds_payload
                if isinstance(item, dict)
            }
            active_kinds = {kind for kind in kinds if kind in {"normal", "build"}}
            if not active_kinds:
                continue
            dep_id = dependency["pkg"]
            dependency_kinds.setdefault(dep_id, set()).update(active_kinds)
            if dep_id not in reachable:
                reachable.add(dep_id)
                stack.append(dep_id)

    result = []
    for package_id in reachable - {root_id}:
        package = package_by_id[package_id]
        source = package.get("source")
        if not isinstance(source, str) or not source.startswith("registry+"):
            raise ValueError(
                f"non-registry dependency requires separate evidence: {package_id}"
            )
        node = node_by_id[package_id]
        features = node.get("features")
        if not isinstance(features, list) or any(not isinstance(item, str) for item in features):
            raise ValueError(f"Cargo features are invalid: {package_id}")
        result.append(
            (package, sorted(dependency_kinds.get(package_id, set())), sorted(features))
        )
    return sorted(
        result,
        key=lambda item: (
            item[0].get("name", ""),
            item[0].get("version", ""),
            item[0].get("source", ""),
        ),
    )


def locked_registry_packages(lock_path: Path) -> dict[tuple[str, str, str], dict[str, Any]]:
    payload = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages = payload.get("package")
    if not isinstance(packages, list):
        raise ValueError("Cargo.lock lacks package records")
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("source"), str):
            continue
        key = (package.get("name"), package.get("version"), package["source"])
        if not all(isinstance(item, str) and item for item in key):
            raise ValueError("Cargo.lock contains an invalid registry package identity")
        if key in result:
            raise ValueError(f"Cargo.lock repeats registry package identity: {key}")
        result[key] = package
    return result


def locate_crate_archive(package: dict[str, Any], checksum: str) -> Path:
    manifest_value = package.get("manifest_path")
    name = package.get("name")
    version = package.get("version")
    if not all(isinstance(item, str) and item for item in (manifest_value, name, version)):
        raise ValueError("Cargo metadata package identity is incomplete")
    package_dir = Path(manifest_value).resolve().parent
    registry_root = package_dir.parent.parent.parent
    filename = f"{name}-{version}.crate"
    candidates = sorted((registry_root / "cache").glob(f"*/{filename}"))
    matches = [candidate for candidate in candidates if sha256(candidate) == checksum]
    if not matches:
        raise ValueError(
            f"verified crate archive unavailable for {name} {version} ({checksum})"
        )
    return matches[0]


def safe_archive_members(
    archive: tarfile.TarFile, name: str, version: str
) -> dict[str, tarfile.TarInfo]:
    prefix = f"{name}-{version}/"
    result: dict[str, tarfile.TarInfo] = {}
    for member in archive.getmembers():
        if "\\" in member.name or not member.name.startswith(prefix):
            raise ValueError(f"unsafe crate archive member: {member.name}")
        relative = member.name.removeprefix(prefix)
        if not relative:
            continue
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe crate archive member: {member.name}")
        if not member.isfile():
            continue
        normalized = path.as_posix()
        if normalized in result:
            raise ValueError(f"duplicate crate archive member: {member.name}")
        result[normalized] = member
    return result


def read_member(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    if member.size > MAX_EVIDENCE_FILE_SIZE:
        raise ValueError(f"license evidence file is too large: {member.name}")
    source = archive.extractfile(member)
    if source is None:
        raise ValueError(f"cannot read crate archive member: {member.name}")
    payload = source.read(MAX_EVIDENCE_FILE_SIZE + 1)
    if len(payload) != member.size or len(payload) > MAX_EVIDENCE_FILE_SIZE:
        raise ValueError(f"invalid crate archive member size: {member.name}")
    return payload


def material_record(path: str, payload: bytes, basis: str) -> dict[str, Any]:
    try:
        content = payload.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        content = base64.b64encode(payload).decode("ascii")
        encoding = "base64"
    return {
        "basis": basis,
        "content": content,
        "encoding": encoding,
        "path": path,
        "sha256": sha256_bytes(payload),
    }


def supplemental_license_evidence(
    archive_path: Path,
    package: dict[str, Any],
    package_manifest: dict[str, Any],
    vcs: dict[str, Any],
    supplements_path: Path,
) -> list[dict[str, Any]]:
    """Read reviewed upstream material without altering the verified crate archive."""
    supplements = json.loads(supplements_path.read_bytes())
    if not isinstance(supplements, dict) or supplements.get("schema_version") != 1:
        raise ValueError("invalid supplemental license manifest")
    entries = supplements.get("entries")
    if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
        raise ValueError("invalid supplemental license entries")
    matches = [entry for entry in entries if (entry.get("name"), entry.get("version")) ==
               (package["name"], package["version"])]
    if len(matches) != 1:
        raise ValueError("missing or duplicate supplemental license identity")
    entry = matches[0]
    identity = {
        "name": package["name"], "version": package["version"],
        "source": package.get("source"), "archive_sha256": sha256(archive_path),
        "declared_license": package_manifest.get("license"),
        "repository": package_manifest.get("repository"),
        "commit": vcs.get("git", {}).get("sha1"),
        "crate_path": vcs.get("path_in_vcs"),
    }
    if any(not isinstance(value, str) or not value for value in identity.values()) or any(
        entry.get(key) != value for key, value in identity.items()
    ):
        raise ValueError("supplemental license crate identity mismatch")
    repository, commit = identity["repository"], identity["commit"]
    if identity["declared_license"] != "MIT" or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("unsupported supplemental license identity")
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("unsupported supplemental repository")
    material = entry.get("material")
    if not isinstance(material, dict):
        raise ValueError("missing supplemental license material")
    paths = [material.get("path"), material.get("upstream_path"), identity["crate_path"]]
    if any(not isinstance(path, str) or not path or "\\" in path or
           PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts for path in paths):
        raise ValueError("unsafe supplemental license path")
    expected_url = ("https://raw.githubusercontent.com/" + repository.removeprefix("https://github.com/")
                    + "/" + commit + "/" + material["upstream_path"])
    if material.get("url") != expected_url:
        raise ValueError("supplemental license URL is not bound to the crate commit")
    root = supplements_path.resolve().parent
    path = root / material["path"]
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("unsafe supplemental license material location")
    if path.stat().st_size > MAX_EVIDENCE_FILE_SIZE:
        raise ValueError("supplemental license material is too large")
    payload = path.read_bytes()
    if not SHA256_RE.fullmatch(str(material.get("sha256", ""))) or sha256_bytes(payload) != material["sha256"]:
        raise ValueError("supplemental license material checksum mismatch")
    if not all(marker in payload.decode("utf-8").lower() for marker in MIT_HEADER_MARKERS):
        raise ValueError("supplemental material lacks complete MIT text")
    record = material_record("upstream/" + material["upstream_path"], payload,
                             "checksum-bound-upstream-license-supplement")
    record["supplement"] = identity | {
        "url": expected_url, "local_path": material["path"],
        "manifest_sha256": sha256(supplements_path),
    }
    return [record]


def crate_license_evidence(
    archive_path: Path,
    package: dict[str, Any],
    declared_license: str,
    supplements_path: Path = DEFAULT_SUPPLEMENTS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    name = package["name"]
    version = package["version"]
    with tarfile.open(archive_path, mode="r:*") as archive:
        members = safe_archive_members(archive, name, version)
        manifest_member = members.get("Cargo.toml")
        if manifest_member is None:
            raise ValueError(f"crate archive lacks Cargo.toml: {name} {version}")
        manifest = tomllib.loads(read_member(archive, manifest_member).decode("utf-8"))
        package_manifest = manifest.get("package")
        if not isinstance(package_manifest, dict):
            raise ValueError(f"crate manifest lacks package metadata: {name} {version}")
        if (
            package_manifest.get("name") != name
            or package_manifest.get("version") != version
            or package_manifest.get("license") != declared_license
        ):
            raise ValueError(f"crate manifest identity/license mismatch: {name} {version}")

        candidates = {
            path
            for path in members
            if "/" not in path and ROOT_LICENSE_RE.fullmatch(path)
        }
        license_file = package_manifest.get("license-file")
        if isinstance(license_file, str) and license_file:
            normalized = PurePosixPath(license_file).as_posix()
            if normalized not in members:
                raise ValueError(
                    f"crate manifest license-file is absent: {name} {version} {normalized}"
                )
            candidates.add(normalized)

        basis = "package-root-license-material"
        if not candidates:
            concluded, _ = normalize_license_expression(declared_license)
            if concluded != "MIT":
                raise ValueError(f"crate lacks package-local license text: {name} {version}")
            embedded = []
            for path, member in sorted(members.items()):
                if not path.endswith(".rs"):
                    continue
                payload = read_member(archive, member)
                lowered = payload.decode("utf-8", errors="ignore").lower()
                if all(marker in lowered for marker in MIT_HEADER_MARKERS):
                    embedded.append(path)
            if not embedded:
                vcs_member = members.get(".cargo_vcs_info.json")
                if vcs_member is None:
                    raise ValueError(f"crate lacks complete embedded MIT text or VCS evidence: {name} {version}")
                vcs = json.loads(read_member(archive, vcs_member))
                if not isinstance(vcs, dict) or not isinstance(vcs.get("git"), dict):
                    raise ValueError("invalid crate VCS evidence")
                materials = supplemental_license_evidence(
                    archive_path, package, package_manifest, vcs, supplements_path
                )
                return materials, {key: package_manifest.get(key) for key in ("authors", "homepage", "repository")}
            candidates.update(embedded)
            basis = "complete-mit-license-header-in-package-source"

        materials = [
            material_record(path, read_member(archive, members[path]), basis)
            for path in sorted(candidates)
        ]
        manifest_identity = {
            "authors": package_manifest.get("authors", []),
            "homepage": package_manifest.get("homepage"),
            "repository": package_manifest.get("repository"),
        }
        return materials, manifest_identity


def generate_evidence(args: argparse.Namespace) -> dict[str, Any]:
    metadata = cargo_metadata(args.cargo_toml.resolve(), args.target)
    closure = target_closure(metadata, args.cargo_toml.resolve())
    locked = locked_registry_packages(args.cargo_lock)
    dependencies = []
    blocking_reasons = []
    for package, dependency_kinds, features in closure:
        name = package.get("name")
        version = package.get("version")
        source = package.get("source")
        key = (name, version, source)
        lock_record = locked.get(key)
        if lock_record is None:
            raise ValueError(f"target dependency is absent from Cargo.lock: {key}")
        checksum = lock_record.get("checksum")
        if not isinstance(checksum, str) or not SHA256_RE.fullmatch(checksum):
            raise ValueError(f"target dependency lacks a Cargo.lock checksum: {key}")
        declared_license = package.get("license")
        record: dict[str, Any] = {
            "cargo_package_id": package.get("id"),
            "checksum": checksum,
            "dependency_kinds": dependency_kinds,
            "features": features,
            "name": name,
            "source": source,
            "version": version,
        }
        try:
            archive_path = locate_crate_archive(package, checksum)
            if sha256(archive_path) != checksum:
                raise ValueError(f"crate archive checksum changed during review: {key}")
            record["archive"] = {
                "filename": archive_path.name,
                "sha256": checksum,
                "verification_status": "verified-against-cargo-lock",
            }
            if not isinstance(declared_license, str) or not declared_license.strip():
                raise ValueError("crate manifest lacks a license declaration")
            concluded_license, normalization = normalize_license_expression(
                declared_license
            )
            materials, manifest_identity = crate_license_evidence(
                archive_path, package, declared_license,
                getattr(args, "supplemental_materials", DEFAULT_SUPPLEMENTS),
            )
            record.update(
                {
                    "concluded_license": concluded_license,
                    "declared_license": declared_license,
                    "license_materials": materials,
                    "manifest_identity": manifest_identity,
                    "normalization": normalization,
                    "resolution_status": "resolved",
                    "review_basis": (
                        "Cargo.lock checksum-verified crate archive, crate manifest "
                        "declaration, and package-local or commit-bound upstream license materials"
                    ),
                }
            )
        except (OSError, UnicodeError, ValueError, tarfile.TarError) as exc:
            reason = f"{name} {version}: {exc}"
            blocking_reasons.append(reason)
            if "archive" not in record:
                record["archive"] = {
                    "expected_filename": f"{name}-{version}.crate",
                    "expected_sha256": checksum,
                    "verification_status": "unavailable-or-checksum-mismatch",
                }
            record.update(
                {
                    "blocking_reasons": [str(exc)],
                    "concluded_license": "NOASSERTION",
                    "declared_license": (
                        declared_license
                        if isinstance(declared_license, str) and declared_license.strip()
                        else "NOASSERTION"
                    ),
                    "license_materials": [],
                    "resolution_status": "blocked",
                    "review_basis": "fail-closed-unresolved-package-license-evidence",
                }
            )
        dependencies.append(record)

    resolved = sum(item["resolution_status"] == "resolved" for item in dependencies)
    created = dt.datetime.fromtimestamp(args.source_date_epoch, dt.UTC).isoformat().replace(
        "+00:00", "Z"
    )
    return {
        "architecture": args.arch,
        "blocking_reasons": blocking_reasons,
        "cargo_lock": {"filename": args.cargo_lock.name, "sha256": sha256(args.cargo_lock)},
        "cargo_license_reference": CARGO_LICENSE_REFERENCE,
        "dependency_summary": {
            "resolved": resolved,
            "total": len(dependencies),
            "unresolved": len(dependencies) - resolved,
        },
        "dependencies": dependencies,
        "generated_at": created,
        "package": PACKAGE_NAME,
        "policy": LICENSE_POLICY,
        "release_eligible": resolved == len(dependencies),
        "schema_version": 1,
        "scope": {
            "dependency_kinds": ["build", "normal"],
            "dev_dependencies_included": False,
            "target": args.target,
        },
        "version": args.version,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cargo-lock", type=Path, required=True)
    parser.add_argument("--cargo-toml", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--supplemental-materials", type=Path, default=DEFAULT_SUPPLEMENTS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = generate_evidence(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(canonical_bytes(payload))
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"third-party license generation failed: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

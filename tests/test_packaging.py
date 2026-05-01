import tomllib
from pathlib import Path


def test_python_fallback_pins_cffi_to_harboros_backend_version() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = set(pyproject["project"]["dependencies"])

    assert "cffi==1.17.1" in dependencies


def test_rust_runtime_exposes_packaging_binaries() -> None:
    workspace = tomllib.loads(Path("Cargo.toml").read_text(encoding="utf-8"))
    assert "rust/harborgate" in workspace["workspace"]["members"]

    manifest = tomllib.loads(Path("rust/harborgate/Cargo.toml").read_text(encoding="utf-8"))
    bin_names = {item["name"] for item in manifest["bin"]}

    assert "harborgate-rust" in bin_names
    assert "harborgate" in bin_names

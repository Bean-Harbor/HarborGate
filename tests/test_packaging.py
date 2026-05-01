import tomllib
from pathlib import Path


def test_python_fallback_pins_cffi_to_harboros_backend_version() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = set(pyproject["project"]["dependencies"])

    assert "cffi==1.17.1" in dependencies

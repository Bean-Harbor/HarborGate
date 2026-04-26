from pathlib import Path

from im_agent import harborbeacon


ROOT = Path(__file__).resolve().parents[1]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_v20_control_pack_documents_exist_and_are_active() -> None:
    required = [
        ROOT / "HarborBeacon-HarborGate-Agent-Contract-v2.0.md",
        ROOT / "HarborBeacon-HarborGate-v2.0-Upgrade-Runbook.md",
        ROOT / "HarborBeacon-HarborGate-v2.0-Cutover-Checklist.md",
    ]
    missing = [str(path) for path in required if not path.exists()]
    assert not missing

    contract = _read(ROOT / "HarborBeacon-HarborGate-Agent-Contract-v2.0.md")
    assert "POST /api/turns" in contract
    assert "conversation.handle" in contract
    assert "active_frame" in contract
    assert "continuation" in contract
    assert "delivery_hints" in contract
    assert "X-Contract-Version: 2.0" in contract


def test_gate_management_docs_point_to_v20() -> None:
    for name in ("README.md", "PLAN.md", "ROADMAP.md", "WORKLOG.md"):
        content = _read(ROOT / name)
        assert "HarborBeacon-HarborGate-Agent-Contract-v2.0.md" in content


def test_default_contract_version_is_v20() -> None:
    assert harborbeacon.DEFAULT_CONTRACT_VERSION == "2.0"


def test_active_client_no_longer_posts_tasks_endpoint() -> None:
    content = _read(ROOT / "src" / "im_agent" / "harborbeacon.py")
    assert '"/api/tasks"' not in content


def test_active_client_does_not_emit_args_resume_token() -> None:
    content = _read(ROOT / "src" / "im_agent" / "harborbeacon.py")
    forbidden = ["resume_token", '"args"']
    offenders = [pattern for pattern in forbidden if pattern in content]
    assert not offenders


def test_gateway_does_not_route_on_beacon_active_frame_kind() -> None:
    gateway_source = _read(ROOT / "src" / "im_agent" / "gateway.py")
    assert "active_frame.kind" not in gateway_source
    assert 'active_frame["kind"]' not in gateway_source

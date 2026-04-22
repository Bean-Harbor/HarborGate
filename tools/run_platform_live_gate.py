#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from im_agent.brain import RuleBasedBrain
from im_agent.gateway import GatewayService
from im_agent.harborbeacon import HarborBeaconTaskClient
from im_agent.models import OutboundMessage
from im_agent.platforms.feishu import FeishuAdapter, FeishuSettings
from im_agent.platforms.weixin import WeixinAdapter
from im_agent.session_store import FileSessionStore
from im_agent.setup_portal import FileSetupPortalStore


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def account_json_paths(accounts_dir: Path) -> list[Path]:
    ignored_suffixes = (
        ".sync.json",
        ".context_tokens.json",
        ".processed_messages.json",
    )
    return [
        path
        for path in sorted(accounts_dir.glob("*.json"))
        if not any(path.name.endswith(suffix) for suffix in ignored_suffixes)
    ]


def discover_weixin_state(state_dir: Path) -> dict[str, Any]:
    accounts_dir = state_dir / "accounts"
    if not accounts_dir.exists():
        return {}
    account_paths = account_json_paths(accounts_dir)
    if not account_paths:
        return {}
    account = load_json(account_paths[0])
    context_paths = sorted(accounts_dir.glob("*.context_tokens.json"))
    context_tokens = load_json(context_paths[0]) if context_paths else {}
    return {
        "account": account if isinstance(account, dict) else {},
        "context_tokens": context_tokens if isinstance(context_tokens, dict) else {},
    }


def discover_latest_weixin_ingress_probe(runtime_dir: Path) -> dict[str, Any]:
    probe_dir = runtime_dir / "weixin-ingress-probe"
    if not probe_dir.exists():
        return {}
    candidates = sorted(probe_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    latest_any: dict[str, Any] = {}
    for path in candidates:
        try:
            payload = load_json(path)
        except Exception:
            continue
        if isinstance(payload, dict):
            payload = dict(payload)
            payload["_report_path"] = str(path)
            if not latest_any:
                latest_any = payload
            if bool(payload.get("provider_private_text_seen")):
                return payload
    return latest_any


def build_feishu_adapter(state_root: Path) -> tuple[FeishuAdapter, dict[str, Any]]:
    state = FileSetupPortalStore(state_root).load_state().get("feishu") or {}
    if not isinstance(state, dict):
        state = {}
    settings = FeishuSettings(
        app_id=str(state.get("app_id") or ""),
        app_secret=str(state.get("app_secret") or ""),
        domain="feishu",
        connection_mode=str(state.get("connection_mode") or "websocket").strip() or "websocket",
        allowed_users=set(),
        group_policy="allowlist",
        bot_open_id=str(state.get("bot_open_id") or ""),
        bot_user_id=str(state.get("bot_user_id") or ""),
        bot_name=str(state.get("app_name") or ""),
        verification_token=str(state.get("verification_token") or ""),
        encrypt_key="",
        webhook_host="127.0.0.1",
        webhook_port=8765,
        webhook_path="/feishu/webhook",
        base_url="https://open.feishu.cn",
        auth_base_url="https://open.feishu.cn",
        enable_live_send=bool(state.get("enable_live_send", True)),
        timeout_seconds=20,
    )
    return FeishuAdapter(settings), state


def discover_feishu_route(session_root: Path) -> dict[str, Any]:
    route_path = session_root / "_routes.json"
    if not route_path.exists():
        return {}
    routes = load_json(route_path)
    if not isinstance(routes, dict):
        return {}
    for route in routes.values():
        if (
            isinstance(route, dict)
            and str(route.get("adapter_name") or "") == "feishu"
            and str(route.get("status") or "active") == "active"
        ):
            return dict(route)
    return {}


def build_gateway(
    *,
    adapter: Any,
    task_api_url: str = "",
    task_api_token: str = "",
) -> tuple[tempfile.TemporaryDirectory[str], GatewayService]:
    tmpdir = tempfile.TemporaryDirectory()
    store = FileSessionStore(Path(tmpdir.name) / "sessions", max_turns=20)
    task_client = (
        HarborBeaconTaskClient(base_url=task_api_url, api_token=task_api_token)
        if task_api_url
        else None
    )
    gateway = GatewayService(
        store=store,
        brain=RuleBasedBrain(),
        task_client=task_client,
    )
    gateway.register_adapter(adapter)
    return tmpdir, gateway


def register_live_route(
    gateway: GatewayService,
    *,
    route_key: str,
    platform: str,
    chat_id: str,
    user_id: str,
    adapter_name: str,
) -> None:
    gateway.store.register_route(
        route_key,
        {
            "route_key": route_key,
            "platform": platform,
            "chat_id": chat_id,
            "user_id": user_id,
            "adapter_name": adapter_name,
            "session_id": f"gw_sess_smoke_{adapter_name}",
            "status": "active",
        },
    )


def run_notification_replay(
    gateway: GatewayService,
    *,
    route_key: str = "",
    idempotency_key: str,
    title: str,
    body: str,
    destination: dict[str, Any] | None = None,
) -> dict[str, Any]:
    destination_payload = destination or {
        "kind": "conversation",
        "route_key": route_key,
    }
    payload = {
        "notification_id": f"notif_{slug_now()}",
        "trace_id": f"trace_{slug_now()}",
        "destination": destination_payload,
        "content": {
            "title": title,
            "body": body,
            "payload_format": "plain_text",
            "structured_payload": {},
            "attachments": [],
        },
        "delivery": {
            "mode": "send",
            "reply_to_message_id": "",
            "update_message_id": "",
            "idempotency_key": idempotency_key,
        },
    }
    first = gateway.handle_notification_delivery(payload)
    second = gateway.handle_notification_delivery(payload)
    classification = classify_notification_delivery(payload, first)
    return {
        "first_ok": bool(first.get("ok")),
        "first_status": str(first.get("status") or ""),
        "provider_message_id_present": bool(
            first.get("provider_message_id")
        ),
        "replay_identical": first == second,
        **classification,
    }


def classify_notification_delivery(payload: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    destination = payload.get("destination")
    destination = destination if isinstance(destination, dict) else {}
    route_key = str(destination.get("route_key") or "").strip()
    recipient = destination.get("recipient")
    recipient = recipient if isinstance(recipient, dict) else {}
    route_mode = "source_bound" if route_key else "proactive"
    if not route_key and not (str(destination.get("platform") or "").strip() or str(destination.get("id") or "").strip() or str(recipient.get("recipient_id") or "").strip()):
        route_mode = "unknown"
    route_source = "route_key" if route_key else "platform_id" if str(destination.get("id") or "").strip() else "recipient"
    ok = bool(response.get("ok"))
    retryable = bool(response.get("retryable"))
    error_block = response.get("error")
    error_block = error_block if isinstance(error_block, dict) else {}
    failure_class = str(error_block.get("code") or "").strip()
    queue_state = "complete" if ok else ("retry_queue" if retryable else "terminal_failure")
    return {
        "route_mode": route_mode,
        "route_source": route_source,
        "failure_class": failure_class,
        "queue_state": queue_state,
        "delivery_class": "sent" if ok else "failed",
    }


def response_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def turn_observability(
    response: dict[str, Any],
    *,
    request_message_id: str,
) -> dict[str, Any]:
    metadata = response_metadata(response)
    return {
        "request_message_id": request_message_id,
        "delivery_sent": bool(response.get("sent")),
        "provider_message_id_present": bool(
            response.get("message_id") or response.get("provider_message_id")
        ),
        "status": str(metadata.get("status") or ""),
        "task_id_present": bool(str(metadata.get("task_id") or "").strip()),
        "trace_id_present": bool(str(metadata.get("trace_id") or "").strip()),
        "route_key_present": bool(str(metadata.get("route_key") or "").strip()),
        "resume_token_present": bool(str(metadata.get("resume_token") or "").strip()),
    }


def notification_replay_ready(summary: dict[str, Any]) -> bool:
    return bool(summary.get("first_ok") and summary.get("replay_identical"))


def proactive_notification_replay_ready(summary: dict[str, Any]) -> bool:
    return bool(
        notification_replay_ready(summary)
        and str(summary.get("route_mode") or "") == "proactive"
        and str(summary.get("delivery_class") or "") == "sent"
    )


def classify_weixin_blocker(result: dict[str, Any]) -> str:
    if not result.get("configured"):
        return "saved_account_missing_or_invalid"

    poll = result.get("poll")
    poll = poll if isinstance(poll, dict) else {}
    ingress_probe = result.get("ingress_probe")
    ingress_probe = ingress_probe if isinstance(ingress_probe, dict) else {}
    poll_status = str(poll.get("status") or "").strip().lower()
    poll_outcome = str(poll.get("outcome") or "").strip().lower()
    error_text = str(poll.get("error") or "").strip().lower()
    private_text_message_count = int(poll.get("private_text_message_count") or 0)
    private_text_confirmed = bool(ingress_probe.get("provider_private_text_seen"))

    if poll_status == "timeout":
        return "weixin_poll_timeout"
    if poll_status == "error":
        if any(marker in error_text for marker in ("401", "403", "auth", "token", "forbidden")):
            return "weixin_provider_auth_failed"
        return "weixin_poll_error"
    if private_text_message_count <= 0 and poll_status == "ok" and not private_text_confirmed:
        if poll_outcome in {"idle_timeout", "empty"}:
            return "weixin_waiting_for_private_text"
        return "weixin_live_ingress_not_confirmed"

    live_send = result.get("live_send")
    live_send = live_send if isinstance(live_send, dict) else {}
    if str(live_send.get("status") or "").strip().lower() != "sent":
        return "weixin_live_send_failed"

    notification = result.get("notification_replay")
    notification = notification if isinstance(notification, dict) else {}
    if not notification_replay_ready(notification):
        return "weixin_notification_replay_failed"

    rehearsal = result.get("harborbeacon_rehearsal")
    rehearsal = rehearsal if isinstance(rehearsal, dict) else {}
    if rehearsal and not rehearsal.get("rehearsal_ready"):
        return "weixin_harborbeacon_rehearsal_failed"

    if private_text_confirmed:
        return ""

    return "weixin_live_ingress_not_confirmed"


def classify_weixin_ingress_blocker(result: dict[str, Any]) -> str:
    if not result.get("configured"):
        return "account_restore"

    poll = result.get("poll")
    poll = poll if isinstance(poll, dict) else {}
    ingress_probe = result.get("ingress_probe")
    ingress_probe = ingress_probe if isinstance(ingress_probe, dict) else {}
    poll_status = str(poll.get("status") or "").strip().lower()
    error_text = str(poll.get("error") or "").strip().lower()
    private_text_message_count = int(poll.get("private_text_message_count") or 0)
    private_text_confirmed = bool(ingress_probe.get("provider_private_text_seen"))

    if poll_status == "timeout":
        return "getupdates"
    if poll_status == "error":
        if any(marker in error_text for marker in ("401", "403", "auth", "token", "forbidden")):
            return "qr_recovery"
        return "getupdates"
    if private_text_message_count <= 0 and not private_text_confirmed:
        return "getupdates"

    live_send = result.get("live_send")
    live_send = live_send if isinstance(live_send, dict) else {}
    if str(live_send.get("status") or "").strip().lower() != "sent":
        return "context_token_send"

    return ""


def run_weixin_task_rehearsal(
    adapter: WeixinAdapter,
    *,
    chat_id: str,
    context_token: str,
    task_api_url: str,
    task_api_token: str,
) -> dict[str, Any]:
    tmpdir, gateway = build_gateway(
        adapter=adapter,
        task_api_url=task_api_url,
        task_api_token=task_api_token,
    )
    try:
        status_payload = {
            "from_user_id": chat_id,
            "context_token": context_token,
            "msg_id": f"wx-smoke-status-{slug_now()}",
            "item_list": [{"type": 1, "text_item": {"text": "status ssh"}}],
            "intent": {"domain": "service", "action": "status"},
            "args": {"service_name": "ssh"},
        }
        first_payload = {
            "from_user_id": chat_id,
            "context_token": context_token,
            "msg_id": f"wx-smoke-restart-{slug_now()}",
            "item_list": [{"type": 1, "text_item": {"text": "restart ssh"}}],
            "intent": {"domain": "service", "action": "restart"},
            "args": {"service_name": "ssh"},
        }
        second_payload = {
            "from_user_id": chat_id,
            "context_token": context_token,
            "msg_id": f"wx-smoke-approve-{slug_now()}",
            "item_list": [
                {
                    "type": 1,
                    "text_item": {"text": "approval_token approved"},
                }
            ],
            "intent": {"domain": "service", "action": "restart"},
            "args": {
                "service_name": "ssh",
                "approval_token": "approved",
            },
        }
        status_response = gateway.handle_inbound("weixin", status_payload)
        first = gateway.handle_inbound("weixin", first_payload)
        second = gateway.handle_inbound("weixin", second_payload)
        replay = gateway.handle_inbound("weixin", first_payload)
        metadata = gateway.store.load_metadata("weixin", chat_id)
        notification = run_notification_replay(
            gateway,
            route_key=str(metadata.get("route_key") or ""),
            idempotency_key=f"idem_weixin_smoke_{slug_now()}",
            title="HarborBeacon",
            body="Weixin live gate notification replay smoke",
        )
        proactive_notification = run_notification_replay(
            gateway,
            route_key="",
            idempotency_key=f"idem_weixin_proactive_{slug_now()}",
            title="HarborBeacon",
            body="Weixin proactive notification replay smoke",
            destination={
                "kind": "conversation",
                "platform": "weixin",
                "id": chat_id,
                "recipient": {
                    "recipient_id": chat_id,
                    "recipient_type": "user",
                },
            },
        )
        status_turn = turn_observability(
            status_response,
            request_message_id=str(status_payload["msg_id"]),
        )
        restart_turn = turn_observability(
            first,
            request_message_id=str(first_payload["msg_id"]),
        )
        resume_turn = turn_observability(
            second,
            request_message_id=str(second_payload["msg_id"]),
        )
        replay_turn = turn_observability(
            replay,
            request_message_id=str(first_payload["msg_id"]),
        )
        transport = adapter.transport_status()
        session_pointer_preserved = str(metadata.get("last_message_id") or "") == str(
            second_payload["msg_id"]
        )
        ingress_observability = {
            "last_poll_at": str(transport.get("last_poll_at") or "").strip(),
            "last_getupdates_at": str(transport.get("last_getupdates_at") or "").strip(),
            "last_getupdates_buf": str(transport.get("last_getupdates_buf") or "").strip(),
            "last_getupdates_count": int(transport.get("last_getupdates_count") or 0),
            "last_private_text_message_count": int(transport.get("last_private_text_message_count") or 0),
            "last_getupdates_message_ids": list(transport.get("last_getupdates_message_ids") or []),
            "last_getupdates_private_message_ids": list(transport.get("last_getupdates_private_message_ids") or []),
            "last_getupdates_error": str(transport.get("last_getupdates_error") or "").strip(),
            "last_inbound_at": str(transport.get("last_inbound_at") or "").strip(),
            "last_inbound_message_id": str(transport.get("last_inbound_message_id") or "").strip(),
            "last_inbound_chat_id": str(transport.get("last_inbound_chat_id") or "").strip(),
        }
        outbound_observability = {
            "last_send_at": str(transport.get("last_send_at") or "").strip(),
            "last_send_status": str(transport.get("last_send_status") or "").strip(),
            "last_send_chunk_count": int(transport.get("last_send_chunk_count") or 0),
            "last_send_retryable": bool(transport.get("last_send_retryable")),
            "last_send_provider_message_id": str(transport.get("last_send_provider_message_id") or "").strip(),
            "last_send_context_token_used": bool(transport.get("last_send_context_token_used")),
            "last_send_error": str(transport.get("last_send_error") or "").strip(),
        }
        rehearsal_ready = bool(
            status_turn["status"] == "completed"
            and restart_turn["status"] == "needs_input"
            and restart_turn["resume_token_present"]
            and resume_turn["status"] == "completed"
            and restart_turn["route_key_present"]
            and resume_turn["route_key_present"]
            and notification_replay_ready(notification)
            and proactive_notification_replay_ready(proactive_notification)
            and replay_turn["status"] == "needs_input"
            and session_pointer_preserved
        )
        return {
            "ran": True,
            "ingress_mode": "synthetic_gateway_payload",
            "status_turn": status_turn,
            "restart_turn": restart_turn,
            "resume_turn": resume_turn,
            "replay_turn": replay_turn,
            "route_key_present": bool(metadata.get("route_key")),
            "replay_task_id_matches": (
                str(response_metadata(first).get("task_id") or "")
                == str(response_metadata(replay).get("task_id") or "")
            ),
            "session_pointer_preserved": session_pointer_preserved,
            "ingress_observability": ingress_observability,
            "outbound_observability": outbound_observability,
            "notification_replay": notification,
            "proactive_notification_replay": proactive_notification,
            "delivery_observability": gateway.store.summarize_delivery_records(),
            "delivery_health": gateway.store.summarize_delivery_health(),
            "rehearsal_ready": rehearsal_ready,
        }
    finally:
        tmpdir.cleanup()


def run_feishu_task_rehearsal(
    adapter: FeishuAdapter,
    *,
    chat_id: str,
    user_id: str,
    task_api_url: str,
    task_api_token: str,
) -> dict[str, Any]:
    tmpdir, gateway = build_gateway(
        adapter=adapter,
        task_api_url=task_api_url,
        task_api_token=task_api_token,
    )
    try:
        status_payload = {
            "chat_id": chat_id,
            "user_id": user_id,
            "text": "status ssh",
            "message_id": f"feishu-smoke-status-{slug_now()}",
            "chat_type": "p2p",
            "intent": {"domain": "service", "action": "status"},
            "args": {"service_name": "ssh"},
        }
        restart_payload = {
            "chat_id": chat_id,
            "user_id": user_id,
            "text": "restart ssh",
            "message_id": f"feishu-smoke-restart-{slug_now()}",
            "chat_type": "p2p",
            "intent": {"domain": "service", "action": "restart"},
            "args": {"service_name": "ssh"},
        }
        approve_payload = {
            "chat_id": chat_id,
            "user_id": user_id,
            "text": "approval_token approved",
            "message_id": f"feishu-smoke-approve-{slug_now()}",
            "chat_type": "p2p",
            "intent": {"domain": "service", "action": "restart"},
            "args": {"service_name": "ssh", "approval_token": "approved"},
        }
        files_payload = {
            "chat_id": chat_id,
            "user_id": user_id,
            "text": "list /mnt",
            "message_id": f"feishu-smoke-files-{slug_now()}",
            "chat_type": "p2p",
            "intent": {"domain": "files", "action": "list"},
            "args": {"path": "/mnt"},
        }

        status_response = gateway.handle_inbound("feishu", status_payload)
        restart_response = gateway.handle_inbound("feishu", restart_payload)
        approve_response = gateway.handle_inbound("feishu", approve_payload)
        files_response = gateway.handle_inbound("feishu", files_payload)
        replay_response = gateway.handle_inbound("feishu", restart_payload)
        metadata = gateway.store.load_metadata("feishu", chat_id)
        notification = run_notification_replay(
            gateway,
            route_key=str(metadata.get("route_key") or ""),
            idempotency_key=f"idem_feishu_smoke_{slug_now()}",
            title="HarborBeacon",
            body="Feishu rollback notification replay smoke",
        )
        proactive_notification = run_notification_replay(
            gateway,
            route_key="",
            idempotency_key=f"idem_feishu_proactive_{slug_now()}",
            title="HarborBeacon",
            body="Feishu proactive notification replay smoke",
            destination={
                "kind": "conversation",
                "platform": "feishu",
                "id": chat_id,
                "recipient": {
                    "recipient_id": user_id or chat_id,
                    "recipient_type": "open_id",
                },
            },
        )
        status_turn = turn_observability(
            status_response,
            request_message_id=str(status_payload["message_id"]),
        )
        restart_turn = turn_observability(
            restart_response,
            request_message_id=str(restart_payload["message_id"]),
        )
        resume_turn = turn_observability(
            approve_response,
            request_message_id=str(approve_payload["message_id"]),
        )
        files_turn = turn_observability(
            files_response,
            request_message_id=str(files_payload["message_id"]),
        )
        replay_turn = turn_observability(
            replay_response,
            request_message_id=str(restart_payload["message_id"]),
        )
        session_pointer_preserved = str(metadata.get("last_message_id") or "") == str(
            files_payload["message_id"]
        )
        replay_task_id_matches = (
            str(response_metadata(restart_response).get("task_id") or "")
            == str(response_metadata(replay_response).get("task_id") or "")
        )
        rehearsal_ready = bool(
            status_turn["status"] == "completed"
            and restart_turn["status"] == "needs_input"
            and restart_turn["resume_token_present"]
            and resume_turn["status"] == "completed"
            and files_turn["status"] == "completed"
            and status_turn["task_id_present"]
            and restart_turn["task_id_present"]
            and resume_turn["task_id_present"]
            and files_turn["task_id_present"]
            and status_turn["trace_id_present"]
            and restart_turn["trace_id_present"]
            and resume_turn["trace_id_present"]
            and files_turn["trace_id_present"]
            and restart_turn["route_key_present"]
            and resume_turn["route_key_present"]
            and files_turn["route_key_present"]
            and notification_replay_ready(notification)
            and proactive_notification_replay_ready(proactive_notification)
            and replay_task_id_matches
            and session_pointer_preserved
        )
        return {
            "ran": True,
            "ingress_mode": "synthetic_gateway_payload",
            "status_turn": status_turn,
            "restart_turn": restart_turn,
            "resume_turn": resume_turn,
            "files_turn": files_turn,
            "replay_turn": replay_turn,
            "route_key_present": bool(metadata.get("route_key")),
            "replay_task_id_matches": replay_task_id_matches,
            "session_pointer_preserved": session_pointer_preserved,
            "notification_replay": notification,
            "proactive_notification_replay": proactive_notification,
            "delivery_observability": gateway.store.summarize_delivery_records(),
            "delivery_health": gateway.store.summarize_delivery_health(),
            "rehearsal_ready": rehearsal_ready,
        }
    finally:
        tmpdir.cleanup()


def run_weixin_surface(args: argparse.Namespace) -> dict[str, Any]:
    state_dir = Path(args.weixin_state_dir)
    runtime_dir = Path(args.runtime_dir)
    state = discover_weixin_state(state_dir)
    latest_probe = discover_latest_weixin_ingress_probe(runtime_dir)
    account = state.get("account") or {}
    context_tokens = state.get("context_tokens") or {}
    account_id = str(account.get("account_id") or "")
    adapter = WeixinAdapter(state_dir=state_dir, account_id=account_id)
    result: dict[str, Any] = {
        "surface": "weixin",
        "configured": bool(adapter.configured),
        "context_token_count": len(context_tokens),
        "gate_complete": False,
        "ingress_blocker_category": "",
        "ingress_probe": latest_probe,
    }

    if not adapter.configured:
        result["blocked_reason"] = "saved_account_missing_or_invalid"
        result["blocker_category"] = "saved_account_missing_or_invalid"
        return result

    try:
        messages = adapter.poll_updates(timeout_ms=args.weixin_poll_timeout_ms)
        transport = dict(adapter.transport_status())
        private_messages = [
            item
            for item in messages
            if isinstance(item, dict) and not str(item.get("room_id") or "").strip()
        ]
        result["poll"] = {
            "status": "ok",
            "outcome": str(transport.get("last_poll_outcome") or ("messages" if messages else "empty")).strip(),
            "message_count": len(messages),
            "private_text_message_count": len(private_messages),
        }
    except Exception as exc:
        status = "timeout" if "timed out" in str(exc).lower() else "error"
        result["poll"] = {
            "status": status,
            "error": str(exc),
        }

    if context_tokens:
        chat_id = str(next(iter(context_tokens.keys())))
        try:
            send_result = adapter.send_outbound(
                OutboundMessage(
                    platform="weixin",
                    chat_id=chat_id,
                    text=f"HarborGate Weixin live gate smoke {utc_now()}",
                )
            )
            result["live_send"] = {
                "status": "sent",
                "provider_message_id_present": bool(send_result.get("message_id")),
                "context_token_used": bool(
                    (send_result.get("metadata") or {}).get("context_token_used")
                ),
            }
        except Exception as exc:
            result["live_send"] = {
                "status": "error",
                "error": str(exc),
            }

        tmpdir, gateway = build_gateway(adapter=adapter)
        try:
            register_live_route(
                gateway,
                route_key=f"gw_route_weixin_live_gate_{slug_now()}",
                platform="weixin",
                chat_id=chat_id,
                user_id=chat_id,
                adapter_name="weixin",
            )
            result["notification_replay"] = run_notification_replay(
                gateway,
                route_key=next(iter(load_json(Path(tmpdir.name) / "sessions" / "_routes.json").keys())),
                idempotency_key=f"idem_weixin_live_gate_{slug_now()}",
                title="HarborGate",
                body="Weixin notification replay live smoke",
            )
            result["transport"] = dict(adapter.transport_status())
        finally:
            tmpdir.cleanup()

        if args.task_api_url:
            result["harborbeacon_rehearsal"] = run_weixin_task_rehearsal(
                adapter,
                chat_id=chat_id,
                context_token=str(context_tokens[chat_id]),
                task_api_url=args.task_api_url,
                task_api_token=args.task_api_token,
            )

    result["gate_complete"] = bool(
        result.get("configured")
        and (
            (
                result.get("poll", {}).get("status") == "ok"
                and int(result.get("poll", {}).get("private_text_message_count") or 0) > 0
            )
            or bool(latest_probe.get("provider_private_text_seen"))
        )
        and result.get("live_send", {}).get("status") == "sent"
        and notification_replay_ready(
            result.get("notification_replay")
            if isinstance(result.get("notification_replay"), dict)
            else {}
        )
    )
    rehearsal_ready = bool(
        not args.task_api_url
        or (
            isinstance(result.get("harborbeacon_rehearsal"), dict)
            and bool(result["harborbeacon_rehearsal"].get("rehearsal_ready"))
        )
    )
    result["rehearsal_ready"] = bool(result["gate_complete"] and rehearsal_ready)
    result["ingress_blocker_category"] = classify_weixin_ingress_blocker(result)
    if not result["rehearsal_ready"]:
        poll = result.get("poll")
        poll = poll if isinstance(poll, dict) else {}
        waiting_for_private_text = bool(
            result["ingress_blocker_category"] == "getupdates"
            and str(poll.get("status") or "").strip().lower() == "ok"
            and int(poll.get("private_text_message_count") or 0) <= 0
            and not bool(latest_probe.get("provider_private_text_seen"))
        )
        result["blocked_reason"] = (
            "weixin_waiting_for_private_text"
            if waiting_for_private_text
            else result["ingress_blocker_category"] or "weixin_live_ingress_not_confirmed"
        )
        result["blocker_category"] = classify_weixin_blocker(result)
    return result


def run_feishu_surface(args: argparse.Namespace) -> dict[str, Any]:
    state_root = Path(args.feishu_state_root)
    session_root = Path(args.session_root)
    adapter, state = build_feishu_adapter(state_root)
    result: dict[str, Any] = {
        "surface": "feishu",
        "configured": bool(adapter.configured),
        "live_send_enabled": bool(adapter.settings.enable_live_send),
    }

    if not adapter.configured:
        result["blocked_reason"] = "saved_feishu_credentials_missing"
        return result

    transport = adapter.transport_status()
    result["transport"] = {
        "mode": str(transport.get("mode") or ""),
        "status": str(transport.get("status") or ""),
        "connected": bool(transport.get("connected")),
    }
    try:
        bot_info = adapter.fetch_bot_info()
        result["bot_info"] = {
            "status": "ok",
            "has_app_name": bool(bot_info.get("app_name")),
            "has_open_id": bool(bot_info.get("open_id")),
        }
    except Exception as exc:
        result["bot_info"] = {
            "status": "error",
            "error": str(exc),
        }

    route = discover_feishu_route(session_root)
    chat_id = str(route.get("chat_id") or "")
    user_id = str(route.get("user_id") or "")
    if chat_id:
        try:
            send_result = adapter.send_outbound(
                OutboundMessage(
                    platform="feishu",
                    chat_id=chat_id,
                    text=f"HarborGate Feishu rollback smoke {utc_now()}",
                )
            )
            result["live_send"] = {
                "status": "sent",
                "provider_message_id_present": bool(
                    send_result.get("message_id") or send_result.get("provider_message_id")
                ),
            }
        except Exception as exc:
            result["live_send"] = {
                "status": "error",
                "error": str(exc),
            }

        tmpdir, gateway = build_gateway(adapter=adapter)
        try:
            route_key = f"gw_route_feishu_live_gate_{slug_now()}"
            register_live_route(
                gateway,
                route_key=route_key,
                platform="feishu",
                chat_id=chat_id,
                user_id=user_id or chat_id,
                adapter_name="feishu",
            )
            result["notification_replay"] = run_notification_replay(
                gateway,
                route_key=route_key,
                idempotency_key=f"idem_feishu_live_gate_{slug_now()}",
                title="HarborGate",
                body="Feishu rollback notification replay smoke",
            )
        finally:
            tmpdir.cleanup()

        if args.task_api_url:
            result["harborbeacon_rehearsal"] = run_feishu_task_rehearsal(
                adapter,
                chat_id=chat_id,
                user_id=user_id or chat_id,
                task_api_url=args.task_api_url,
                task_api_token=args.task_api_token,
            )

    result["rollback_ready"] = bool(
        result.get("bot_info", {}).get("status") == "ok"
        and result.get("live_send", {}).get("status") == "sent"
        and result.get("notification_replay", {}).get("first_ok")
    )
    rehearsal_ready = bool(
        not args.task_api_url
        or (
            isinstance(result.get("harborbeacon_rehearsal"), dict)
            and bool(result["harborbeacon_rehearsal"].get("rehearsal_ready"))
        )
    )
    result["rehearsal_ready"] = bool(result["rollback_ready"] and rehearsal_ready)
    if not result["rollback_ready"]:
        result["blocked_reason"] = "feishu_live_send_or_notification_unavailable"
    elif not result["rehearsal_ready"]:
        result["blocked_reason"] = "feishu_harborbeacon_rehearsal_failed"
    return result


def summarize_decision(report: dict[str, Any]) -> None:
    weixin = report.get("weixin") or {}
    feishu = report.get("feishu") or {}
    report["feishu"] = dict(feishu)
    report["weixin"] = dict(weixin)
    report["feishu"]["rehearsal_ready"] = bool(feishu.get("rehearsal_ready"))
    report["weixin"]["rehearsal_ready"] = bool(weixin.get("rehearsal_ready"))
    report["weixin_blocker_category"] = str(
        weixin.get("ingress_blocker_category")
        or weixin.get("blocker_category")
        or weixin.get("blocked_reason")
        or ""
    )
    report["parity_ready"] = bool(
        report["feishu"]["rehearsal_ready"] and report["weixin"]["rehearsal_ready"]
    )
    delivery_health = weixin.get("delivery_health") if isinstance(weixin.get("delivery_health"), dict) else {}
    if not delivery_health:
        delivery_health = feishu.get("delivery_health") if isinstance(feishu.get("delivery_health"), dict) else {}
    delivery_health = dict(delivery_health) if isinstance(delivery_health, dict) else {}
    source_bound_health = delivery_health.get("source_bound") if isinstance(delivery_health.get("source_bound"), dict) else {}
    proactive_health = delivery_health.get("proactive") if isinstance(delivery_health.get("proactive"), dict) else {}
    report["delivery_policy"] = {
        "interactive_reply": "source_bound",
        "proactive_delivery": "user-default-configured",
    }
    report["delivery_health"] = delivery_health
    report["release_v1"] = {
        "delivery_policy": report["delivery_policy"],
        "feishu_rehearsal_ready": bool(report["feishu"]["rehearsal_ready"]),
        "weixin_rehearsal_ready": bool(report["weixin"]["rehearsal_ready"]),
        "parity_ready": bool(report["parity_ready"]),
        "dual_surface_ready": bool(report["parity_ready"]),
        "decision": "",
        "decision_reason": "",
        "weixin_blocker_category": str(report["weixin_blocker_category"] or ""),
        "source_bound_delivery_health": dict(source_bound_health),
        "proactive_delivery_health": dict(proactive_health),
        "delivery_health": delivery_health,
        "weixin_ingress_proof": dict(weixin.get("ingress_probe") or {}),
        "release_v1_ready": bool(
            bool(report["parity_ready"])
            and bool(source_bound_health.get("ready"))
            and bool(proactive_health.get("ready"))
        ),
    }
    if report["parity_ready"]:
        report["decision"] = "dual_surface_ready"
        report["decision_reason"] = "feishu_and_weixin_rehearsal_ready"
    elif report["feishu"]["rehearsal_ready"]:
        report["decision"] = "feishu_baseline_with_weixin_parity_track"
        report["decision_reason"] = report["weixin_blocker_category"] or "weixin_parity_pending"
    else:
        report["decision"] = "blocked"
        report["decision_reason"] = "|".join(
                part
                for part in [
                    str(weixin.get("blocker_category") or weixin.get("blocked_reason") or "").strip(),
                    str(feishu.get("blocked_reason") or "").strip(),
                ]
                if part
        ) or "no_live_surface_ready"
    report["release_v1"]["decision"] = str(report.get("decision") or "")
    report["release_v1"]["decision_reason"] = str(report.get("decision_reason") or "")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run HarborGate live-gate checks for Feishu baseline, Weixin parity, and dual-surface readiness."
    )
    parser.add_argument(
        "--surface",
        choices=("all", "weixin", "feishu"),
        default="all",
    )
    parser.add_argument(
        "--weixin-state-dir",
        default=str(REPO_ROOT / "data" / "weixin"),
    )
    parser.add_argument(
        "--feishu-state-root",
        default=str(REPO_ROOT / "data"),
    )
    parser.add_argument(
        "--session-root",
        default=str(REPO_ROOT / "data" / "sessions"),
    )
    parser.add_argument(
        "--task-api-url",
        default="",
        help="Optional HarborBeacon task API URL to run synthetic gateway -> HarborBeacon rehearsal.",
    )
    parser.add_argument(
        "--task-api-token",
        default="",
    )
    parser.add_argument(
        "--weixin-poll-timeout-ms",
        type=int,
        default=5000,
    )
    parser.add_argument(
        "--runtime-dir",
        default=str(REPO_ROOT / "data" / "runtime"),
    )
    parser.add_argument(
        "--report-dir",
        default=str(REPO_ROOT / "data" / "runtime" / "platform-live-gate"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "generated_at": utc_now(),
        "report_version": 2,
        "task_api_url_present": bool(args.task_api_url),
    }

    if args.surface in {"all", "weixin"}:
        report["weixin"] = run_weixin_surface(args)
    if args.surface in {"all", "feishu"}:
        report["feishu"] = run_feishu_surface(args)

    summarize_decision(report)
    report_path = report_dir / f"platform-live-gate-{slug_now()}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"decision": report["decision"], "report_path": str(report_path)}, ensure_ascii=False))
    return 0 if report["decision"] != "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())

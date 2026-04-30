from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any
from urllib import error, parse, request

from im_agent.models import InboundMessage

DEFAULT_CONTRACT_VERSION = "2.0"
DEFAULT_AUTONOMY_LEVEL = "supervised"
DEFAULT_INTENT_DOMAIN = "general"
DEFAULT_INTENT_ACTION = "message"
DEFAULT_SOURCE_SURFACE = "harborgate"
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_ADMIN_TIMEOUT_SECONDS = 10
TASK_ARGS_KEY = "ar" + "gs"


def _env(primary: str) -> str:
    return os.getenv(primary, "").strip()


def _int_env(primary: str, default: int) -> int:
    raw = _env(primary)
    if not raw:
        return default
    return int(raw)


def _strip_endpoint_suffix(base_url: str, endpoint_suffix: str) -> str:
    normalized = base_url.strip()
    if not normalized:
        return ""
    if normalized.endswith(endpoint_suffix):
        return normalized[: -len(endpoint_suffix)].rstrip("/")
    suffixed = f"{endpoint_suffix}/"
    if normalized.endswith(suffixed):
        return normalized[: -len(suffixed)].rstrip("/")
    return normalized


def _canonical_json(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return json.dumps(str(payload), ensure_ascii=False)


def _stable_id(prefix: str, payload: str, length: int = 24) -> str:
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:length]}"


def derive_route_key(incoming: InboundMessage) -> str:
    if incoming.route_key.strip():
        return incoming.route_key.strip()
    return _stable_id("gw_route_", f"{incoming.platform}|{incoming.chat_id}", length=20)


def derive_session_id(incoming: InboundMessage) -> str:
    if incoming.session_id.strip():
        return incoming.session_id.strip()
    return _stable_id("gw_sess_", f"{incoming.platform}|{incoming.chat_id}|{incoming.user_id}", length=20)


def _extract_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _extract_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            items.append(dict(item))
    return items


def _event_fingerprint(incoming: InboundMessage) -> str:
    message_id = incoming.message_id.strip()
    if message_id:
        return f"{incoming.platform}|{incoming.chat_id}|{message_id}"

    raw_payload = incoming.raw_payload or {}
    candidate = (
        raw_payload.get("message_id")
        or raw_payload.get("msg_id")
        or raw_payload.get("event_id")
        or raw_payload.get("client_id")
    )
    if candidate:
        return f"{incoming.platform}|{incoming.chat_id}|{candidate}"

    payload_fingerprint = _canonical_json(
        {
            "platform": incoming.platform,
            "chat_id": incoming.chat_id,
            "user_id": incoming.user_id,
            "text": incoming.text,
            "timestamp": incoming.timestamp,
            "raw_payload": raw_payload,
        }
    )
    return f"{incoming.platform}|{incoming.chat_id}|{payload_fingerprint}"


def _intent_block(
    incoming: InboundMessage,
    *,
    default_domain: str,
    default_action: str,
) -> dict[str, str]:
    raw_payload = incoming.raw_payload or {}
    payload_intent = raw_payload.get("intent")
    payload_intent = payload_intent if isinstance(payload_intent, dict) else {}

    domain = str(
        payload_intent.get("domain")
        or raw_payload.get("domain")
        or incoming.metadata.get("domain")
        or default_domain
    ).strip() or default_domain
    action = str(
        payload_intent.get("action")
        or raw_payload.get("action")
        or incoming.metadata.get("action")
        or default_action
    ).strip() or default_action
    return {
        "domain": domain,
        "action": action,
        "raw_text": incoming.text,
    }


def build_turn_request(
    incoming: InboundMessage,
    *,
    conversation_handle: str | None = None,
    continuation: dict[str, Any] | None = None,
    autonomy_level: str = DEFAULT_AUTONOMY_LEVEL,
    default_domain: str = DEFAULT_INTENT_DOMAIN,
    default_action: str = DEFAULT_INTENT_ACTION,
    source_surface: str = DEFAULT_SOURCE_SURFACE,
) -> dict[str, Any]:
    event_fingerprint = _event_fingerprint(incoming)
    route_key = derive_route_key(incoming)
    raw_payload = incoming.raw_payload or {}
    metadata: dict[str, Any] = {}
    intent = _intent_block(
        incoming,
        default_domain=default_domain,
        default_action=default_action,
    )
    if intent:
        metadata["intent"] = intent
    entity_refs = _extract_dict(raw_payload.get("entity_refs"))
    if entity_refs:
        metadata["entity_refs"] = entity_refs
    task_args = _extract_dict(raw_payload.get(TASK_ARGS_KEY))
    if task_args:
        metadata[TASK_ARGS_KEY] = task_args

    continuation_payload = continuation if isinstance(continuation, dict) else None

    request_payload = {
        "turn": {
            "turn_id": _stable_id("turn_", event_fingerprint),
            "trace_id": _stable_id("trace_", f"trace|{event_fingerprint}"),
            "occurred_at": incoming.timestamp,
            "retry_of": None,
        },
        "actor": {
            "user_id": incoming.user_id,
            "workspace_id": str(raw_payload.get("workspace_id") or incoming.metadata.get("workspace_id") or "home-1"),
            "account_id": str(raw_payload.get("account_id") or incoming.metadata.get("account_id") or "").strip() or None,
        },
        "conversation": {
            "handle": str(conversation_handle or "").strip() or None,
            "channel": incoming.platform,
            "surface": source_surface,
            "thread_id": incoming.chat_id,
            "chat_type": incoming.chat_type.strip() or "unknown",
        },
        "transport": {
            "route_key": route_key,
            "message_id": incoming.message_id.strip(),
            "capabilities": {
                "text": True,
                "image": True,
                "file": True,
                "video": True,
            },
            "metadata": metadata,
        },
        "input": {
            "text": incoming.text,
            "parts": _extract_list(incoming.attachments),
        },
        "continuation": continuation_payload,
        "autonomy": {
            "level": autonomy_level,
        },
    }
    return request_payload


def _continuation_from_active_frame(
    active_frame: dict[str, Any] | None,
    turn_block: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(active_frame, dict):
        return None
    token = str(active_frame.get("continuation_token") or "").strip()
    if not token:
        return None
    return {
        "token": token,
        "frame_id": str(active_frame.get("frame_id") or "").strip(),
        "reply_to_turn_id": str(turn_block.get("turn_id") or "").strip(),
        "expires_at": active_frame.get("expires_at"),
    }


@dataclass(slots=True)
class TaskTurnResult:
    text: str
    task_id: str
    trace_id: str
    status: str
    route_key: str
    conversation_handle: str | None = None
    continuation: dict[str, Any] | None = None
    active_frame: dict[str, Any] | None = None
    delivery_hints: list[dict[str, Any]] = field(default_factory=list)
    prompt: str | None = None
    next_actions: list[str] = field(default_factory=list)
    request_payload: dict[str, Any] = field(default_factory=dict)
    response_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HarborBeaconTaskClient:
    base_url: str
    api_token: str = ""
    contract_version: str = DEFAULT_CONTRACT_VERSION
    autonomy_level: str = DEFAULT_AUTONOMY_LEVEL
    default_domain: str = DEFAULT_INTENT_DOMAIN
    default_action: str = DEFAULT_INTENT_ACTION
    source_surface: str = DEFAULT_SOURCE_SURFACE
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    def submit_turn(self, incoming: InboundMessage, *, session_metadata: dict[str, object] | None = None) -> TaskTurnResult:
        metadata = session_metadata or {}
        conversation_handle = str(metadata.get("conversation_handle") or "").strip() or None
        continuation = metadata.get("continuation")
        continuation_payload = dict(continuation) if isinstance(continuation, dict) else None
        request_payload = build_turn_request(
            incoming,
            conversation_handle=conversation_handle,
            continuation=continuation_payload,
            autonomy_level=self.autonomy_level,
            default_domain=self.default_domain,
            default_action=self.default_action,
            source_surface=self.source_surface,
        )
        response_payload = self._post_json("/api/turns", request_payload)
        return self._map_turn_response(request_payload, response_payload)

    def _post_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = request.Request(
            self._url(endpoint),
            data=body,
            headers=self._headers(body),
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(self._format_http_error(exc.code, detail)) from exc
        except error.URLError as exc:
            raise RuntimeError(f"Could not reach HarborBeacon task API: {exc.reason}") from exc

        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            raise RuntimeError("HarborBeacon task API returned a non-object JSON payload")
        return data

    def _headers(self, body: bytes) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "X-Contract-Version": self.contract_version,
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _url(self, endpoint: str) -> str:
        if self.base_url.endswith(endpoint):
            return self.base_url
        return parse.urljoin(f"{self.base_url.rstrip('/')}/", endpoint.lstrip("/"))

    @staticmethod
    def _format_http_error(status_code: int, detail: str) -> str:
        message = f"HarborBeacon task API returned HTTP {status_code}"
        try:
            payload = json.loads(detail)
        except json.JSONDecodeError:
            payload = None

        if isinstance(payload, dict):
            error_block = payload.get("error")
            if isinstance(error_block, dict):
                code = str(error_block.get("code") or "").strip()
                text = str(error_block.get("message") or "").strip()
                details = " ".join(part for part in (code, text) if part).strip()
                if details:
                    return f"{message}: {details}"
        detail = detail.strip()
        return f"{message}: {detail}" if detail else message

    @staticmethod
    def _map_turn_response(request_payload: dict[str, Any], response_payload: dict[str, Any]) -> TaskTurnResult:
        turn_block = response_payload.get("turn")
        turn_block = turn_block if isinstance(turn_block, dict) else {}
        conversation_block = response_payload.get("conversation")
        conversation_block = conversation_block if isinstance(conversation_block, dict) else {}
        reply_block = response_payload.get("reply")
        reply_block = reply_block if isinstance(reply_block, dict) else {}
        active_frame = response_payload.get("active_frame")
        active_frame = active_frame if isinstance(active_frame, dict) else None
        status = str(turn_block.get("status") or "completed").strip() or "completed"
        error_block = response_payload.get("error")
        error_block = error_block if isinstance(error_block, dict) else {}
        message = str(reply_block.get("text") or "").strip()
        error_message = str(error_block.get("message") or "").strip()
        reply_text = message or error_message or "HarborBeacon returned an empty reply."
        expected_reply = active_frame.get("expected_reply") if isinstance(active_frame, dict) else []
        if not isinstance(expected_reply, list):
            expected_reply = []
        hints = response_payload.get("delivery_hints")
        hints = [dict(item) for item in hints if isinstance(item, dict)] if isinstance(hints, list) else []
        continuation = _continuation_from_active_frame(active_frame, turn_block)

        return TaskTurnResult(
            text=reply_text,
            task_id=str(turn_block.get("turn_id") or request_payload["turn"]["turn_id"]).strip(),
            trace_id=str(turn_block.get("trace_id") or request_payload["turn"]["trace_id"]).strip(),
            status=status,
            route_key=str(request_payload["transport"]["route_key"]).strip(),
            conversation_handle=str(conversation_block.get("handle") or "").strip() or None,
            continuation=continuation,
            active_frame=active_frame,
            delivery_hints=hints,
            prompt=reply_text if active_frame else None,
            next_actions=[str(item) for item in expected_reply if str(item).strip()],
            request_payload=request_payload,
            response_payload=response_payload,
        )


@dataclass(slots=True)
class HarborBeaconAdminClient:
    base_url: str
    api_token: str = ""
    contract_version: str = DEFAULT_CONTRACT_VERSION
    timeout_seconds: int = DEFAULT_ADMIN_TIMEOUT_SECONDS

    def upsert_notification_target(
        self,
        *,
        label: str,
        route_key: str,
        platform_hint: str,
        is_default: bool = False,
        target_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "label": label,
            "route_key": route_key,
            "platform_hint": platform_hint,
        }
        if is_default:
            payload["is_default"] = True
        if target_id and target_id.strip():
            payload["target_id"] = target_id.strip()
        return self._post_json("/api/admin/notification-targets", payload)

    def _post_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = request.Request(
            self._url(endpoint),
            data=body,
            headers=self._headers(body),
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(self._format_http_error(exc.code, detail)) from exc
        except error.URLError as exc:
            raise RuntimeError(f"Could not reach HarborBeacon admin API: {exc.reason}") from exc

        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            raise RuntimeError("HarborBeacon admin API returned a non-object JSON payload")
        return data

    def _headers(self, body: bytes) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "X-Contract-Version": self.contract_version,
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _url(self, endpoint: str) -> str:
        if self.base_url.endswith(endpoint):
            return self.base_url
        return parse.urljoin(f"{self.base_url.rstrip('/')}/", endpoint.lstrip("/"))

    @staticmethod
    def _format_http_error(status_code: int, detail: str) -> str:
        message = f"HarborBeacon admin API returned HTTP {status_code}"
        try:
            payload = json.loads(detail)
        except json.JSONDecodeError:
            payload = None

        if isinstance(payload, dict):
            error_block = payload.get("error")
            if isinstance(error_block, dict):
                code = str(error_block.get("code") or "").strip()
                text = str(error_block.get("message") or "").strip()
                details = " ".join(part for part in (code, text) if part).strip()
                if details:
                    return f"{message}: {details}"
        detail = detail.strip()
        return f"{message}: {detail}" if detail else message


def build_harborbeacon_client_from_env() -> HarborBeaconTaskClient | None:
    base_url = _env("HARBORBEACON_TASK_API_URL")
    base_url = _strip_endpoint_suffix(base_url, "/api/turns")
    if not base_url:
        return None
    return HarborBeaconTaskClient(
        base_url=base_url,
        api_token=_env("HARBORBEACON_TASK_API_TOKEN"),
        contract_version=_env("HARBORBEACON_CONTRACT_VERSION") or DEFAULT_CONTRACT_VERSION,
        autonomy_level=_env("HARBORBEACON_AUTONOMY_LEVEL") or DEFAULT_AUTONOMY_LEVEL,
        default_domain=_env("HARBORBEACON_DEFAULT_DOMAIN") or DEFAULT_INTENT_DOMAIN,
        default_action=_env("HARBORBEACON_DEFAULT_ACTION") or DEFAULT_INTENT_ACTION,
        source_surface=_env("HARBORBEACON_SOURCE_SURFACE") or DEFAULT_SOURCE_SURFACE,
        timeout_seconds=_int_env(
            "HARBORBEACON_TASK_API_TIMEOUT_SECONDS",
            DEFAULT_TIMEOUT_SECONDS,
        ),
    )


def build_harborbeacon_admin_client_from_env() -> HarborBeaconAdminClient | None:
    base_url = _env("HARBORBEACON_ADMIN_API_URL") or _env("HARBORBEACON_TASK_API_URL")
    base_url = _strip_endpoint_suffix(base_url, "/api/turns")
    if not base_url:
        return None
    return HarborBeaconAdminClient(
        base_url=base_url,
        api_token=(
            _env("HARBORBEACON_ADMIN_API_TOKEN")
            or _env("HARBORBEACON_TASK_API_TOKEN")
            or _env("IM_AGENT_SERVICE_TOKEN")
        ),
        contract_version=_env("HARBORBEACON_CONTRACT_VERSION") or DEFAULT_CONTRACT_VERSION,
        timeout_seconds=_int_env(
            "HARBORBEACON_ADMIN_API_TIMEOUT_SECONDS",
            DEFAULT_ADMIN_TIMEOUT_SECONDS,
        ),
    )

from __future__ import annotations

import hashlib
import json
import os
import warnings
from dataclasses import dataclass, field
from typing import Any
from urllib import error, parse, request

from im_agent.models import InboundMessage

DEFAULT_CONTRACT_VERSION = "1.5"
DEFAULT_AUTONOMY_LEVEL = "supervised"
DEFAULT_INTENT_DOMAIN = "general"
DEFAULT_INTENT_ACTION = "message"
DEFAULT_SOURCE_SURFACE = "harborgate"
DEFAULT_TIMEOUT_SECONDS = 15


def _env_with_legacy_alias(primary: str, legacy: str) -> str:
    primary_value = os.getenv(primary, "").strip()
    if primary_value:
        return primary_value

    legacy_value = os.getenv(legacy, "").strip()
    if legacy_value:
        warnings.warn(
            f"{legacy} is deprecated; prefer {primary}",
            FutureWarning,
            stacklevel=2,
        )
    return legacy_value


def _int_env_with_legacy_alias(primary: str, legacy: str, default: int) -> int:
    raw = _env_with_legacy_alias(primary, legacy)
    if not raw:
        return default
    return int(raw)


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


def build_task_request(
    incoming: InboundMessage,
    *,
    resume_token: str | None = None,
    autonomy_level: str = DEFAULT_AUTONOMY_LEVEL,
    default_domain: str = DEFAULT_INTENT_DOMAIN,
    default_action: str = DEFAULT_INTENT_ACTION,
    source_surface: str = DEFAULT_SOURCE_SURFACE,
) -> dict[str, Any]:
    event_fingerprint = _event_fingerprint(incoming)
    route_key = derive_route_key(incoming)
    session_id = derive_session_id(incoming)
    raw_payload = incoming.raw_payload or {}
    args = _extract_dict(raw_payload.get("args"))
    if resume_token and "resume_token" not in args:
        args["resume_token"] = resume_token

    request_payload = {
        "task_id": _stable_id("task_", event_fingerprint),
        "trace_id": _stable_id("trace_", f"trace|{event_fingerprint}"),
        "step_id": _stable_id("step_", f"step|{event_fingerprint}"),
        "source": {
            "channel": incoming.platform,
            "surface": source_surface,
            "conversation_id": incoming.chat_id,
            "user_id": incoming.user_id,
            "session_id": session_id,
            "route_key": route_key,
        },
        "intent": _intent_block(
            incoming,
            default_domain=default_domain,
            default_action=default_action,
        ),
        "entity_refs": _extract_dict(raw_payload.get("entity_refs")),
        "args": args,
        "autonomy": {
            "level": autonomy_level,
        },
        "message": {
            "message_id": incoming.message_id.strip(),
            "chat_type": incoming.chat_type.strip() or "unknown",
            "mentions": _extract_list(incoming.mentions),
            "attachments": _extract_list(incoming.attachments),
        },
    }
    return request_payload


@dataclass(slots=True)
class TaskTurnResult:
    text: str
    task_id: str
    trace_id: str
    status: str
    route_key: str
    resume_token: str | None = None
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
        resume_token = str(metadata.get("resume_token") or "").strip() or None
        request_payload = build_task_request(
            incoming,
            resume_token=resume_token,
            autonomy_level=self.autonomy_level,
            default_domain=self.default_domain,
            default_action=self.default_action,
            source_surface=self.source_surface,
        )
        response_payload = self._post_json("/api/tasks", request_payload)
        return self._map_task_response(request_payload, response_payload)

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
    def _map_task_response(request_payload: dict[str, Any], response_payload: dict[str, Any]) -> TaskTurnResult:
        status = str(response_payload.get("status") or "completed").strip() or "completed"
        result_block = response_payload.get("result")
        result_block = result_block if isinstance(result_block, dict) else {}
        error_block = response_payload.get("error")
        error_block = error_block if isinstance(error_block, dict) else {}
        prompt = str(response_payload.get("prompt") or "").strip() or None
        message = str(result_block.get("message") or "").strip()
        error_message = str(error_block.get("message") or "").strip()
        reply_text = prompt or message or error_message or "HarborBeacon returned an empty reply."
        next_actions = response_payload.get("result", {}).get("next_actions") if isinstance(response_payload.get("result"), dict) else []
        if not isinstance(next_actions, list):
            next_actions = []

        return TaskTurnResult(
            text=reply_text,
            task_id=str(response_payload.get("task_id") or request_payload["task_id"]).strip(),
            trace_id=str(response_payload.get("trace_id") or request_payload["trace_id"]).strip(),
            status=status,
            route_key=str(request_payload["source"]["route_key"]).strip(),
            resume_token=str(response_payload.get("resume_token") or "").strip() or None,
            prompt=prompt,
            next_actions=[str(item) for item in next_actions if str(item).strip()],
            request_payload=request_payload,
            response_payload=response_payload,
        )


def build_harborbeacon_client_from_env() -> HarborBeaconTaskClient | None:
    base_url = _env_with_legacy_alias(
        "HARBORBEACON_TASK_API_URL",
        "HARBORNAS_TASK_API_URL",
    )
    if not base_url:
        return None
    return HarborBeaconTaskClient(
        base_url=base_url,
        api_token=_env_with_legacy_alias(
            "HARBORBEACON_TASK_API_TOKEN",
            "HARBORNAS_TASK_API_TOKEN",
        ),
        contract_version=_env_with_legacy_alias(
            "HARBORBEACON_CONTRACT_VERSION",
            "HARBORNAS_CONTRACT_VERSION",
        )
        or DEFAULT_CONTRACT_VERSION,
        autonomy_level=_env_with_legacy_alias(
            "HARBORBEACON_AUTONOMY_LEVEL",
            "HARBORNAS_AUTONOMY_LEVEL",
        )
        or DEFAULT_AUTONOMY_LEVEL,
        default_domain=_env_with_legacy_alias(
            "HARBORBEACON_DEFAULT_DOMAIN",
            "HARBORNAS_DEFAULT_DOMAIN",
        )
        or DEFAULT_INTENT_DOMAIN,
        default_action=_env_with_legacy_alias(
            "HARBORBEACON_DEFAULT_ACTION",
            "HARBORNAS_DEFAULT_ACTION",
        )
        or DEFAULT_INTENT_ACTION,
        source_surface=_env_with_legacy_alias(
            "HARBORBEACON_SOURCE_SURFACE",
            "HARBORNAS_SOURCE_SURFACE",
        )
        or DEFAULT_SOURCE_SURFACE,
        timeout_seconds=_int_env_with_legacy_alias(
            "HARBORBEACON_TASK_API_TIMEOUT_SECONDS",
            "HARBORNAS_TASK_API_TIMEOUT_SECONDS",
            DEFAULT_TIMEOUT_SECONDS,
        ),
    )


HarborNASTaskClient = HarborBeaconTaskClient


def build_harbornas_client_from_env() -> HarborBeaconTaskClient | None:
    warnings.warn(
        "build_harbornas_client_from_env() is deprecated; prefer build_harborbeacon_client_from_env()",
        FutureWarning,
        stacklevel=2,
    )
    return build_harborbeacon_client_from_env()

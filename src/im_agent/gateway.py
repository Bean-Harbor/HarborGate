from __future__ import annotations

import hashlib
import json
import logging

from im_agent.brain import Brain, build_brain_from_env
from im_agent.errors import GatewayContractError
from im_agent.harborbeacon import (
    HarborBeaconTaskClient,
    build_harborbeacon_client_from_env,
    derive_route_key,
    derive_session_id,
)
from im_agent.models import ConversationTurn, InboundMessage, OutboundMessage
from im_agent.platforms.base import PlatformAdapter
from im_agent.platforms.registry import build_enabled_adapters
from im_agent.session_store import FileSessionStore

logger = logging.getLogger(__name__)


def _log_observation(event: str, fields: dict[str, object]) -> None:
    observation = {"event": event}
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        observation[key] = value
    logger.info("harborgate_observation %s", json.dumps(observation, ensure_ascii=False, sort_keys=True))


def _attachment_summary(attachments: list[dict[str, object]]) -> dict[str, object]:
    attachment_types: set[str] = set()
    attachment_metadata_keys: set[str] = set()
    for attachment in attachments:
        attachment_metadata_keys.update(str(key) for key in attachment.keys() if str(key).strip())
        for candidate_key in ("type", "kind"):
            candidate_value = str(attachment.get(candidate_key) or "").strip()
            if candidate_value:
                attachment_types.add(candidate_value)

    return {
        "has_attachments": bool(attachments),
        "attachment_count": len(attachments),
        "attachment_types": sorted(attachment_types),
        "attachment_metadata_keys": sorted(attachment_metadata_keys),
    }


def _looks_like_retrieval_turn(text: str, attachments: list[dict[str, object]]) -> bool:
    if attachments:
        return True
    normalized_text = text.strip().lower()
    if not normalized_text:
        return False
    retrieval_markers = (
        "search",
        "find",
        "lookup",
        "retrieve",
        "query",
        "检索",
        "搜索",
        "查找",
        "查一下",
        "找一下",
        "找出",
    )
    return any(marker in normalized_text for marker in retrieval_markers)


def _coerce_record_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    records: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, dict):
            records.append(dict(item))
    return records


def _first_nonempty_text(record: dict[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(record.get(key) or "").strip()
        if value:
            return value
    return ""


def _render_retrieval_entry(record: dict[str, object], *, kind: str) -> str:
    if kind == "citation":
        primary = _first_nonempty_text(record, ("title", "name", "headline", "summary", "snippet", "text", "id"))
        secondary = _first_nonempty_text(record, ("source", "provider", "origin"))
        if primary and secondary:
            return f"{primary} [{secondary}]"
        return primary or secondary or "未命名引用"

    primary = _first_nonempty_text(record, ("title", "name", "filename", "file_name", "label", "id"))
    secondary = _first_nonempty_text(record, ("type", "kind", "mime_type"))
    if primary and secondary:
        return f"{primary} ({secondary})"
    return primary or secondary or "未命名附件"


def _render_retrieval_reply(base_text: str, response_payload: dict[str, object]) -> tuple[str, dict[str, object]]:
    result = response_payload.get("result")
    result = result if isinstance(result, dict) else {}
    citation_candidates = _coerce_record_list(
        result.get("citations")
        or result.get("references")
        or result.get("sources")
        or result.get("top_hits")
        or result.get("hits")
    )
    artifact_candidates = _coerce_record_list(
        result.get("artifacts")
        or result.get("attachments")
        or result.get("evidence")
    )

    citation_entries = [_render_retrieval_entry(item, kind="citation") for item in citation_candidates[:3]]
    artifact_entries = [_render_retrieval_entry(item, kind="artifact") for item in artifact_candidates[:3]]

    sections: list[str] = []
    if base_text.strip():
        sections.append(base_text.strip())
    if citation_entries:
        citation_block = "\n".join(f"{index}. {entry}" for index, entry in enumerate(citation_entries, start=1))
        if len(citation_candidates) > len(citation_entries):
            citation_block = f"{citation_block}\n... 还有 {len(citation_candidates) - len(citation_entries)} 条引用"
        sections.append(f"引用\n{citation_block}")
    if artifact_entries:
        artifact_block = "\n".join(f"{index}. {entry}" for index, entry in enumerate(artifact_entries, start=1))
        if len(artifact_candidates) > len(artifact_entries):
            artifact_block = f"{artifact_block}\n... 还有 {len(artifact_candidates) - len(artifact_entries)} 个附件"
        sections.append(f"附件\n{artifact_block}")

    retrieval_summary = {
        "content_kind": "retrieval_reply" if (citation_candidates or artifact_candidates) else "plain_reply",
        "citation_count": len(citation_candidates),
        "artifact_count": len(artifact_candidates),
        "rendered_sections": [section.split("\n", 1)[0] for section in sections[1:]],
    }
    if citation_candidates or artifact_candidates:
        header = f"检索结果（{len(citation_candidates)} 条引用，{len(artifact_candidates)} 个附件）"
        sections.insert(0, header)

    rendered_text = "\n\n".join(section for section in sections if section.strip()).strip()
    return rendered_text or base_text.strip(), retrieval_summary


def _ingress_profile(
    inbound: InboundMessage,
    *,
    route_key: str,
    session_id: str,
) -> dict[str, object]:
    attachments = [item for item in inbound.attachments if isinstance(item, dict)]
    attachment_summary = _attachment_summary(attachments)
    content_kind = "retrieval_candidate" if _looks_like_retrieval_turn(inbound.text, attachments) else "plain_chat"
    return {
        "content_kind": content_kind,
        "raw_text": inbound.text,
        "text_length": len(inbound.text),
        "message_id": inbound.message_id.strip(),
        "route_key": route_key.strip(),
        "session_id": session_id.strip(),
        **attachment_summary,
    }


def _message_task_ids(session_metadata: dict[str, object]) -> dict[str, str]:
    raw = session_metadata.get("message_task_ids")
    if not isinstance(raw, dict):
        return {}

    message_task_ids: dict[str, str] = {}
    for message_id, task_id in raw.items():
        normalized_message_id = str(message_id or "").strip()
        normalized_task_id = str(task_id or "").strip()
        if normalized_message_id and normalized_task_id:
            message_task_ids[normalized_message_id] = normalized_task_id
    return message_task_ids


def _adapter_profile(adapter: PlatformAdapter) -> dict[str, object]:
    raw_profile = adapter.get_profile() if hasattr(adapter, "get_profile") else {}
    raw_profile = raw_profile if isinstance(raw_profile, dict) else {}
    profile: dict[str, object] = {}
    for key in (
        "adapter_name",
        "surface_family",
        "transport_mode",
        "supports_mentions",
        "supports_attachments",
        "supports_replies",
        "supports_updates",
        "supports_live_receive",
    ):
        value = raw_profile.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        profile[key] = value
    profile.setdefault("adapter_name", adapter.name)
    profile.setdefault("surface_family", "generic")
    profile.setdefault("transport_mode", "normalized")
    return profile


class GatewayService:
    def __init__(
        self,
        *,
        store: FileSessionStore,
        brain: Brain,
        task_client: HarborBeaconTaskClient | None = None,
    ) -> None:
        self.store = store
        self.brain = brain
        self.task_client = task_client
        self._adapters: dict[str, PlatformAdapter] = {}
        self._started = False

    def register_adapter(self, adapter: PlatformAdapter) -> None:
        self._adapters[adapter.name] = adapter
        if self._started:
            self._connect_adapter(adapter)

    def get_adapter(self, adapter_name: str) -> PlatformAdapter | None:
        return self._adapters.get(adapter_name)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for adapter in self._adapters.values():
            self._connect_adapter(adapter)

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        for adapter in self._adapters.values():
            try:
                adapter.disconnect()
            except Exception:  # pragma: no cover - defensive shutdown boundary
                logger.exception("Adapter shutdown failed for %s", getattr(adapter, "name", "unknown"))

    def handle_inbound(self, adapter_name: str, payload: dict) -> dict:
        adapter = self._adapters.get(adapter_name)
        if adapter is None:
            raise ValueError(f"Unknown adapter: {adapter_name}")

        inbound = adapter.normalize_inbound(payload)
        adapter_profile = _adapter_profile(adapter)
        history = self.store.load_history(inbound.platform, inbound.chat_id)
        session_metadata = self.store.load_metadata(inbound.platform, inbound.chat_id)
        known_message_tasks = _message_task_ids(session_metadata)
        replayed_task_id = known_message_tasks.get(inbound.message_id.strip(), "")
        resolved_route_key = inbound.route_key or str(session_metadata.get("route_key") or "").strip() or derive_route_key(inbound)
        resolved_session_id = inbound.session_id or derive_session_id(inbound)
        ingress_profile = _ingress_profile(
            inbound,
            route_key=resolved_route_key,
            session_id=resolved_session_id,
        )
        outbound_metadata = {"adapter": adapter_name}
        outbound_metadata["ingress_profile"] = ingress_profile
        outbound_metadata["adapter_profile"] = adapter_profile

        if self.task_client is not None:
            task_result = self.task_client.submit_turn(inbound, session_metadata=session_metadata)
            resolved_route_key = task_result.route_key or resolved_route_key
            reply_text, retrieval_summary = _render_retrieval_reply(task_result.text, task_result.response_payload)
            next_metadata = {
                **session_metadata,
                "route_key": resolved_route_key,
                "session_id": resolved_session_id,
            }
            preserve_latest_pointer = bool(
                replayed_task_id
                and replayed_task_id != str(session_metadata.get("last_task_id") or "").strip()
            )
            if not preserve_latest_pointer:
                next_metadata["last_task_id"] = task_result.task_id
                next_metadata["last_trace_id"] = task_result.trace_id
                if inbound.message_id.strip():
                    next_metadata["last_message_id"] = inbound.message_id.strip()
                if task_result.resume_token:
                    next_metadata["resume_token"] = task_result.resume_token
                else:
                    next_metadata.pop("resume_token", None)
            if inbound.message_id.strip():
                next_metadata["message_task_ids"] = {
                    **known_message_tasks,
                    inbound.message_id.strip(): task_result.task_id,
                }
            self.store.set_metadata(inbound.platform, inbound.chat_id, next_metadata)
            outbound_metadata.update(
                {
                    "source": "harborbeacon",
                    "task_id": task_result.task_id,
                    "trace_id": task_result.trace_id,
                    "status": task_result.status,
                    "route_key": resolved_route_key,
                    "next_actions": task_result.next_actions,
                    "retrieval_render": retrieval_summary,
                }
            )
            if task_result.resume_token:
                outbound_metadata["resume_token"] = task_result.resume_token
            if task_result.prompt:
                outbound_metadata["prompt"] = task_result.prompt
            _log_observation(
                "retrieval_reply_classified",
                {
                    "adapter": adapter_name,
                    "platform": inbound.platform,
                    "chat_id": inbound.chat_id,
                    "message_id": inbound.message_id.strip(),
                    "route_key": resolved_route_key,
                    "session_id": resolved_session_id,
                    "adapter_profile": adapter_profile,
                    "task_id": task_result.task_id,
                    "trace_id": task_result.trace_id,
                    "retrieval_render_kind": retrieval_summary["content_kind"],
                    "citation_count": retrieval_summary["citation_count"],
                    "artifact_count": retrieval_summary["artifact_count"],
                },
            )
            if retrieval_summary["content_kind"] == "retrieval_reply":
                _log_observation(
                    "retrieval_reply_rendered",
                    {
                    "adapter": adapter_name,
                    "platform": inbound.platform,
                    "chat_id": inbound.chat_id,
                    "message_id": inbound.message_id.strip(),
                    "route_key": resolved_route_key,
                    "session_id": resolved_session_id,
                    "adapter_profile": adapter_profile,
                    "task_id": task_result.task_id,
                    "trace_id": task_result.trace_id,
                    "citation_count": retrieval_summary["citation_count"],
                    "artifact_count": retrieval_summary["artifact_count"],
                    "rendered_sections": retrieval_summary["rendered_sections"],
                    },
                )
            _log_observation(
                "inbound_task_handled",
                {
                    "adapter": adapter_name,
                    "platform": inbound.platform,
                    "chat_id": inbound.chat_id,
                    "message_id": inbound.message_id.strip(),
                    "raw_text": inbound.text,
                    "route_key": resolved_route_key,
                    "session_id": resolved_session_id,
                    "adapter_profile": adapter_profile,
                    "content_kind": ingress_profile["content_kind"],
                    "has_attachments": ingress_profile["has_attachments"],
                    "attachment_count": ingress_profile["attachment_count"],
                    "attachment_types": ingress_profile["attachment_types"],
                    "attachment_metadata_keys": ingress_profile["attachment_metadata_keys"],
                    "retrieval_render_kind": retrieval_summary["content_kind"],
                    "citation_count": retrieval_summary["citation_count"],
                    "artifact_count": retrieval_summary["artifact_count"],
                    "task_id": task_result.task_id,
                    "trace_id": task_result.trace_id,
                    "status": task_result.status,
                },
            )
        else:
            reply_text = self.brain.reply(history, inbound)
            next_metadata = dict(session_metadata)
            next_metadata["route_key"] = resolved_route_key
            next_metadata["session_id"] = resolved_session_id
            if next_metadata != session_metadata:
                self.store.set_metadata(inbound.platform, inbound.chat_id, next_metadata)
            _log_observation(
                "inbound_brain_reply",
                {
                    "adapter": adapter_name,
                    "platform": inbound.platform,
                    "chat_id": inbound.chat_id,
                    "message_id": inbound.message_id.strip(),
                    "raw_text": inbound.text,
                    "route_key": resolved_route_key,
                    "session_id": resolved_session_id,
                    "adapter_profile": adapter_profile,
                    "content_kind": ingress_profile["content_kind"],
                    "has_attachments": ingress_profile["has_attachments"],
                    "attachment_count": ingress_profile["attachment_count"],
                    "attachment_types": ingress_profile["attachment_types"],
                    "attachment_metadata_keys": ingress_profile["attachment_metadata_keys"],
                    "status": "completed",
                },
            )

        self.store.register_route(
            resolved_route_key,
            {
                "route_key": resolved_route_key,
                "platform": inbound.platform,
                "chat_id": inbound.chat_id,
                "user_id": inbound.user_id,
                "adapter_name": adapter_name,
                "session_id": resolved_session_id,
                "status": "active",
            },
        )

        self.store.append_turns(
            inbound.platform,
            inbound.chat_id,
            [
                ConversationTurn(role="user", content=inbound.text),
                ConversationTurn(role="assistant", content=reply_text),
            ],
        )

        outbound = OutboundMessage(
            platform=inbound.platform,
            chat_id=inbound.chat_id,
            text=reply_text,
            metadata=outbound_metadata,
        )
        return adapter.send_outbound(outbound)

    def handle_notification_delivery(self, payload: dict) -> dict:
        trace_id = str(payload.get("trace_id") or "").strip()
        notification_id = str(payload.get("notification_id") or "").strip()
        if not notification_id:
            raise GatewayContractError(422, "VALIDATION_ERROR", "notification_id is required", trace_id)

        destination = payload.get("destination")
        if not isinstance(destination, dict):
            raise GatewayContractError(422, "VALIDATION_ERROR", "destination must be an object", trace_id)
        content = payload.get("content")
        if not isinstance(content, dict):
            raise GatewayContractError(422, "VALIDATION_ERROR", "content must be an object", trace_id)
        delivery = payload.get("delivery")
        if not isinstance(delivery, dict):
            raise GatewayContractError(422, "VALIDATION_ERROR", "delivery must be an object", trace_id)

        mode = str(delivery.get("mode") or "").strip().lower()
        reply_to_message_id = str(delivery.get("reply_to_message_id") or "").strip()
        update_message_id = str(delivery.get("update_message_id") or "").strip()
        idempotency_key = str(delivery.get("idempotency_key") or "").strip()
        if not idempotency_key:
            raise GatewayContractError(422, "VALIDATION_ERROR", "delivery.idempotency_key is required", trace_id)
        self._validate_delivery_mode(
            mode=mode,
            reply_to_message_id=reply_to_message_id,
            update_message_id=update_message_id,
            trace_id=trace_id,
        )

        route_key = str(destination.get("route_key") or "").strip()
        route = self._resolve_notification_route(destination=destination, route_key=route_key, trace_id=trace_id)
        adapter_name = str(route.get("adapter_name") or route.get("platform") or "").strip()
        adapter = self.get_adapter(adapter_name)
        if adapter is None:
            raise GatewayContractError(
                422,
                "VALIDATION_ERROR",
                f"No adapter is enabled for outbound platform route: {adapter_name or 'unknown'}",
                trace_id,
            )

        effective_request = {
            "notification_id": notification_id,
            "trace_id": trace_id,
            "destination": {
                "route_key": route_key,
                "platform": route.get("platform"),
                "chat_id": route.get("chat_id"),
                "recipient": destination.get("recipient"),
            },
            "content": content,
            "delivery": {
                "mode": mode,
                "reply_to_message_id": reply_to_message_id,
                "update_message_id": update_message_id,
            },
        }
        request_fingerprint = self._fingerprint_payload(effective_request)
        record = self.store.load_delivery_record(idempotency_key)
        if record:
            existing_fingerprint = str(record.get("request_fingerprint") or "")
            if existing_fingerprint != request_fingerprint:
                raise GatewayContractError(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "delivery.idempotency_key was reused with a different effective request",
                    trace_id,
                )
            response_payload = record.get("response_payload")
            if isinstance(response_payload, dict):
                _log_observation(
                    "delivery_replayed",
                    {
                        "notification_id": notification_id,
                        "trace_id": trace_id,
                        "route_key": route_key,
                        "delivery.idempotency_key": idempotency_key,
                        "platform": str(response_payload.get("platform") or route.get("platform") or adapter_name),
                        "status": str(response_payload.get("status") or ""),
                        "provider_message_id": response_payload.get("provider_message_id"),
                    },
                )
                return dict(response_payload)

        outbound_text = self._build_notification_text(content)
        outbound = OutboundMessage(
            platform=str(route.get("platform") or adapter_name),
            chat_id=str(route.get("chat_id") or ""),
            text=outbound_text,
            metadata={
                "source": "notification_delivery",
                "notification_id": notification_id,
                "trace_id": trace_id,
                "delivery_mode": mode,
                "route_key": route_key,
                "reply_to_message_id": reply_to_message_id,
                "update_message_id": update_message_id,
                "payload_format": str(content.get("payload_format") or "plain_text").strip() or "plain_text",
                "attachments": content.get("attachments") if isinstance(content.get("attachments"), list) else [],
                "structured_payload": content.get("structured_payload") if isinstance(content.get("structured_payload"), dict) else {},
            },
        )
        delivery_id = self._delivery_id(idempotency_key)

        try:
            adapter_response = adapter.send_outbound(outbound)
            provider_message_id = str(
                adapter_response.get("message_id")
                or adapter_response.get("provider_message_id")
                or ""
            ).strip() or None
            response_payload: dict[str, object] = {
                "delivery_id": delivery_id,
                "notification_id": notification_id,
                "trace_id": trace_id,
                "ok": True,
                "status": "sent",
                "platform": str(route.get("platform") or adapter_name),
                "provider_message_id": provider_message_id,
                "retryable": False,
                "error": None,
            }
        except Exception as exc:
            error_code, retryable = self._map_delivery_failure(exc)
            response_payload = {
                "delivery_id": delivery_id,
                "notification_id": notification_id,
                "trace_id": trace_id,
                "ok": False,
                "status": "failed",
                "platform": str(route.get("platform") or adapter_name),
                "provider_message_id": None,
                "retryable": retryable,
                "error": {
                    "code": error_code,
                    "message": str(exc),
                },
            }

        self.store.save_delivery_record(
            idempotency_key,
            request_fingerprint=request_fingerprint,
            response_payload=response_payload,
        )
        _log_observation(
            "delivery_attempted",
            {
                "notification_id": notification_id,
                "trace_id": trace_id,
                "route_key": route_key,
                "delivery.idempotency_key": idempotency_key,
                "platform": str(route.get("platform") or adapter_name),
                "status": str(response_payload.get("status") or ""),
                "provider_message_id": response_payload.get("provider_message_id"),
                "retryable": response_payload.get("retryable"),
                "ok": response_payload.get("ok"),
            },
        )
        return response_payload

    @staticmethod
    def _validate_delivery_mode(
        *,
        mode: str,
        reply_to_message_id: str,
        update_message_id: str,
        trace_id: str,
    ) -> None:
        if mode not in {"send", "reply", "update"}:
            raise GatewayContractError(422, "VALIDATION_ERROR", "delivery.mode must be send, reply, or update", trace_id)
        if mode == "send" and (reply_to_message_id or update_message_id):
            raise GatewayContractError(
                422,
                "VALIDATION_ERROR",
                "delivery.mode=send requires empty reply_to_message_id and update_message_id",
                trace_id,
            )
        if mode == "reply" and (not reply_to_message_id or update_message_id):
            raise GatewayContractError(
                422,
                "VALIDATION_ERROR",
                "delivery.mode=reply requires reply_to_message_id and forbids update_message_id",
                trace_id,
            )
        if mode == "update" and (not update_message_id or reply_to_message_id):
            raise GatewayContractError(
                422,
                "VALIDATION_ERROR",
                "delivery.mode=update requires update_message_id and forbids reply_to_message_id",
                trace_id,
            )

    def _resolve_notification_route(
        self,
        *,
        destination: dict,
        route_key: str,
        trace_id: str,
    ) -> dict[str, object]:
        if route_key:
            route = self.store.resolve_route(route_key)
            if route is None:
                raise GatewayContractError(404, "ROUTE_NOT_FOUND", f"route_key not found: {route_key}", trace_id)
            if str(route.get("status") or "active").strip().lower() == "expired":
                raise GatewayContractError(410, "ROUTE_EXPIRED", f"route_key expired: {route_key}", trace_id)
            return route

        platform = str(destination.get("platform") or "").strip()
        chat_id = str(destination.get("id") or "").strip()
        if not platform or not chat_id:
            raise GatewayContractError(
                422,
                "VALIDATION_ERROR",
                "destination.route_key is preferred; otherwise destination.platform and destination.id are required",
                trace_id,
            )
        return {
            "platform": platform,
            "chat_id": chat_id,
            "adapter_name": platform,
            "status": "active",
        }

    @staticmethod
    def _build_notification_text(content: dict) -> str:
        title = str(content.get("title") or "").strip()
        body = str(content.get("body") or "").strip()
        if title and body:
            return f"{title}\n\n{body}"
        return body or title

    @staticmethod
    def _map_delivery_failure(exc: Exception) -> tuple[str, bool]:
        message = str(exc).lower()
        if "context_token" in message:
            return ("INVALID_RECIPIENT", False)
        if "not configured" in message or "authorization" in message or "auth" in message:
            return ("PROVIDER_AUTH_FAILED", False)
        if "unsupported" in message:
            return ("UNSUPPORTED_CONTENT", False)
        return ("PLATFORM_UNAVAILABLE", True)

    @staticmethod
    def _fingerprint_payload(payload: dict) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _delivery_id(idempotency_key: str) -> str:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return f"delivery_{digest[:24]}"

    def _connect_adapter(self, adapter: PlatformAdapter) -> None:
        try:
            adapter.connect(lambda payload, adapter_name=adapter.name: self._handle_transport_inbound(adapter_name, payload))
        except Exception:  # pragma: no cover - defensive transport boundary
            logger.exception("Adapter startup failed for %s", getattr(adapter, "name", "unknown"))

    def _handle_transport_inbound(self, adapter_name: str, payload: dict) -> None:
        try:
            self.handle_inbound(adapter_name, payload)
        except GatewayContractError:
            logger.exception("Contract error while handling %s transport event", adapter_name)
        except ValueError as exc:
            logger.info("Ignoring %s transport event: %s", adapter_name, exc)
        except Exception:  # pragma: no cover - defensive transport boundary
            logger.exception("Unhandled %s transport event", adapter_name)


def build_default_gateway(data_root: str = "data/sessions") -> GatewayService:
    store = FileSessionStore(data_root)
    gateway = GatewayService(
        store=store,
        brain=build_brain_from_env(),
        task_client=build_harborbeacon_client_from_env(),
    )
    for adapter in build_enabled_adapters():
        gateway.register_adapter(adapter)
    return gateway

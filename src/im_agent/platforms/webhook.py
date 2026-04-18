from __future__ import annotations

from typing import Any

from im_agent.models import InboundMessage, OutboundMessage
from im_agent.platforms.base import PlatformAdapter


class WebhookAdapter(PlatformAdapter):
    """Generic adapter for a normalized webhook format.

    This lets us exercise the gateway immediately, even before any real IM
    platform is wired in. Later adapters can keep the same internal contract.
    """

    name = "webhook"

    def normalize_inbound(self, payload: dict[str, Any]) -> InboundMessage:
        platform = str(payload.get("platform") or "webhook").strip()
        chat_id = str(payload.get("chat_id") or "").strip()
        user_id = str(payload.get("user_id") or "anonymous").strip()
        text = str(payload.get("text") or "").strip()
        message_id = str(payload.get("message_id") or "").strip()
        chat_type = str(payload.get("chat_type") or "p2p").strip().lower() or "p2p"
        route_key = str(payload.get("route_key") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        mentions = payload.get("mentions") or []
        attachments = payload.get("attachments") or []
        metadata = payload.get("metadata") or {}

        if not chat_id:
            raise ValueError("Payload must include chat_id")
        if not text:
            raise ValueError("Payload must include text")

        return InboundMessage(
            platform=platform,
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            message_id=message_id,
            chat_type=chat_type,
            route_key=route_key,
            session_id=session_id,
            mentions=[item for item in mentions if isinstance(item, dict)],
            attachments=[item for item in attachments if isinstance(item, dict)],
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
            raw_payload=payload,
        )

    def build_delivery_payload(self, outbound: OutboundMessage) -> dict[str, Any]:
        payload = super().build_delivery_payload(outbound)
        payload["delivery"] = "webhook"
        return payload

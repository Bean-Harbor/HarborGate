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

        if not chat_id:
            raise ValueError("Payload must include chat_id")
        if not text:
            raise ValueError("Payload must include text")

        return InboundMessage(
            platform=platform,
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            raw_payload=payload,
        )

    def build_delivery_payload(self, outbound: OutboundMessage) -> dict[str, Any]:
        payload = super().build_delivery_payload(outbound)
        payload["delivery"] = "webhook"
        return payload

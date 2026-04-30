from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from im_agent.models import InboundMessage, OutboundMessage
from im_agent.platforms.base import PlatformAdapter


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True, slots=True)
class PlaceholderPlatformSpec:
    name: str
    display_name: str
    surface_family: str = "im"
    supports_mentions: bool = True
    supports_attachments: bool = True
    supports_replies: bool = True
    supports_updates: bool = False
    supports_live_receive: bool = False
    credential_envs: tuple[str, ...] = ()


def _configured_from_env(env_names: tuple[str, ...]) -> bool:
    return any(os.getenv(env_name, "").strip() for env_name in env_names)


class PlaceholderAdapter(PlatformAdapter):
    def __init__(self, spec: PlaceholderPlatformSpec) -> None:
        self.spec = spec
        self.name = spec.name

    @property
    def configured(self) -> bool:
        return _configured_from_env(self.spec.credential_envs)

    def transport_status(self) -> dict[str, Any]:
        configured = self.configured
        return {
            "mode": "placeholder",
            "status": "configured_placeholder" if configured else "not_configured",
            "connected": False,
            "thread_alive": False,
            "last_connected_at": "",
            "last_event_at": "",
            "last_error": "" if configured else "placeholder adapter not configured",
        }

    def normalize_inbound(self, payload: dict[str, Any]) -> InboundMessage:
        attachments = [
            item for item in (payload.get("attachments") or []) if isinstance(item, dict)
        ]
        metadata = (
            dict(payload.get("metadata") or {})
            if isinstance(payload.get("metadata"), dict)
            else {}
        )
        metadata.update(
            {
                "placeholder_adapter": True,
                "configuration_state": (
                    "configured_placeholder" if self.configured else "not_configured"
                ),
            }
        )
        return InboundMessage(
            platform=str(payload.get("platform") or self.name).strip() or self.name,
            chat_id=(
                str(
                    payload.get("chat_id")
                    or payload.get("conversation_id")
                    or payload.get("channel_id")
                    or payload.get("thread_id")
                    or payload.get("room_id")
                    or f"{self.name}-placeholder-chat"
                ).strip()
                or f"{self.name}-placeholder-chat"
            ),
            user_id=(
                str(
                    payload.get("user_id")
                    or payload.get("sender_id")
                    or payload.get("from")
                    or f"{self.name}-placeholder-user"
                ).strip()
                or f"{self.name}-placeholder-user"
            ),
            text=(
                str(
                    payload.get("text")
                    or payload.get("body")
                    or payload.get("content")
                    or payload.get("subject")
                    or ""
                ).strip()
            ),
            message_id=(
                str(
                    payload.get("message_id")
                    or payload.get("msg_id")
                    or payload.get("event_id")
                    or f"{self.name}-placeholder-message"
                ).strip()
                or f"{self.name}-placeholder-message"
            ),
            chat_type=(
                str(payload.get("chat_type") or payload.get("conversation_type") or "p2p")
                .strip()
                .lower()
                or "p2p"
            ),
            route_key=str(payload.get("route_key") or "").strip(),
            session_id=str(payload.get("session_id") or "").strip(),
            mentions=[
                item for item in (payload.get("mentions") or []) if isinstance(item, dict)
            ],
            attachments=attachments,
            metadata=metadata,
            raw_payload=dict(payload),
        )

    def get_profile(self) -> dict[str, Any]:
        return {
            "adapter_name": self.name,
            "display_name": self.spec.display_name,
            "surface_family": self.spec.surface_family,
            "transport_mode": "placeholder",
            "supports_mentions": self.spec.supports_mentions,
            "supports_attachments": self.spec.supports_attachments,
            "supports_replies": self.spec.supports_replies,
            "supports_updates": self.spec.supports_updates,
            "supports_live_receive": self.spec.supports_live_receive,
            "placeholder": True,
            "configuration_state": (
                "configured_placeholder" if self.configured else "not_configured"
            ),
        }

    def send_outbound(self, outbound: OutboundMessage) -> dict[str, Any]:
        placeholder_status = (
            "configured_placeholder" if self.configured else "not_configured"
        )
        return {
            "platform": self.name,
            "chat_id": outbound.chat_id,
            "text": outbound.text,
            "timestamp": outbound.timestamp,
            "message_id": f"{self.name}-placeholder-{int(time.time())}",
            "provider_message_id": f"{self.name}-placeholder-provider",
            "placeholder": True,
            "placeholder_status": placeholder_status,
            "delivery_mode": "simulated",
            "metadata": {
                **dict(outbound.metadata),
                "placeholder_adapter": True,
                "configuration_state": placeholder_status,
                "generated_at": _now_utc(),
            },
        }


def build_placeholder_adapter(spec: PlaceholderPlatformSpec) -> PlaceholderAdapter:
    return PlaceholderAdapter(spec)

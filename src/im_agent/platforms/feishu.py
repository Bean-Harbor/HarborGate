from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from im_agent.models import InboundMessage, OutboundMessage
from im_agent.platforms.base import PlatformAdapter


@dataclass(slots=True)
class FeishuSettings:
    app_id: str
    app_secret: str
    domain: str = "feishu"
    connection_mode: str = "websocket"
    allowed_users: set[str] = field(default_factory=set)
    group_policy: str = "allowlist"
    bot_open_id: str = ""
    bot_user_id: str = ""
    bot_name: str = ""
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8765
    webhook_path: str = "/feishu/webhook"


def parse_csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def build_feishu_settings_from_env() -> FeishuSettings:
    return FeishuSettings(
        app_id=os.getenv("FEISHU_APP_ID", "").strip(),
        app_secret=os.getenv("FEISHU_APP_SECRET", "").strip(),
        domain=(os.getenv("FEISHU_DOMAIN", "feishu").strip() or "feishu").lower(),
        connection_mode=(os.getenv("FEISHU_CONNECTION_MODE", "websocket").strip() or "websocket").lower(),
        allowed_users=parse_csv_set(os.getenv("FEISHU_ALLOWED_USERS", "")),
        group_policy=(os.getenv("FEISHU_GROUP_POLICY", "allowlist").strip() or "allowlist").lower(),
        bot_open_id=os.getenv("FEISHU_BOT_OPEN_ID", "").strip(),
        bot_user_id=os.getenv("FEISHU_BOT_USER_ID", "").strip(),
        bot_name=os.getenv("FEISHU_BOT_NAME", "").strip(),
        webhook_host=os.getenv("FEISHU_WEBHOOK_HOST", "127.0.0.1").strip() or "127.0.0.1",
        webhook_port=int(os.getenv("FEISHU_WEBHOOK_PORT", "8765")),
        webhook_path=os.getenv("FEISHU_WEBHOOK_PATH", "/feishu/webhook").strip() or "/feishu/webhook",
    )


def parse_feishu_message_content(raw_content: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw_content, dict):
        return raw_content
    text = str(raw_content or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}
    return payload if isinstance(payload, dict) else {}


def build_feishu_text_payload(chat_id: str, text: str) -> dict[str, Any]:
    return {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }


class FeishuAdapter(PlatformAdapter):
    """Protocol-facing skeleton for Feishu / Lark.

    This adapter intentionally focuses on the translation boundary:
    raw Feishu payload -> internal message model -> outbound Feishu request body.
    Transport concerns such as the SDK websocket loop or webhook server can be
    added later without changing the gateway/agent contract.
    """

    name = "feishu"

    def __init__(self, settings: FeishuSettings | None = None) -> None:
        self.settings = settings or build_feishu_settings_from_env()

    @property
    def configured(self) -> bool:
        return bool(self.settings.app_id and self.settings.app_secret)

    def normalize_inbound(self, payload: dict[str, Any]) -> InboundMessage:
        if "header" in payload and "event" in payload:
            return self._normalize_raw_event(payload)
        return self._normalize_compact_payload(payload)

    def build_delivery_payload(self, outbound: OutboundMessage) -> dict[str, Any]:
        request = build_feishu_text_payload(outbound.chat_id, outbound.text)
        return {
            **outbound.to_dict(),
            "delivery": "feishu",
            "sent": False,
            "connection_mode": self.settings.connection_mode,
            "domain": self.settings.domain,
            "request": request,
            "note": (
                "Feishu adapter skeleton only. The protocol translation is implemented, "
                "but the live websocket/webhook transport is not wired yet."
            ),
        }

    def _normalize_compact_payload(self, payload: dict[str, Any]) -> InboundMessage:
        chat_id = str(payload.get("chat_id") or "").strip()
        user_id = str(payload.get("user_id") or "").strip()
        text = str(payload.get("text") or "").strip()
        chat_type = str(payload.get("chat_type") or "p2p").strip().lower()
        mentions = payload.get("mentions") or []
        raw_content = str(payload.get("raw_content") or text)

        if not chat_id:
            raise ValueError("Feishu payload must include chat_id")
        if not user_id:
            raise ValueError("Feishu payload must include user_id")
        if not text:
            raise ValueError("Feishu payload must include text")

        self._enforce_access_policy(
            chat_type=chat_type,
            sender_open_id=user_id,
            raw_content=raw_content,
            mentions=mentions,
        )

        return InboundMessage(
            platform="feishu",
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            raw_payload=payload,
        )

    def _normalize_raw_event(self, payload: dict[str, Any]) -> InboundMessage:
        header = payload.get("header") or {}
        event_type = str(header.get("event_type") or "").strip()
        if event_type != "im.message.receive_v1":
            raise ValueError(f"Unsupported Feishu event_type: {event_type or 'unknown'}")

        event = payload.get("event") or {}
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        sender_id = sender.get("sender_id") or {}
        sender_open_id = str(sender_id.get("open_id") or sender_id.get("user_id") or "").strip()
        chat_id = str(message.get("chat_id") or "").strip()
        chat_type = str(message.get("chat_type") or "p2p").strip().lower()
        message_type = str(message.get("message_type") or "").strip().lower()
        if message_type != "text":
            raise ValueError(f"Unsupported Feishu message_type for this skeleton: {message_type or 'unknown'}")

        content = parse_feishu_message_content(message.get("content") or "")
        text = str(content.get("text") or "").strip()
        mentions = message.get("mentions") or []

        if not sender_open_id:
            raise ValueError("Feishu event is missing sender open_id/user_id")
        if not chat_id:
            raise ValueError("Feishu event is missing chat_id")
        if not text:
            raise ValueError("Feishu text message is empty")

        self._enforce_access_policy(
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            raw_content=str(message.get("content") or ""),
            mentions=mentions,
        )

        return InboundMessage(
            platform="feishu",
            chat_id=chat_id,
            user_id=sender_open_id,
            text=text,
            raw_payload=payload,
        )

    def _enforce_access_policy(
        self,
        *,
        chat_type: str,
        sender_open_id: str,
        raw_content: str,
        mentions: list[Any],
    ) -> None:
        if self.settings.allowed_users and sender_open_id not in self.settings.allowed_users:
            raise ValueError("Feishu sender is not in FEISHU_ALLOWED_USERS")

        if chat_type == "p2p":
            return

        if self.settings.group_policy == "disabled":
            raise ValueError("Feishu group messages are disabled by FEISHU_GROUP_POLICY")

        if not self._message_mentions_bot(raw_content=raw_content, mentions=mentions):
            raise ValueError("Feishu group messages must explicitly @mention the bot")

    def _message_mentions_bot(self, *, raw_content: str, mentions: list[Any]) -> bool:
        if "@_all" in raw_content:
            return True

        if not mentions:
            return False

        for mention in mentions:
            mention_id = mention.get("id") if isinstance(mention, dict) else getattr(mention, "id", None)
            mention_name = mention.get("name") if isinstance(mention, dict) else getattr(mention, "name", "")

            if isinstance(mention_id, dict):
                mention_open_id = str(mention_id.get("open_id") or "").strip()
                mention_user_id = str(mention_id.get("user_id") or "").strip()
            else:
                mention_open_id = str(getattr(mention_id, "open_id", "") or "").strip()
                mention_user_id = str(getattr(mention_id, "user_id", "") or "").strip()

            if self.settings.bot_open_id and mention_open_id == self.settings.bot_open_id:
                return True
            if self.settings.bot_user_id and mention_user_id == self.settings.bot_user_id:
                return True
            if self.settings.bot_name and str(mention_name or "").strip() == self.settings.bot_name:
                return True

        # Skeleton fallback: if no bot identity is configured, accept any explicit
        # mention as a temporary group-entry signal until live identity hydration is added.
        return not any((self.settings.bot_open_id, self.settings.bot_user_id, self.settings.bot_name))

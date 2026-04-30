from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class InboundMessage:
    platform: str
    chat_id: str
    user_id: str
    text: str
    message_id: str = ""
    chat_type: str = "p2p"
    route_key: str = ""
    session_id: str = ""
    mentions: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=utc_now_iso)
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class OutboundMessage:
    platform: str
    chat_id: str
    text: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    timestamp: str = field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ConversationTurn:
    role: str
    content: str
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversationTurn":
        return cls(
            role=str(data.get("role", "user")),
            content=str(data.get("content", "")),
            timestamp=str(data.get("timestamp", utc_now_iso())),
        )

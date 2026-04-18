from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from im_agent.models import InboundMessage, OutboundMessage


class PlatformAdapter(ABC):
    """Translate platform payloads into internal models and back out again."""

    name: str

    @abstractmethod
    def normalize_inbound(self, payload: dict[str, Any]) -> InboundMessage:
        raise NotImplementedError

    def connect(self, inbound_handler: Any) -> None:
        """Start any optional live transport for inbound events."""

    def disconnect(self) -> None:
        """Stop any optional live transport for inbound events."""

    def build_delivery_payload(self, outbound: OutboundMessage) -> dict[str, Any]:
        return outbound.to_dict()

    def get_profile(self) -> dict[str, Any]:
        return {
            "adapter_name": self.name,
            "surface_family": "generic",
            "transport_mode": "normalized",
            "supports_mentions": False,
            "supports_attachments": True,
            "supports_replies": True,
            "supports_updates": False,
            "supports_live_receive": False,
        }

    def send_outbound(self, outbound: OutboundMessage) -> dict[str, Any]:
        """Deliver an outbound message.

        The default implementation is side-effect free and simply returns the
        payload that would be delivered. Platform adapters with a real transport
        should override this method.
        """
        return self.build_delivery_payload(outbound)

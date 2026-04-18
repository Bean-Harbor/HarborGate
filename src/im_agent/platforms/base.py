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

    def build_delivery_payload(self, outbound: OutboundMessage) -> dict[str, Any]:
        return outbound.to_dict()

    def send_outbound(self, outbound: OutboundMessage) -> dict[str, Any]:
        """Deliver an outbound message.

        The default implementation is side-effect free and simply returns the
        payload that would be delivered. Platform adapters with a real transport
        should override this method.
        """
        return self.build_delivery_payload(outbound)

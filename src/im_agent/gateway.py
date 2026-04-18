from __future__ import annotations

from im_agent.brain import Brain, build_brain_from_env
from im_agent.models import ConversationTurn, OutboundMessage
from im_agent.platforms.base import PlatformAdapter
from im_agent.platforms.registry import build_enabled_adapters
from im_agent.session_store import FileSessionStore


class GatewayService:
    def __init__(self, *, store: FileSessionStore, brain: Brain) -> None:
        self.store = store
        self.brain = brain
        self._adapters: dict[str, PlatformAdapter] = {}

    def register_adapter(self, adapter: PlatformAdapter) -> None:
        self._adapters[adapter.name] = adapter

    def get_adapter(self, adapter_name: str) -> PlatformAdapter | None:
        return self._adapters.get(adapter_name)

    def handle_inbound(self, adapter_name: str, payload: dict) -> dict:
        adapter = self._adapters.get(adapter_name)
        if adapter is None:
            raise ValueError(f"Unknown adapter: {adapter_name}")

        inbound = adapter.normalize_inbound(payload)
        history = self.store.load_history(inbound.platform, inbound.chat_id)
        reply_text = self.brain.reply(history, inbound)

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
            metadata={"adapter": adapter_name},
        )
        return adapter.send_outbound(outbound)


def build_default_gateway(data_root: str = "data/sessions") -> GatewayService:
    store = FileSessionStore(data_root)
    gateway = GatewayService(store=store, brain=build_brain_from_env())
    for adapter in build_enabled_adapters():
        gateway.register_adapter(adapter)
    return gateway

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import Any
from urllib import error, parse, request

from im_agent.models import ConversationTurn, InboundMessage


class Brain(ABC):
    @abstractmethod
    def reply(self, history: list[ConversationTurn], incoming: InboundMessage) -> str:
        raise NotImplementedError


class RuleBasedBrain(Brain):
    """Small local fallback so the gateway works before any model is configured."""

    def reply(self, history: list[ConversationTurn], incoming: InboundMessage) -> str:
        text = incoming.text.strip()
        lower = text.lower()

        if not text:
            return "I received an empty message. Send some text and I will route it through the starter pipeline."

        if lower in {"/help", "help", "帮助"}:
            return (
                "This starter is wired correctly. Send messages to /messages/webhook now, "
                "or set LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL to upgrade to a real model backend."
            )

        if history:
            return (
                f"[{incoming.platform}] I received: {text}\n"
                f"This chat already has {len(history)} stored turns, so session memory is working too."
            )

        return (
            f"[{incoming.platform}] I received: {text}\n"
            "The clean-room gateway is working. Set LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL "
            "to switch from demo replies to a real model."
        )


class OpenAICompatibleBrain(Brain):
    """Thin client for any OpenAI-compatible chat completions backend."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        system_prompt: str | None = None,
        timeout_seconds: int = 45,
        max_history_turns: int = 12,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.system_prompt = system_prompt or (
            "You are an IM assistant behind a messaging gateway. "
            "Keep replies concise, helpful, and suitable for chat applications."
        )
        self.timeout_seconds = timeout_seconds
        self.max_history_turns = max_history_turns

    def reply(self, history: list[ConversationTurn], incoming: InboundMessage) -> str:
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        for turn in history[-self.max_history_turns :]:
            messages.append({"role": turn.role, "content": turn.content})
        messages.append({"role": "user", "content": incoming.text})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
        }
        body = json.dumps(payload).encode("utf-8")
        endpoint = self._chat_completions_url()
        req = request.Request(
            endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM backend returned HTTP {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Could not reach LLM backend: {exc.reason}") from exc

        data = json.loads(raw)
        return self._extract_text(data)

    def _chat_completions_url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/chat/completions"
        return parse.urljoin(f"{self.base_url}/", "chat/completions")

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str:
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("LLM backend response did not include choices")

        message = choices[0].get("message") or {}
        content = message.get("content", "")
        if isinstance(content, str):
            return content.strip() or "The model returned an empty string."
        if isinstance(content, list):
            text_chunks: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_chunks.append(str(item.get("text", "")))
            joined = "\n".join(chunk for chunk in text_chunks if chunk.strip()).strip()
            if joined:
                return joined
        raise RuntimeError("LLM backend response did not include readable text content")


def build_brain_from_env() -> Brain:
    base_url = os.getenv("LLM_BASE_URL", "").strip()
    api_key = os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    if base_url and api_key and model:
        return OpenAICompatibleBrain(base_url=base_url, api_key=api_key, model=model)
    return RuleBasedBrain()

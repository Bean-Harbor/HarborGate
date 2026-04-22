from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from im_agent.platforms.base import PlatformAdapter
from im_agent.platforms.feishu import FeishuAdapter
from im_agent.platforms.placeholder import (
    PlaceholderPlatformSpec,
    build_placeholder_adapter,
)
from im_agent.platforms.webhook import WebhookAdapter
from im_agent.platforms.weixin import WeixinAdapter


@dataclass(frozen=True, slots=True)
class AdapterRegistration:
    name: str
    builder: Callable[[], PlatformAdapter]
    enabled: Callable[[], bool]
    description: str


def _weixin_enabled() -> bool:
    return bool(os.getenv("WEIXIN_ACCOUNT_ID", "").strip())


def _feishu_enabled() -> bool:
    return bool(
        os.getenv("FEISHU_APP_ID", "").strip()
        and os.getenv("FEISHU_APP_SECRET", "").strip()
    )


def _placeholder_enabled(name: str) -> bool:
    return os.getenv(f"HARBORGATE_DISABLE_{name.upper()}", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }


PLACEHOLDER_SPECS: tuple[PlaceholderPlatformSpec, ...] = (
    PlaceholderPlatformSpec(
        name="telegram",
        display_name="Telegram",
        surface_family="telegram",
        credential_envs=("TELEGRAM_BOT_TOKEN",),
    ),
    PlaceholderPlatformSpec(
        name="discord",
        display_name="Discord",
        surface_family="discord",
        credential_envs=("DISCORD_BOT_TOKEN",),
    ),
    PlaceholderPlatformSpec(
        name="slack",
        display_name="Slack",
        surface_family="slack",
        credential_envs=("SLACK_BOT_TOKEN",),
    ),
    PlaceholderPlatformSpec(
        name="whatsapp",
        display_name="WhatsApp",
        surface_family="whatsapp",
        credential_envs=("WHATSAPP_ACCESS_TOKEN",),
    ),
    PlaceholderPlatformSpec(
        name="signal",
        display_name="Signal",
        surface_family="signal",
        credential_envs=("SIGNAL_SERVICE_TOKEN", "SIGNAL_PHONE_NUMBER"),
    ),
    PlaceholderPlatformSpec(
        name="email",
        display_name="Email",
        surface_family="email",
        supports_mentions=False,
        supports_updates=False,
        credential_envs=("EMAIL_SMTP_HOST", "EMAIL_IMAP_HOST"),
    ),
    PlaceholderPlatformSpec(
        name="wecom",
        display_name="WeCom",
        surface_family="wecom",
        credential_envs=("WECOM_CORP_ID", "WECOM_AGENT_SECRET"),
    ),
)


REGISTRATIONS: tuple[AdapterRegistration, ...] = (
    AdapterRegistration(
        name="webhook",
        builder=WebhookAdapter,
        enabled=lambda: True,
        description="Generic normalized webhook adapter",
    ),
    AdapterRegistration(
        name="weixin",
        builder=WeixinAdapter,
        enabled=_weixin_enabled,
        description="Personal WeChat adapter via iLink",
    ),
    AdapterRegistration(
        name="feishu",
        builder=FeishuAdapter,
        enabled=_feishu_enabled,
        description="Feishu / Lark adapter with websocket-first receive mode and live send",
    ),
    *tuple(
        AdapterRegistration(
            name=spec.name,
            builder=lambda spec=spec: build_placeholder_adapter(spec),
            enabled=lambda spec=spec: _placeholder_enabled(spec.name),
            description=f"{spec.display_name} placeholder adapter with normalized gateway entry points",
        )
        for spec in PLACEHOLDER_SPECS
    ),
)


def build_enabled_adapters() -> list[PlatformAdapter]:
    adapters: list[PlatformAdapter] = []
    for registration in REGISTRATIONS:
        if registration.enabled():
            adapters.append(registration.builder())
    return adapters


def get_adapter_registration_names() -> list[str]:
    return [registration.name for registration in REGISTRATIONS]

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from im_agent.platforms.base import PlatformAdapter
from im_agent.platforms.feishu import FeishuAdapter
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
        description="Feishu / Lark adapter skeleton",
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

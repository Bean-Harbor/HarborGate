"""Platform adapters for HarborGate."""

from im_agent.platforms.feishu import FeishuAdapter
from im_agent.platforms.webhook import WebhookAdapter
from im_agent.platforms.weixin import WeixinAdapter

__all__ = ["FeishuAdapter", "WebhookAdapter", "WeixinAdapter"]

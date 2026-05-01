import os
import tempfile
import unittest
from unittest.mock import patch

from im_agent.platforms.registry import build_enabled_adapters, get_adapter_registration_names


class PlatformRegistryTests(unittest.TestCase):
    def test_registration_names_include_live_and_placeholder_platforms(self) -> None:
        self.assertEqual(
            get_adapter_registration_names(),
            [
                "webhook",
                "weixin",
                "feishu",
                "telegram",
                "discord",
                "slack",
                "whatsapp",
                "signal",
                "email",
                "wecom",
            ],
        )

    def test_webhook_and_placeholders_are_enabled_without_live_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "WEIXIN_ACCOUNT_ID": "",
                    "WEIXIN_STATE_DIR": tmp,
                    "FEISHU_APP_ID": "",
                    "FEISHU_APP_SECRET": "",
                },
                clear=False,
            ):
                adapters = build_enabled_adapters()
        self.assertEqual(
            [adapter.name for adapter in adapters],
            ["webhook", "telegram", "discord", "slack", "whatsapp", "signal", "email", "wecom"],
        )

    def test_feishu_is_enabled_when_credentials_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "WEIXIN_ACCOUNT_ID": "",
                    "WEIXIN_STATE_DIR": tmp,
                    "FEISHU_APP_ID": "cli_xxx",
                    "FEISHU_APP_SECRET": "secret_xxx",
                },
                clear=False,
            ):
                adapters = build_enabled_adapters()
        self.assertEqual(
            [adapter.name for adapter in adapters],
            [
                "webhook",
                "feishu",
                "telegram",
                "discord",
                "slack",
                "whatsapp",
                "signal",
                "email",
                "wecom",
            ],
        )

    def test_weixin_is_enabled_when_account_id_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "WEIXIN_ACCOUNT_ID": "wx-account-1",
                    "WEIXIN_STATE_DIR": tmp,
                    "FEISHU_APP_ID": "",
                    "FEISHU_APP_SECRET": "",
                },
                clear=False,
            ):
                adapters = build_enabled_adapters()
        self.assertEqual(
            [adapter.name for adapter in adapters],
            [
                "webhook",
                "weixin",
                "telegram",
                "discord",
                "slack",
                "whatsapp",
                "signal",
                "email",
                "wecom",
            ],
        )


if __name__ == "__main__":
    unittest.main()

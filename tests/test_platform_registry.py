import os
import unittest
from unittest.mock import patch

from im_agent.platforms.registry import build_enabled_adapters, get_adapter_registration_names


class PlatformRegistryTests(unittest.TestCase):
    def test_registration_names_include_webhook_weixin_and_feishu(self) -> None:
        self.assertEqual(
            get_adapter_registration_names(),
            ["webhook", "weixin", "feishu"],
        )

    def test_webhook_is_always_enabled(self) -> None:
        with patch.dict(os.environ, {"WEIXIN_ACCOUNT_ID": "", "FEISHU_APP_ID": "", "FEISHU_APP_SECRET": ""}, clear=False):
            adapters = build_enabled_adapters()
        self.assertEqual([adapter.name for adapter in adapters], ["webhook"])

    def test_feishu_is_enabled_when_credentials_exist(self) -> None:
        with patch.dict(
            os.environ,
            {
                "WEIXIN_ACCOUNT_ID": "",
                "FEISHU_APP_ID": "cli_xxx",
                "FEISHU_APP_SECRET": "secret_xxx",
            },
            clear=False,
        ):
            adapters = build_enabled_adapters()
        self.assertEqual([adapter.name for adapter in adapters], ["webhook", "feishu"])


if __name__ == "__main__":
    unittest.main()

import runpy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from im_agent import weixin_runner
from im_agent.platforms.weixin import save_weixin_account


class _FakeWeixinAdapter:
    def __init__(self, account_id: str = "bot-1", state_dir: str = "") -> None:
        self.account_id = account_id
        self.state_dir = state_dir
        self.poll_count = 0
        self.configured = True

    def assert_configured(self) -> None:
        return None

    def poll_updates(self) -> list[dict]:
        self.poll_count += 1
        raise KeyboardInterrupt


class _FakeGateway:
    def __init__(self, adapter: _FakeWeixinAdapter) -> None:
        self._adapter = adapter

    def get_adapter(self, adapter_name: str):
        if adapter_name == "weixin":
            return self._adapter
        return None


class WeixinRunnerModuleTests(unittest.TestCase):
    def test_module_entrypoint_invokes_main(self) -> None:
        adapter = _FakeWeixinAdapter()
        gateway = _FakeGateway(adapter)
        with patch("im_agent.gateway.build_default_gateway", return_value=gateway) as mocked_build_gateway:
            with patch("im_agent.platforms.weixin.WeixinAdapter", _FakeWeixinAdapter):
                runpy.run_module("im_agent.weixin_runner", run_name="__main__")

        mocked_build_gateway.assert_called_once()
        self.assertEqual(adapter.poll_count, 1)

    def test_runner_reloads_when_newer_saved_account_appears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_weixin_account(
                tmp,
                account_id="old@im.bot",
                token="old-token",
                base_url="https://ilinkai.weixin.qq.com",
                user_id="wx-user",
            )
            adapter = _FakeWeixinAdapter(account_id="old@im.bot", state_dir=tmp)

            with patch.object(weixin_runner, "WeixinAdapter", _FakeWeixinAdapter):
                self.assertFalse(weixin_runner._weixin_adapter_should_reload(adapter))

                time.sleep(0.01)
                save_weixin_account(
                    tmp,
                    account_id="new@im.bot",
                    token="new-token",
                    base_url="https://ilinkai.weixin.qq.com",
                    user_id="wx-user",
                )

                self.assertTrue(weixin_runner._weixin_adapter_should_reload(adapter))

    def test_runner_reloads_when_current_account_state_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_weixin_account(
                tmp,
                account_id="old@im.bot",
                token="old-token",
                base_url="https://ilinkai.weixin.qq.com",
                user_id="wx-user",
            )
            adapter = _FakeWeixinAdapter(account_id="old@im.bot", state_dir=tmp)

            with patch.object(weixin_runner, "WeixinAdapter", _FakeWeixinAdapter):
                self.assertFalse(weixin_runner._weixin_adapter_should_reload(adapter))

                for path in (Path(tmp) / "accounts").glob("*.json"):
                    path.unlink()

                self.assertTrue(weixin_runner._weixin_adapter_should_reload(adapter))

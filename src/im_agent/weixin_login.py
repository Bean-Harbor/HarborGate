from __future__ import annotations

import os

from im_agent.platforms.weixin import run_weixin_qr_login


def main() -> None:
    state_dir = os.getenv("WEIXIN_STATE_DIR", "data/weixin")
    result = run_weixin_qr_login(state_dir=state_dir)
    if result is None:
        raise SystemExit(1)

    print("\n登录已保存到本机 Weixin state dir。下一步启动 runner：")
    print("harborgate-weixin-runner")
    print(f"account_id={result.account_id}")

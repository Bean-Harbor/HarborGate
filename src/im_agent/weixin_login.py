from __future__ import annotations

import os

from im_agent.platforms.weixin import run_weixin_qr_login


def main() -> None:
    state_dir = os.getenv("WEIXIN_STATE_DIR", "data/weixin")
    result = run_weixin_qr_login(state_dir=state_dir)
    if result is None:
        raise SystemExit(1)

    print("\n下一步请设置环境变量后再启动 runner：")
    print(f"$env:WEIXIN_ACCOUNT_ID='{result.account_id}'")
    print("im-agent-weixin-runner")

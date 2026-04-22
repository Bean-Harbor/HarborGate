# HarborBeacon HarborGate v1.5 Closeout - 2026-04-19

## Status

Feishu baseline rehearsal is ready on the frozen HarborBeacon `v1.5` seam.
Weixin remains on the parity track with blockers only in the four fixed ingress classes:
`account_restore`, `qr_recovery`, `getupdates`, and `context_token_send`.

## Change Summary

- Clarified the README cutover note so Feishu is explicitly the stable baseline and Weixin stays on the parity track with the same four fixed blocker classes.
- Tightened the cutover checklist with a `Known Pending` section that names the same four Weixin blocker classes.
- Added a today-specific worklog entry with the closeout snapshot, validation command, and pending items.
- Kept the boundary frozen: no HarborBeacon contract changes, no recipient-shape changes, and no group-chat expansion.

## Validation Commands

```powershell
pytest tests/test_platform_live_gate.py tests/test_gateway.py tests/test_weixin_adapter.py
```

## Known Pending

- Weixin private-DM ingress still needs one of the four fixed blocker classes cleared before it reaches rehearsal parity with Feishu on the same frozen seam.
- Group chats remain explicitly out of scope for this cutover.
- Any new failure outside the four fixed Weixin classes should be treated as a separate regression, not as part of the closeout taxonomy.

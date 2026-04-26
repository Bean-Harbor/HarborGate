# HarborGate v2.0 Upgrade Runbook

## Status

This is the active HarborGate control pack entry for the HarborBeacon seam.

Authoritative contract:

- `HarborBeacon-HarborGate-Agent-Contract-v2.0.md`

The v1.5 documents are historical references only.

## Daily Start

1. Read the v2.0 contract.
2. Read this runbook and the v2.0 cutover checklist.
3. Check local git status in HarborGate and HarborBeacon.
4. Identify the current phase and today's one main line.
5. Do not start live target work until the target registry has been confirmed.

## Phases

### Phase 1: Control Pack

- Publish v2.0 as the active contract.
- Update README, PLAN, ROADMAP, WORKLOG, checklist, and tests.
- Add drift guards for v1.5 active paths.

### Phase 2: Turn Client

- Replace task-client request building with v2 turn request building.
- Default to `X-Contract-Version: 2.0`.
- Submit inbound IM turns to `/api/turns`.
- Store Beacon-owned `conversation.handle` opaquely.

### Phase 3: Continuation Cache

- Replace `resume_token` metadata with opaque `continuation`.
- Store `frame_id`, `token`, `reply_to_turn_id`, and `expires_at` only as
  transport correlation values.
- Do not interpret active-frame business meaning.

### Phase 4: Delivery

- Keep notification delivery in HarborGate.
- Use v2 `delivery_hints` for native video/file selection.
- Keep `native_attachment_kind` and `native_attachment_fallback` as Gate-side
  observability only.

### Phase 5: Evidence

- Run local tests.
- Run the platform live gate.
- Capture Weixin private-DM matrix evidence.
- Write daily closeout.

## Drift Guards

- Active path must not use `X-Contract-Version: 1.5`.
- Active path must not post `/api/tasks`.
- Active request builders must not emit `args.resume_token`.
- Gate must not parse Beacon business active-frame kinds for routing.
- Group chat remains out of scope.

## Stop-The-Line Conditions

Stop and ask the user when:

- a new contract field is required.
- Beacon/Gate ownership changes.
- v1.5 runtime compatibility is requested.
- group chat is needed.
- live target, credential, DNS, or provider state blocks the path.

## Daily Closeout

Record:

- completed
- changed files
- tests run
- drift check result
- blockers
- next exact step

## 2026-04-26 Closeout

- Completed: active Gate client now emits v2.0 turn envelopes to
  `/api/turns`, caches only opaque `conversation_handle` and `continuation`,
  and keeps Weixin native video/file delivery driven by `delivery_hints`.
- Changed files: `src/im_agent/harborbeacon.py`, `src/im_agent/gateway.py`,
  `src/im_agent/setup_portal.py`, `tools/run_platform_live_gate.py`, and
  related tests.
- Tests run: `python -m pytest tests/test_v20_control_pack.py -q`,
  targeted HarborBeacon/Gateway/Weixin/live-gate/server tests,
  `python -m pytest`, and `git diff --check`.
- Drift check: Gate v2.0 guard passed; active client no longer posts
  `/api/tasks` or emits `args.resume_token`.
- Blockers: `.182` live Weixin validation is still pending target-registry
  confirmation.
- Next exact step: confirm `.182`, then run the Weixin private-DM v2.0 matrix.

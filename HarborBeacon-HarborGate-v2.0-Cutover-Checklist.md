# HarborBeacon HarborGate v2.0 Cutover Checklist

## Status

This checklist replaces the v1.5 cutover checklist for active work.

The active contract is:

- `HarborBeacon-HarborGate-Agent-Contract-v2.0.md`

## Implemented First

Control-pack readiness requires:

- v2.0 contract exists and is referenced by README, PLAN, ROADMAP, and WORKLOG.
- v1.5 docs are marked historical.
- drift guard tests exist.
- failing drift guards are treated as the code-upgrade queue.

## Code Cutover Checklist

- HarborGate defaults to `X-Contract-Version: 2.0`.
- HarborGate submits inbound IM turns to `/api/turns`.
- Gate stores Beacon-owned `conversation.handle` opaquely.
- Gate stores continuation values opaquely.
- Gate stops emitting `args.resume_token`.
- Gate stops posting `/api/tasks` on active HarborBeacon-backed path.
- Gate delivery consumes platform-neutral `delivery_hints`.
- Weixin native video may fall back to file without changing Beacon contract.

## What HarborBeacon Can Rely On

- `transport.route_key` remains opaque and Gate-owned.
- `conversation.handle` returned by Beacon is echoed by Gate.
- `continuation` returned by Beacon is echoed by Gate.
- Gate does not reinterpret `reply.text`.
- Gate does not route business behavior from `active_frame.kind`.

## What Remains Out Of Scope

- v1.5/v2.0 dual-stack compatibility.
- group chat.
- direct IM delivery from HarborBeacon.
- raw platform credentials in HarborBeacon.

## Live Weixin Matrix

Use private DM only:

- `你能干什么` -> capability reply.
- `非常好` -> conversation continue.
- `今天天气怎么样` -> boundary conversation.
- `帮我看一下门口` -> active clarification frame.
- `非常好` while clarifying -> clarify continue, frame retained.
- `算了` -> cancel, frame cleared.
- `录一段` -> record clip.
- neutral/positive ack after clip prompt -> frame retained unless explicit yes,
  no, or playback.
- `放一下` / `回放一下短视频` -> recent clip playback with native video or file
  fallback.

## Evidence Fields

Capture:

- `turn_id`
- `trace_id`
- `conversation_handle`
- `route_key`
- `message_id`
- `active_frame_id`
- `reply_kind`
- `artifact_count`
- `native_attachment_count`
- `native_attachment_kind`
- `native_attachment_fallback`
- `contract_version`

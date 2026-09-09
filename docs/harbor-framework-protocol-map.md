# Harbor Framework Protocol Map

## Purpose

This document is HarborGate's local view of the HarborBeacon-centered framework and protocol map. Phase 1 is documentation only: it records the current HTTP/JSON contract boundary and does not change HarborGate runtime behavior or public APIs.

HarborGate is the IM/channel edge. It normalizes external channel traffic into HarborBeacon task turns and delivers HarborBeacon notifications back to platforms.

## Repository Role

HarborGate owns:

- Channel adapters, webhook/websocket/long-poll ingress, and outbound delivery workers.
- Platform credential storage and platform-specific formatting.
- Route registry and route key resolution for IM/channel traffic.
- Gateway-side HTTP/JSON surfaces for HarborBeacon integration.
- Notification delivery fanout after HarborBeacon has produced a delivery intent.

HarborGate does not own:

- HarborBeacon task/business semantics.
- HarborBeacon runtime state, policy, model selection, approval, audit, or artifacts.
- HarborCloud entitlement/account state.
- HarborLink MQTT connector state.
- HarborDock or WebUI display state.

## Shared Frame

The active collaboration frame is:

- HarborBeacon is the business-core framework.
- HarborGate is the IM/channel edge.
- HarborCloud is the cloud control plane.
- HarborLink is the Hub-side outbound connector.
- harbor-dock is the Android/Paper client surface.
- HarborNAS-webui is the HarborOS WebUI surface.

HarborGate keeps this local map so the channel boundary is visible from the transport repository itself.

## Northbound Interfaces

HarborGate's northbound-facing work is transport and gateway HTTP, not business logic ownership.

- Active HarborBeacon integration contract: `docs/HarborBeacon-HarborGate-Agent-Contract-v2.0.md`.
- Contract header when calling HarborBeacon: `X-Contract-Version: 2.0`.
- HarborBeacon task ingress target: `POST /api/web/turns`.
- HarborBeacon turn envelope: `TaskTurnEnvelope`.
- Required shared vocabulary: `conversation.handle`, `transport.route_key`, `active_frame`, `continuation`, `delivery_hints`.
- Gateway-facing turn ingress may accept web/channel requests and convert them into the HarborBeacon v2.0 HTTP/JSON shape.
- Beacon admin/config visibility is exposed through HarborBeacon-owned `/api/beacon/*` style APIs; HarborGate may proxy or reference those APIs but must not redefine their semantics.

The v2.0 HTTP/JSON contract is current. v3.0 can be used for future evolution only if it keeps the same channel/core split or explicitly versions a new one.

Shared HarborBeacon contract guardrails are `X-Contract-Version: 2.0`, `TaskTurnEnvelope`, `conversation.handle`, `transport.route_key`, `active_frame`, `continuation`, `delivery_hints`, and the notification delivery contract.

## Core Ownership

HarborGate core ownership is transport edge work:

- Validate channel identity and route eligibility.
- Normalize platform events into the v2.0 task turn contract.
- Preserve `transport.route_key` as a routing key, not as business state.
- Maintain outbound delivery state, retry behavior, channel formatting, and platform-specific acknowledgements.
- Surface gateway health/status for HarborBeacon admin readiness.

Business interpretation of the turn belongs to HarborBeacon after the v2.0 envelope is accepted.

## Southbound Interfaces

HarborGate southbound interfaces are platform/channel integrations:

- IM platform webhooks, websocket streams, long-poll transports, and platform APIs.
- Outbound delivery APIs for each configured platform.
- Credential stores and signing/verification hooks for platform traffic.
- HarborBeacon notification delivery endpoint for accepting delivery jobs from Beacon.

HarborGate does not call HarborOS middleware, Home Assistant, RTSP, ONVIF, HarborLink MQTT, or HarborCloud entitlement APIs as part of task semantics.

## Build And Deployment Fit

HarborGate deployment is the channel edge beside HarborBeacon:

- It can run independently of the HarborBeacon package, provided the v2.0 HTTP/JSON contract and route registry are configured.
- For Nexus / HarborOS amd64, it remains the IM transport companion to the HarborBeacon business core.
- For HarborNavi K3 riscv64 work, HarborGate remains external channel infrastructure unless a later product decision adds a K3-specific transport package.

Target-specific HarborBeacon builds must not require HarborGate to import HarborBeacon runtime internals.

## Frozen Boundaries

- Do not move business semantics, model policy, approval, audit, artifacts, or device execution into HarborGate.
- Do not let platform credentials or channel formatting leak into HarborBeacon.
- Do not treat `transport.route_key` as a domain object; it is an opaque routing key.
- Do not replace the active v2.0 contract with older task/IM shapes.

## Cross-Repo References

- Bean-Harbor/HarborBeacon: `docs/harbor-framework-protocol-map.md` and `docs/HarborBeacon-Harbor-Collaboration-Contract-v2.md`.
- Bean-Harbor/HarborGate: `docs/harbor-framework-protocol-map.md` and `docs/HarborBeacon-HarborGate-Agent-Contract-v2.0.md`.
- Bean-Harbor/HarborCloud: `docs/harbor-framework-protocol-map.md`.
- Bean-Harbor/HarborLink: `docs/harbor-framework-protocol-map.md`.
- Bean-Harbor/harbor-dock: `docs/harbor-framework-protocol-map.md`.
- HarborNAS/webui: `docs/harbor-framework-protocol-map.md`.

## Verification Scope

For Phase 1, verify that this document agrees with the active v2.0 contract, exposes no stale task ingress as current authority, and passes repository diff/whitespace checks.

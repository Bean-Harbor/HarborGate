# Navi WhatsApp delivery authorization

This supplemental service profile preserves the Gate/Beacon v2.0 turn, result
and notification envelopes. It closes a gap between accepting a home action
and sending its result: a queued result can outlive its member binding. Gate
owns delivery and retries; Beacon remains the authority for current membership
and binding generations. No shared state files or runtime imports are introduced.

## Service endpoint

Beacon serves `POST /api/im/whatsapp/delivery-authorization` on the same internal
HTTP origin as the turn API, including the packaged single-port service. It
requires the existing Gate-to-Beacon bearer credential and
`X-Contract-Version: 2.0`. User/browser credentials are insufficient.

The JSON request has exactly these fields:

```json
{
  "recipient": "15555550101",
  "route_key": "opaque-gate-route",
  "conversation_handle": "opaque-beacon-handle",
  "text": "The text about to be sent",
  "has_attachments": false
}
```

Beacon derives the home, user and binding from its own storage. The request
cannot select those identities. Success is HTTP 200 with `{"allowed":true}`;
revoked, replaced or otherwise invalid binding context returns HTTP 403 with
the shared error envelope and `IM_DELIVERY_NOT_ALLOWED`. Missing service auth,
wrong version and invalid requests retain the shared 401/400/422 behavior.
Storage or identity-epoch unavailability returns 503; no home data is returned.

The conversation handle must match the current active phone/route binding,
and the workspace, user and membership must remain active. Setup repairs have
an opaque HMAC handle bound to recipient, route and exact text. Their lifetime
is five minutes from the original message timestamp, allowing the existing
60-second future-clock tolerance. They cannot carry attachments. Replay uses
the same handle; restarting does not refresh expiry. Identity reset revokes it.

## Gate behavior

- Check before downloading an unmaterialized WhatsApp attachment, before a
  provider preparation/upload, after preparation, and immediately before each
  actual send. Persist the original handle and route in each queued item.
- Recheck reused provider uploads and cached media after restart. Do not use a
  newer route's conversation handle for an old queued response or notification.
- Notifications use their existing v2 `conversation.handle`; it also participates
  in WhatsApp notification idempotency. A changed handle with the same delivery
  key is a 409 request rejection, including after a previous failed attempt.
- Explicit denial ends the item; service unavailability pauses it for retry.
  Old WhatsApp plans lacking an originating handle cannot send. Other channels
  keep their existing fingerprint format.
- Camera media consent checks still apply independently. Platform-accepted
  sends cannot be recalled by this check; the authorization RPC and Meta's
  eventual delivery are separate operations.

## Rollout and evidence

Deploy Beacon and Gate as one coordinated candidate once the eight-function
release condition is met. Gate fails closed if the supplemental endpoint is
absent. No v1.5/v2 dual mode, Meta credential provisioning or cloud deployment
is part of this increment.

Tests cover real local HTTP auth/version boundaries, active membership,
replacement and revoke, repair expiry/reset, denied text, revocation during
media/text preparation, persisted upload retries, outage recovery and
notification identity conflicts. Gate library regression and packaged Beacon
entrypoint checks accompany the RISC-V components.

Authenticated routing to multiple remote Navi devices and its return transport
remain separate implementation work. These tests do not prove a fleet relay or
real Meta delivery. In that relay, this request must reach the originating
Navi, and central route selection must also be rechecked before delivery.

## Native event notices

Binding a phone does not opt into proactive event notices. The signed-in Owner
changes that preference using `POST /api/onboarding/whatsapp/notifications` with
exactly `binding_id`, `revision`, `enabled`, and `idempotency_key`. The browser
uses HarborOS CSRF protection. The binding must still be active for this home.
The last 32 requests retain their receipts; retrying an earlier enable does not
undo a later disable. Replacement bindings begin with notices disabled.

Beacon's Guardian rule actions and local vision notification actions select
the Owner's active, opted-in binding for the originating workspace. They do
not create a global notification target. The camera must be explicitly selected
in that home's setup and still have Link `view_enabled`. Existing rules decide
which events trigger actions; this does not add an Arlo event ingestion path.

Beacon creates a signed opaque `im_notice_` handle covering binding generation,
notification revision, camera, original event expiry, recipient and route.
The event must follow consent (events in the same second are excluded), with
at most 60 seconds of forward clock tolerance. Authority expires 24 hours after
the original event; retries do not extend it. This expiry is a local stale-event
limit, not a claim about Meta's messaging eligibility. Notices contain no media.

The delivery authorization callback checks this handle, the current binding,
notification preference, active workspace/user/Owner membership, selected
camera and current Link permission. Gate treats the handle as opaque and uses
the same callback before sending and retrying. Disabling or re-enabling notices,
replacing the binding, changing homes or revoking camera access blocks old
queued notices. Callback requests with notice handles and attachments fail.

Native WhatsApp producers serialize the existing v2 notification envelope and
persist the original handle. Notification IDs include the binding and consent
generation. User-requested camera analysis notifications retain the requesting
conversation; explicit local UI results stay local. Provider fixtures verify
these paths; real Meta notifications and remote Navi routing remain pending.

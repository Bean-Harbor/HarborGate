# Navi WhatsApp phone proof and endpoint selection

This internal service profile accompanies the unchanged external IM v2 envelopes.
Gate owns its durable IM endpoint directory and provider queues. Beacon owns
household membership, phone proof and separate browser confirmation. Cloud owns
device certificate identity; Link forwards the bounded request to local Beacon.
No component reads another component's household state files.

## Phone and browser flow

Trusted Navi provisioning supplies Beacon with `HARBORNAVI_CLOUD_HUB_ID`.
The existing Owner-only binding-session endpoint then generates the prefilled
message `NAVI {hubId}.{64-hex-token}`. The Hub ID is public routing information;
the random token is short-lived proof and is excluded from QR labels and logs.
Without that provisioning, the existing local single-Navi form is retained.

Only Meta-verified inbox events may initiate this flow. Both generic WhatsApp
message aliases reject requests with `WHATSAPP_SIGNED_WEBHOOK_REQUIRED`.
Gate preserves the provider timestamp, sender and official-number-scoped route.
It forwards proof to the indicated device without forwarding a household turn.

Beacon serves `POST /api/im/whatsapp/binding-proof` on its task API origin and
packaged single-port service. It requires the existing service bearer credential
and `X-Contract-Version: 2.0`, and accepts at most 4,096 bytes with exactly:

```json
{
  "token": "64-hex-single-use-session-proof",
  "recipient": "15555550101",
  "route_key": "opaque-official-account-route",
  "occurred_at": "2026-09-12T12:00:00Z"
}
```

The token resolves the intended household/member locally. A proof must come from
the session's lifetime and match the originally observed phone and route. Proof
records observation only; the signed-in Owner must separately confirm the phone
in the browser. An active workspace, user and membership are checked before a
result is returned. No household ID, member ID or token is returned to Gate.

HTTP 200 returns `status`, `session_id`, and `expires_at` (Unix seconds). Status
is `awaiting_owner_confirmation` or `bound`; bound also includes `binding_id`
and `confirmed_at`. Invalid/expired/revoked proof returns 403
`IM_BINDING_NOT_ALLOWED`; missing service auth, wrong contract and invalid JSON
use 401/400/422. Unavailable identity/storage returns 503. A successful binding
outlives the five-minute proof; reusing proof cannot reactivate an old selection.

## Gate selection and recovery

Both `HARBORCLOUD_RELAY_URL` and `HARBORCLOUD_RELAY_REGION` enable the fleet path.
Gate uses its ECS task role and the existing Cloud exchanges API; `bindingProof`
has a five-second deadline. Cloud receipts contain the certificate-ID SHA-256
as `hubIdentity`. The first proof poll pins it, and every selected turn and
delivery-authorization request includes the same expected identity. Cloud
rejects a changed identity before publishing household input.

The private `state_dir/navi-routes` directory has bounded JSON, exclusive locking
and atomic replacement. It persists pending proofs, replay markers and selected
Hub/certificate/binding/generation. A new phone proof pauses ordinary routing and
queued sends until confirmation, rejection or expiry. Polling resumes after a
Gate restart. A stale response cannot complete a newer pending proof. Failed or
expired proof does not select another device; it releases the pause on the old
confirmed selection, whose Beacon permissions are still checked on delivery.

Only a bound proof replaces the selection. History and opaque continuation are
scoped to official route, recipient, Hub and selection generation. Messages from
before or the same second as selection cannot access the new home. Outgoing
plans retain their original selection; preparation, delivery and restart retries
recheck it and ask the originating Beacon for current permission. Remote queued
plans cannot silently fall back to a local Beacon if fleet mode is disabled.

This candidate assumes one logical Gate directory on durable storage. It does
not establish multi-host coordinated storage or a fleet service deployment.
Remote media and asynchronous notification return currently fail closed.
Consumer activation, public WhatsApp profile delivery, factory-reset identity
rotation and real Meta/phone acceptance remain unfinished. Browser-local binding
completion alone is not evidence that the central route has activated; the
receipt flow below now supplies that separate result. These are required before the
eight-function completion gate and subsequent PR/Main/IMG/OTA work.


## Central route receipt and browser completion

Gate persists route updates atomically with the selection change. Each update
contains the original selection, recipient, official route, monotonic revision,
status (`active`, `paused`, or `retired`) and issue time. Newer revisions replace
pending older updates for the same generation; a late acknowledgement cannot
remove a newer update. Restart resumes delivery. Transport failures, missing
endpoints and credential outages retain the update for retry; a revoked binding
or changed device identity ends that update. A cancelled/expired switch sends a
new active revision for the former device. The bounded directory permits at
most 64 outstanding generations per phone/official route.

The Cloud operation `bindingRoute` requires the selected `hubIdentity` and a
five-second deadline. Link forwards it only to the service-authenticated Beacon
endpoint `POST /api/im/whatsapp/binding-route`. Its body has exactly these fields:

```json
{
  "binding_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "recipient": "15555550101",
  "route_key": "opaque-official-account-route",
  "generation": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "revision": 3,
  "status": "active",
  "issued_at": 1789214400
}
```

Beacon requires a confirmed binding with the same phone and route and a trusted
Hub association. It checks the active household/member before returning a
receipt. A different generation, conflicting same-revision request or revoked
binding is rejected. Older revisions are acknowledged with `applied:false`
without changing the current result; a retired generation cannot become active
again. Retired bindings also reject old home turns and queued deliveries.
Successful HTTP 200 acknowledges `binding_id`, `generation`, `revision`, `status`
and `applied`; it contains no household or member ID. The service uses the same
401/400/422/403/503 boundaries and 4,096-byte limit as phone proof.

The existing Owner-only browser session and confirmation endpoints now include
`connection_status`: `awaiting_route`, `ready`, `switching`, or `other_home`.
`active` means both the local binding and its latest central route receipt allow
this home. Local single-Navi bindings remain ready after Owner confirmation.
Remote confirmation preserves setup's messaging phase until a matching active
receipt arrives. Status reads do not advance tutorial progress; the user then
continues, with an idempotent confirmation request. Interrupted pages reload the
same binding, and waiting/switching states offer checking again or local use.
This is the last acknowledged route configuration, not a Meta delivery probe or
continuous device-online heartbeat. Consumer cloud provisioning remains needed.

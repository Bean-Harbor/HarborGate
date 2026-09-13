# Navi Cloud relay client

Gate can now send the existing WhatsApp v2 turn and delivery-authorization body
to a selected Navi through Cloud's internal `beacon-exchanges` HTTP API. The
client uses the AWS Rust SigV4 library for `execute-api`, a short-lived role
credential provider and the exact CDK API origin. It never holds a Navi's local
Gate-to-Beacon secret or publishes MQTT. Cloud/Link retain the device connection;
Beacon retains all household authority and opaque dialogue state.

`CloudRelayClient::from_ecs` obtains the Gate task role from ECS's fixed link-local
credential endpoint. It accepts only the ECS relative credential path, refreshes
the cached role before expiry, and refuses redirects or expired credentials.
`new` accepts an AWS `SharedCredentialsProvider` for another service deployment;
the returned credentials must still include a session token and expiry. Neither
constructor supports static end-user keys. Credentials and raw failed response
bodies are excluded from returned errors.

`HarborBeaconTaskClient::from_cloud_relay` binds one client to one validated Hub
ID. Setting both `HARBORCLOUD_RELAY_URL` and `HARBORCLOUD_RELAY_REGION` now enables
the Gate-owned [Navi route directory](navi-whatsapp-binding-proof.md) for WhatsApp
ingress. With neither set, the existing local service path remains available.
The fleet path requires phone proof and separate Owner confirmation before
selecting a device; ordinary message text cannot select a Hub. Central selection
receipts now return to the originating Navi through a durable update queue. The
browser waits for that receipt before continuing. Consumer cloud activation and
public service profile installation remain implementation work.

## Exchange behavior

- A turn keeps its original v2 body, including turn ID, provider timestamp,
  route and opaque continuation. A new HTTP exchange does not create a new
  business turn. Beacon's existing turn idempotency remains authoritative.
- During one attempt, interrupted POSTs reuse identical bytes and request ID.
  Pending requests are polled, and republished every two seconds to recover a
  lost device request/response. The initial total time budget is never extended.
- Turns have a maximum 180-second budget. Each delivery authorization has a new
  exchange ID and a five-second total budget, including credential lookup and
  HTTP retry. A previous allowed receipt cannot satisfy a later check.
- Receipt IDs and deadlines must match; malformed, mixed, expired or redirected
  responses fail. Requests and responses have the Cloud protocol's byte limits.
  Service/transport unavailability permits later retry; a valid Navi denial
  remains a terminal delivery denial.
- The selected device's `hubIdentity` is pinned in new exchanges and receipts.
  Cloud checks it before publishing household input. Certificate replacement
  denies old selections; new phone/browser proof is required. This does not
  replace the still-required factory-reset/cloud-identity lifecycle.
- Remote camera artifacts use the device-pinned `mediaArtifact` operation.
  Gate accepts only canonical artifact references, verifies each bounded chunk
  and writes through its private cache capability. The full transfer has a
  120-second deadline; failure removes the partial batch. Link applies current
  chat permission and fixed local HTTP routing. Gate never fetches a Navi's
  loopback URL itself.
- `notificationOutbox` and `notificationReceipt` use ten-second device-pinned
  exchanges. They carry Beacon intent and v2 provider receipts. Provider attempts
  and retry idempotency remain in Gate; household permission remains in Beacon.
- In-flight duplicate device exchanges do not consume a second Link worker.
  `RELAY_BUSY` keeps the Cloud exchange pending for retry within its original
  deadline, so a slow successful media read cannot lose to a busy duplicate.

## Validation

Gate tests exercise real local HTTP with two selected clients, immutable retries,
fresh authorization, five-second expiry, request/response rejection and rotating
task credentials. The Navi integration runner separately loads the actual Cloud
Lambda asset, checks Gate's signatures with the AWS JavaScript signer, then runs
the real Gate client against the Cloud handlers. Persistence and device responses
in that integration are fixtures, not real AWS or Navi devices.

The opt-in `actual_cloud_link_media_roundtrip` test runs through HarborNavi's
`tools/audit/remote-media-runner.py`. It also calls actual Link relay code and
compares Gate cache bytes, then exercises revocation and malformed ranges.
The Beacon camera backend and AWS persistence/broker are fixtures; this does
not establish real Arlo or Meta delivery.

Run the ignored `actual_cloud_bundle_roundtrip` test only through the owned local
fixture runner in HarborNavi `tools/audit/whatsapp-relay-gate-cloud-runner.py`.
The runner verifies sixteen signed HTTP calls, eight exchanges for two devices,
fresh allow/deny results and central route receipts, then stops its container. Regular Gate tests, formatting,
Clippy and RISC-V compilation accompany that test.

References: [AWS Rust signing library](https://docs.rs/aws-sigv4/1.5.1/aws_sigv4/http_request/)
and [AWS container credentials](https://docs.aws.amazon.com/sdkref/latest/guide/feature-container-credentials.html).
All eight Navi functions still precede PR integration, mainline merge, full image,
OTA and `.154` acceptance.

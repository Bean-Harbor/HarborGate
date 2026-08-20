# HarborGate / HarborBeacon Service Auth Rotation Runbook

## Scope

This runbook covers the RAG RC transition from the legacy shared bearer to two
directional authentication domains. It does not implement UDS and does not
change the v2.0 HTTP/JSON contract.

Authentication domains:

- `gate_to_beacon`: Gate sends; Beacon accepts current/previous.
- `beacon_to_gate`: Beacon sends; Gate accepts current/previous.

The Gate package is the single credential writer. The legacy
`/etc/default/harboros-beacon-gate` file is read only as migration input and is
left unchanged for package rollback.

## Credential State

The helper stores six root-owned `0600` files below
`/etc/harboros/service-auth`, itself root-owned `0700`:

```text
gate-to-beacon.send
gate-to-beacon.accept-current
gate-to-beacon.accept-previous
beacon-to-gate.send
beacon-to-gate.accept-current
beacon-to-gate.accept-previous
```

Services receive only the files required for their caller and verifier roles
through systemd `LoadCredential=`.

## Upgrade Order

1. Install the Gate RC package. Its `postinst` runs `prepare` and restarts Gate.
   Callers still send the legacy key while the new Gate verifier accepts
   current and previous.
2. Install the Beacon RC package. Restarted Beacon still sends the legacy key
   while its verifier accepts current and previous.
3. Verify both v2.0 directions, retry/idempotency behavior, and absence of 401.
4. Switch callers:

   ```bash
   /usr/lib/harboros-im-gate/ensure-harborbeacon-token-env switch
   systemctl restart harboros-beacon.service
   systemctl restart harboros-im-gate.service
   ```

5. Verify both directions again and observe the agreed release window.
6. Remove old accepted keys only after successful observation:

   ```bash
   /usr/lib/harboros-im-gate/ensure-harborbeacon-token-env finalize
   systemctl restart harboros-beacon.service
   systemctl restart harboros-im-gate.service
   ```

7. Prove old, missing, wrong-domain, and malformed credentials are rejected.

Do not combine `prepare`, `switch`, and `finalize` in one maintainer-script
execution. Each phase is idempotent and uses a six-file rollback transaction.

## Rollback

Before `switch`, roll back Beacon and Gate packages in the approved package
transaction; both callers still use the legacy credentials.

After `switch`, stop both services, roll back Gate first and Beacon second, then
start both services. The unchanged legacy env supplies the previous approved
credentials; no secret reconstruction is required.

After `finalize`, use the same stopped two-package rollback. Do not attempt a
single-service live rollback because the remaining RC verifier no longer
accepts the legacy key.

## Stop Conditions

Stop if either direction cannot be verified before switching, the credential
transaction cannot restore every file after a failpoint, the v2.0 contract
header or error envelope changes, or deployment would require a request-level
authentication fallback.

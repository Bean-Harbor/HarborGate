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

No historical Beacon package is currently approved as a rollback artifact.
Known legacy `postinst` implementations can preserve the model token while
changing `/etc/default/harboros-beacon` to `0644`, so directly downgrading to an
unverified historical package is unsafe.

Before any rollback rehearsal or release, pin the actual installed Gate and
Beacon package names, versions, binary digests, package SHA256 values, and
systemd `ExecStart` commands. Inspect the exact rollback packages' maintainer
scripts, then build or select approved compatibility rollback artifacts that
preserve every secret-bearing environment file as `root:root 0600`.

Validate the approved artifacts in a disposable systemd-enabled Debian
environment through the full old -> RC -> rollback lifecycle. The evidence must
show the selected rollback plan, package digests, service startup, credential
owner/mode, and token-free logs. Manual post-downgrade `chmod` is not an
acceptable recovery control.

Until that evidence exists, rollback after `prepare`, `switch`, or `finalize` is
`NO-GO`; do not use this runbook to operate on a target machine.

## Stop Conditions

Stop if either direction cannot be verified before switching, the credential
transaction cannot restore every file after a failpoint, approved rollback
artifacts and their SHA256 values are not pinned, the v2.0 contract header or
error envelope changes, or deployment would require a request-level
authentication fallback.

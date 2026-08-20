#![cfg(unix)]

use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use tempfile::TempDir;

const LEGACY_TOKEN: &str = "legacy_shared_token_0123456789abcdef0123456789abcdef";
const G2B_LEGACY_TOKEN: &str = "legacy_gate_to_beacon_0123456789abcdef0123456789";
const B2G_LEGACY_TOKEN: &str = "legacy_beacon_to_gate_0123456789abcdef0123456789";
const UNRELATED_TOKEN: &str = "unrelated_previous_0123456789abcdef0123456789ab";
const CREDENTIALS: [&str; 6] = [
    "gate-to-beacon.send",
    "gate-to-beacon.accept-current",
    "gate-to-beacon.accept-previous",
    "beacon-to-gate.send",
    "beacon-to-gate.accept-current",
    "beacon-to-gate.accept-previous",
];

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .canonicalize()
        .expect("repo root should resolve")
}

fn writer_command(
    mode: &str,
    auth_dir: &Path,
    legacy_env: &Path,
    failpoint: Option<&str>,
) -> Command {
    let mut command = Command::new("bash");
    command
        .arg(repo_root().join("debian/ensure-harborbeacon-token-env"))
        .arg(mode)
        .env("HARBOR_SERVICE_AUTH_DIR", auth_dir)
        .env("HARBOR_LEGACY_SHARED_ENV_FILE", legacy_env);
    if let Some(failpoint) = failpoint {
        command.env("HARBOR_SERVICE_AUTH_FAILPOINT", failpoint);
    }
    command
}

fn run_writer(mode: &str, auth_dir: &Path, legacy_env: &Path, failpoint: Option<&str>) -> Output {
    writer_command(mode, auth_dir, legacy_env, failpoint)
        .output()
        .expect("service-auth writer should execute")
}

fn credential(auth_dir: &Path, name: &str) -> String {
    fs::read_to_string(auth_dir.join(name))
        .expect("credential should exist")
        .trim()
        .to_string()
}

fn snapshot(auth_dir: &Path) -> BTreeMap<String, Vec<u8>> {
    CREDENTIALS
        .into_iter()
        .map(|name| {
            (
                name.to_string(),
                fs::read(auth_dir.join(name)).expect("credential should exist"),
            )
        })
        .collect()
}

fn assert_success_without_secret_output(output: &Output, secrets: &[&str]) {
    assert!(
        output.status.success(),
        "writer failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_no_secret_output(output, secrets);
}

fn assert_no_secret_output(output: &Output, secrets: &[&str]) {
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    for secret in secrets {
        assert!(!combined.contains(secret), "writer leaked a token");
    }
}

#[test]
fn fresh_install_creates_distinct_directional_credentials() {
    let temp = TempDir::new().unwrap();
    let auth_dir = temp.path().join("service-auth");
    let legacy_env = temp.path().join("missing-legacy-env");

    let output = run_writer("prepare", &auth_dir, &legacy_env, None);

    assert_success_without_secret_output(&output, &[]);
    let g2b = credential(&auth_dir, "gate-to-beacon.accept-current");
    let b2g = credential(&auth_dir, "beacon-to-gate.accept-current");
    assert!(g2b.len() >= 48);
    assert!(b2g.len() >= 48);
    assert_ne!(g2b, b2g);
    assert_eq!(credential(&auth_dir, "gate-to-beacon.send"), g2b);
    assert_eq!(credential(&auth_dir, "beacon-to-gate.send"), b2g);
    assert!(credential(&auth_dir, "gate-to-beacon.accept-previous").is_empty());
    assert!(credential(&auth_dir, "beacon-to-gate.accept-previous").is_empty());
    assert_eq!(
        fs::metadata(&auth_dir).unwrap().permissions().mode() & 0o777,
        0o700
    );
    for name in CREDENTIALS {
        assert_eq!(
            fs::metadata(auth_dir.join(name))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }
}

#[test]
fn legacy_upgrade_uses_prepare_switch_finalize_without_downtime_gap() {
    let temp = TempDir::new().unwrap();
    let auth_dir = temp.path().join("service-auth");
    let legacy_env = temp.path().join("harboros-beacon-gate");
    let legacy = format!(
        "HARBOR_TASK_API_BEARER_TOKEN={LEGACY_TOKEN}\nHARBORBEACON_WEB_API_TOKEN={LEGACY_TOKEN}\nIM_AGENT_SERVICE_TOKEN={LEGACY_TOKEN}\n"
    );
    fs::write(&legacy_env, &legacy).unwrap();

    let prepare = run_writer("prepare", &auth_dir, &legacy_env, None);
    assert_success_without_secret_output(&prepare, &[LEGACY_TOKEN]);
    let g2b_current = credential(&auth_dir, "gate-to-beacon.accept-current");
    let b2g_current = credential(&auth_dir, "beacon-to-gate.accept-current");
    assert_ne!(g2b_current, b2g_current);
    assert_ne!(g2b_current, LEGACY_TOKEN);
    assert_ne!(b2g_current, LEGACY_TOKEN);
    assert_eq!(credential(&auth_dir, "gate-to-beacon.send"), LEGACY_TOKEN);
    assert_eq!(credential(&auth_dir, "beacon-to-gate.send"), LEGACY_TOKEN);
    assert_eq!(
        credential(&auth_dir, "gate-to-beacon.accept-previous"),
        LEGACY_TOKEN
    );
    assert_eq!(
        credential(&auth_dir, "beacon-to-gate.accept-previous"),
        LEGACY_TOKEN
    );
    assert_eq!(fs::read_to_string(&legacy_env).unwrap(), legacy);

    let switch = run_writer("switch", &auth_dir, &legacy_env, None);
    assert_success_without_secret_output(&switch, &[LEGACY_TOKEN]);
    assert_eq!(credential(&auth_dir, "gate-to-beacon.send"), g2b_current);
    assert_eq!(credential(&auth_dir, "beacon-to-gate.send"), b2g_current);
    assert_eq!(
        credential(&auth_dir, "gate-to-beacon.accept-previous"),
        LEGACY_TOKEN
    );

    let finalize = run_writer("finalize", &auth_dir, &legacy_env, None);
    assert_success_without_secret_output(&finalize, &[LEGACY_TOKEN]);
    assert!(credential(&auth_dir, "gate-to-beacon.accept-previous").is_empty());
    assert!(credential(&auth_dir, "beacon-to-gate.accept-previous").is_empty());
    assert_eq!(fs::read_to_string(&legacy_env).unwrap(), legacy);
}

#[test]
fn legacy_upgrade_parses_quoted_and_unquoted_environment_values() {
    let cases = [
        (G2B_LEGACY_TOKEN.to_string(), B2G_LEGACY_TOKEN.to_string()),
        (
            format!("\"{G2B_LEGACY_TOKEN}\""),
            format!("'{B2G_LEGACY_TOKEN}'"),
        ),
    ];

    for (g2b_assignment, b2g_assignment) in cases {
        let temp = TempDir::new().unwrap();
        let auth_dir = temp.path().join("service-auth");
        let legacy_env = temp.path().join("harboros-beacon-gate");
        fs::write(
            &legacy_env,
            format!(
                "  HARBORBEACON_WEB_API_TOKEN = {g2b_assignment}  \nIM_AGENT_SERVICE_TOKEN={b2g_assignment}\n"
            ),
        )
        .unwrap();

        let output = run_writer("prepare", &auth_dir, &legacy_env, None);

        assert_success_without_secret_output(&output, &[G2B_LEGACY_TOKEN, B2G_LEGACY_TOKEN]);
        assert_eq!(
            credential(&auth_dir, "gate-to-beacon.send"),
            G2B_LEGACY_TOKEN
        );
        assert_eq!(
            credential(&auth_dir, "beacon-to-gate.send"),
            B2G_LEGACY_TOKEN
        );
    }
}

#[test]
fn invalid_known_legacy_assignment_fails_without_generating_credentials() {
    let invalid_assignments = [
        "HARBORBEACON_WEB_API_TOKEN=too-short\n",
        "HARBORBEACON_WEB_API_TOKEN=\"unterminated_token_0123456789abcdef0123456789\n",
        "HARBORBEACON_WEB_API_TOKEN malformed_token_0123456789abcdef0123456789\n",
    ];

    for assignment in invalid_assignments {
        let temp = TempDir::new().unwrap();
        let auth_dir = temp.path().join("service-auth");
        let legacy_env = temp.path().join("harboros-beacon-gate");
        fs::write(&legacy_env, assignment).unwrap();

        let output = run_writer("prepare", &auth_dir, &legacy_env, None);

        assert!(!output.status.success());
        assert!(CREDENTIALS.iter().all(|name| !auth_dir.join(name).exists()));
        assert!(!auth_dir.join(".credential-transaction").exists());
    }
}

#[test]
fn repeated_prepare_is_idempotent() {
    let temp = TempDir::new().unwrap();
    let auth_dir = temp.path().join("service-auth");
    let legacy_env = temp.path().join("missing-legacy-env");
    assert_success_without_secret_output(&run_writer("prepare", &auth_dir, &legacy_env, None), &[]);
    let first = snapshot(&auth_dir);

    assert_success_without_secret_output(&run_writer("prepare", &auth_dir, &legacy_env, None), &[]);

    assert_eq!(snapshot(&auth_dir), first);
}

#[test]
fn recover_mode_never_initializes_or_rotates_credentials() {
    let temp = TempDir::new().unwrap();
    let auth_dir = temp.path().join("service-auth");
    let legacy_env = temp.path().join("missing-legacy-env");

    let uninitialized = run_writer("recover", &auth_dir, &legacy_env, None);
    assert!(!uninitialized.status.success());
    assert!(CREDENTIALS.iter().all(|name| !auth_dir.join(name).exists()));

    assert_success_without_secret_output(&run_writer("prepare", &auth_dir, &legacy_env, None), &[]);
    let before = snapshot(&auth_dir);
    assert_success_without_secret_output(&run_writer("recover", &auth_dir, &legacy_env, None), &[]);
    assert_eq!(snapshot(&auth_dir), before);
}

#[test]
fn transaction_failure_restores_all_credential_files() {
    let temp = TempDir::new().unwrap();
    let auth_dir = temp.path().join("service-auth");
    let legacy_env = temp.path().join("harboros-beacon-gate");
    fs::write(
        &legacy_env,
        format!(
            "HARBORBEACON_WEB_API_TOKEN={LEGACY_TOKEN}\nIM_AGENT_SERVICE_TOKEN={LEGACY_TOKEN}\n"
        ),
    )
    .unwrap();
    assert_success_without_secret_output(
        &run_writer("prepare", &auth_dir, &legacy_env, None),
        &[LEGACY_TOKEN],
    );
    let before = snapshot(&auth_dir);

    let output = run_writer("switch", &auth_dir, &legacy_env, Some("after_rename_3"));

    assert!(!output.status.success());
    assert_no_secret_output(&output, &[LEGACY_TOKEN]);
    assert_eq!(snapshot(&auth_dir), before);
}

#[test]
fn sigkill_interruption_is_recovered_to_the_complete_old_snapshot() {
    let temp = TempDir::new().unwrap();
    let auth_dir = temp.path().join("service-auth");
    let legacy_env = temp.path().join("harboros-beacon-gate");
    fs::write(
        &legacy_env,
        format!(
            "HARBORBEACON_WEB_API_TOKEN={LEGACY_TOKEN}\nIM_AGENT_SERVICE_TOKEN={LEGACY_TOKEN}\n"
        ),
    )
    .unwrap();
    assert_success_without_secret_output(
        &run_writer("prepare", &auth_dir, &legacy_env, None),
        &[LEGACY_TOKEN],
    );
    let before = snapshot(&auth_dir);

    let interrupted = run_writer(
        "switch",
        &auth_dir,
        &legacy_env,
        Some("after_rename_3_sigkill"),
    );

    assert!(!interrupted.status.success());
    assert!(auth_dir.join(".credential-transaction").is_dir());
    assert_ne!(snapshot(&auth_dir), before);

    let recovery = run_writer("recover", &auth_dir, &legacy_env, None);
    assert_success_without_secret_output(&recovery, &[LEGACY_TOKEN]);
    assert_eq!(snapshot(&auth_dir), before);
    assert!(!auth_dir.join(".credential-transaction").exists());
}

#[test]
fn journal_boundaries_recover_prepared_or_keep_committed_snapshot() {
    let rollback_failpoints = [
        "before_prepared_sigkill",
        "after_prepared_sigkill",
        "after_rename_6_sigkill",
        "after_auth_dir_sync_sigkill",
    ];

    for failpoint in rollback_failpoints {
        let temp = TempDir::new().unwrap();
        let auth_dir = temp.path().join("service-auth");
        let legacy_env = temp.path().join("harboros-beacon-gate");
        fs::write(
            &legacy_env,
            format!(
                "HARBORBEACON_WEB_API_TOKEN={LEGACY_TOKEN}\nIM_AGENT_SERVICE_TOKEN={LEGACY_TOKEN}\n"
            ),
        )
        .unwrap();
        assert_success_without_secret_output(
            &run_writer("prepare", &auth_dir, &legacy_env, None),
            &[LEGACY_TOKEN],
        );
        let before = snapshot(&auth_dir);

        let interrupted = run_writer("switch", &auth_dir, &legacy_env, Some(failpoint));

        assert!(
            !interrupted.status.success(),
            "failpoint succeeded: {failpoint}"
        );
        assert_no_secret_output(&interrupted, &[LEGACY_TOKEN]);
        let transaction = auth_dir.join(".credential-transaction");
        assert!(transaction.is_dir());
        if failpoint == "before_prepared_sigkill" {
            assert!(!transaction.join("state").exists());
        } else {
            assert_eq!(
                fs::read_to_string(transaction.join("state")).unwrap(),
                "prepared\n"
            );
        }

        let recovery = run_writer("recover", &auth_dir, &legacy_env, None);
        assert_success_without_secret_output(&recovery, &[LEGACY_TOKEN]);
        assert_eq!(snapshot(&auth_dir), before, "wrong recovery: {failpoint}");
        assert!(!transaction.exists());
    }

    let temp = TempDir::new().unwrap();
    let auth_dir = temp.path().join("service-auth");
    let legacy_env = temp.path().join("harboros-beacon-gate");
    fs::write(
        &legacy_env,
        format!(
            "HARBORBEACON_WEB_API_TOKEN={LEGACY_TOKEN}\nIM_AGENT_SERVICE_TOKEN={LEGACY_TOKEN}\n"
        ),
    )
    .unwrap();
    assert_success_without_secret_output(
        &run_writer("prepare", &auth_dir, &legacy_env, None),
        &[LEGACY_TOKEN],
    );
    let mut expected = snapshot(&auth_dir);
    let g2b_current = expected["gate-to-beacon.accept-current"].clone();
    let b2g_current = expected["beacon-to-gate.accept-current"].clone();
    expected.insert("gate-to-beacon.send".to_string(), g2b_current);
    expected.insert("beacon-to-gate.send".to_string(), b2g_current);

    let interrupted = run_writer(
        "switch",
        &auth_dir,
        &legacy_env,
        Some("after_committed_sigkill"),
    );

    assert!(!interrupted.status.success());
    assert_no_secret_output(&interrupted, &[LEGACY_TOKEN]);
    let transaction = auth_dir.join(".credential-transaction");
    assert_eq!(
        fs::read_to_string(transaction.join("state")).unwrap(),
        "committed\n"
    );
    let recovery = run_writer("recover", &auth_dir, &legacy_env, None);
    assert_success_without_secret_output(&recovery, &[LEGACY_TOKEN]);
    assert_eq!(snapshot(&auth_dir), expected);
    assert!(!transaction.exists());
}

#[test]
fn recovery_is_idempotent_when_sigkill_interrupts_snapshot_restore() {
    let temp = TempDir::new().unwrap();
    let auth_dir = temp.path().join("service-auth");
    let legacy_env = temp.path().join("harboros-beacon-gate");
    fs::write(
        &legacy_env,
        format!(
            "HARBORBEACON_WEB_API_TOKEN={LEGACY_TOKEN}\nIM_AGENT_SERVICE_TOKEN={LEGACY_TOKEN}\n"
        ),
    )
    .unwrap();
    assert_success_without_secret_output(
        &run_writer("prepare", &auth_dir, &legacy_env, None),
        &[LEGACY_TOKEN],
    );
    let before = snapshot(&auth_dir);
    let commit_interruption = run_writer(
        "switch",
        &auth_dir,
        &legacy_env,
        Some("after_rename_6_sigkill"),
    );
    assert!(!commit_interruption.status.success());

    let recovery_interruption = run_writer(
        "recover",
        &auth_dir,
        &legacy_env,
        Some("after_restore_3_sigkill"),
    );

    assert!(!recovery_interruption.status.success());
    assert_no_secret_output(&recovery_interruption, &[LEGACY_TOKEN]);
    assert_ne!(snapshot(&auth_dir), before);
    assert_eq!(
        fs::read_to_string(auth_dir.join(".credential-transaction/state")).unwrap(),
        "prepared\n"
    );

    let recovery = run_writer("recover", &auth_dir, &legacy_env, None);
    assert_success_without_secret_output(&recovery, &[LEGACY_TOKEN]);
    assert_eq!(snapshot(&auth_dir), before);
    assert!(!auth_dir.join(".credential-transaction").exists());
}

#[test]
fn concurrent_prepare_writers_are_serialized() {
    let temp = TempDir::new().unwrap();
    let auth_dir = temp.path().join("service-auth");
    let legacy_env = temp.path().join("missing-legacy-env");
    let mut children = (0..8)
        .map(|_| {
            writer_command("prepare", &auth_dir, &legacy_env, None)
                .spawn()
                .expect("concurrent writer should start")
        })
        .collect::<Vec<_>>();

    for child in &mut children {
        let status = child.wait().expect("concurrent writer should finish");
        assert!(status.success(), "concurrent writer failed: {status}");
    }

    let g2b = credential(&auth_dir, "gate-to-beacon.accept-current");
    let b2g = credential(&auth_dir, "beacon-to-gate.accept-current");
    assert_ne!(g2b, b2g);
    assert_eq!(credential(&auth_dir, "gate-to-beacon.send"), g2b);
    assert_eq!(credential(&auth_dir, "beacon-to-gate.send"), b2g);
    assert!(!auth_dir.join(".credential-transaction").exists());
}

#[test]
fn repeated_prepare_repairs_previous_to_match_the_active_sender() {
    let temp = TempDir::new().unwrap();
    let auth_dir = temp.path().join("service-auth");
    let legacy_env = temp.path().join("harboros-beacon-gate");
    fs::write(
        &legacy_env,
        format!(
            "HARBORBEACON_WEB_API_TOKEN={LEGACY_TOKEN}\nIM_AGENT_SERVICE_TOKEN={LEGACY_TOKEN}\n"
        ),
    )
    .unwrap();
    assert_success_without_secret_output(
        &run_writer("prepare", &auth_dir, &legacy_env, None),
        &[LEGACY_TOKEN],
    );
    fs::write(
        auth_dir.join("gate-to-beacon.accept-previous"),
        format!("{UNRELATED_TOKEN}\n"),
    )
    .unwrap();

    let output = run_writer("prepare", &auth_dir, &legacy_env, None);

    assert_success_without_secret_output(&output, &[LEGACY_TOKEN, UNRELATED_TOKEN]);
    assert_eq!(
        credential(&auth_dir, "gate-to-beacon.accept-previous"),
        LEGACY_TOKEN
    );
}

#[test]
fn finalize_rejects_unswitched_callers() {
    let temp = TempDir::new().unwrap();
    let auth_dir = temp.path().join("service-auth");
    let legacy_env = temp.path().join("harboros-beacon-gate");
    fs::write(
        &legacy_env,
        format!(
            "HARBORBEACON_WEB_API_TOKEN={LEGACY_TOKEN}\nIM_AGENT_SERVICE_TOKEN={LEGACY_TOKEN}\n"
        ),
    )
    .unwrap();
    assert_success_without_secret_output(
        &run_writer("prepare", &auth_dir, &legacy_env, None),
        &[LEGACY_TOKEN],
    );

    let output = run_writer("finalize", &auth_dir, &legacy_env, None);

    assert!(!output.status.success());
    assert_no_secret_output(&output, &[LEGACY_TOKEN]);
}

#[test]
fn writer_refuses_symlinked_credential_target() {
    use std::os::unix::fs::symlink;

    let temp = TempDir::new().unwrap();
    let auth_dir = temp.path().join("service-auth");
    fs::create_dir(&auth_dir).unwrap();
    let protected = temp.path().join("protected");
    fs::write(&protected, "do-not-change\n").unwrap();
    symlink(&protected, auth_dir.join("gate-to-beacon.send")).unwrap();

    let output = run_writer(
        "prepare",
        &auth_dir,
        &temp.path().join("missing-legacy-env"),
        None,
    );

    assert!(!output.status.success());
    assert_eq!(fs::read_to_string(&protected).unwrap(), "do-not-change\n");
}

#[test]
fn package_uses_role_scoped_systemd_credentials_and_prepare_only() {
    let root = repo_root();
    let workflow = fs::read_to_string(root.join(".github/workflows/release.yml")).unwrap();
    let postinst = fs::read_to_string(root.join("debian/postinst")).unwrap();
    let control = fs::read_to_string(root.join("debian/control")).unwrap();
    let unit = fs::read_to_string(root.join("debian/harboros-im-gate.service")).unwrap();
    let recovery_unit =
        fs::read_to_string(root.join("debian/harboros-service-auth-recovery.service")).unwrap();

    assert!(workflow.contains("ensure-harborbeacon-token-env"));
    assert!(workflow.contains("harboros-service-auth-recovery.service"));
    assert!(control.contains("util-linux"));
    assert!(control.contains("Provides: harboros-service-auth-abi (= 1)"));
    assert!(postinst.contains("ensure-harborbeacon-token-env prepare"));
    assert!(!postinst.contains("ensure-harborbeacon-token-env switch"));
    assert!(!postinst.contains("ensure-harborbeacon-token-env finalize"));
    assert!(unit.contains("Requires=harboros-service-auth-recovery.service"));
    assert!(unit.contains("After=harboros-service-auth-recovery.service"));
    assert!(!unit.contains("EnvironmentFile=-/etc/default/harboros-beacon-gate"));
    assert!(unit.contains("LoadCredential=gate-to-beacon-send:"));
    assert!(unit.contains("LoadCredential=beacon-to-gate-accept-current:"));
    assert!(unit.contains("LoadCredential=beacon-to-gate-accept-previous:"));
    assert!(!unit.contains("HARBOR_TASK_API_BEARER_TOKEN"));
    assert!(!unit.contains("HARBOR_MODEL_API_TOKEN"));
    assert!(recovery_unit.contains("Type=oneshot"));
    assert!(recovery_unit.contains("Before=harboros-im-gate.service harboros-beacon.service"));
    assert!(recovery_unit.contains("ensure-harborbeacon-token-env recover"));
    assert!(!recovery_unit.contains("RemainAfterExit"));
}

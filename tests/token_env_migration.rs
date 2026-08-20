#![cfg(unix)]

use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use tempfile::TempDir;

const LEGACY_TOKEN: &str = "legacy_shared_token_0123456789abcdef0123456789abcdef";
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

fn run_writer(mode: &str, auth_dir: &Path, legacy_env: &Path, failpoint: Option<&str>) -> Output {
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
    let unit = fs::read_to_string(root.join("debian/harboros-im-gate.service")).unwrap();

    assert!(workflow.contains("ensure-harborbeacon-token-env"));
    assert!(postinst.contains("ensure-harborbeacon-token-env prepare"));
    assert!(!postinst.contains("ensure-harborbeacon-token-env switch"));
    assert!(!postinst.contains("ensure-harborbeacon-token-env finalize"));
    assert!(!unit.contains("EnvironmentFile=-/etc/default/harboros-beacon-gate"));
    assert!(unit.contains("LoadCredential=gate-to-beacon-send:"));
    assert!(unit.contains("LoadCredential=beacon-to-gate-accept-current:"));
    assert!(unit.contains("LoadCredential=beacon-to-gate-accept-previous:"));
    assert!(!unit.contains("HARBOR_TASK_API_BEARER_TOKEN"));
    assert!(!unit.contains("HARBOR_MODEL_API_TOKEN"));
}

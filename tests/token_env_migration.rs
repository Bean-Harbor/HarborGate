#![cfg(unix)]

use std::collections::HashMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use tempfile::TempDir;

const LEGACY_TOKEN: &str = "legacy_shared_token_0123456789abcdef0123456789abcdef";

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .canonicalize()
        .expect("repo root should resolve")
}

fn run_writer(env_file: &Path, failpoint: Option<&str>) -> Output {
    let mut command = Command::new("bash");
    command
        .arg(repo_root().join("debian/ensure-harborbeacon-token-env"))
        .env("HARBORBEACON_GATE_ENV_FILE", env_file);
    if let Some(failpoint) = failpoint {
        command.env("HARBOR_TOKEN_ENV_FAILPOINT", failpoint);
    }
    command.output().expect("token writer should execute")
}

fn parse_env(path: &Path) -> HashMap<String, String> {
    fs::read_to_string(path)
        .expect("environment file should be readable")
        .lines()
        .filter_map(|line| line.split_once('='))
        .map(|(key, value)| (key.to_string(), value.to_string()))
        .collect()
}

fn assert_success_without_secret_output(output: &Output, secrets: &[&str]) {
    assert!(
        output.status.success(),
        "writer failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
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
fn fresh_install_creates_distinct_domain_tokens_with_mode_0600() {
    let temp = TempDir::new().unwrap();
    let env_file = temp.path().join("harboros-beacon-gate");

    let output = run_writer(&env_file, None);

    assert_success_without_secret_output(&output, &[]);
    let values = parse_env(&env_file);
    let task = &values["HARBOR_TASK_API_BEARER_TOKEN"];
    let web = &values["HARBORBEACON_WEB_API_TOKEN"];
    let im = &values["IM_AGENT_SERVICE_TOKEN"];
    assert!(task.len() >= 48);
    assert!(web.len() >= 48);
    assert!(im.len() >= 48);
    assert_ne!(task, web);
    assert_ne!(task, im);
    assert_ne!(web, im);
    assert_eq!(
        fs::metadata(&env_file).unwrap().permissions().mode() & 0o777,
        0o600
    );
}

#[test]
fn legacy_shared_token_migration_preserves_web_and_rotates_other_domains() {
    let temp = TempDir::new().unwrap();
    let env_file = temp.path().join("harboros-beacon-gate");
    fs::write(
        &env_file,
        format!(
            "UNRELATED=value\nHARBOR_TASK_API_BEARER_TOKEN={LEGACY_TOKEN}\nHARBORBEACON_WEB_API_TOKEN={LEGACY_TOKEN}\nIM_AGENT_SERVICE_TOKEN={LEGACY_TOKEN}\n"
        ),
    )
    .unwrap();
    fs::set_permissions(&env_file, fs::Permissions::from_mode(0o644)).unwrap();

    let output = run_writer(&env_file, None);

    assert_success_without_secret_output(&output, &[LEGACY_TOKEN]);
    let values = parse_env(&env_file);
    assert_eq!(values["UNRELATED"], "value");
    assert_eq!(values["HARBORBEACON_WEB_API_TOKEN"], LEGACY_TOKEN);
    assert_ne!(values["HARBOR_TASK_API_BEARER_TOKEN"], LEGACY_TOKEN);
    assert_ne!(values["IM_AGENT_SERVICE_TOKEN"], LEGACY_TOKEN);
    assert_ne!(
        values["HARBOR_TASK_API_BEARER_TOKEN"],
        values["IM_AGENT_SERVICE_TOKEN"]
    );
    assert_eq!(
        fs::metadata(&env_file).unwrap().permissions().mode() & 0o777,
        0o600
    );
}

#[test]
fn repeated_install_is_idempotent_for_valid_tokens() {
    let temp = TempDir::new().unwrap();
    let env_file = temp.path().join("harboros-beacon-gate");
    assert_success_without_secret_output(&run_writer(&env_file, None), &[]);
    let first = fs::read(&env_file).unwrap();

    assert_success_without_secret_output(&run_writer(&env_file, None), &[]);

    assert_eq!(fs::read(&env_file).unwrap(), first);
}

#[test]
fn failure_before_atomic_rename_keeps_original_recoverable_and_redacts_logs() {
    let temp = TempDir::new().unwrap();
    let env_file = temp.path().join("harboros-beacon-gate");
    let original = format!(
        "HARBOR_TASK_API_BEARER_TOKEN={LEGACY_TOKEN}\nHARBORBEACON_WEB_API_TOKEN={LEGACY_TOKEN}\nIM_AGENT_SERVICE_TOKEN={LEGACY_TOKEN}\n"
    );
    fs::write(&env_file, &original).unwrap();

    let output = run_writer(&env_file, Some("before_rename"));

    assert!(!output.status.success());
    assert_eq!(fs::read_to_string(&env_file).unwrap(), original);
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(!combined.contains(LEGACY_TOKEN));
}

#[test]
fn failure_after_atomic_rename_restores_original_file() {
    let temp = TempDir::new().unwrap();
    let env_file = temp.path().join("harboros-beacon-gate");
    let original = format!(
        "HARBOR_TASK_API_BEARER_TOKEN={LEGACY_TOKEN}\nHARBORBEACON_WEB_API_TOKEN={LEGACY_TOKEN}\nIM_AGENT_SERVICE_TOKEN={LEGACY_TOKEN}\n"
    );
    fs::write(&env_file, &original).unwrap();

    let output = run_writer(&env_file, Some("after_rename"));

    assert!(!output.status.success());
    assert_eq!(fs::read_to_string(&env_file).unwrap(), original);
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(!combined.contains(LEGACY_TOKEN));
}

#[test]
fn failure_after_fresh_install_rename_restores_absent_state() {
    let temp = TempDir::new().unwrap();
    let env_file = temp.path().join("harboros-beacon-gate");

    let output = run_writer(&env_file, Some("after_rename"));

    assert!(!output.status.success());
    assert!(!env_file.exists());
}

#[test]
fn writer_refuses_symlink_target() {
    use std::os::unix::fs::symlink;

    let temp = TempDir::new().unwrap();
    let protected = temp.path().join("protected");
    let env_file = temp.path().join("harboros-beacon-gate");
    fs::write(&protected, "do-not-change\n").unwrap();
    symlink(&protected, &env_file).unwrap();

    let output = run_writer(&env_file, None);

    assert!(!output.status.success());
    assert_eq!(fs::read_to_string(&protected).unwrap(), "do-not-change\n");
}

#[test]
fn release_package_installs_the_single_writer_and_postinst_only_invokes_it() {
    let root = repo_root();
    let workflow = fs::read_to_string(root.join(".github/workflows/release.yml")).unwrap();
    let postinst = fs::read_to_string(root.join("debian/postinst")).unwrap();

    assert!(workflow.contains("ensure-harborbeacon-token-env"));
    assert!(postinst.contains("/usr/lib/harboros-im-gate/ensure-harborbeacon-token-env"));
    assert!(!postinst.contains("HARBOR_TASK_API_BEARER_TOKEN="));
    assert!(!postinst.contains("HARBORBEACON_WEB_API_TOKEN="));
    assert!(!postinst.contains("IM_AGENT_SERVICE_TOKEN="));
}

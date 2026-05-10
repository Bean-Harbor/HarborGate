use std::fs;
use std::path::{Path, PathBuf};

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .expect("repo root should resolve")
}

fn rust_source_files(root: &Path) -> Vec<PathBuf> {
    let mut pending = vec![root.join("rust/harborgate/src")];
    let mut files = Vec::new();
    while let Some(path) = pending.pop() {
        for entry in fs::read_dir(path).expect("source directory should be readable") {
            let entry = entry.expect("source entry should be readable");
            let path = entry.path();
            if path.is_dir() {
                pending.push(path);
            } else if path.extension().and_then(|value| value.to_str()) == Some("rs") {
                files.push(path);
            }
        }
    }
    files
}

fn python_files_under(path: &Path) -> Vec<PathBuf> {
    if !path.exists() {
        return Vec::new();
    }
    let mut pending = vec![path.to_path_buf()];
    let mut files = Vec::new();
    while let Some(path) = pending.pop() {
        for entry in fs::read_dir(path).expect("directory should be readable") {
            let entry = entry.expect("directory entry should be readable");
            let path = entry.path();
            if path.is_dir() {
                pending.push(path);
            } else if path.extension().and_then(|value| value.to_str()) == Some("py") {
                files.push(path);
            }
        }
    }
    files
}

#[test]
fn main_branch_has_no_python_runtime_packaging() {
    let root = repo_root();

    assert!(!root.join("pyproject.toml").exists());
    assert!(python_files_under(&root.join("src/im_agent")).is_empty());
    assert!(python_files_under(&root.join("tests")).is_empty());
    assert!(python_files_under(&root.join("tools")).is_empty());
    assert!(!root.join("tools/run_platform_live_gate.py").exists());
    assert!(!root.join("tools/run_weixin_ingress_probe.py").exists());

    let manifest = fs::read_to_string(root.join("rust/harborgate/Cargo.toml"))
        .expect("crate manifest should be readable");
    assert!(manifest.contains("name = \"harborgate\""));
    assert!(!manifest.contains("harborgate-rust"));
}

#[test]
fn active_rust_source_stays_on_v2_turn_contract() {
    let root = repo_root();
    let mut joined_source = String::new();
    for path in rust_source_files(&root) {
        joined_source.push_str(&fs::read_to_string(path).expect("source file should be readable"));
        joined_source.push('\n');
    }

    assert!(!joined_source.contains("\"/api/tasks\""));
    assert!(!joined_source.contains("X-Contract-Version: 1.5"));
    assert!(!joined_source.contains("args.resume_token"));
    assert!(!joined_source.contains("active_frame.kind"));
}

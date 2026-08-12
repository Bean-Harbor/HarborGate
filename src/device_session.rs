use atomicwrites::{AtomicFile, OverwriteBehavior};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs::{self, OpenOptions};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};
use thiserror::Error;
use uuid::Uuid;

const STATE_FILE: &str = "device-sessions.json";
const LOCK_FILE: &str = "device-sessions.lock";

#[derive(Debug, Error)]
pub enum DeviceSessionError {
    #[error("pairing code is invalid or expired")]
    InvalidPairing,
    #[error("device session is invalid or expired")]
    InvalidSession,
    #[error("device session is not authorized for this camera")]
    CameraDenied,
    #[error("device session state is unavailable: {0}")]
    Unavailable(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IssuedPairing {
    pub code: String,
    pub camera_id: String,
    pub expires_at_epoch_seconds: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeviceSessionPrincipal {
    pub principal_id: String,
    pub camera_id: String,
    pub expires_at_epoch_seconds: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExchangedDeviceSession {
    pub token: String,
    pub principal: DeviceSessionPrincipal,
}

#[derive(Debug, Clone)]
pub struct DeviceSessionStore {
    root: PathBuf,
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct DeviceSessionState {
    #[serde(default)]
    pairings: Vec<PairingRecord>,
    #[serde(default)]
    sessions: Vec<SessionRecord>,
}

#[derive(Debug, Serialize, Deserialize)]
struct PairingRecord {
    pairing_id: String,
    code_sha256: String,
    camera_id: String,
    expires_at_epoch_seconds: u64,
    #[serde(default)]
    consumed_at_epoch_seconds: Option<u64>,
}

#[derive(Debug, Serialize, Deserialize)]
struct SessionRecord {
    session_id: String,
    token_sha256: String,
    camera_id: String,
    expires_at_epoch_seconds: u64,
    #[serde(default)]
    revoked_at_epoch_seconds: Option<u64>,
}

impl DeviceSessionStore {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    pub fn issue_pairing(
        &self,
        camera_id: &str,
        ttl_seconds: u64,
    ) -> Result<IssuedPairing, DeviceSessionError> {
        let camera_id = validate_camera_id(camera_id)?;
        if !(60..=1800).contains(&ttl_seconds) {
            return Err(DeviceSessionError::Unavailable(
                "pairing ttl must be between 60 and 1800 seconds".to_string(),
            ));
        }
        let now = epoch_seconds();
        let expires_at = now.saturating_add(ttl_seconds);
        let code = random_secret();
        self.with_locked_state(|state| {
            state.pairings.retain(|record| {
                record.expires_at_epoch_seconds > now && record.consumed_at_epoch_seconds.is_none()
            });
            state.pairings.push(PairingRecord {
                pairing_id: Uuid::new_v4().simple().to_string(),
                code_sha256: sha256(&code),
                camera_id: camera_id.clone(),
                expires_at_epoch_seconds: expires_at,
                consumed_at_epoch_seconds: None,
            });
            Ok(())
        })?;
        Ok(IssuedPairing {
            code,
            camera_id,
            expires_at_epoch_seconds: expires_at,
        })
    }

    pub fn exchange(
        &self,
        code: &str,
        session_ttl_seconds: u64,
    ) -> Result<ExchangedDeviceSession, DeviceSessionError> {
        if code.trim().is_empty() || session_ttl_seconds == 0 {
            return Err(DeviceSessionError::InvalidPairing);
        }
        let now = epoch_seconds();
        let code_hash = sha256(code.trim());
        let token = random_secret();
        let token_hash = sha256(&token);
        let session_id = Uuid::new_v4().simple().to_string();
        let expires_at = now.saturating_add(session_ttl_seconds);
        let mut exchanged_camera_id = None;
        self.with_locked_state(|state| {
            let pairing = state
                .pairings
                .iter_mut()
                .find(|record| {
                    record.consumed_at_epoch_seconds.is_none()
                        && record.expires_at_epoch_seconds > now
                        && constant_time_eq(record.code_sha256.as_bytes(), code_hash.as_bytes())
                })
                .ok_or(DeviceSessionError::InvalidPairing)?;
            pairing.consumed_at_epoch_seconds = Some(now);
            exchanged_camera_id = Some(pairing.camera_id.clone());
            state.sessions.retain(|record| {
                record.expires_at_epoch_seconds > now && record.revoked_at_epoch_seconds.is_none()
            });
            state.sessions.push(SessionRecord {
                session_id: session_id.clone(),
                token_sha256: token_hash.clone(),
                camera_id: pairing.camera_id.clone(),
                expires_at_epoch_seconds: expires_at,
                revoked_at_epoch_seconds: None,
            });
            Ok(())
        })?;
        Ok(ExchangedDeviceSession {
            token,
            principal: DeviceSessionPrincipal {
                principal_id: format!("harbornavi-device:{session_id}"),
                camera_id: exchanged_camera_id.ok_or(DeviceSessionError::InvalidPairing)?,
                expires_at_epoch_seconds: expires_at,
            },
        })
    }

    pub fn authenticate(
        &self,
        token: &str,
        camera_id: &str,
    ) -> Result<DeviceSessionPrincipal, DeviceSessionError> {
        let camera_id = validate_camera_id(camera_id)?;
        let principal = self.current(token)?;
        if principal.camera_id != camera_id {
            return Err(DeviceSessionError::CameraDenied);
        }
        Ok(principal)
    }

    pub fn current(&self, token: &str) -> Result<DeviceSessionPrincipal, DeviceSessionError> {
        if token.trim().is_empty() {
            return Err(DeviceSessionError::InvalidSession);
        }
        let now = epoch_seconds();
        let token_hash = sha256(token.trim());
        self.with_locked_state_read(|state| {
            let session = state
                .sessions
                .iter()
                .find(|record| {
                    record.revoked_at_epoch_seconds.is_none()
                        && record.expires_at_epoch_seconds > now
                        && constant_time_eq(record.token_sha256.as_bytes(), token_hash.as_bytes())
                })
                .ok_or(DeviceSessionError::InvalidSession)?;
            Ok(DeviceSessionPrincipal {
                principal_id: format!("harbornavi-device:{}", session.session_id),
                camera_id: session.camera_id.clone(),
                expires_at_epoch_seconds: session.expires_at_epoch_seconds,
            })
        })
    }

    pub fn revoke(&self, token: &str) -> Result<(), DeviceSessionError> {
        let now = epoch_seconds();
        let token_hash = sha256(token.trim());
        self.with_locked_state(|state| {
            let session = state
                .sessions
                .iter_mut()
                .find(|record| {
                    record.revoked_at_epoch_seconds.is_none()
                        && constant_time_eq(record.token_sha256.as_bytes(), token_hash.as_bytes())
                })
                .ok_or(DeviceSessionError::InvalidSession)?;
            session.revoked_at_epoch_seconds = Some(now);
            Ok(())
        })
    }

    fn with_locked_state<T>(
        &self,
        action: impl FnOnce(&mut DeviceSessionState) -> Result<T, DeviceSessionError>,
    ) -> Result<T, DeviceSessionError> {
        fs::create_dir_all(&self.root).map_err(unavailable)?;
        set_directory_permissions(&self.root).map_err(unavailable)?;
        let lock_path = self.root.join(LOCK_FILE);
        let lock = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .truncate(false)
            .open(&lock_path)
            .map_err(unavailable)?;
        set_file_permissions(&lock_path).map_err(unavailable)?;
        lock.lock_exclusive().map_err(unavailable)?;
        let result = (|| {
            let state_path = self.root.join(STATE_FILE);
            let mut state = load_state(&state_path)?;
            let result = action(&mut state)?;
            persist_state(&state_path, &state)?;
            Ok(result)
        })();
        let _ = FileExt::unlock(&lock);
        result
    }

    fn with_locked_state_read<T>(
        &self,
        action: impl FnOnce(&DeviceSessionState) -> Result<T, DeviceSessionError>,
    ) -> Result<T, DeviceSessionError> {
        fs::create_dir_all(&self.root).map_err(unavailable)?;
        set_directory_permissions(&self.root).map_err(unavailable)?;
        let lock_path = self.root.join(LOCK_FILE);
        let lock = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .truncate(false)
            .open(&lock_path)
            .map_err(unavailable)?;
        set_file_permissions(&lock_path).map_err(unavailable)?;
        lock.lock_shared().map_err(unavailable)?;
        let result = load_state(&self.root.join(STATE_FILE)).and_then(|state| action(&state));
        let _ = FileExt::unlock(&lock);
        result
    }
}

fn load_state(path: &Path) -> Result<DeviceSessionState, DeviceSessionError> {
    match fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .map_err(|error| DeviceSessionError::Unavailable(error.to_string())),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            Ok(DeviceSessionState::default())
        }
        Err(error) => Err(unavailable(error)),
    }
}

fn persist_state(path: &Path, state: &DeviceSessionState) -> Result<(), DeviceSessionError> {
    let bytes = serde_json::to_vec_pretty(state)
        .map_err(|error| DeviceSessionError::Unavailable(error.to_string()))?;
    AtomicFile::new(path, OverwriteBehavior::AllowOverwrite)
        .write(|file| {
            use std::io::Write;
            file.write_all(&bytes)?;
            file.sync_all()
        })
        .map_err(|error| DeviceSessionError::Unavailable(error.to_string()))?;
    set_file_permissions(path).map_err(unavailable)
}

fn validate_camera_id(camera_id: &str) -> Result<String, DeviceSessionError> {
    let camera_id = camera_id.trim();
    if camera_id.is_empty()
        || camera_id.len() > 128
        || camera_id.contains('/')
        || camera_id.chars().any(char::is_control)
    {
        return Err(DeviceSessionError::CameraDenied);
    }
    Ok(camera_id.to_string())
}

fn random_secret() -> String {
    format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple())
}

fn sha256(value: &str) -> String {
    format!("{:x}", Sha256::digest(value.as_bytes()))
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    left.iter()
        .zip(right.iter())
        .fold(0u8, |difference, (left, right)| difference | (left ^ right))
        == 0
}

fn epoch_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

fn unavailable(error: std::io::Error) -> DeviceSessionError {
    DeviceSessionError::Unavailable(error.to_string())
}

#[cfg(unix)]
fn set_directory_permissions(path: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
}

#[cfg(not(unix))]
fn set_directory_permissions(_path: &Path) -> std::io::Result<()> {
    Ok(())
}

#[cfg(unix)]
fn set_file_permissions(path: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))
}

#[cfg(not(unix))]
fn set_file_permissions(_path: &Path) -> std::io::Result<()> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pairing_is_one_time_and_session_is_camera_scoped_and_durable() {
        let dir = tempfile::tempdir().unwrap();
        let store = DeviceSessionStore::new(dir.path());
        let pairing = store.issue_pairing("camera-252", 300).unwrap();
        let session = store.exchange(&pairing.code, 3600).unwrap();

        assert_eq!(session.principal.camera_id, "camera-252");
        assert!(matches!(
            store.exchange(&pairing.code, 3600),
            Err(DeviceSessionError::InvalidPairing)
        ));
        assert!(store.authenticate(&session.token, "camera-252").is_ok());
        assert!(matches!(
            store.authenticate(&session.token, "camera-999"),
            Err(DeviceSessionError::CameraDenied)
        ));

        let restarted = DeviceSessionStore::new(dir.path());
        assert!(restarted.authenticate(&session.token, "camera-252").is_ok());
        let persisted = fs::read_to_string(dir.path().join(STATE_FILE)).unwrap();
        assert!(!persisted.contains(&pairing.code));
        assert!(!persisted.contains(&session.token));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;

            assert_eq!(
                fs::metadata(dir.path()).unwrap().permissions().mode() & 0o777,
                0o700
            );
            assert_eq!(
                fs::metadata(dir.path().join(STATE_FILE))
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
            assert_eq!(
                fs::metadata(dir.path().join(LOCK_FILE))
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
        }
        restarted.revoke(&session.token).unwrap();
        assert!(matches!(
            restarted.authenticate(&session.token, "camera-252"),
            Err(DeviceSessionError::InvalidSession)
        ));
    }

    #[test]
    fn rejects_unsafe_camera_ids_and_short_pairing_ttl() {
        let dir = tempfile::tempdir().unwrap();
        let store = DeviceSessionStore::new(dir.path());

        assert!(matches!(
            store.issue_pairing("../camera", 300),
            Err(DeviceSessionError::CameraDenied)
        ));
        assert!(matches!(
            store.issue_pairing("camera-252", 10),
            Err(DeviceSessionError::Unavailable(_))
        ));
    }
}

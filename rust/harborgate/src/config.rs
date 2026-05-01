use serde_json::Value;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub struct AppConfig {
    pub host: String,
    pub port: u16,
    pub data_dir: PathBuf,
    pub state_dir: PathBuf,
    pub public_origin: String,
    pub contract_version: String,
    pub service_token: String,
    pub harborbeacon_base_url: String,
    pub harborbeacon_token: String,
    pub harborbeacon_turn_endpoint: String,
    pub feishu: FeishuConfig,
    pub weixin: WeixinConfig,
    pub enable_feishu_websocket: bool,
    pub enable_weixin_runtime: bool,
}

#[derive(Debug, Clone)]
pub struct FeishuConfig {
    pub app_id: String,
    pub app_secret: String,
    pub domain: String,
    pub connection_mode: String,
    pub allowed_users: Vec<String>,
    pub group_policy: String,
    pub bot_open_id: String,
    pub bot_user_id: String,
    pub bot_name: String,
    pub verification_token: String,
    pub webhook_path: String,
    pub base_url: String,
    pub auth_base_url: String,
    pub enable_live_send: bool,
    pub timeout_seconds: u64,
}

#[derive(Debug, Clone)]
pub struct WeixinConfig {
    pub state_dir: PathBuf,
    pub account_id: String,
    pub token: String,
    pub base_url: String,
    pub user_id: String,
    pub cdn_base_url: String,
    pub timeout_seconds: u64,
    pub poll_timeout_ms: u64,
}

impl AppConfig {
    pub fn from_env() -> Self {
        let data_dir = env_or("IM_AGENT_DATA_DIR", "data/sessions");
        let state_dir = env_or_else("IM_AGENT_STATE_DIR", || {
            PathBuf::from(&data_dir)
                .parent()
                .map(|path| path.to_string_lossy().to_string())
                .unwrap_or_else(|| "data".to_string())
        });
        let mut turn_endpoint = env_or("HARBORBEACON_TURN_ENDPOINT", "/api/web/turns");
        if !turn_endpoint.starts_with('/') {
            turn_endpoint = format!("/{turn_endpoint}");
        }
        let base_url = strip_endpoint_suffix(
            &strip_endpoint_suffix(
                &strip_endpoint_suffix(
                    &env_first(&["HARBORBEACON_WEB_API_URL", "HARBORBEACON_TASK_API_URL"]),
                    &turn_endpoint,
                ),
                "/api/web/turns",
            ),
            "/api/turns",
        );
        Self {
            host: env_or("IM_AGENT_HOST", "127.0.0.1"),
            port: env_or("IM_AGENT_PORT", "8787").parse().unwrap_or(8787),
            data_dir: PathBuf::from(data_dir),
            state_dir: PathBuf::from(state_dir),
            public_origin: env_trim("IM_AGENT_PUBLIC_ORIGIN"),
            contract_version: env_or("IM_AGENT_CONTRACT_VERSION", "2.0"),
            service_token: env_trim("IM_AGENT_SERVICE_TOKEN"),
            harborbeacon_base_url: base_url,
            harborbeacon_token: env_first(&[
                "HARBORBEACON_WEB_API_TOKEN",
                "HARBORBEACON_TASK_API_TOKEN",
            ]),
            harborbeacon_turn_endpoint: turn_endpoint,
            feishu: FeishuConfig::from_env(),
            weixin: WeixinConfig::from_env(),
            enable_feishu_websocket: env_flag_default("HARBORGATE_RUST_FEISHU_WEBSOCKET", true),
            enable_weixin_runtime: env_flag_default("HARBORGATE_WEIXIN_RUNTIME_ENABLED", true),
        }
    }

    pub fn harborbeacon_enabled(&self) -> bool {
        !self.harborbeacon_base_url.trim().is_empty()
    }
}

impl WeixinConfig {
    pub const ILINK_BASE_URL: &'static str = "https://ilinkai.weixin.qq.com";
    pub const DEFAULT_CDN_BASE_URL: &'static str = "https://novac2c.cdn.weixin.qq.com/c2c";

    pub fn from_env() -> Self {
        Self {
            state_dir: PathBuf::from(env_or("WEIXIN_STATE_DIR", "data/weixin")),
            account_id: env_trim("WEIXIN_ACCOUNT_ID"),
            token: env_trim("WEIXIN_BOT_TOKEN"),
            base_url: env_or("WEIXIN_BASE_URL", Self::ILINK_BASE_URL),
            user_id: env_trim("WEIXIN_USER_ID"),
            cdn_base_url: env_or("WEIXIN_CDN_BASE_URL", Self::DEFAULT_CDN_BASE_URL),
            timeout_seconds: env_or("WEIXIN_TIMEOUT_SECONDS", "45").parse().unwrap_or(45),
            poll_timeout_ms: env_or("WEIXIN_POLL_TIMEOUT_MS", "35000")
                .parse()
                .unwrap_or(35000),
        }
    }
}

impl FeishuConfig {
    pub fn from_env() -> Self {
        let connection_mode_env = env_trim("FEISHU_CONNECTION_MODE");
        let enable_live_send_env = env_present("FEISHU_ENABLE_LIVE_SEND");
        let mut config = Self {
            app_id: env_trim("FEISHU_APP_ID"),
            app_secret: env_trim("FEISHU_APP_SECRET"),
            domain: env_or("FEISHU_DOMAIN", "feishu").to_lowercase(),
            connection_mode: if connection_mode_env.is_empty() {
                "websocket".to_string()
            } else {
                connection_mode_env.to_lowercase()
            },
            allowed_users: env_trim("FEISHU_ALLOWED_USERS")
                .split(',')
                .filter_map(|item| {
                    let item = item.trim();
                    (!item.is_empty()).then(|| item.to_string())
                })
                .collect(),
            group_policy: env_or("FEISHU_GROUP_POLICY", "allowlist").to_lowercase(),
            bot_open_id: env_trim("FEISHU_BOT_OPEN_ID"),
            bot_user_id: env_trim("FEISHU_BOT_USER_ID"),
            bot_name: env_trim("FEISHU_BOT_NAME"),
            verification_token: env_trim("FEISHU_VERIFICATION_TOKEN"),
            webhook_path: env_or("FEISHU_WEBHOOK_PATH", "/feishu/webhook"),
            base_url: env_or("FEISHU_BASE_URL", "https://open.feishu.cn"),
            auth_base_url: env_or("FEISHU_AUTH_BASE_URL", "https://open.feishu.cn"),
            enable_live_send: env_flag("FEISHU_ENABLE_LIVE_SEND"),
            timeout_seconds: env_or("FEISHU_TIMEOUT_SECONDS", "20").parse().unwrap_or(20),
        };
        for path in setup_portal_candidates() {
            if config.apply_setup_portal_file(
                &path,
                !connection_mode_env.is_empty(),
                enable_live_send_env,
            ) {
                break;
            }
        }
        config
    }

    pub fn configured(&self) -> bool {
        !self.app_id.is_empty() && !self.app_secret.is_empty()
    }

    fn apply_setup_portal_file(
        &mut self,
        path: &Path,
        connection_mode_is_env: bool,
        enable_live_send_is_env: bool,
    ) -> bool {
        let Ok(raw) = fs::read_to_string(path) else {
            return false;
        };
        let Ok(payload) = serde_json::from_str::<Value>(&raw) else {
            return false;
        };
        let Some(feishu) = setup_portal_feishu(&payload) else {
            return false;
        };
        fill_text_if_empty(&mut self.app_id, feishu, "app_id");
        fill_text_if_empty(&mut self.app_secret, feishu, "app_secret");
        fill_text_if_empty(&mut self.bot_open_id, feishu, "bot_open_id");
        fill_text_if_empty(&mut self.bot_user_id, feishu, "bot_user_id");
        fill_text_if_empty(&mut self.bot_name, feishu, "bot_name");
        fill_text_if_empty(&mut self.bot_name, feishu, "app_name");
        fill_text_if_empty(&mut self.verification_token, feishu, "verification_token");
        if !connection_mode_is_env {
            if let Some(connection_mode) = text_field(feishu, "connection_mode") {
                self.connection_mode = connection_mode.to_lowercase();
            }
        }
        if !enable_live_send_is_env {
            if let Some(enable_live_send) = bool_field(feishu, "enable_live_send") {
                self.enable_live_send = enable_live_send;
            }
        }
        true
    }
}

pub fn env_flag(key: &str) -> bool {
    matches!(
        env_trim(key).to_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

pub fn env_flag_default(key: &str, default_value: bool) -> bool {
    let value = env_trim(key);
    if value.is_empty() {
        default_value
    } else {
        matches!(value.to_lowercase().as_str(), "1" | "true" | "yes" | "on")
    }
}

fn env_trim(key: &str) -> String {
    env::var(key).unwrap_or_default().trim().to_string()
}

fn env_present(key: &str) -> bool {
    env::var_os(key).is_some()
}

fn env_or(key: &str, default_value: &str) -> String {
    let value = env_trim(key);
    if value.is_empty() {
        default_value.to_string()
    } else {
        value
    }
}

fn env_or_else(key: &str, default_value: impl FnOnce() -> String) -> String {
    let value = env_trim(key);
    if value.is_empty() {
        default_value()
    } else {
        value
    }
}

fn env_first(keys: &[&str]) -> String {
    for key in keys {
        let value = env_trim(key);
        if !value.is_empty() {
            return value;
        }
    }
    String::new()
}

fn strip_endpoint_suffix(base_url: &str, endpoint_suffix: &str) -> String {
    let normalized = base_url.trim().trim_end_matches('/').to_string();
    if normalized.is_empty() {
        return normalized;
    }
    let suffix = endpoint_suffix.trim_end_matches('/');
    if normalized.ends_with(suffix) {
        normalized[..normalized.len() - suffix.len()]
            .trim_end_matches('/')
            .to_string()
    } else {
        normalized
    }
}

fn setup_portal_candidates() -> Vec<PathBuf> {
    let mut paths = Vec::new();
    push_candidate(&mut paths, env_trim("FEISHU_SETUP_PORTAL_PATH"));
    let state_dir = env_trim("IM_AGENT_STATE_DIR");
    if !state_dir.is_empty() {
        push_candidate(
            &mut paths,
            PathBuf::from(state_dir)
                .join("_setup_portal.json")
                .to_string_lossy()
                .to_string(),
        );
    }
    let data_dir = env_trim("IM_AGENT_DATA_DIR");
    if !data_dir.is_empty() {
        let data_path = PathBuf::from(data_dir);
        if let Some(parent) = data_path.parent() {
            push_candidate(
                &mut paths,
                parent
                    .join("_setup_portal.json")
                    .to_string_lossy()
                    .to_string(),
            );
        }
    }
    paths
}

fn push_candidate(paths: &mut Vec<PathBuf>, raw: String) {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return;
    }
    let path = PathBuf::from(trimmed);
    if !paths.iter().any(|item| item == &path) {
        paths.push(path);
    }
}

fn setup_portal_feishu(payload: &Value) -> Option<&Value> {
    payload
        .get("feishu")
        .or_else(|| payload.pointer("/platforms/feishu"))
        .or_else(|| payload.pointer("/setup/feishu"))
}

fn fill_text_if_empty(field: &mut String, object: &Value, key: &str) {
    if field.trim().is_empty() {
        if let Some(value) = text_field(object, key) {
            *field = value;
        }
    }
}

fn text_field(object: &Value, key: &str) -> Option<String> {
    object
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToString::to_string)
}

fn bool_field(object: &Value, key: &str) -> Option<bool> {
    match object.get(key)? {
        Value::Bool(value) => Some(*value),
        Value::String(value) => match value.trim().to_lowercase().as_str() {
            "1" | "true" | "yes" | "on" => Some(true),
            "0" | "false" | "no" | "off" => Some(false),
            _ => None,
        },
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use tempfile::tempdir;

    fn empty_feishu_config() -> FeishuConfig {
        FeishuConfig {
            app_id: String::new(),
            app_secret: String::new(),
            domain: "feishu".into(),
            connection_mode: "websocket".into(),
            allowed_users: vec![],
            group_policy: "allowlist".into(),
            bot_open_id: String::new(),
            bot_user_id: String::new(),
            bot_name: String::new(),
            verification_token: String::new(),
            webhook_path: "/feishu/webhook".into(),
            base_url: "https://open.feishu.cn".into(),
            auth_base_url: "https://open.feishu.cn".into(),
            enable_live_send: false,
            timeout_seconds: 20,
        }
    }

    #[test]
    fn setup_portal_fills_feishu_config_when_env_is_empty() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("_setup_portal.json");
        fs::write(
            &path,
            json!({
                "feishu": {
                    "app_id": "cli_live",
                    "app_secret": "secret_live",
                    "app_name": "harbor-beacon",
                    "connection_mode": "webhook",
                    "enable_live_send": true,
                    "verification_token": "verify_live"
                }
            })
            .to_string(),
        )
        .unwrap();

        let mut config = empty_feishu_config();
        assert!(config.apply_setup_portal_file(&path, false, false));

        assert_eq!(config.app_id, "cli_live");
        assert_eq!(config.app_secret, "secret_live");
        assert_eq!(config.bot_name, "harbor-beacon");
        assert_eq!(config.connection_mode, "webhook");
        assert!(config.enable_live_send);
        assert_eq!(config.verification_token, "verify_live");
    }

    #[test]
    fn setup_portal_does_not_override_explicit_env_values() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("_setup_portal.json");
        fs::write(
            &path,
            json!({
                "feishu": {
                    "app_id": "cli_portal",
                    "app_secret": "secret_portal",
                    "connection_mode": "webhook",
                    "enable_live_send": true
                }
            })
            .to_string(),
        )
        .unwrap();

        let mut config = empty_feishu_config();
        config.app_id = "cli_env".into();
        config.app_secret = "secret_env".into();
        config.connection_mode = "websocket".into();
        config.enable_live_send = false;

        assert!(config.apply_setup_portal_file(&path, true, true));

        assert_eq!(config.app_id, "cli_env");
        assert_eq!(config.app_secret, "secret_env");
        assert_eq!(config.connection_mode, "websocket");
        assert!(!config.enable_live_send);
    }
}

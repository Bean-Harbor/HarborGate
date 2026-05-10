use crate::adapters::PlatformAdapter;
use crate::config::WeixinConfig;
use crate::error::GatewayError;
use crate::models::{utc_now_iso, InboundMessage, OutboundMessage};
use aes::cipher::{block_padding::Pkcs7, BlockEncryptMut, KeyInit};
use async_trait::async_trait;
use axum::http::StatusCode;
use base64::Engine as _;
use reqwest::header::HeaderMap;
use reqwest::Client;
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;
use uuid::Uuid;

const CHANNEL_VERSION: &str = "2.2.0";
const ILINK_APP_ID: &str = "bot";
const ILINK_APP_CLIENT_VERSION: &str = "131584";
const EP_GET_UPDATES: &str = "ilink/bot/getupdates";
const EP_GET_UPLOAD_URL: &str = "ilink/bot/getuploadurl";
const EP_SEND_MESSAGE: &str = "ilink/bot/sendmessage";
const EP_GET_BOT_QR: &str = "ilink/bot/get_bot_qrcode";
const EP_GET_QR_STATUS: &str = "ilink/bot/get_qrcode_status";

const ITEM_TEXT: i64 = 1;
const ITEM_IMAGE: i64 = 2;
const ITEM_FILE: i64 = 4;
const ITEM_VIDEO: i64 = 5;
const MSG_TYPE_BOT: i64 = 2;
const MSG_STATE_FINISH: i64 = 2;
const UPLOAD_MEDIA_IMAGE: i64 = 1;
const UPLOAD_MEDIA_VIDEO: i64 = 2;
const UPLOAD_MEDIA_FILE: i64 = 3;
const WEIXIN_MEDIA_ENCRYPT_TYPE: i64 = 1;
const MAX_TEXT_CHUNK_LENGTH: usize = 900;
const CDN_UPLOAD_MAX_RETRIES: usize = 3;

type Aes128EcbEnc = ecb::Encryptor<aes::Aes128>;

macro_rules! json_map {
    ({$($key:literal : $value:expr),* $(,)?}) => {{
        let mut map = serde_json::Map::new();
        $(map.insert($key.to_string(), serde_json::json!($value));)*
        map
    }};
}

#[derive(Debug, Clone)]
pub struct QRChallenge {
    pub qrcode: String,
    pub qrcode_img_content: String,
    pub bot_type: String,
}

#[derive(Debug, Clone, Default)]
pub struct WeixinAccount {
    pub account_id: String,
    pub token: String,
    pub base_url: String,
    pub user_id: String,
}

#[derive(Debug, Clone)]
struct NativeWeixinAttachment {
    delivery_kind: String,
    path: PathBuf,
    file_name: String,
}

#[derive(Debug, Clone)]
struct WeixinUploadedImage {
    original_download_param: String,
    aeskey_hex: String,
    original_ciphertext_size: usize,
}

#[derive(Debug, Clone)]
struct WeixinUploadedMedia {
    download_param: String,
    aeskey_hex: String,
    plaintext_size: usize,
    ciphertext_size: usize,
}

pub struct WeixinAdapter {
    config: WeixinConfig,
    http: Client,
    state: Mutex<WeixinState>,
}

#[derive(Debug, Clone)]
struct WeixinState {
    account: WeixinAccount,
    transport: serde_json::Map<String, Value>,
}

impl WeixinAdapter {
    pub fn new(config: WeixinConfig) -> Self {
        let account = discover_weixin_account(&config.state_dir, &config.account_id)
            .unwrap_or_else(|| WeixinAccount {
                account_id: config.account_id.clone(),
                token: config.token.clone(),
                base_url: if config.base_url.trim().is_empty() {
                    WeixinConfig::ILINK_BASE_URL.to_string()
                } else {
                    config.base_url.clone()
                },
                user_id: config.user_id.clone(),
            });
        let mut transport = default_transport_state(account.configured());
        if account.configured() {
            if let Some(persisted) =
                load_weixin_transport_state(&config.state_dir, &account.account_id)
            {
                for (key, value) in persisted {
                    transport.insert(key, value);
                }
                transport.insert("mode".into(), json!("polling"));
            }
        }
        Self {
            config,
            http: Client::new(),
            state: Mutex::new(WeixinState { account, transport }),
        }
    }

    pub fn configured(&self) -> bool {
        self.account().configured()
    }

    pub fn account(&self) -> WeixinAccount {
        self.refresh_account_from_disk();
        self.state
            .lock()
            .expect("weixin state lock poisoned")
            .account
            .clone()
    }

    pub fn refresh_account_from_disk(&self) {
        let current_account_id = {
            self.state
                .lock()
                .expect("weixin state lock poisoned")
                .account
                .account_id
                .clone()
        };
        let account = discover_weixin_account(&self.config.state_dir, &current_account_id)
            .or_else(|| discover_weixin_account(&self.config.state_dir, &self.config.account_id));
        if let Some(account) = account {
            let mut state = self.state.lock().expect("weixin state lock poisoned");
            if state.account.account_id != account.account_id
                || state.account.token != account.token
            {
                state.account = account.clone();
                state.transport = default_transport_state(true);
                if let Some(persisted) =
                    load_weixin_transport_state(&self.config.state_dir, &account.account_id)
                {
                    for (key, value) in persisted {
                        state.transport.insert(key, value);
                    }
                }
            }
        }
    }

    pub fn unbind(&self) -> Value {
        let account = self.account();
        let deleted = if account.account_id.trim().is_empty() {
            vec![]
        } else {
            clear_weixin_account_state(&self.config.state_dir, &account.account_id)
        };
        let mut state = self.state.lock().expect("weixin state lock poisoned");
        state.account = WeixinAccount::default();
        state.transport = default_transport_state(false);
        json!({
            "ok": true,
            "platform": "weixin",
            "account_id_configured": !account.account_id.trim().is_empty(),
            "account_id_masked": mask_secret(&account.account_id),
            "deleted_state_files": deleted,
            "configured": false,
            "status": "waiting_for_credentials",
        })
    }

    pub fn context_token_count(&self) -> usize {
        let account = self.account();
        if account.account_id.trim().is_empty() {
            return 0;
        }
        load_context_tokens(&self.config.state_dir, &account.account_id).len()
    }

    pub async fn request_qr_challenge(&self, bot_type: &str) -> Result<QRChallenge, GatewayError> {
        let bot_type = if bot_type.trim().is_empty() {
            "3"
        } else {
            bot_type.trim()
        };
        let endpoint = format!("{EP_GET_BOT_QR}?bot_type={}", urlencoding::encode(bot_type));
        let payload = self
            .get_json(
                WeixinConfig::ILINK_BASE_URL,
                &endpoint,
                None,
                self.config.timeout_seconds,
            )
            .await?;
        let qrcode = payload
            .get("qrcode")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        if qrcode.is_empty() {
            return Err(self.weixin_error("Weixin QR response did not include qrcode"));
        }
        Ok(QRChallenge {
            qrcode,
            qrcode_img_content: payload
                .get("qrcode_img_content")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_string(),
            bot_type: bot_type.to_string(),
        })
    }

    pub async fn poll_qr_status(
        &self,
        current_base_url: &str,
        qrcode: &str,
    ) -> Result<Value, GatewayError> {
        let base_url = if current_base_url.trim().is_empty() {
            WeixinConfig::ILINK_BASE_URL
        } else {
            current_base_url.trim()
        };
        let endpoint = format!("{EP_GET_QR_STATUS}?qrcode={}", urlencoding::encode(qrcode));
        self.get_json(base_url, &endpoint, None, 8).await
    }

    pub fn save_account(&self, account: WeixinAccount) -> Result<(), GatewayError> {
        save_weixin_account(&self.config.state_dir, &account)?;
        let mut state = self.state.lock().expect("weixin state lock poisoned");
        state.account = account;
        state.transport = default_transport_state(true);
        Ok(())
    }

    pub async fn poll_updates(&self) -> Result<Vec<Value>, GatewayError> {
        self.refresh_account_from_disk();
        let account = self.account();
        if !account.configured() {
            self.update_transport(json_map!({
                "status": "waiting_for_credentials",
                "connected": false,
                "last_poll_outcome": "waiting_for_credentials",
            }));
            return Ok(vec![]);
        }
        self.update_transport(json_map!({
            "status": "polling",
            "connected": true,
            "last_error": "",
            "last_poll_outcome": "polling",
            "last_poll_at": utc_now_iso(),
            "last_getupdates_error": "",
            "last_getupdates_message_ids": Vec::<Value>::new(),
            "last_getupdates_private_message_ids": Vec::<Value>::new(),
        }));
        let sync_buf = load_sync_buf(&self.config.state_dir, &account.account_id);
        let response = self
            .post_json(
                &account.base_url,
                EP_GET_UPDATES,
                json!({"get_updates_buf": sync_buf}),
                Some(&account.token),
                (self.config.poll_timeout_ms / 1000).max(1) + 10,
            )
            .await;
        let response = match response {
            Ok(response) => response,
            Err(error) => {
                let error_text =
                    redact_sensitive_text(&error.message, &[&account.account_id, &account.token]);
                let poll_status = poll_status_for_error(&error_text);
                let observed_at = utc_now_iso();
                if poll_status == "idle_timeout" {
                    self.update_transport(json_map!({
                        "status": "polling_idle",
                        "connected": true,
                        "last_error": "",
                        "last_poll_outcome": "idle_timeout",
                        "last_poll_at": observed_at.clone(),
                        "last_getupdates_at": observed_at,
                        "last_getupdates_buf": sync_buf,
                        "last_getupdates_error": "",
                        "last_getupdates_count": 0,
                        "last_private_text_message_count": 0,
                        "last_getupdates_message_ids": Vec::<Value>::new(),
                        "last_getupdates_private_message_ids": Vec::<Value>::new(),
                    }));
                    return Ok(vec![]);
                }
                self.update_transport(json_map!({
                    "status": poll_status,
                    "connected": false,
                    "last_error": error_text.clone(),
                    "last_poll_outcome": "error",
                    "last_poll_at": observed_at.clone(),
                    "last_getupdates_at": observed_at,
                    "last_getupdates_error": error_text,
                    "last_getupdates_count": 0,
                    "last_private_text_message_count": 0,
                    "last_getupdates_message_ids": Vec::<Value>::new(),
                    "last_getupdates_private_message_ids": Vec::<Value>::new(),
                }));
                return Err(error);
            }
        };
        let next_sync = response
            .get("get_updates_buf")
            .and_then(Value::as_str)
            .unwrap_or(&sync_buf)
            .to_string();
        save_sync_buf(&self.config.state_dir, &account.account_id, &next_sync)?;
        let messages: Vec<Value> = response
            .get("msgs")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
            .into_iter()
            .filter(Value::is_object)
            .collect();
        let private_messages: Vec<Value> = messages
            .iter()
            .filter(|item| {
                item.get("room_id")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim()
                    .is_empty()
            })
            .cloned()
            .collect();
        let message_ids: Vec<Value> = messages
            .iter()
            .filter_map(|item| {
                let message_id = extract_weixin_message_id(item);
                (!message_id.is_empty()).then(|| json!(message_id))
            })
            .collect();
        let private_message_ids: Vec<Value> = private_messages
            .iter()
            .filter_map(|item| {
                let message_id = extract_weixin_message_id(item);
                (!message_id.is_empty()).then(|| json!(message_id))
            })
            .collect();
        let now = utc_now_iso();
        let mut updates = json_map!({
            "status": "polling_idle",
            "connected": true,
            "last_error": "",
            "last_poll_outcome": if messages.is_empty() { "empty" } else { "messages" },
            "last_getupdates_at": now.clone(),
            "last_getupdates_buf": next_sync,
            "last_getupdates_count": messages.len(),
            "last_private_text_message_count": private_messages.len(),
            "last_getupdates_message_ids": message_ids,
            "last_getupdates_private_message_ids": private_message_ids,
            "last_getupdates_error": "",
        });
        if !private_messages.is_empty() {
            updates.insert("last_private_text_message_at".into(), json!(now));
        }
        self.update_transport(updates);
        Ok(messages)
    }

    pub fn is_duplicate_update(&self, payload: &Value) -> bool {
        let account = self.account();
        let message_id = extract_weixin_message_id(payload);
        if message_id.is_empty() || account.account_id.is_empty() {
            return false;
        }
        load_processed_messages(&self.config.state_dir, &account.account_id)
            .iter()
            .any(|item| item == &message_id)
    }

    pub fn mark_update_processed(&self, payload: &Value) -> Result<(), GatewayError> {
        let account = self.account();
        let message_id = extract_weixin_message_id(payload);
        if message_id.is_empty() || account.account_id.is_empty() {
            return Ok(());
        }
        let mut messages = load_processed_messages(&self.config.state_dir, &account.account_id);
        messages.retain(|item| item != &message_id);
        messages.push_back(message_id);
        while messages.len() > 500 {
            messages.pop_front();
        }
        save_processed_messages(&self.config.state_dir, &account.account_id, &messages)
    }

    fn update_transport(&self, updates: serde_json::Map<String, Value>) {
        let mut state = self.state.lock().expect("weixin state lock poisoned");
        for (key, value) in updates {
            state.transport.insert(key, value);
        }
        state.transport.insert("mode".into(), json!("polling"));
        if !state.account.account_id.is_empty() {
            let _ = save_weixin_transport_state(
                &self.config.state_dir,
                &state.account.account_id,
                &state.transport,
            );
        }
    }

    fn transport_status_value(&self) -> Value {
        self.refresh_account_from_disk();
        let state = self.state.lock().expect("weixin state lock poisoned");
        let mut transport = state.transport.clone();
        let account = state.account.clone();
        for key in ["last_error", "last_getupdates_error", "last_send_error"] {
            if let Some(value) = transport.get(key).and_then(Value::as_str) {
                transport.insert(
                    key.to_string(),
                    json!(redact_sensitive_text(
                        value,
                        &[&account.account_id, &account.token]
                    )),
                );
            }
        }
        if transport
            .get("last_getupdates_buf")
            .and_then(Value::as_str)
            .is_some_and(|value| !value.trim().is_empty())
        {
            transport.insert("last_getupdates_buf".into(), json!("[REDACTED]"));
        }
        transport.insert("configured".into(), json!(account.configured()));
        Value::Object(transport)
    }

    async fn send_text_chunks(
        &self,
        account: &WeixinAccount,
        chat_id: &str,
        context_token: &str,
        text: &str,
    ) -> Result<String, GatewayError> {
        let mut last_client_id = String::new();
        for chunk in split_text_for_weixin(text, MAX_TEXT_CHUNK_LENGTH) {
            let payload = build_send_message_payload(chat_id, &chunk, Some(context_token), None);
            last_client_id = payload
                .pointer("/msg/client_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            self.post_json(
                &account.base_url,
                EP_SEND_MESSAGE,
                payload,
                Some(&account.token),
                self.config.timeout_seconds,
            )
            .await?;
        }
        Ok(last_client_id)
    }

    async fn send_message_items(
        &self,
        account: &WeixinAccount,
        chat_id: &str,
        context_token: &str,
        item_list: Vec<Value>,
    ) -> Result<String, GatewayError> {
        let payload =
            build_send_message_payload_items(chat_id, item_list, Some(context_token), None);
        let client_id = payload
            .pointer("/msg/client_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        self.post_json(
            &account.base_url,
            EP_SEND_MESSAGE,
            payload,
            Some(&account.token),
            self.config.timeout_seconds,
        )
        .await?;
        Ok(client_id)
    }

    async fn upload_image(
        &self,
        account: &WeixinAccount,
        attachment: &NativeWeixinAttachment,
        to_user_id: &str,
    ) -> Result<WeixinUploadedImage, GatewayError> {
        let bytes = tokio::fs::read(&attachment.path).await.map_err(|err| {
            self.weixin_error(format!(
                "Could not read Weixin image artifact {}: {err}",
                attachment.path.display()
            ))
        })?;
        if bytes.is_empty() {
            return Err(self.weixin_error(format!(
                "Weixin image artifact is empty: {}",
                attachment.path.display()
            )));
        }
        let filekey = Uuid::new_v4().simple().to_string();
        let aeskey = *Uuid::new_v4().as_bytes();
        let payload = json!({
            "filekey": filekey,
            "media_type": UPLOAD_MEDIA_IMAGE,
            "to_user_id": to_user_id,
            "rawsize": bytes.len(),
            "rawfilemd5": format!("{:x}", md5::compute(&bytes)),
            "filesize": aes_ecb_padded_size(bytes.len()),
            "no_need_thumb": true,
            "aeskey": hex_lower(&aeskey),
        });
        let upload_response = self
            .post_json(
                &account.base_url,
                EP_GET_UPLOAD_URL,
                payload,
                Some(&account.token),
                self.config.timeout_seconds,
            )
            .await?;
        let download_param = self
            .upload_binary_to_cdn(
                &bytes,
                &upload_response,
                &filekey,
                &aeskey,
                "weixin-image-orig",
            )
            .await?;
        Ok(WeixinUploadedImage {
            original_download_param: download_param,
            aeskey_hex: hex_lower(&aeskey),
            original_ciphertext_size: aes_ecb_padded_size(bytes.len()),
        })
    }

    async fn upload_media(
        &self,
        account: &WeixinAccount,
        attachment: &NativeWeixinAttachment,
        media_type: i64,
        to_user_id: &str,
    ) -> Result<WeixinUploadedMedia, GatewayError> {
        let bytes = tokio::fs::read(&attachment.path).await.map_err(|err| {
            self.weixin_error(format!(
                "Could not read Weixin media artifact {}: {err}",
                attachment.path.display()
            ))
        })?;
        if bytes.is_empty() {
            return Err(self.weixin_error(format!(
                "Weixin media artifact is empty: {}",
                attachment.path.display()
            )));
        }
        let filekey = Uuid::new_v4().simple().to_string();
        let aeskey = *Uuid::new_v4().as_bytes();
        let payload = json!({
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": bytes.len(),
            "rawfilemd5": format!("{:x}", md5::compute(&bytes)),
            "filesize": aes_ecb_padded_size(bytes.len()),
            "no_need_thumb": true,
            "aeskey": hex_lower(&aeskey),
        });
        let upload_response = self
            .post_json(
                &account.base_url,
                EP_GET_UPLOAD_URL,
                payload,
                Some(&account.token),
                self.config.timeout_seconds,
            )
            .await?;
        let download_param = self
            .upload_binary_to_cdn(
                &bytes,
                &upload_response,
                &filekey,
                &aeskey,
                "weixin-media-orig",
            )
            .await?;
        Ok(WeixinUploadedMedia {
            download_param,
            aeskey_hex: hex_lower(&aeskey),
            plaintext_size: bytes.len(),
            ciphertext_size: aes_ecb_padded_size(bytes.len()),
        })
    }

    async fn upload_binary_to_cdn(
        &self,
        plaintext: &[u8],
        upload_response: &Value,
        filekey: &str,
        aeskey: &[u8; 16],
        label: &str,
    ) -> Result<String, GatewayError> {
        let ciphertext =
            Aes128EcbEnc::new(aeskey.into()).encrypt_padded_vec_mut::<Pkcs7>(plaintext);
        let upload_full_url = upload_response
            .get("upload_full_url")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        let upload_url = if upload_full_url.is_empty() {
            let upload_param = upload_response
                .get("upload_param")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            if upload_param.is_empty() {
                return Err(self.weixin_error(format!("{label}: CDN upload URL missing")));
            }
            format!(
                "{}/upload?encrypted_query_param={}&filekey={}",
                self.config.cdn_base_url.trim_end_matches('/'),
                urlencoding::encode(upload_param),
                urlencoding::encode(filekey)
            )
        } else {
            upload_full_url
        };
        let mut last_error = String::new();
        for _attempt in 1..=CDN_UPLOAD_MAX_RETRIES {
            match self
                .http
                .post(&upload_url)
                .timeout(Duration::from_secs(self.config.timeout_seconds))
                .header("Content-Type", "application/octet-stream")
                .body(ciphertext.clone())
                .send()
                .await
            {
                Ok(response) if response.status().is_success() => {
                    let headers = response.headers().clone();
                    let encrypted_param = headers
                        .get("x-encrypted-param")
                        .and_then(|value| value.to_str().ok())
                        .unwrap_or("")
                        .trim()
                        .to_string();
                    if encrypted_param.is_empty() {
                        last_error =
                            "CDN upload response missing x-encrypted-param header".to_string();
                    } else {
                        return Ok(encrypted_param);
                    }
                }
                Ok(response) => {
                    let status = response.status();
                    let text = response.text().await.unwrap_or_default();
                    last_error = format!("{label}: CDN HTTP {status}: {text}");
                    if status.is_client_error() {
                        break;
                    }
                }
                Err(error) => {
                    last_error = format!("{label}: CDN request failed: {error}");
                }
            }
        }
        Err(self.weixin_error(last_error))
    }

    async fn get_json(
        &self,
        base_url: &str,
        endpoint: &str,
        token: Option<&str>,
        timeout_seconds: u64,
    ) -> Result<Value, GatewayError> {
        let response = self
            .http
            .get(join_endpoint(base_url, endpoint))
            .timeout(Duration::from_secs(timeout_seconds))
            .headers(weixin_headers(token, None))
            .send()
            .await
            .map_err(|err| self.weixin_error(format!("Weixin GET {endpoint} failed: {err}")))?;
        decode_json_response(response, || format!("Weixin GET {endpoint} failed")).await
    }

    async fn post_json(
        &self,
        base_url: &str,
        endpoint: &str,
        payload: Value,
        token: Option<&str>,
        timeout_seconds: u64,
    ) -> Result<Value, GatewayError> {
        let body = serde_json::to_vec(&payload)
            .map_err(|err| GatewayError::infrastructure(format!("JSON encode failed: {err}")))?;
        let response = self
            .http
            .post(join_endpoint(base_url, endpoint))
            .timeout(Duration::from_secs(timeout_seconds))
            .headers(weixin_headers(token, Some(body.len())))
            .body(body)
            .send()
            .await
            .map_err(|err| self.weixin_error(format!("Weixin POST {endpoint} failed: {err}")))?;
        decode_json_response(response, || format!("Weixin POST {endpoint} failed")).await
    }

    fn weixin_error(&self, message: impl Into<String>) -> GatewayError {
        let account = self
            .state
            .lock()
            .expect("weixin state lock poisoned")
            .account
            .clone();
        GatewayError::new(
            StatusCode::BAD_GATEWAY,
            "PLATFORM_UNAVAILABLE",
            redact_sensitive_text(&message.into(), &[&account.account_id, &account.token]),
        )
    }
}

impl WeixinAccount {
    pub fn configured(&self) -> bool {
        !self.account_id.trim().is_empty() && !self.token.trim().is_empty()
    }
}

#[async_trait]
impl PlatformAdapter for WeixinAdapter {
    fn name(&self) -> &str {
        "weixin"
    }

    fn normalize_inbound(&self, payload: Value) -> Result<InboundMessage, GatewayError> {
        let sender_id = payload
            .get("from_user_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        let room_id = payload
            .get("room_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        let chat_id = if room_id.is_empty() {
            sender_id.clone()
        } else {
            room_id.clone()
        };
        let text = extract_text_from_item_list(payload.get("item_list").and_then(Value::as_array));
        let message_id = extract_weixin_message_id(&payload);
        let context_token = payload
            .get("context_token")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        let route_key = payload
            .get("route_key")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();

        if !room_id.is_empty() {
            return Err(GatewayError::validation(
                "Weixin group chats are not supported yet",
            ));
        }
        if sender_id.is_empty() {
            return Err(GatewayError::validation(
                "Weixin payload must include from_user_id",
            ));
        }
        if text.is_empty() {
            return Err(GatewayError::validation(
                "Weixin payload does not contain a text message",
            ));
        }

        let observed_at = utc_now_iso();
        self.update_transport(json_map!({
            "connected": true,
            "last_inbound_at": observed_at.clone(),
            "last_inbound_message_id": message_id.clone(),
            "last_inbound_chat_id": chat_id.clone(),
            "last_private_text_message_at": observed_at.clone(),
        }));
        if !context_token.is_empty() {
            let account = self.account();
            if !account.account_id.is_empty() {
                let _ = save_context_token(
                    &self.config.state_dir,
                    &account.account_id,
                    &chat_id,
                    &context_token,
                );
                self.update_transport(json_map!({"last_context_token_at": observed_at}));
            }
        }

        Ok(InboundMessage {
            platform: "weixin".to_string(),
            chat_id,
            user_id: sender_id,
            text,
            message_id,
            chat_type: "p2p".to_string(),
            route_key,
            session_id: String::new(),
            mentions: vec![],
            attachments: vec![],
            metadata: serde_json::Map::new(),
            timestamp: utc_now_iso(),
            raw_payload: payload,
        })
    }

    async fn send_outbound(&self, outbound: OutboundMessage) -> Result<Value, GatewayError> {
        self.refresh_account_from_disk();
        let account = self.account();
        if !account.configured() {
            return Err(GatewayError::validation(
                "Weixin adapter is not configured. Open Weixin setup first.",
            ));
        }
        let context_token = load_context_tokens(&self.config.state_dir, &account.account_id)
            .get(&outbound.chat_id)
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        if context_token.is_empty() {
            return Err(GatewayError::validation(format!(
                "No Weixin context_token cached for chat_id={}. Send a DM from WeChat first.",
                outbound.chat_id
            )));
        }

        let native_attachments = if should_send_native_attachment_reply(&outbound) {
            resolve_native_media_attachments(&outbound)?
        } else {
            vec![]
        };
        let has_native_attachments = !native_attachments.is_empty();
        let native_caption = outbound.text.trim().to_string();
        let chunks = if has_native_attachments {
            vec![]
        } else {
            split_text_for_weixin(&outbound.text, MAX_TEXT_CHUNK_LENGTH)
        };
        if !has_native_attachments && chunks.is_empty() {
            return Err(GatewayError::validation("Outbound Weixin message is empty"));
        }
        let send_unit_count = if has_native_attachments {
            native_attachments.len() + usize::from(!native_caption.is_empty())
        } else {
            chunks.len()
        };
        let delivered_attachment_kind = if has_native_attachments {
            if native_attachments
                .iter()
                .all(|attachment| attachment.delivery_kind == "image")
            {
                "image".to_string()
            } else {
                native_attachments[0].delivery_kind.clone()
            }
        } else {
            String::new()
        };
        let native_attachment_count = if has_native_attachments {
            native_attachments.len()
        } else {
            outbound.attachments.len()
        };

        self.update_transport(json_map!({
            "status": "sending",
            "connected": true,
            "last_error": "",
            "last_send_at": utc_now_iso(),
            "last_send_chunk_count": send_unit_count,
            "last_inbound_chat_id": outbound.chat_id.clone(),
            "last_send_status": "sending",
            "last_send_error": "",
            "last_send_retryable": false,
            "last_send_provider_message_id": "",
            "last_send_context_token_used": true,
            "last_send_attachment_count": native_attachment_count,
            "last_send_content_kind": if has_native_attachments { format!("text+{delivered_attachment_kind}") } else { "text".to_string() },
        }));

        let mut last_client_id = String::new();
        let mut attachment_fallback_used = false;
        let send_result = async {
            if has_native_attachments
                && native_attachments
                    .iter()
                    .all(|attachment| attachment.delivery_kind == "image")
            {
                let mut uploaded_images = Vec::new();
                for attachment in &native_attachments {
                    uploaded_images.push(
                        self.upload_image(&account, attachment, &outbound.chat_id)
                            .await?,
                    );
                }
                if !native_caption.is_empty() {
                    last_client_id = self
                        .send_message_items(
                            &account,
                            &outbound.chat_id,
                            &context_token,
                            vec![build_text_message_item(&native_caption)],
                        )
                        .await?;
                }
                for uploaded in uploaded_images {
                    last_client_id = self
                        .send_message_items(
                            &account,
                            &outbound.chat_id,
                            &context_token,
                            vec![build_native_image_message_item(&uploaded)],
                        )
                        .await?;
                }
            } else if let Some(attachment) = native_attachments.first() {
                let media_type = if attachment.delivery_kind == "video" {
                    UPLOAD_MEDIA_VIDEO
                } else {
                    UPLOAD_MEDIA_FILE
                };
                let uploaded = self
                    .upload_media(&account, attachment, media_type, &outbound.chat_id)
                    .await?;
                if !native_caption.is_empty() {
                    last_client_id = self
                        .send_message_items(
                            &account,
                            &outbound.chat_id,
                            &context_token,
                            vec![build_text_message_item(&native_caption)],
                        )
                        .await?;
                }
                let item = if attachment.delivery_kind == "video" {
                    build_native_video_message_item(&uploaded)
                } else {
                    build_native_file_message_item(&uploaded, &attachment.file_name)
                };
                if attachment.delivery_kind == "file" {
                    attachment_fallback_used = true;
                }
                last_client_id = self
                    .send_message_items(&account, &outbound.chat_id, &context_token, vec![item])
                    .await?;
            } else {
                last_client_id = self
                    .send_text_chunks(&account, &outbound.chat_id, &context_token, &outbound.text)
                    .await?;
            }
            Ok::<(), GatewayError>(())
        }
        .await;

        if let Err(error) = send_result {
            let error_text = redact_sensitive_text(
                &error.message,
                &[&account.account_id, &account.token, &context_token],
            );
            self.update_transport(json_map!({
                "status": "send_failed",
                "connected": true,
                "last_send_at": utc_now_iso(),
                "last_error": error_text.clone(),
                "last_send_status": "failed",
                "last_send_error": error_text,
                "last_send_retryable": !has_native_attachments,
                "last_send_provider_message_id": last_client_id.clone(),
                "last_send_context_token_used": true,
                "last_send_attachment_count": native_attachment_count,
                "last_send_content_kind": if has_native_attachments { format!("text+{delivered_attachment_kind}") } else { "text".to_string() },
            }));
            return Err(error);
        }

        self.update_transport(json_map!({
            "status": "polling_idle",
            "connected": true,
            "last_send_at": utc_now_iso(),
            "last_error": "",
            "last_send_status": "sent",
            "last_send_error": "",
            "last_send_retryable": false,
            "last_send_provider_message_id": last_client_id.clone(),
            "last_send_context_token_used": true,
            "last_send_attachment_count": native_attachment_count,
            "last_send_content_kind": if has_native_attachments { format!("text+{delivered_attachment_kind}") } else { "text".to_string() },
        }));

        let response_attachments: Vec<Value> = if has_native_attachments {
            outbound
                .attachments
                .iter()
                .take(native_attachment_count)
                .cloned()
                .collect()
        } else {
            outbound.attachments.clone()
        };
        Ok(json!({
            "platform": "weixin",
            "chat_id": outbound.chat_id,
            "text": outbound.text,
            "timestamp": outbound.timestamp,
            "delivery": "weixin",
            "sent": true,
            "message_id": last_client_id,
            "provider_message_id": last_client_id,
            "attachments": response_attachments,
            "metadata": {
                "context_token_used": true,
                "chunk_count": send_unit_count,
                "attachment_count": native_attachment_count,
                "native_image_reply": has_native_attachments && delivered_attachment_kind == "image",
                "native_attachment_count": native_attachments.len(),
                "native_attachment_kind": delivered_attachment_kind,
                "native_attachment_fallback": attachment_fallback_used,
            },
        }))
    }

    fn profile(&self) -> Value {
        json!({
            "adapter_name": "weixin",
            "surface_family": "weixin",
            "transport_mode": "polling",
            "supports_mentions": false,
            "supports_attachments": true,
            "supports_replies": true,
            "supports_updates": false,
            "supports_live_receive": true,
        })
    }

    fn status(&self) -> Value {
        self.transport_status_value()
    }
}

fn default_transport_state(configured: bool) -> serde_json::Map<String, Value> {
    json_map!({
        "mode": "polling",
        "status": if configured { "polling_idle" } else { "waiting_for_credentials" },
        "connected": configured,
        "last_error": "",
        "last_poll_outcome": if configured { "never_polled" } else { "waiting_for_credentials" },
        "last_poll_at": "",
        "last_getupdates_at": "",
        "last_getupdates_buf": "",
        "last_getupdates_count": 0,
        "last_private_text_message_count": 0,
        "last_private_text_message_at": "",
        "last_getupdates_message_ids": Vec::<Value>::new(),
        "last_getupdates_private_message_ids": Vec::<Value>::new(),
        "last_getupdates_error": "",
        "last_context_token_at": "",
        "last_send_at": "",
        "last_send_chunk_count": 0,
        "last_send_status": "",
        "last_send_error": "",
        "last_send_retryable": false,
        "last_send_provider_message_id": "",
        "last_send_context_token_used": false,
        "last_send_attachment_count": 0,
        "last_send_content_kind": "",
        "last_inbound_at": "",
        "last_inbound_message_id": "",
        "last_inbound_chat_id": "",
    })
}

fn account_dir(state_dir: &Path) -> PathBuf {
    state_dir.join("accounts")
}

fn account_file(state_dir: &Path, account_id: &str) -> PathBuf {
    account_dir(state_dir).join(format!("{}.json", safe_slug(account_id)))
}

fn sync_file(state_dir: &Path, account_id: &str) -> PathBuf {
    account_dir(state_dir).join(format!("{}.sync.json", safe_slug(account_id)))
}

fn context_file(state_dir: &Path, account_id: &str) -> PathBuf {
    account_dir(state_dir).join(format!("{}.context_tokens.json", safe_slug(account_id)))
}

fn processed_file(state_dir: &Path, account_id: &str) -> PathBuf {
    account_dir(state_dir).join(format!("{}.processed_messages.json", safe_slug(account_id)))
}

fn transport_state_file(state_dir: &Path, account_id: &str) -> PathBuf {
    account_dir(state_dir).join(format!("{}.runtime.json", safe_slug(account_id)))
}

pub fn safe_slug(value: &str) -> String {
    let text = value.trim();
    let text = if text.is_empty() { "default" } else { text };
    text.chars()
        .map(|ch| {
            if ch.is_alphanumeric() || matches!(ch, '.' | '_' | '-') {
                ch
            } else {
                '_'
            }
        })
        .collect()
}

pub fn save_weixin_account(state_dir: &Path, account: &WeixinAccount) -> Result<(), GatewayError> {
    fs::create_dir_all(account_dir(state_dir)).map_err(|err| {
        GatewayError::infrastructure(format!("create weixin state dir failed: {err}"))
    })?;
    fs::write(
        account_file(state_dir, &account.account_id),
        serde_json::to_string_pretty(&json!({
            "account_id": account.account_id,
            "token": account.token,
            "base_url": account.base_url,
            "user_id": account.user_id,
            "saved_at": utc_now_iso(),
        }))?,
    )
    .map_err(|err| GatewayError::infrastructure(format!("save weixin account failed: {err}")))
}

pub fn discover_weixin_account(state_dir: &Path, account_id: &str) -> Option<WeixinAccount> {
    if !account_id.trim().is_empty() {
        return load_weixin_account(state_dir, account_id);
    }
    let dir = account_dir(state_dir);
    let entries = fs::read_dir(dir).ok()?;
    let mut candidates = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        let name = path
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("");
        if !name.ends_with(".json")
            || name.ends_with(".sync.json")
            || name.ends_with(".context_tokens.json")
            || name.ends_with(".processed_messages.json")
            || name.ends_with(".runtime.json")
        {
            continue;
        }
        let Some(account) = load_weixin_account_path(&path) else {
            continue;
        };
        if !account.configured() {
            continue;
        }
        let modified = entry
            .metadata()
            .and_then(|metadata| metadata.modified())
            .ok();
        candidates.push((modified, name.to_string(), account));
    }
    candidates.sort_by(|left, right| left.0.cmp(&right.0).then(left.1.cmp(&right.1)));
    candidates.pop().map(|item| item.2)
}

fn load_weixin_account(state_dir: &Path, account_id: &str) -> Option<WeixinAccount> {
    load_weixin_account_path(&account_file(state_dir, account_id))
}

fn load_weixin_account_path(path: &Path) -> Option<WeixinAccount> {
    let payload: Value = serde_json::from_str(&fs::read_to_string(path).ok()?).ok()?;
    Some(WeixinAccount {
        account_id: payload.get("account_id")?.as_str()?.trim().to_string(),
        token: payload.get("token")?.as_str()?.trim().to_string(),
        base_url: payload
            .get("base_url")
            .and_then(Value::as_str)
            .unwrap_or(WeixinConfig::ILINK_BASE_URL)
            .trim()
            .to_string(),
        user_id: payload
            .get("user_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string(),
    })
}

fn clear_weixin_account_state(state_dir: &Path, account_id: &str) -> Vec<String> {
    let mut deleted = Vec::new();
    for path in [
        account_file(state_dir, account_id),
        sync_file(state_dir, account_id),
        context_file(state_dir, account_id),
        processed_file(state_dir, account_id),
        transport_state_file(state_dir, account_id),
    ] {
        if fs::remove_file(&path).is_ok() {
            if let Some(name) = path.file_name().and_then(|name| name.to_str()) {
                deleted.push(name.to_string());
            }
        }
    }
    deleted
}

fn load_sync_buf(state_dir: &Path, account_id: &str) -> String {
    let path = sync_file(state_dir, account_id);
    serde_json::from_str::<Value>(&fs::read_to_string(path).unwrap_or_default())
        .ok()
        .and_then(|value| {
            value
                .get("get_updates_buf")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .unwrap_or_default()
}

fn save_sync_buf(state_dir: &Path, account_id: &str, sync_buf: &str) -> Result<(), GatewayError> {
    fs::create_dir_all(account_dir(state_dir))?;
    fs::write(
        sync_file(state_dir, account_id),
        serde_json::to_string_pretty(&json!({"get_updates_buf": sync_buf}))?,
    )?;
    Ok(())
}

fn load_context_tokens(state_dir: &Path, account_id: &str) -> serde_json::Map<String, Value> {
    let path = context_file(state_dir, account_id);
    serde_json::from_str::<Value>(&fs::read_to_string(path).unwrap_or_default())
        .ok()
        .and_then(|value| value.as_object().cloned())
        .unwrap_or_default()
}

fn save_context_token(
    state_dir: &Path,
    account_id: &str,
    chat_id: &str,
    context_token: &str,
) -> Result<(), GatewayError> {
    fs::create_dir_all(account_dir(state_dir))?;
    let mut tokens = load_context_tokens(state_dir, account_id);
    tokens.insert(chat_id.to_string(), json!(context_token));
    fs::write(
        context_file(state_dir, account_id),
        serde_json::to_string_pretty(&Value::Object(tokens))?,
    )?;
    Ok(())
}

fn load_processed_messages(state_dir: &Path, account_id: &str) -> VecDeque<String> {
    let path = processed_file(state_dir, account_id);
    let payload: Value = serde_json::from_str(&fs::read_to_string(path).unwrap_or_default())
        .unwrap_or_else(|_| json!([]));
    let values = payload
        .as_array()
        .cloned()
        .or_else(|| {
            payload
                .get("message_ids")
                .and_then(Value::as_array)
                .cloned()
        })
        .unwrap_or_default();
    values
        .into_iter()
        .filter_map(|value| value.as_str().map(str::to_string))
        .collect()
}

fn save_processed_messages(
    state_dir: &Path,
    account_id: &str,
    messages: &VecDeque<String>,
) -> Result<(), GatewayError> {
    fs::create_dir_all(account_dir(state_dir))?;
    fs::write(
        processed_file(state_dir, account_id),
        serde_json::to_string_pretty(&messages.iter().collect::<Vec<_>>())?,
    )?;
    Ok(())
}

fn load_weixin_transport_state(
    state_dir: &Path,
    account_id: &str,
) -> Option<serde_json::Map<String, Value>> {
    serde_json::from_str::<Value>(
        &fs::read_to_string(transport_state_file(state_dir, account_id)).ok()?,
    )
    .ok()?
    .as_object()
    .cloned()
}

fn save_weixin_transport_state(
    state_dir: &Path,
    account_id: &str,
    payload: &serde_json::Map<String, Value>,
) -> Result<(), GatewayError> {
    fs::create_dir_all(account_dir(state_dir))?;
    fs::write(
        transport_state_file(state_dir, account_id),
        serde_json::to_string_pretty(&Value::Object(payload.clone()))?,
    )?;
    Ok(())
}

pub fn split_text_for_weixin(text: &str, max_length: usize) -> Vec<String> {
    let content = text.trim();
    if content.is_empty() {
        return vec![];
    }
    if content.chars().count() <= max_length {
        return vec![content.to_string()];
    }
    let mut chunks = Vec::new();
    let mut current = String::new();
    for line in content.split_inclusive('\n') {
        if current.chars().count() + line.chars().count() <= max_length {
            current.push_str(line);
            continue;
        }
        if !current.trim().is_empty() {
            chunks.push(current.trim_end().to_string());
            current.clear();
        }
        let mut segment = String::new();
        for ch in line.chars() {
            if segment.chars().count() >= max_length {
                chunks.push(segment.trim_end().to_string());
                segment.clear();
            }
            segment.push(ch);
        }
        current = segment;
    }
    if !current.trim().is_empty() {
        chunks.push(current.trim_end().to_string());
    }
    if chunks.is_empty() {
        vec![content.chars().take(max_length).collect()]
    } else {
        chunks
    }
}

pub fn extract_weixin_message_id(payload: &Value) -> String {
    payload
        .get("msg_id")
        .or_else(|| payload.get("client_id"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string()
}

fn extract_text_from_item_list(item_list: Option<&Vec<Value>>) -> String {
    let Some(items) = item_list else {
        return String::new();
    };
    items
        .iter()
        .filter(|item| item.get("type").and_then(Value::as_i64).unwrap_or(0) == ITEM_TEXT)
        .filter_map(|item| {
            item.pointer("/text_item/text")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|text| !text.is_empty())
                .map(str::to_string)
        })
        .collect::<Vec<_>>()
        .join("\n")
}

pub fn build_send_message_payload(
    to_user_id: &str,
    text: &str,
    context_token: Option<&str>,
    client_id: Option<&str>,
) -> Value {
    let item_list = if text.is_empty() {
        vec![]
    } else {
        vec![build_text_message_item(text)]
    };
    build_send_message_payload_items(to_user_id, item_list, context_token, client_id)
}

fn build_send_message_payload_items(
    to_user_id: &str,
    item_list: Vec<Value>,
    context_token: Option<&str>,
    client_id: Option<&str>,
) -> Value {
    let mut msg = serde_json::Map::new();
    msg.insert("from_user_id".into(), json!(""));
    msg.insert("to_user_id".into(), json!(to_user_id));
    msg.insert(
        "client_id".into(),
        json!(client_id
            .map(str::to_string)
            .unwrap_or_else(|| format!("harborgate-{}", Uuid::new_v4().simple()))),
    );
    msg.insert("message_type".into(), json!(MSG_TYPE_BOT));
    msg.insert("message_state".into(), json!(MSG_STATE_FINISH));
    if !item_list.is_empty() {
        msg.insert("item_list".into(), Value::Array(item_list));
    }
    if let Some(context_token) = context_token.filter(|value| !value.trim().is_empty()) {
        msg.insert("context_token".into(), json!(context_token));
    }
    json!({"msg": msg})
}

fn build_text_message_item(text: &str) -> Value {
    json!({"type": ITEM_TEXT, "text_item": {"text": text}})
}

fn build_native_image_message_item(uploaded: &WeixinUploadedImage) -> Value {
    let aes_key = base64::engine::general_purpose::STANDARD.encode(uploaded.aeskey_hex.as_bytes());
    json!({
        "type": ITEM_IMAGE,
        "image_item": {
            "media": {
                "encrypt_query_param": uploaded.original_download_param,
                "aes_key": aes_key,
                "encrypt_type": WEIXIN_MEDIA_ENCRYPT_TYPE,
            },
            "mid_size": uploaded.original_ciphertext_size,
        }
    })
}

fn build_cdn_media_reference(download_param: &str, aeskey_hex: &str) -> Value {
    let aes_key = base64::engine::general_purpose::STANDARD.encode(aeskey_hex.as_bytes());
    json!({
        "encrypt_query_param": download_param,
        "aes_key": aes_key,
        "encrypt_type": WEIXIN_MEDIA_ENCRYPT_TYPE,
    })
}

fn build_native_video_message_item(uploaded: &WeixinUploadedMedia) -> Value {
    json!({
        "type": ITEM_VIDEO,
        "video_item": {
            "media": build_cdn_media_reference(&uploaded.download_param, &uploaded.aeskey_hex),
            "video_size": uploaded.ciphertext_size,
        }
    })
}

fn build_native_file_message_item(uploaded: &WeixinUploadedMedia, file_name: &str) -> Value {
    json!({
        "type": ITEM_FILE,
        "file_item": {
            "media": build_cdn_media_reference(&uploaded.download_param, &uploaded.aeskey_hex),
            "file_name": file_name,
            "len": uploaded.plaintext_size.to_string(),
        }
    })
}

fn should_send_native_attachment_reply(outbound: &OutboundMessage) -> bool {
    outbound
        .metadata
        .get("source")
        .and_then(Value::as_str)
        .unwrap_or("")
        == "harborbeacon"
        && !outbound.attachments.is_empty()
}

fn resolve_native_media_attachments(
    outbound: &OutboundMessage,
) -> Result<Vec<NativeWeixinAttachment>, GatewayError> {
    let mut resolved = Vec::new();
    for attachment in outbound
        .attachments
        .iter()
        .filter(|value| value.is_object())
    {
        resolved.push(native_media_attachment_from_value(attachment)?);
    }
    if resolved.is_empty() {
        return Err(GatewayError::validation(
            "Weixin native media reply requires at least one attachment",
        ));
    }
    if resolved.len() > 1
        && resolved
            .iter()
            .any(|attachment| attachment.delivery_kind != "image")
    {
        return Err(GatewayError::validation(
            "Weixin multi-attachment native reply only supports images",
        ));
    }
    Ok(resolved.into_iter().take(3).collect())
}

fn native_media_attachment_from_value(
    value: &Value,
) -> Result<NativeWeixinAttachment, GatewayError> {
    let object = value
        .as_object()
        .ok_or_else(|| GatewayError::validation("Weixin attachment must be an object"))?;
    let kind = string_field(object, "kind")
        .or_else(|| string_field(object, "type"))
        .unwrap_or_default()
        .to_lowercase();
    let mime_type = string_field(object, "mime_type")
        .unwrap_or_default()
        .to_lowercase();
    let raw_path = string_field(object, "path").unwrap_or_default();
    let path = resolve_local_attachment_path(&raw_path).ok_or_else(|| {
        GatewayError::validation(
            "Weixin native media reply requires a readable same-host attachment path",
        )
    })?;
    let delivery_kind = if kind == "image" || mime_type.starts_with("image/") {
        "image"
    } else if kind == "video" || mime_type.starts_with("video/") {
        "video"
    } else {
        "file"
    }
    .to_string();
    let file_name = object
        .get("metadata")
        .and_then(Value::as_object)
        .and_then(|metadata| metadata.get("file_name"))
        .and_then(Value::as_str)
        .or_else(|| object.get("label").and_then(Value::as_str))
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
        .unwrap_or_else(|| {
            path.file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("attachment")
                .to_string()
        });
    Ok(NativeWeixinAttachment {
        delivery_kind,
        path,
        file_name,
    })
}

fn resolve_local_attachment_path(raw_path: &str) -> Option<PathBuf> {
    let normalized = raw_path.trim();
    if normalized.is_empty() {
        return None;
    }
    let candidate = PathBuf::from(normalized);
    if candidate.is_file() {
        return Some(candidate);
    }
    if candidate.is_absolute() {
        return None;
    }
    let mut roots = Vec::new();
    for env_name in [
        "HARBOR_CAPTURE_ROOT",
        "HARBOR_HARBOROS_WRITABLE_ROOT",
        "HARBOR_RELEASE_INSTALL_ROOT",
        "WORKSPACE_ROOT",
    ] {
        if let Ok(root) = std::env::var(env_name) {
            if !root.trim().is_empty() {
                roots.push(PathBuf::from(root));
            }
        }
    }
    roots.push(std::env::current_dir().ok()?);
    roots
        .into_iter()
        .map(|root| root.join(normalized))
        .find(|path| path.is_file())
}

fn aes_ecb_padded_size(plaintext_size: usize) -> usize {
    ((plaintext_size / 16) + 1) * 16
}

fn hex_lower(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn poll_status_for_error(error_text: &str) -> &'static str {
    let normalized = error_text.to_lowercase();
    if normalized.contains("read operation timed out") {
        "idle_timeout"
    } else if normalized.contains("timed out") || normalized.contains("timeout") {
        "timeout"
    } else {
        "error"
    }
}

pub fn is_weixin_dns_resolution_error(error_text: &str) -> bool {
    let normalized = error_text.to_lowercase();
    [
        "getaddrinfo",
        "name resolution",
        "temporary failure in name resolution",
        "name or service not known",
        "nameresolutionerror",
        "socket.gaierror",
    ]
    .iter()
    .any(|marker| normalized.contains(marker))
}

pub fn is_weixin_provider_auth_error(error_text: &str) -> bool {
    let normalized = error_text.to_lowercase();
    ["401", "403", "auth", "token", "forbidden"]
        .iter()
        .any(|marker| normalized.contains(marker))
}

fn redact_sensitive_text(value: &str, secrets: &[&str]) -> String {
    let mut redacted = value.to_string();
    for secret in secrets {
        let secret = secret.trim();
        if !secret.is_empty() {
            redacted = redacted.replace(secret, "[REDACTED]");
        }
    }
    redacted = redacted.replace("Bearer ", "Bearer [REDACTED] ");
    redacted
}

fn mask_secret(value: &str) -> String {
    let text = value.trim();
    if text.len() <= 6 {
        "*".repeat(text.len())
    } else {
        format!("{}***{}", &text[..4], &text[text.len() - 2..])
    }
}

fn join_endpoint(base_url: &str, endpoint: &str) -> String {
    format!(
        "{}/{}",
        base_url.trim_end_matches('/'),
        endpoint.trim_start_matches('/')
    )
}

fn weixin_headers(token: Option<&str>, body_len: Option<usize>) -> HeaderMap {
    let mut headers = HeaderMap::new();
    headers.insert("Content-Type", "application/json".parse().unwrap());
    headers.insert("AuthorizationType", "ilink_bot_token".parse().unwrap());
    headers.insert("iLink-App-Id", ILINK_APP_ID.parse().unwrap());
    headers.insert(
        "iLink-App-ClientVersion",
        ILINK_APP_CLIENT_VERSION.parse().unwrap(),
    );
    headers.insert("iLink-Channel-Version", CHANNEL_VERSION.parse().unwrap());
    if let Some(token) = token.filter(|value| !value.trim().is_empty()) {
        headers.insert("Authorization", format!("Bearer {token}").parse().unwrap());
    }
    if let Some(body_len) = body_len {
        headers.insert("Content-Length", body_len.to_string().parse().unwrap());
    }
    headers
}

async fn decode_json_response(
    response: reqwest::Response,
    context: impl FnOnce() -> String,
) -> Result<Value, GatewayError> {
    let status = response.status();
    let raw = response.text().await.unwrap_or_default();
    if !status.is_success() {
        return Err(GatewayError::new(
            StatusCode::BAD_GATEWAY,
            "PLATFORM_UNAVAILABLE",
            format!("{} with HTTP {status}: {raw}", context()),
        ));
    }
    if raw.trim().is_empty() {
        return Ok(json!({}));
    }
    serde_json::from_str(&raw).map_err(|err| {
        GatewayError::new(
            StatusCode::BAD_GATEWAY,
            "PLATFORM_UNAVAILABLE",
            format!("{} returned invalid JSON: {err}", context()),
        )
    })
}

fn string_field(object: &serde_json::Map<String, Value>, key: &str) -> Option<String> {
    object
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn split_text_preserves_content() {
        let content = format!("{}\n{}", "A".repeat(950), "B".repeat(200));
        let chunks = split_text_for_weixin(&content, 500);
        assert!(chunks.len() > 1);
        assert_eq!(chunks.join("").replace('\n', ""), content.replace('\n', ""));
    }

    #[test]
    fn account_state_uses_python_compatible_names() {
        let dir = tempdir().unwrap();
        let account = WeixinAccount {
            account_id: "d5ba3cf20a24@im.bot".into(),
            token: "secret".into(),
            base_url: "https://example.com".into(),
            user_id: "user".into(),
        };
        save_weixin_account(dir.path(), &account).unwrap();
        save_context_token(dir.path(), &account.account_id, "wx-user", "ctx").unwrap();
        let restored = discover_weixin_account(dir.path(), "").unwrap();
        assert_eq!(restored.account_id, account.account_id);
        assert!(dir
            .path()
            .join("accounts/d5ba3cf20a24_im.bot.context_tokens.json")
            .exists());
    }

    #[test]
    fn configured_account_projects_connected_polling_state() {
        let dir = tempdir().unwrap();
        let account = WeixinAccount {
            account_id: "d5ba3cf20a24@im.bot".into(),
            token: "secret".into(),
            base_url: "https://example.com".into(),
            user_id: "user".into(),
        };
        save_weixin_account(dir.path(), &account).unwrap();
        let adapter = WeixinAdapter::new(WeixinConfig {
            state_dir: dir.path().to_path_buf(),
            account_id: String::new(),
            token: String::new(),
            base_url: "https://example.com".into(),
            user_id: String::new(),
            cdn_base_url: WeixinConfig::DEFAULT_CDN_BASE_URL.into(),
            timeout_seconds: 45,
            poll_timeout_ms: 35000,
        });

        let status = adapter.status();
        assert_eq!(status["configured"], true);
        assert_eq!(status["connected"], true);
        assert_eq!(status["status"], "polling_idle");
    }

    #[test]
    fn normalize_inbound_stores_context_token() {
        let dir = tempdir().unwrap();
        let config = WeixinConfig {
            state_dir: dir.path().to_path_buf(),
            account_id: "bot-1".into(),
            token: "secret".into(),
            base_url: "https://example.com".into(),
            user_id: "self".into(),
            cdn_base_url: WeixinConfig::DEFAULT_CDN_BASE_URL.into(),
            timeout_seconds: 45,
            poll_timeout_ms: 35000,
        };
        let adapter = WeixinAdapter::new(config);
        let inbound = adapter
            .normalize_inbound(json!({
                "from_user_id": "wx-user-1",
                "context_token": "ctx-001",
                "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
            }))
            .unwrap();
        assert_eq!(inbound.chat_id, "wx-user-1");
        assert_eq!(inbound.text, "你好");
        assert_eq!(
            load_context_tokens(dir.path(), "bot-1")
                .get("wx-user-1")
                .and_then(Value::as_str),
            Some("ctx-001")
        );
    }

    #[test]
    fn build_send_payload_includes_context_token() {
        let payload = build_send_message_payload("wx-user", "hi", Some("ctx"), Some("client-1"));
        assert_eq!(payload["msg"]["to_user_id"], "wx-user");
        assert_eq!(payload["msg"]["client_id"], "client-1");
        assert_eq!(payload["msg"]["context_token"], "ctx");
    }
}

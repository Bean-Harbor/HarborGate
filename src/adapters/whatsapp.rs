//! Meta WhatsApp Business Platform transport. No household/device authority is
//! inferred from phone numbers: Beacon still resolves the authenticated route.
use super::{PlatformAdapter, PreparedOutbound};
#[cfg(test)]
use crate::models::utc_now_iso;
use crate::{
    error::GatewayError,
    models::{InboundMessage, OutboundMessage},
};
use async_trait::async_trait;
use axum::http::StatusCode;
use hmac::{Hmac, Mac};
use reqwest::{
    multipart::{Form, Part},
    Client,
};
use serde_json::{json, Value};
use sha2::Digest;
use sha2::Sha256;
use std::{
    path::{Path, PathBuf},
    time::Duration,
};

#[derive(Clone, Default)]
pub struct WhatsAppConfig {
    pub phone_number_id: String,
    pub business_number: String,
    pub app_secret: String,
    pub verify_token: String,
    pub access_token: String,
    pub graph_version: String,
}
impl WhatsAppConfig {
    pub fn from_env() -> Self {
        let get = |name| std::env::var(name).unwrap_or_default();
        Self {
            phone_number_id: get("WHATSAPP_PHONE_NUMBER_ID"),
            business_number: get("WHATSAPP_BUSINESS_NUMBER"),
            app_secret: get("WHATSAPP_APP_SECRET"),
            verify_token: get("WHATSAPP_VERIFY_TOKEN"),
            access_token: get("WHATSAPP_ACCESS_TOKEN"),
            graph_version: get("WHATSAPP_GRAPH_VERSION"),
        }
    }
    pub fn configured(&self) -> bool {
        digits(&self.phone_number_id)
            && digits(&self.business_number)
            && self.app_secret.len() >= 16
            && self.verify_token.len() >= 32
            && !self.access_token.is_empty()
            && valid_version(&self.graph_version)
    }
}

pub struct WhatsAppAdapter {
    config: WhatsAppConfig,
    http: Client,
    cache: PathBuf,
}
impl WhatsAppAdapter {
    pub fn new(config: WhatsAppConfig, cache: PathBuf) -> Self {
        Self {
            config,
            cache,
            http: Client::builder()
                .timeout(Duration::from_secs(30))
                .redirect(reqwest::redirect::Policy::none())
                .build()
                .expect("WhatsApp HTTP client"),
        }
    }
    pub fn verification(
        &self,
        mode: &str,
        token: &str,
        challenge: &str,
    ) -> Result<String, GatewayError> {
        self.require_config()?;
        if mode != "subscribe"
            || challenge.len() > 256
            || challenge.is_empty()
            || !constant_time_eq::constant_time_eq(
                token.as_bytes(),
                self.config.verify_token.as_bytes(),
            )
        {
            return Err(denied());
        }
        Ok(challenge.into())
    }
    pub fn verified_messages(
        &self,
        body: &[u8],
        signature: &str,
    ) -> Result<Vec<Value>, GatewayError> {
        self.require_config()?;
        if body.len() > 1024 * 1024 {
            return Err(GatewayError::validation("WhatsApp webhook is too large"));
        }
        let signature = signature.strip_prefix("sha256=").ok_or_else(denied)?;
        if signature.len() != 64 || !signature.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(denied());
        }
        let signature = (0..64)
            .step_by(2)
            .map(|i| u8::from_str_radix(&signature[i..i + 2], 16).map_err(|_| denied()))
            .collect::<Result<Vec<_>, _>>()?;
        let mut mac = Hmac::<Sha256>::new_from_slice(self.config.app_secret.as_bytes())
            .map_err(|_| denied())?;
        mac.update(body);
        mac.verify_slice(&signature).map_err(|_| denied())?;
        let payload: Value = serde_json::from_slice(body)
            .map_err(|_| GatewayError::validation("Invalid WhatsApp webhook"))?;
        if payload["object"] != "whatsapp_business_account" {
            return Err(GatewayError::validation("Invalid WhatsApp account event"));
        }
        let mut messages = Vec::new();
        for entry in payload["entry"].as_array().into_iter().flatten() {
            for change in entry["changes"].as_array().into_iter().flatten() {
                let value = &change["value"];
                if change["field"] != "messages"
                    || value
                        .pointer("/metadata/phone_number_id")
                        .and_then(Value::as_str)
                        != Some(&self.config.phone_number_id)
                {
                    continue;
                }
                for message in value["messages"].as_array().into_iter().flatten() {
                    if message["type"] == "text" {
                        messages.push(json!({"phone_number_id":self.config.phone_number_id,"message":message}));
                    }
                    if messages.len() > 100 {
                        return Err(GatewayError::validation(
                            "WhatsApp webhook contains too many messages",
                        ));
                    }
                }
            }
        }
        Ok(messages)
    }
    fn require_config(&self) -> Result<(), GatewayError> {
        if self.config.configured() {
            Ok(())
        } else {
            Err(GatewayError::new(
                StatusCode::SERVICE_UNAVAILABLE,
                "WHATSAPP_NOT_CONFIGURED",
                "WhatsApp is not configured. Continue in the Navi web app.",
            ))
        }
    }
    fn inbox(&self) -> Result<PathBuf, GatewayError> {
        let path = self
            .cache
            .parent()
            .ok_or_else(denied)?
            .join("whatsapp-inbox");
        std::fs::create_dir_all(&path)
            .map_err(|_| GatewayError::infrastructure("WhatsApp inbox is unavailable"))?;
        if std::fs::symlink_metadata(&path)?.file_type().is_symlink() {
            return Err(denied());
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700))?;
        }
        Ok(path)
    }
    pub fn enqueue(&self, messages: Vec<Value>) -> Result<usize, GatewayError> {
        use fs2::FileExt;
        let directory = self.inbox()?;
        let lock = std::fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(directory.join("inbox.lock"))?;
        lock.lock_exclusive()?;
        let mut count = 0;
        for payload in messages {
            let inbound = self.normalize_inbound(payload.clone())?;
            let id = format!(
                "{:x}",
                Sha256::digest(
                    json!([self.config.phone_number_id, inbound.message_id])
                        .to_string()
                        .as_bytes()
                )
            );
            let path = directory.join(format!("{id}.json"));
            if path.exists() {
                continue;
            }
            if std::fs::read_dir(&directory)?.count() > 10000 {
                return Err(GatewayError::new(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "WHATSAPP_INBOX_FULL",
                    "WhatsApp inbox is at capacity",
                ));
            }
            inbox_write(
                &path,
                &json!({"id":id,"status":"pending","created_at":chrono::Utc::now().timestamp(),"attempts":0,"payload":payload}),
            )?;
            count += 1;
        }
        Ok(count)
    }
    pub fn claim_message(&self) -> Result<Option<Value>, GatewayError> {
        if !self.config.configured() {
            return Ok(None);
        }
        use fs2::FileExt;
        let directory = self.inbox()?;
        let lock = std::fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(directory.join("inbox.lock"))?;
        lock.lock_exclusive()?;
        let now = chrono::Utc::now().timestamp();
        for entry in std::fs::read_dir(&directory)? {
            let path = entry?.path();
            if path.extension().and_then(|s| s.to_str()) != Some("json") {
                continue;
            }
            if !std::fs::symlink_metadata(&path)?.is_file()
                || std::fs::metadata(&path)?.len() > 65536
            {
                continue;
            }
            let mut item: Value = serde_json::from_slice(&std::fs::read(&path)?)?;
            if item["created_at"].as_i64().unwrap_or(now) < now - 7 * 86400
                && item["status"] == "done"
            {
                std::fs::remove_file(path)?;
                continue;
            }
            if item["status"] != "done"
                && (item["created_at"].as_i64().unwrap_or(0) < now - 86400
                    || item["attempts"].as_u64().unwrap_or(0) >= 30)
            {
                item["status"] = json!("done");
                item["payload"] = Value::Null;
                item["error_code"] = json!("WHATSAPP_RECEIVE_EXPIRED");
                inbox_write(&path, &item)?;
                continue;
            }
            if item["status"] == "pending"
                || (item["status"] == "running"
                    && item["claimed_at"].as_i64().unwrap_or(now) < now - 600)
            {
                if item["retry_at"].as_i64().unwrap_or(0) > now {
                    continue;
                }
                item["status"] = json!("running");
                item["claimed_at"] = json!(now);
                item["attempts"] = json!(item["attempts"].as_u64().unwrap_or(0) + 1);
                inbox_write(&path, &item)?;
                return Ok(Some(item));
            }
        }
        Ok(None)
    }
    pub fn finish_message(&self, mut item: Value, retry: bool) -> Result<(), GatewayError> {
        use fs2::FileExt;
        let id = item["id"].as_str().unwrap_or("").to_owned();
        if id.len() != 64 || !id.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(denied());
        }
        let directory = self.inbox()?;
        let lock = std::fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(directory.join("inbox.lock"))?;
        lock.lock_exclusive()?;
        let path = directory.join(format!("{id}.json"));
        let current: Value = serde_json::from_slice(&std::fs::read(&path)?)?;
        if current["status"] != "running"
            || current["attempts"] != item["attempts"]
            || current["claimed_at"] != item["claimed_at"]
        {
            return Ok(());
        }
        if retry {
            item["status"] = json!("pending");
            item["retry_at"] = json!(chrono::Utc::now().timestamp() + 60);
        } else {
            item["status"] = json!("done");
            item["payload"] = Value::Null;
        }
        inbox_write(&path, &item)
    }
    fn endpoint(&self, suffix: &str) -> String {
        format!(
            "https://graph.facebook.com/{}/{}/{}",
            self.config.graph_version, self.config.phone_number_id, suffix
        )
    }
    async fn decode(&self, response: reqwest::Response) -> Result<Value, GatewayError> {
        let status = response.status();
        if !status.is_success() {
            return Err(GatewayError::new(
                StatusCode::BAD_GATEWAY,
                "WHATSAPP_DELIVERY_FAILED",
                "WhatsApp rejected the message. Check the account, recipient and messaging window.",
            ));
        }
        if response
            .content_length()
            .is_some_and(|size| size > 1024 * 1024)
        {
            return Err(GatewayError::infrastructure(
                "WhatsApp response is too large",
            ));
        }
        response
            .json()
            .await
            .map_err(|_| GatewayError::infrastructure("WhatsApp returned an invalid response"))
    }
    fn message_body(
        &self,
        outbound: &OutboundMessage,
        prepared: Option<&PreparedOutbound>,
    ) -> Result<Value, GatewayError> {
        if !digits(&outbound.chat_id) || outbound.text.chars().count() > 4096 {
            return Err(GatewayError::validation(
                "Invalid WhatsApp recipient or message size",
            ));
        }
        let mut body = json!({"messaging_product":"whatsapp","recipient_type":"individual","to":outbound.chat_id});
        if let Some(media) = prepared {
            body["type"] = json!("image");
            body["image"] = json!({"id":media.provider_media_id});
            if !outbound.text.is_empty() {
                if outbound.text.chars().count() > 1024 {
                    return Err(GatewayError::validation(
                        "WhatsApp image caption is too long",
                    ));
                }
                body["image"]["caption"] = json!(outbound.text);
            }
        } else {
            if !outbound.attachments.is_empty() || outbound.text.trim().is_empty() {
                return Err(GatewayError::validation(
                    "WhatsApp media must be prepared before delivery",
                ));
            }
            body["type"] = json!("text");
            body["text"] = json!({"preview_url":false,"body":outbound.text});
        }
        Ok(body)
    }
}

#[async_trait]
impl PlatformAdapter for WhatsAppAdapter {
    fn name(&self) -> &str {
        "whatsapp"
    }
    fn normalize_inbound(&self, payload: Value) -> Result<InboundMessage, GatewayError> {
        self.require_config()?;
        if payload["phone_number_id"].as_str() != Some(&self.config.phone_number_id) {
            return Err(denied());
        }
        let message = &payload["message"];
        let from = message["from"].as_str().unwrap_or("");
        let id = message["id"].as_str().unwrap_or("");
        let text = message
            .pointer("/text/body")
            .and_then(Value::as_str)
            .unwrap_or("");
        let occurred = message["timestamp"]
            .as_str()
            .and_then(|value| value.parse::<i64>().ok())
            .filter(|value| *value > 0)
            .and_then(|value| chrono::DateTime::from_timestamp(value, 0))
            .ok_or_else(|| GatewayError::validation("WhatsApp event timestamp is invalid"))?;
        if !digits(from)
            || id.is_empty()
            || id.len() > 512
            || text.is_empty()
            || text.chars().count() > 4096
        {
            return Err(GatewayError::validation("Invalid WhatsApp text event"));
        }
        Ok(InboundMessage {
            platform: "whatsapp".into(),
            chat_id: from.into(),
            user_id: from.into(),
            text: text.into(),
            message_id: id.into(),
            chat_type: "p2p".into(),
            route_key: crate::harborbeacon::stable_id(
                "gw_route_",
                &json!(["whatsapp", self.config.phone_number_id, from]).to_string(),
                24,
            ),
            session_id: String::new(),
            mentions: vec![],
            attachments: vec![],
            metadata: serde_json::Map::new(),
            timestamp: occurred.to_rfc3339(),
            raw_payload: Value::Null,
        })
    }
    async fn prepare_outbound(
        &self,
        outbound: &OutboundMessage,
    ) -> Result<Option<PreparedOutbound>, GatewayError> {
        self.require_config()?;
        if outbound.attachments.is_empty() {
            return Ok(None);
        }
        if outbound.attachments.len() != 1 {
            return Err(GatewayError::validation(
                "Prepare one WhatsApp image per delivery item",
            ));
        }
        let item = &outbound.attachments[0];
        let mime = item["mime_type"].as_str().unwrap_or("");
        if !["image/jpeg", "image/png"].contains(&mime) {
            return Err(GatewayError::validation(
                "WhatsApp supports JPEG or PNG image replies",
            ));
        }
        let path = Path::new(item["path"].as_str().unwrap_or(""));
        let relative = path.strip_prefix(&self.cache).map_err(|_| denied())?;
        if relative
            .components()
            .any(|part| !matches!(part, std::path::Component::Normal(_)))
        {
            return Err(denied());
        }
        let root = cap_std::fs::Dir::open_ambient_dir(&self.cache, cap_std::ambient_authority())
            .map_err(|_| denied())?;
        let mut file = root.open(relative).map_err(|_| denied())?;
        if file.metadata().map_err(|_| denied())?.len() > 5 * 1024 * 1024 {
            return Err(GatewayError::validation("WhatsApp image is too large"));
        }
        use std::io::Read;
        let mut bytes = Vec::new();
        file.by_ref()
            .take(5 * 1024 * 1024 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| denied())?;
        if bytes.is_empty() || bytes.len() > 5 * 1024 * 1024 {
            return Err(GatewayError::validation("Invalid WhatsApp image"));
        }
        let form = Form::new().text("messaging_product", "whatsapp").part(
            "file",
            Part::bytes(bytes)
                .file_name(if mime == "image/png" {
                    "snapshot.png"
                } else {
                    "snapshot.jpg"
                })
                .mime_str(mime)
                .map_err(|_| denied())?,
        );
        let response = self
            .http
            .post(self.endpoint("media"))
            .bearer_auth(&self.config.access_token)
            .multipart(form)
            .send()
            .await
            .map_err(|_| GatewayError::infrastructure("WhatsApp media upload is unavailable"))?;
        let result = self.decode(response).await?;
        let id = result["id"]
            .as_str()
            .filter(|v| !v.is_empty())
            .ok_or_else(|| {
                GatewayError::infrastructure("WhatsApp upload did not return a media ID")
            })?;
        Ok(Some(PreparedOutbound {
            provider_media_id: id.into(),
            provider_client_id: None,
            state: json!({"kind":"image"}),
        }))
    }
    async fn send_outbound(&self, outbound: OutboundMessage) -> Result<Value, GatewayError> {
        let prepared = self.prepare_outbound(&outbound).await?;
        self.send_prepared_outbound(outbound, prepared.as_ref())
            .await
    }
    async fn send_prepared_outbound(
        &self,
        outbound: OutboundMessage,
        prepared: Option<&PreparedOutbound>,
    ) -> Result<Value, GatewayError> {
        self.require_config()?;
        let body = self.message_body(&outbound, prepared)?;
        let response = self
            .http
            .post(self.endpoint("messages"))
            .bearer_auth(&self.config.access_token)
            .json(&body)
            .send()
            .await
            .map_err(|_| {
                GatewayError::new(
                    StatusCode::BAD_GATEWAY,
                    "WHATSAPP_DELIVERY_UNCERTAIN",
                    "WhatsApp delivery could not be confirmed",
                )
            })?;
        let result = self.decode(response).await?;
        let id = result
            .pointer("/messages/0/id")
            .and_then(Value::as_str)
            .filter(|v| !v.is_empty())
            .ok_or_else(|| GatewayError::infrastructure("WhatsApp did not return a message ID"))?;
        Ok(
            json!({"platform":"whatsapp","delivery":"whatsapp","sent":true,"message_id":id,"provider_message_id":id}),
        )
    }
    fn profile(&self) -> Value {
        json!({"adapter_name":"whatsapp","surface_family":"whatsapp","transport_mode":"cloud_api",
        "configured":self.config.configured(),"supports_live_receive":self.config.configured(),"supports_attachments":true,
        "supports_replies":true,"supports_updates":false,"supports_mentions":false,
        "business_number":if self.config.configured(){Some(&self.config.business_number)}else{None}})
    }
}
fn denied() -> GatewayError {
    GatewayError::new(
        StatusCode::FORBIDDEN,
        "WHATSAPP_VERIFICATION_FAILED",
        "WhatsApp verification failed",
    )
}
fn inbox_write(path: &Path, value: &Value) -> Result<(), GatewayError> {
    atomicwrites::AtomicFile::new(path, atomicwrites::AllowOverwrite)
        .write(|file| {
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                file.set_permissions(std::fs::Permissions::from_mode(0o600))?;
            }
            serde_json::to_writer(&mut *file, value)?;
            file.sync_all()
        })
        .map_err(|_| GatewayError::infrastructure("WhatsApp inbox could not be saved"))
}
fn digits(value: &str) -> bool {
    !value.is_empty() && value.len() <= 32 && value.bytes().all(|b| b.is_ascii_digit())
}
fn valid_version(value: &str) -> bool {
    value.strip_prefix('v').is_some_and(|v| {
        v.split_once('.')
            .is_some_and(|(a, b)| digits(a) && digits(b))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn adapter() -> WhatsAppAdapter {
        WhatsAppAdapter::new(
            WhatsAppConfig {
                phone_number_id: "123".into(),
                business_number: "15555550100".into(),
                app_secret: "test-app-secret-value".into(),
                verify_token: "x".repeat(32),
                access_token: "test-token".into(),
                graph_version: "v25.0".into(),
            },
            PathBuf::from("/tmp/gate-cache"),
        )
    }
    fn sign(body: &[u8]) -> String {
        let mut mac = Hmac::<Sha256>::new_from_slice(b"test-app-secret-value").unwrap();
        mac.update(body);
        format!(
            "sha256={}",
            mac.finalize()
                .into_bytes()
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect::<String>()
        )
    }
    #[test]
    fn challenge_and_disabled_config_fail_closed() {
        let a = adapter();
        assert_eq!(
            a.verification("subscribe", &"x".repeat(32), "1234")
                .unwrap(),
            "1234"
        );
        assert!(a.verification("subscribe", "wrong", "1234").is_err());
        assert!(!WhatsAppConfig::default().configured());
    }
    #[test]
    fn raw_signature_batch_and_phone_filter() {
        let a = adapter();
        let body=json!({"object":"whatsapp_business_account","entry":[{"changes":[{"field":"messages","value":{
            "metadata":{"phone_number_id":"123"},"messages":[{"from":"15555550101","id":"wamid.1","timestamp":"1700000000","type":"text","text":{"body":"Hi Navi"}},
            {"from":"15555550101","id":"wamid.2","timestamp":"1700000001","type":"text","text":{"body":"Next"}}]}}]}]}).to_string().into_bytes();
        let messages = a.verified_messages(&body, &sign(&body)).unwrap();
        assert_eq!(messages.len(), 2);
        assert_eq!(
            a.normalize_inbound(messages[0].clone()).unwrap().message_id,
            "wamid.1"
        );
        let mut tampered = body.clone();
        tampered.push(b' ');
        assert!(a.verified_messages(&tampered, &sign(&body)).is_err());
        assert!(a.verified_messages(&body, "sha256=bad").is_err());
        let other = String::from_utf8(body)
            .unwrap()
            .replace("123", "456")
            .into_bytes();
        assert!(a
            .verified_messages(&other, &sign(&other))
            .unwrap()
            .is_empty());
    }
    #[test]
    fn payloads_never_treat_media_as_delivered_without_preparation() {
        let a = adapter();
        let mut out = OutboundMessage {
            platform: "whatsapp".into(),
            chat_id: "15555550101".into(),
            text: "hello".into(),
            attachments: vec![],
            timestamp: utc_now_iso(),
            metadata: Default::default(),
        };
        assert_eq!(
            a.message_body(&out, None).unwrap()["messaging_product"],
            "whatsapp"
        );
        out.attachments
            .push(json!({"url":"https://example.com/private.jpg"}));
        assert!(a.message_body(&out, None).is_err());
        assert!(!a.profile().to_string().contains("test-token"));
    }
    #[test]
    fn inbox_deduplicates_and_survives_restart() {
        let root = tempfile::tempdir().unwrap();
        let mut a = adapter();
        a.cache = root.path().join("attachment-cache");
        let payload = json!({"phone_number_id":"123","message":{"from":"15555550101","id":"wamid.1","timestamp":"1700000000","text":{"body":"Hi"}}});
        assert_eq!(
            a.enqueue(vec![payload.clone(), payload.clone()]).unwrap(),
            1
        );
        let claimed = a.claim_message().unwrap().unwrap();
        assert!(a.claim_message().unwrap().is_none());
        a.finish_message(claimed, false).unwrap();
        assert_eq!(a.enqueue(vec![payload]).unwrap(), 0);
    }

    #[test]
    fn provider_time_and_official_number_scope_survive_normalization_and_retries() {
        let mut a = adapter();
        let root = tempfile::tempdir().unwrap();
        a.cache = root.path().join("attachment-cache");
        let payload = json!({"phone_number_id":"123","message":{"from":"15555550101","id":"same-wamid","timestamp":"1700000000","text":{"body":"Hi"}}});
        let first = a.normalize_inbound(payload.clone()).unwrap();
        assert_eq!(first.timestamp, "2023-11-14T22:13:20+00:00");
        a.enqueue(vec![payload.clone()]).unwrap();
        let claimed = a.claim_message().unwrap().unwrap();
        assert_eq!(
            a.normalize_inbound(claimed["payload"].clone())
                .unwrap()
                .timestamp,
            first.timestamp
        );
        a.finish_message(claimed, false).unwrap();
        a.config.phone_number_id = "456".into();
        let mut second = payload.clone();
        second["phone_number_id"] = json!("456");
        let next = a.normalize_inbound(second.clone()).unwrap();
        assert_ne!(next.route_key, first.route_key);
        assert_ne!(
            crate::harborbeacon::build_turn_request(&first, None, None)["turn"]["turn_id"],
            crate::harborbeacon::build_turn_request(&next, None, None)["turn"]["turn_id"]
        );
        assert_eq!(a.enqueue(vec![second]).unwrap(), 1);
        let mut malformed = payload;
        malformed["phone_number_id"] = json!("456");
        malformed["message"]["timestamp"] = Value::Null;
        assert!(a.normalize_inbound(malformed).is_err());
    }
}

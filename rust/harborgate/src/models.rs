use chrono::{SecondsFormat, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;

pub fn utc_now_iso() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct InboundMessage {
    pub platform: String,
    pub chat_id: String,
    pub user_id: String,
    pub text: String,
    #[serde(default)]
    pub message_id: String,
    #[serde(default = "default_chat_type")]
    pub chat_type: String,
    #[serde(default)]
    pub route_key: String,
    #[serde(default)]
    pub session_id: String,
    #[serde(default)]
    pub mentions: Vec<Value>,
    #[serde(default)]
    pub attachments: Vec<Value>,
    #[serde(default)]
    pub metadata: serde_json::Map<String, Value>,
    #[serde(default = "utc_now_iso")]
    pub timestamp: String,
    #[serde(default)]
    pub raw_payload: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct OutboundMessage {
    pub platform: String,
    pub chat_id: String,
    pub text: String,
    #[serde(default)]
    pub attachments: Vec<Value>,
    #[serde(default = "utc_now_iso")]
    pub timestamp: String,
    #[serde(default)]
    pub metadata: serde_json::Map<String, Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ConversationTurn {
    pub role: String,
    pub content: String,
    #[serde(default = "utc_now_iso")]
    pub timestamp: String,
}

fn default_chat_type() -> String {
    "p2p".to_string()
}

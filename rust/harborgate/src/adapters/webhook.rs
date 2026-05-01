use crate::adapters::PlatformAdapter;
use crate::error::GatewayError;
use crate::models::{utc_now_iso, InboundMessage, OutboundMessage};
use async_trait::async_trait;
use serde_json::{json, Value};

pub struct WebhookAdapter;

#[async_trait]
impl PlatformAdapter for WebhookAdapter {
    fn name(&self) -> &str {
        "webhook"
    }

    fn normalize_inbound(&self, payload: Value) -> Result<InboundMessage, GatewayError> {
        let object = payload
            .as_object()
            .ok_or_else(|| GatewayError::validation("Payload must be a JSON object"))?;
        let platform = str_field(object, "platform").unwrap_or_else(|| "webhook".to_string());
        let chat_id = str_field(object, "chat_id").unwrap_or_default();
        let user_id = str_field(object, "user_id").unwrap_or_else(|| "anonymous".to_string());
        let text = str_field(object, "text").unwrap_or_default();
        if chat_id.trim().is_empty() {
            return Err(GatewayError::validation("Payload must include chat_id"));
        }
        if text.trim().is_empty() {
            return Err(GatewayError::validation("Payload must include text"));
        }
        Ok(InboundMessage {
            platform,
            chat_id,
            user_id,
            text,
            message_id: str_field(object, "message_id").unwrap_or_default(),
            chat_type: str_field(object, "chat_type").unwrap_or_else(|| "p2p".to_string()),
            route_key: str_field(object, "route_key").unwrap_or_default(),
            session_id: str_field(object, "session_id").unwrap_or_default(),
            mentions: value_array(object.get("mentions")),
            attachments: value_array(object.get("attachments")),
            metadata: object
                .get("metadata")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default(),
            timestamp: utc_now_iso(),
            raw_payload: Value::Object(object.clone()),
        })
    }

    async fn send_outbound(&self, outbound: OutboundMessage) -> Result<Value, GatewayError> {
        Ok(json!({
            "platform": outbound.platform,
            "chat_id": outbound.chat_id,
            "text": outbound.text,
            "attachments": outbound.attachments,
            "timestamp": outbound.timestamp,
            "metadata": outbound.metadata,
            "delivery": "webhook",
            "sent": false,
        }))
    }

    fn profile(&self) -> Value {
        json!({
            "adapter_name": "webhook",
            "surface_family": "webhook",
            "transport_mode": "normalized",
            "supports_mentions": false,
            "supports_attachments": true,
            "supports_replies": true,
            "supports_updates": false,
            "supports_live_receive": false,
        })
    }
}

fn str_field(object: &serde_json::Map<String, Value>, key: &str) -> Option<String> {
    object
        .get(key)
        .and_then(Value::as_str)
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

pub(crate) fn value_array(value: Option<&Value>) -> Vec<Value> {
    value
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter(|item| item.is_object())
                .cloned()
                .collect()
        })
        .unwrap_or_default()
}

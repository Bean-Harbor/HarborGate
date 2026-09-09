use crate::error::GatewayError;
use crate::models::{InboundMessage, OutboundMessage};
use async_trait::async_trait;
use serde_json::Value;

pub mod feishu;
pub mod feishu_mail;
pub mod webhook;
pub mod weixin;

#[derive(Debug, Clone)]
pub struct PreparedOutbound {
    pub provider_media_id: String,
    pub provider_client_id: Option<String>,
    pub state: Value,
}

#[async_trait]
pub trait PlatformAdapter: Send + Sync {
    fn name(&self) -> &str;
    fn delivery_claim_lease_seconds(&self) -> u64 {
        300
    }
    fn normalize_inbound(&self, payload: Value) -> Result<InboundMessage, GatewayError>;
    async fn send_outbound(&self, outbound: OutboundMessage) -> Result<Value, GatewayError>;
    async fn prepare_outbound(
        &self,
        _outbound: &OutboundMessage,
    ) -> Result<Option<PreparedOutbound>, GatewayError> {
        Ok(None)
    }
    async fn send_prepared_outbound(
        &self,
        outbound: OutboundMessage,
        _prepared: Option<&PreparedOutbound>,
    ) -> Result<Value, GatewayError> {
        self.send_outbound(outbound).await
    }
    fn profile(&self) -> Value;
    fn status(&self) -> Value {
        self.profile()
    }
}

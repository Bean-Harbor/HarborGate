use crate::error::GatewayError;
use crate::models::{InboundMessage, OutboundMessage};
use async_trait::async_trait;
use serde_json::Value;

pub mod feishu;
pub mod webhook;
pub mod weixin;

#[async_trait]
pub trait PlatformAdapter: Send + Sync {
    fn name(&self) -> &str;
    fn normalize_inbound(&self, payload: Value) -> Result<InboundMessage, GatewayError>;
    async fn send_outbound(&self, outbound: OutboundMessage) -> Result<Value, GatewayError>;
    fn profile(&self) -> Value;
    fn status(&self) -> Value {
        self.profile()
    }
}

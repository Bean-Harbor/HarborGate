use crate::adapters::feishu::{build_response_frame, parse_ws_frame_payload, PbFrame};
use crate::config::FeishuConfig;
use crate::gateway::GatewayService;
use prost::Message as _;
use serde_json::{json, Value};
use std::sync::Arc;
use std::thread;
use std::time::Duration;
use tokio::runtime::Handle;
use tracing::{info, warn};
use tungstenite::connect;
use tungstenite::Message as WsMessage;
use uuid::Uuid;

pub fn maybe_start_feishu_websocket_runtime(
    gateway: Arc<GatewayService>,
    config: FeishuConfig,
    enabled: bool,
) {
    if !enabled || !config.configured() || config.connection_mode != "websocket" {
        return;
    }
    let handle = Handle::current();
    thread::spawn(move || run_feishu_websocket_runtime(gateway, config, handle));
}

pub fn maybe_start_weixin_poll_runtime(gateway: Arc<GatewayService>, enabled: bool) {
    if !enabled {
        return;
    }
    let handle = Handle::current();
    thread::spawn(move || run_weixin_poll_runtime(gateway, handle));
}

pub fn start_delivery_recovery_runtime(gateway: Arc<GatewayService>) {
    tokio::spawn(async move {
        loop {
            match gateway.retry_pending_deliveries().await {
                Ok(attempted) if attempted > 0 => {
                    info!(attempted, "Retryable delivery recovery pass completed")
                }
                Ok(_) => {}
                Err(error) => warn!("Retryable delivery recovery pass failed: {error}"),
            }
            tokio::time::sleep(Duration::from_secs(5)).await;
        }
    });
}

fn run_weixin_poll_runtime(gateway: Arc<GatewayService>, handle: Handle) {
    let mut backoff_seconds = 1u64;
    let owner = format!("weixin-runtime-{}", Uuid::new_v4().simple());
    loop {
        let adapter = gateway.weixin_adapter();
        if !adapter.configured() {
            thread::sleep(Duration::from_secs(5));
            continue;
        }
        drain_weixin_inbox(&gateway, &adapter, &handle, &owner);
        match handle.block_on(adapter.poll_updates()) {
            Ok(batch) => {
                if let Err(error) = adapter.persist_inbound_batch(&batch.updates) {
                    let delay = backoff_seconds.min(30);
                    warn!("Weixin durable inbox commit failed: {error}; cursor not advanced; retrying in {delay}s");
                    thread::sleep(Duration::from_secs(delay));
                    backoff_seconds = (backoff_seconds * 2).min(30);
                    continue;
                }
                if let Err(error) = adapter.commit_poll_cursor(&batch.next_cursor) {
                    let delay = backoff_seconds.min(30);
                    warn!("Weixin cursor commit failed after durable inbox commit: {error}; provider replay will be deduplicated; retrying in {delay}s");
                    thread::sleep(Duration::from_secs(delay));
                    backoff_seconds = (backoff_seconds * 2).min(30);
                    continue;
                }
                backoff_seconds = 1;
                drain_weixin_inbox(&gateway, &adapter, &handle, &owner);
            }
            Err(error) => {
                let delay = backoff_seconds.min(30);
                warn!("Weixin poll failed: {error}; retrying in {delay}s");
                thread::sleep(Duration::from_secs(delay));
                backoff_seconds = (backoff_seconds * 2).min(30);
            }
        }
    }
}

fn drain_weixin_inbox(
    gateway: &Arc<GatewayService>,
    adapter: &Arc<crate::adapters::weixin::WeixinAdapter>,
    handle: &Handle,
    owner: &str,
) {
    for _ in 0..100 {
        let claim = match adapter.claim_inbound(owner, 15 * 60) {
            Ok(Some(claim)) => claim,
            Ok(None) => return,
            Err(error) => {
                warn!("Weixin inbox claim failed: {error}");
                return;
            }
        };
        let Some(inbox_id) = claim.get("inbox_id").and_then(Value::as_str) else {
            warn!("Weixin inbox claim did not include inbox_id");
            return;
        };
        let Some(claim_token) = claim.get("claim_token").and_then(Value::as_str) else {
            warn!("Weixin inbox claim did not include claim_token");
            return;
        };
        let payload = claim.get("payload").cloned().unwrap_or(Value::Null);
        match handle.block_on(gateway.handle_inbound("weixin", payload)) {
            Ok(_) => {
                if let Err(error) = adapter.finish_inbound(inbox_id, claim_token, true, false, None)
                {
                    warn!("Weixin inbox success tombstone persist failed: {error}");
                    return;
                }
            }
            Err(error) => {
                let retryable = error.status.is_server_error()
                    || matches!(
                        error.status,
                        axum::http::StatusCode::REQUEST_TIMEOUT
                            | axum::http::StatusCode::TOO_MANY_REQUESTS
                    );
                if let Err(store_error) = adapter.finish_inbound(
                    inbox_id,
                    claim_token,
                    false,
                    retryable,
                    Some(&error.to_string()),
                ) {
                    warn!("Weixin inbox failure state persist failed: {store_error}");
                }
                warn!(inbox_id, retryable, error = %error, "Weixin inbound inbox processing failed");
                if retryable {
                    return;
                }
            }
        }
    }
    warn!("Weixin inbox recovery pass reached its 100 item bound");
}

fn run_feishu_websocket_runtime(
    gateway: Arc<GatewayService>,
    config: FeishuConfig,
    handle: Handle,
) {
    let client = reqwest::blocking::Client::new();
    let mut reconnect_attempt = 0u64;
    loop {
        let ws_url = match fetch_ws_url(&client, &config) {
            Ok(url) => url,
            Err(error) => {
                reconnect_attempt = reconnect_attempt.saturating_add(1);
                let delay = reconnect_attempt.min(5);
                gateway.feishu_adapter().mark_websocket_error(&error);
                warn!("Feishu websocket endpoint fetch failed: {error}; retrying in {delay}s");
                thread::sleep(Duration::from_secs(delay));
                continue;
            }
        };

        match connect(ws_url.as_str()) {
            Ok((mut socket, _response)) => {
                reconnect_attempt = 0;
                gateway.feishu_adapter().mark_websocket_connected();
                info!("Feishu websocket connected");
                let disconnect_reason = loop {
                    match socket.read() {
                        Ok(WsMessage::Binary(bytes)) => {
                            let frame = match PbFrame::decode(bytes.as_slice()) {
                                Ok(frame) => frame,
                                Err(error) => {
                                    warn!("Feishu websocket protobuf decode failed: {error}");
                                    continue;
                                }
                            };
                            let message_type = frame.header("type").unwrap_or("");
                            if frame.method == 0 && matches!(message_type, "ping" | "pong") {
                                if let Err(error) =
                                    socket.send(WsMessage::Binary(build_response_frame(&frame)))
                                {
                                    break format!("heartbeat ack failed: {error}");
                                }
                                continue;
                            }
                            if frame.method == 1 && message_type == "event" {
                                if let Err(error) =
                                    socket.send(WsMessage::Binary(build_response_frame(&frame)))
                                {
                                    break format!("event ack failed: {error}");
                                }
                                match parse_ws_frame_payload(&bytes) {
                                    Ok(Some(payload)) => {
                                        gateway.feishu_adapter().mark_websocket_event();
                                        let gateway_for_event = gateway.clone();
                                        handle.spawn(async move {
                                            if let Err(error) = gateway_for_event
                                                .handle_inbound("feishu", payload)
                                                .await
                                            {
                                                warn!("Feishu websocket event handling failed: {error}");
                                            }
                                        });
                                    }
                                    Ok(None) => {}
                                    Err(error) => {
                                        warn!("Feishu websocket payload parse failed: {error}")
                                    }
                                }
                            }
                        }
                        Ok(WsMessage::Ping(data)) => {
                            if let Err(error) = socket.send(WsMessage::Pong(data)) {
                                break format!("pong failed: {error}");
                            }
                        }
                        Ok(WsMessage::Close(reason)) => {
                            break format!("closed: {reason:?}");
                        }
                        Ok(_) => {}
                        Err(error) => break format!("read failed: {error}"),
                    }
                };
                reconnect_attempt = reconnect_attempt.saturating_add(1);
                let delay = reconnect_attempt.min(5);
                gateway
                    .feishu_adapter()
                    .mark_websocket_error(&disconnect_reason);
                warn!("Feishu websocket disconnected: {disconnect_reason}; retrying in {delay}s");
                thread::sleep(Duration::from_secs(delay));
            }
            Err(error) => {
                reconnect_attempt = reconnect_attempt.saturating_add(1);
                let delay = reconnect_attempt.min(5);
                let message = format!("connect failed: {error}");
                gateway.feishu_adapter().mark_websocket_error(&message);
                warn!("Feishu websocket {message}; retrying in {delay}s");
                thread::sleep(Duration::from_secs(delay));
            }
        }
    }
}

fn fetch_ws_url(
    client: &reqwest::blocking::Client,
    config: &FeishuConfig,
) -> Result<String, String> {
    let url = format!(
        "{}/callback/ws/endpoint",
        config.base_url.trim_end_matches('/')
    );
    let response = client
        .post(url)
        .header("locale", "zh")
        .json(&json!({
            "AppID": config.app_id,
            "AppSecret": config.app_secret,
        }))
        .send()
        .map_err(|error| format!("request failed: {error}"))?;
    let status = response.status();
    let payload: Value = response
        .json()
        .map_err(|error| format!("invalid endpoint response JSON: {error}"))?;
    if !status.is_success() {
        return Err(format!("HTTP {status}: {payload}"));
    }
    if payload.get("code").and_then(Value::as_i64).unwrap_or(-1) != 0 {
        return Err(format!("provider code != 0: {payload}"));
    }
    payload
        .pointer("/data/URL")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .map(str::to_string)
        .ok_or_else(|| "endpoint response did not include data.URL".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::adapters::feishu::{PbFrame, PbHeader};

    #[test]
    fn response_frame_preserves_request_identity() {
        let request = PbFrame {
            seq_id: 7,
            log_id: 9,
            service: 100,
            method: 1,
            headers: vec![PbHeader {
                key: "type".into(),
                value: "event".into(),
            }],
            payload_encoding: None,
            payload_type: None,
            payload: Some(br#"{"hello":"world"}"#.to_vec()),
            log_id_new: None,
        };
        let response = PbFrame::decode(build_response_frame(&request).as_slice()).unwrap();
        assert_eq!(response.seq_id, 7);
        assert_eq!(response.log_id, 9);
        assert_eq!(response.header("biz_rt"), Some("0"));
    }
}

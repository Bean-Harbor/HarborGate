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

fn run_weixin_poll_runtime(gateway: Arc<GatewayService>, handle: Handle) {
    let mut backoff_seconds = 1u64;
    loop {
        let adapter = gateway.weixin_adapter();
        if !adapter.configured() {
            thread::sleep(Duration::from_secs(5));
            continue;
        }
        match handle.block_on(adapter.poll_updates()) {
            Ok(updates) => {
                backoff_seconds = 1;
                for payload in updates {
                    if adapter.is_duplicate_update(&payload) {
                        continue;
                    }
                    let handled =
                        handle.block_on(gateway.handle_inbound("weixin", payload.clone()));
                    match handled {
                        Ok(_) => {
                            if let Err(error) = adapter.mark_update_processed(&payload) {
                                warn!("Weixin update duplicate guard persist failed: {error}");
                            }
                        }
                        Err(error) => warn!("Weixin inbound update handling failed: {error}"),
                    }
                }
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

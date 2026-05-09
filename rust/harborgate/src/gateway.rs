use crate::adapters::feishu::FeishuAdapter;
use crate::adapters::webhook::WebhookAdapter;
use crate::adapters::weixin::WeixinAdapter;
use crate::adapters::PlatformAdapter;
use crate::config::AppConfig;
use crate::error::GatewayError;
use crate::harborbeacon::{
    build_channel_turn_request, derive_route_key, derive_session_id, stable_id,
    HarborBeaconTaskClient,
};
use crate::models::{ConversationTurn, InboundMessage, OutboundMessage};
use crate::store::FileSessionStore;
use axum::http::StatusCode;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::sync::Arc;

pub struct GatewayService {
    store: Arc<FileSessionStore>,
    task_client: Option<HarborBeaconTaskClient>,
    adapters: BTreeMap<String, Arc<dyn PlatformAdapter>>,
    feishu_adapter: Arc<FeishuAdapter>,
    weixin_adapter: Arc<WeixinAdapter>,
}

impl GatewayService {
    pub fn from_config(config: &AppConfig) -> anyhow::Result<Self> {
        let store = Arc::new(FileSessionStore::new(&config.data_dir)?);
        let mut adapters: BTreeMap<String, Arc<dyn PlatformAdapter>> = BTreeMap::new();
        let webhook = Arc::new(WebhookAdapter);
        adapters.insert(webhook.name().to_string(), webhook);
        let feishu = Arc::new(FeishuAdapter::new(config.feishu.clone()));
        adapters.insert(feishu.name().to_string(), feishu.clone());
        let weixin = Arc::new(WeixinAdapter::new(config.weixin.clone()));
        adapters.insert(weixin.name().to_string(), weixin.clone());
        Ok(Self {
            store,
            task_client: HarborBeaconTaskClient::from_config(config),
            adapters,
            feishu_adapter: feishu,
            weixin_adapter: weixin,
        })
    }

    pub fn adapter(&self, name: &str) -> Option<Arc<dyn PlatformAdapter>> {
        self.adapters.get(name).cloned()
    }

    pub fn feishu_adapter(&self) -> Arc<FeishuAdapter> {
        self.feishu_adapter.clone()
    }

    pub fn weixin_adapter(&self) -> Arc<WeixinAdapter> {
        self.weixin_adapter.clone()
    }

    pub async fn handle_inbound(
        &self,
        adapter_name: &str,
        payload: Value,
    ) -> Result<Value, GatewayError> {
        let adapter = self
            .adapter(adapter_name)
            .ok_or_else(|| GatewayError::validation(format!("Unknown adapter: {adapter_name}")))?;
        let inbound = adapter.normalize_inbound(payload)?;
        let history = self
            .store
            .load_history(&inbound.platform, &inbound.chat_id)
            .map_err(|err| GatewayError::infrastructure(err.to_string()))?;
        let session_metadata = self
            .store
            .load_metadata(&inbound.platform, &inbound.chat_id)
            .map_err(|err| GatewayError::infrastructure(err.to_string()))?;
        let resolved_route_key = inbound
            .route_key
            .trim()
            .to_string()
            .if_empty_then(|| {
                session_metadata
                    .get("route_key")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim()
                    .to_string()
            })
            .if_empty_then(|| derive_route_key(&inbound));
        let resolved_session_id = inbound
            .session_id
            .trim()
            .to_string()
            .if_empty_then(|| derive_session_id(&inbound));

        let (reply_text, outbound_attachments, mut outbound_metadata, next_metadata) =
            if let Some(task_client) = &self.task_client {
                let task_result = task_client.submit_turn(&inbound, &session_metadata).await?;
                let attachments =
                    native_source_bound_attachments(adapter_name, &task_result.response_payload);
                let reply_text = render_retrieval_reply(
                    &task_result.text,
                    &task_result.response_payload,
                    !attachments.is_empty(),
                );
                let mut next_metadata = session_metadata.clone();
                next_metadata.insert("route_key".into(), json!(task_result.route_key));
                next_metadata.insert("session_id".into(), json!(resolved_session_id));
                next_metadata.insert("last_turn_id".into(), json!(task_result.task_id));
                next_metadata.insert("last_trace_id".into(), json!(task_result.trace_id));
                if !inbound.message_id.trim().is_empty() {
                    next_metadata.insert("last_message_id".into(), json!(inbound.message_id));
                }
                if let Some(handle) = &task_result.conversation_handle {
                    next_metadata.insert("conversation_handle".into(), json!(handle));
                }
                if let Some(continuation) = &task_result.continuation {
                    next_metadata.insert("continuation".into(), continuation.clone());
                } else {
                    next_metadata.remove("continuation");
                }
                if !inbound.message_id.trim().is_empty() {
                    let mut message_turns = session_metadata
                        .get("message_turn_ids")
                        .and_then(Value::as_object)
                        .cloned()
                        .unwrap_or_default();
                    message_turns.insert(inbound.message_id.clone(), json!(task_result.task_id));
                    next_metadata.insert("message_turn_ids".into(), Value::Object(message_turns));
                }
                let mut metadata = serde_json::Map::new();
                metadata.insert("adapter".into(), json!(adapter_name));
                metadata.insert("source".into(), json!("harborbeacon"));
                metadata.insert("turn_id".into(), json!(task_result.task_id));
                metadata.insert("task_id".into(), json!(task_result.task_id));
                metadata.insert("trace_id".into(), json!(task_result.trace_id));
                metadata.insert("status".into(), json!(task_result.status));
                metadata.insert("route_key".into(), json!(task_result.route_key));
                metadata.insert(
                    "conversation_handle".into(),
                    json!(task_result.conversation_handle),
                );
                metadata.insert(
                    "active_frame".into(),
                    task_result.active_frame.unwrap_or(Value::Null),
                );
                metadata.insert(
                    "continuation".into(),
                    task_result.continuation.unwrap_or(Value::Null),
                );
                metadata.insert("next_actions".into(), json!(task_result.next_actions));
                metadata.insert("native_attachment_count".into(), json!(attachments.len()));
                (reply_text, attachments, metadata, next_metadata)
            } else {
                let mut next_metadata = session_metadata.clone();
                next_metadata.insert("route_key".into(), json!(resolved_route_key));
                next_metadata.insert("session_id".into(), json!(resolved_session_id));
                let mut metadata = serde_json::Map::new();
                metadata.insert("adapter".into(), json!(adapter_name));
                metadata.insert("source".into(), json!("rule_based_fallback"));
                (
                    fallback_reply(&history, &inbound),
                    vec![],
                    metadata,
                    next_metadata,
                )
            };

        self.store
            .set_metadata(&inbound.platform, &inbound.chat_id, next_metadata)
            .map_err(|err| GatewayError::infrastructure(err.to_string()))?;
        self.store
            .register_route(
                &resolved_route_key,
                json!({
                    "route_key": resolved_route_key,
                    "platform": inbound.platform,
                    "chat_id": inbound.chat_id,
                    "user_id": inbound.user_id,
                    "adapter_name": adapter_name,
                    "session_id": resolved_session_id,
                    "status": "active",
                }),
            )
            .map_err(|err| GatewayError::infrastructure(err.to_string()))?;
        self.store
            .append_turns(
                &inbound.platform,
                &inbound.chat_id,
                vec![
                    ConversationTurn {
                        role: "user".into(),
                        content: inbound.text.clone(),
                        timestamp: crate::models::utc_now_iso(),
                    },
                    ConversationTurn {
                        role: "assistant".into(),
                        content: reply_text.clone(),
                        timestamp: crate::models::utc_now_iso(),
                    },
                ],
            )
            .map_err(|err| GatewayError::infrastructure(err.to_string()))?;
        outbound_metadata.insert("route_key".into(), json!(resolved_route_key));
        let outbound = OutboundMessage {
            platform: inbound.platform,
            chat_id: inbound.chat_id,
            text: reply_text,
            attachments: outbound_attachments,
            timestamp: crate::models::utc_now_iso(),
            metadata: outbound_metadata,
        };
        adapter.send_outbound(outbound).await
    }

    pub async fn handle_gateway_turn(&self, payload: Value) -> Result<Value, GatewayError> {
        let Some(task_client) = &self.task_client else {
            return Err(GatewayError::new(
                StatusCode::SERVICE_UNAVAILABLE,
                "HARBORBEACON_DISABLED",
                "HarborBeacon turn forwarding is not configured",
            ));
        };
        let inbound = gateway_turn_to_inbound(&payload)?;
        let mut session_metadata = self
            .store
            .load_metadata(&inbound.platform, &inbound.chat_id)
            .map_err(|err| GatewayError::infrastructure(err.to_string()))?;
        if let Some(handle) = payload
            .pointer("/conversation/handle")
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
        {
            session_metadata.insert("conversation_handle".into(), json!(handle.trim()));
        }
        if let Some(continuation) = payload
            .get("continuation")
            .filter(|value| value.is_object())
        {
            session_metadata.insert("continuation".into(), continuation.clone());
        }

        let conversation_handle = session_metadata
            .get("conversation_handle")
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .map(str::to_string);
        let continuation = session_metadata
            .get("continuation")
            .filter(|value| value.is_object())
            .cloned();
        let request_payload = build_channel_turn_request(
            &inbound,
            &payload,
            conversation_handle.as_deref(),
            continuation,
        );
        let task_result = task_client.submit_turn_payload(request_payload).await?;

        let resolved_route_key = task_result
            .route_key
            .trim()
            .to_string()
            .if_empty_then(|| derive_route_key(&inbound));
        let resolved_session_id = derive_session_id(&inbound);
        let mut next_metadata = session_metadata;
        next_metadata.insert("route_key".into(), json!(resolved_route_key));
        next_metadata.insert("session_id".into(), json!(resolved_session_id));
        next_metadata.insert("last_turn_id".into(), json!(task_result.task_id));
        next_metadata.insert("last_trace_id".into(), json!(task_result.trace_id));
        if let Some(handle) = &task_result.conversation_handle {
            next_metadata.insert("conversation_handle".into(), json!(handle));
        }
        if let Some(continuation) = &task_result.continuation {
            next_metadata.insert("continuation".into(), continuation.clone());
        } else {
            next_metadata.remove("continuation");
        }
        if !inbound.message_id.trim().is_empty() {
            next_metadata.insert("last_message_id".into(), json!(inbound.message_id));
        }
        self.store
            .set_metadata(&inbound.platform, &inbound.chat_id, next_metadata)
            .map_err(|err| GatewayError::infrastructure(err.to_string()))?;
        self.store
            .register_route(
                &resolved_route_key,
                json!({
                    "route_key": resolved_route_key,
                    "platform": inbound.platform,
                    "chat_id": inbound.chat_id,
                    "user_id": inbound.user_id,
                    "adapter_name": inbound.platform,
                    "session_id": resolved_session_id,
                    "status": "active",
                    "route_mode": "channel_edge",
                    "route_source": "gateway_turn",
                }),
            )
            .map_err(|err| GatewayError::infrastructure(err.to_string()))?;
        self.store
            .append_turns(
                &inbound.platform,
                &inbound.chat_id,
                vec![
                    ConversationTurn {
                        role: "user".into(),
                        content: inbound.text.clone(),
                        timestamp: crate::models::utc_now_iso(),
                    },
                    ConversationTurn {
                        role: "assistant".into(),
                        content: task_result.text.clone(),
                        timestamp: crate::models::utc_now_iso(),
                    },
                ],
            )
            .map_err(|err| GatewayError::infrastructure(err.to_string()))?;

        Ok(task_result.response_payload)
    }

    pub async fn handle_notification_delivery(
        &self,
        payload: Value,
    ) -> Result<Value, GatewayError> {
        let trace_id = notification_trace_id(&payload);
        let notification_id = notification_id(&payload);
        if notification_id.is_empty() {
            return Err(
                GatewayError::validation("notification_id is required").with_trace(trace_id)
            );
        }
        let destination = payload
            .get("destination")
            .and_then(Value::as_object)
            .ok_or_else(|| {
                GatewayError::validation("destination must be an object")
                    .with_trace(trace_id.clone())
            })?;
        let delivery = payload
            .get("delivery")
            .and_then(Value::as_object)
            .ok_or_else(|| {
                GatewayError::validation("delivery must be an object").with_trace(trace_id.clone())
            })?;
        let mode = delivery
            .get("mode")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_lowercase();
        let idempotency_key = delivery
            .get("idempotency_key")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        let reply_to_message_id = delivery
            .get("reply_to_message_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        let update_message_id = delivery
            .get("update_message_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        validate_delivery_mode(&mode, &reply_to_message_id, &update_message_id, &trace_id)?;
        if idempotency_key.is_empty() {
            return Err(
                GatewayError::validation("delivery.idempotency_key is required")
                    .with_trace(trace_id),
            );
        }

        let route_key = destination
            .get("route_key")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        let route = self.resolve_notification_route(destination, &route_key, &trace_id)?;
        let adapter_name = route
            .get("adapter_name")
            .or_else(|| route.get("platform"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        let adapter = self.adapter(&adapter_name).ok_or_else(|| {
            GatewayError::validation(format!(
                "No adapter is enabled for outbound platform route: {}",
                if adapter_name.is_empty() {
                    "unknown"
                } else {
                    &adapter_name
                }
            ))
            .with_trace(trace_id.clone())
        })?;

        let effective_request = json!({
            "notification_id": notification_id,
            "trace_id": trace_id,
            "destination": {
                "route_key": route_key,
                "platform": route.get("platform").cloned().unwrap_or(Value::Null),
                "chat_id": route.get("chat_id").cloned().unwrap_or(Value::Null),
                "recipient": destination.get("recipient").cloned().unwrap_or(Value::Null),
            },
            "content": delivery_content(&payload),
            "delivery": {
                "mode": mode,
                "reply_to_message_id": reply_to_message_id,
                "update_message_id": update_message_id,
            },
        });
        let fingerprint = fingerprint(&effective_request);
        if let Some(record) = self
            .store
            .load_delivery_record(&idempotency_key)
            .map_err(|err| {
                GatewayError::infrastructure(err.to_string()).with_trace(trace_id.clone())
            })?
        {
            let existing = record
                .get("request_fingerprint")
                .and_then(Value::as_str)
                .unwrap_or("");
            if existing != fingerprint {
                return Err(GatewayError::new(
                    StatusCode::CONFLICT,
                    "IDEMPOTENCY_CONFLICT",
                    "delivery.idempotency_key was reused with a different effective request",
                )
                .with_trace(trace_id));
            }
            if let Some(response) = record.get("response_payload") {
                return Ok(response.clone());
            }
        }

        let content = delivery_content(&payload);
        let outbound = OutboundMessage {
            platform: route
                .get("platform")
                .and_then(Value::as_str)
                .unwrap_or(&adapter_name)
                .to_string(),
            chat_id: route
                .get("chat_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            text: notification_text(&content),
            attachments: content
                .get("attachments")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default(),
            timestamp: crate::models::utc_now_iso(),
            metadata: outbound_delivery_metadata(
                &notification_id,
                &trace_id,
                &mode,
                &route_key,
                &reply_to_message_id,
                &update_message_id,
                &route,
                &content,
            ),
        };
        let delivery_id = stable_id("delivery_", &idempotency_key, 24);
        let response_payload = match adapter.send_outbound(outbound).await {
            Ok(adapter_response) => {
                let provider_message_id = adapter_response
                    .get("message_id")
                    .or_else(|| adapter_response.get("provider_message_id"))
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim()
                    .to_string();
                json!({
                    "delivery_id": delivery_id,
                    "notification_id": notification_id,
                    "trace_id": trace_id,
                    "ok": true,
                    "status": adapter_response.get("placeholder_status").and_then(Value::as_str).unwrap_or("sent"),
                    "platform": route.get("platform").and_then(Value::as_str).unwrap_or(&adapter_name),
                    "provider_message_id": if provider_message_id.is_empty() { Value::Null } else { json!(provider_message_id) },
                    "retryable": false,
                    "error": null,
                })
            }
            Err(error) => {
                let (code, retryable) = map_delivery_failure(&error.message);
                json!({
                    "delivery_id": delivery_id,
                    "notification_id": notification_id,
                    "trace_id": trace_id,
                    "ok": false,
                    "status": "failed",
                    "platform": route.get("platform").and_then(Value::as_str).unwrap_or(&adapter_name),
                    "provider_message_id": null,
                    "retryable": retryable,
                    "error": {
                        "code": code,
                        "message": error.message,
                    },
                })
            }
        };
        let classification = classify_delivery_attempt(&route, &response_payload);
        self.store
            .save_delivery_record(
                &idempotency_key,
                &fingerprint,
                response_payload.clone(),
                classification,
            )
            .map_err(|err| GatewayError::infrastructure(err.to_string()).with_trace(trace_id))?;
        Ok(response_payload)
    }

    pub fn status(&self) -> Value {
        let mut adapters = serde_json::Map::new();
        for (name, adapter) in &self.adapters {
            adapters.insert(
                name.clone(),
                json!({
                    "name": name,
                    "enabled": true,
                    "profile": adapter.profile(),
                    "transport": adapter.status(),
                }),
            );
        }
        json!({
            "status": "ok",
            "runtime": "rust",
            "contract_version": "2.0",
            "gateway_turn_contract_version": "3.0",
            "gateway_turn_endpoint": "/api/gateway/turns",
            "beacon_proxy_prefix": "/api/beacon",
            "turn_endpoint": "/api/web/turns",
            "adapters": adapters,
            "delivery_health": self.store.delivery_health().unwrap_or_else(|_| json!({"record_count": 0})),
        })
    }

    fn resolve_notification_route(
        &self,
        destination: &serde_json::Map<String, Value>,
        route_key: &str,
        trace_id: &str,
    ) -> Result<Value, GatewayError> {
        if !route_key.is_empty() {
            let route = self
                .store
                .resolve_route(route_key)
                .map_err(|err| {
                    GatewayError::infrastructure(err.to_string()).with_trace(trace_id.to_string())
                })?
                .ok_or_else(|| {
                    GatewayError::new(
                        StatusCode::NOT_FOUND,
                        "ROUTE_NOT_FOUND",
                        format!("route_key not found: {route_key}"),
                    )
                    .with_trace(trace_id.to_string())
                })?;
            if route
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or("active")
                == "expired"
            {
                return Err(GatewayError::new(
                    StatusCode::GONE,
                    "ROUTE_EXPIRED",
                    format!("route_key expired: {route_key}"),
                )
                .with_trace(trace_id.to_string()));
            }
            let mut object = route.as_object().cloned().unwrap_or_default();
            object.insert("route_mode".into(), json!("source_bound"));
            object.insert("route_source".into(), json!("route_key"));
            if !object.contains_key("adapter_name") {
                let platform = object.get("platform").cloned().unwrap_or(Value::Null);
                object.insert("adapter_name".into(), platform);
            }
            return Ok(Value::Object(object));
        }
        let recipient = destination
            .get("recipient")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        let platform = destination
            .get("platform")
            .and_then(Value::as_str)
            .or_else(|| recipient.get("platform").and_then(Value::as_str))
            .unwrap_or("")
            .trim()
            .to_string();
        let chat_id = destination
            .get("id")
            .and_then(Value::as_str)
            .or_else(|| recipient.get("recipient_id").and_then(Value::as_str))
            .unwrap_or("")
            .trim()
            .to_string();
        if platform.is_empty() || chat_id.is_empty() {
            return Err(GatewayError::validation(
                "destination.route_key is preferred; otherwise destination.platform with destination.id or destination.recipient is required",
            )
            .with_trace(trace_id.to_string()));
        }
        Ok(json!({
            "platform": platform,
            "chat_id": chat_id,
            "adapter_name": platform,
            "status": "active",
            "route_mode": "proactive",
            "route_source": if destination.get("id").is_some() { "platform_id" } else { "recipient" },
        }))
    }
}

fn gateway_turn_to_inbound(payload: &Value) -> Result<InboundMessage, GatewayError> {
    let channel = first_string(
        payload,
        &[
            "/conversation/channel",
            "/channel",
            "/surface",
            "/transport/channel",
        ],
    )
    .unwrap_or_else(|| "webui".to_string());
    let thread_id = first_string(
        payload,
        &[
            "/conversation/thread_id",
            "/thread_id",
            "/session_id",
            "/transport/session_id",
            "/actor/user_id",
        ],
    )
    .unwrap_or_else(|| stable_id("thread_", &crate::harborbeacon::canonical_json(payload), 16));
    let user_id = first_string(payload, &["/actor/user_id", "/user_id", "/open_id"])
        .unwrap_or_else(|| "anonymous".to_string());
    let text = first_string(payload, &["/input/text", "/text", "/message/text"])
        .ok_or_else(|| GatewayError::validation("input.text is required"))?;
    let mut metadata = payload
        .pointer("/transport/metadata")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    metadata.insert("source".into(), json!("gateway_turn"));
    if let Some(surface) = payload
        .pointer("/conversation/surface")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
    {
        metadata.insert("surface".into(), json!(surface.trim()));
    }
    Ok(InboundMessage {
        platform: channel,
        chat_id: thread_id,
        user_id,
        text,
        message_id: first_string(
            payload,
            &["/transport/message_id", "/message_id", "/turn/turn_id"],
        )
        .unwrap_or_default(),
        chat_type: first_string(payload, &["/conversation/chat_type", "/chat_type"])
            .unwrap_or_else(|| "p2p".to_string()),
        route_key: first_string(payload, &["/transport/route_key", "/route_key"])
            .unwrap_or_default(),
        session_id: first_string(payload, &["/transport/session_id", "/session_id"])
            .unwrap_or_default(),
        mentions: vec![],
        attachments: payload
            .pointer("/input/parts")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default(),
        metadata,
        timestamp: first_string(payload, &["/turn/occurred_at"])
            .unwrap_or_else(crate::models::utc_now_iso),
        raw_payload: payload.clone(),
    })
}

fn first_string(payload: &Value, pointers: &[&str]) -> Option<String> {
    pointers.iter().find_map(|pointer| {
        payload
            .pointer(pointer)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(ToString::to_string)
    })
}

fn fallback_reply(history: &[ConversationTurn], inbound: &InboundMessage) -> String {
    if history.is_empty() {
        format!("收到：{}", inbound.text)
    } else {
        format!("继续收到：{}", inbound.text)
    }
}

fn native_source_bound_attachments(adapter_name: &str, response_payload: &Value) -> Vec<Value> {
    if adapter_name != "feishu" && adapter_name != "weixin" {
        return vec![];
    }
    let artifacts = artifact_candidates(response_payload);
    if adapter_name == "weixin" {
        return weixin_native_attachments(artifacts, response_payload);
    }
    let images: Vec<Value> = artifacts
        .into_iter()
        .filter(|artifact| {
            let kind = artifact
                .get("kind")
                .or_else(|| artifact.get("type"))
                .and_then(Value::as_str)
                .unwrap_or("");
            let mime_type = artifact
                .get("mime_type")
                .and_then(Value::as_str)
                .unwrap_or("");
            let path = artifact.get("path").and_then(Value::as_str).unwrap_or("");
            kind == "image" && mime_type.starts_with("image/") && !path.trim().is_empty()
        })
        .collect();
    if images.is_empty() {
        return vec![];
    }
    if let Some(limit) = native_image_limit(response_payload) {
        return images.into_iter().take(limit).collect();
    }
    if images.len() == 1 {
        images
    } else {
        vec![]
    }
}

fn weixin_native_attachments(artifacts: Vec<Value>, response_payload: &Value) -> Vec<Value> {
    let media: Vec<Value> = artifacts
        .into_iter()
        .filter(|artifact| {
            let kind = artifact
                .get("kind")
                .or_else(|| artifact.get("type"))
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_lowercase();
            let mime_type = artifact
                .get("mime_type")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_lowercase();
            let path = artifact.get("path").and_then(Value::as_str).unwrap_or("");
            !path.trim().is_empty()
                && (kind == "image"
                    || kind == "video"
                    || kind == "file"
                    || mime_type.starts_with("image/")
                    || mime_type.starts_with("video/"))
        })
        .collect();
    if media.is_empty() {
        return vec![];
    }
    let all_images = media.iter().all(|artifact| {
        let kind = artifact
            .get("kind")
            .or_else(|| artifact.get("type"))
            .and_then(Value::as_str)
            .unwrap_or("");
        let mime_type = artifact
            .get("mime_type")
            .and_then(Value::as_str)
            .unwrap_or("");
        kind == "image" || mime_type.starts_with("image/")
    });
    if all_images {
        if let Some(limit) = native_image_limit(response_payload) {
            return media.into_iter().take(limit).collect();
        }
        return if media.len() == 1 { media } else { vec![] };
    }
    if media.len() == 1 {
        media
    } else {
        vec![]
    }
}

fn artifact_candidates(response_payload: &Value) -> Vec<Value> {
    response_payload
        .get("artifacts")
        .and_then(Value::as_array)
        .or_else(|| {
            response_payload
                .pointer("/result/artifacts")
                .and_then(Value::as_array)
        })
        .or_else(|| {
            response_payload
                .pointer("/result/attachments")
                .and_then(Value::as_array)
        })
        .or_else(|| {
            response_payload
                .pointer("/result/evidence")
                .and_then(Value::as_array)
        })
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter(|value| value.is_object())
        .collect()
}

fn native_image_limit(response_payload: &Value) -> Option<usize> {
    let hint = response_payload
        .get("delivery_hints")
        .and_then(Value::as_array)?
        .iter()
        .find(|hint| {
            matches!(
                hint.get("kind").and_then(Value::as_str),
                Some("native_image" | "native_images")
            )
        })?;
    let raw = hint
        .get("max_items")
        .or_else(|| hint.pointer("/metadata/max_items"))
        .or_else(|| hint.pointer("/metadata/limit"))
        .and_then(Value::as_u64)
        .unwrap_or(3);
    Some(raw.clamp(1, 3) as usize)
}

fn render_retrieval_reply(
    base_text: &str,
    response_payload: &Value,
    suppress_artifacts: bool,
) -> String {
    let citations = response_payload
        .pointer("/result/citations")
        .and_then(Value::as_array)
        .or_else(|| {
            response_payload
                .pointer("/result/references")
                .and_then(Value::as_array)
        })
        .or_else(|| {
            response_payload
                .pointer("/result/sources")
                .and_then(Value::as_array)
        })
        .or_else(|| {
            response_payload
                .pointer("/result/top_hits")
                .and_then(Value::as_array)
        })
        .or_else(|| {
            response_payload
                .pointer("/result/hits")
                .and_then(Value::as_array)
        })
        .cloned()
        .unwrap_or_default();
    let artifacts = artifact_candidates(response_payload);
    if citations.is_empty() && (artifacts.is_empty() || suppress_artifacts) {
        return base_text.trim().to_string();
    }
    let mut sections = Vec::new();
    sections.push(format!(
        "检索结果（{} 条引用，{} 个附件）",
        citations.len(),
        artifacts.len()
    ));
    if !base_text.trim().is_empty() {
        sections.push(base_text.trim().to_string());
    }
    if !citations.is_empty() {
        sections.push(format!("引用\n{}", render_entries(&citations, "citation")));
    }
    if !artifacts.is_empty() && !suppress_artifacts {
        sections.push(format!("附件\n{}", render_entries(&artifacts, "artifact")));
    }
    sections.join("\n\n")
}

fn render_entries(records: &[Value], kind: &str) -> String {
    records
        .iter()
        .take(3)
        .enumerate()
        .map(|(index, record)| {
            let entry = if kind == "citation" {
                first_text(
                    record,
                    &[
                        "title", "name", "headline", "summary", "snippet", "text", "id",
                    ],
                )
            } else {
                first_text(
                    record,
                    &["title", "name", "filename", "file_name", "label", "id"],
                )
            };
            format!(
                "{}. {}",
                index + 1,
                entry.unwrap_or_else(|| "未命名".to_string())
            )
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn first_text(record: &Value, keys: &[&str]) -> Option<String> {
    for key in keys {
        if let Some(text) = record.get(*key).and_then(Value::as_str) {
            if !text.trim().is_empty() {
                return Some(text.trim().to_string());
            }
        }
    }
    None
}

fn validate_delivery_mode(
    mode: &str,
    reply_to_message_id: &str,
    update_message_id: &str,
    trace_id: &str,
) -> Result<(), GatewayError> {
    if !matches!(mode, "send" | "reply" | "update") {
        return Err(
            GatewayError::validation("delivery.mode must be send, reply, or update")
                .with_trace(trace_id.to_string()),
        );
    }
    if mode == "send" && (!reply_to_message_id.is_empty() || !update_message_id.is_empty()) {
        return Err(GatewayError::validation(
            "delivery.mode=send requires empty reply_to_message_id and update_message_id",
        )
        .with_trace(trace_id.to_string()));
    }
    if mode == "reply" && (reply_to_message_id.is_empty() || !update_message_id.is_empty()) {
        return Err(GatewayError::validation(
            "delivery.mode=reply requires reply_to_message_id and forbids update_message_id",
        )
        .with_trace(trace_id.to_string()));
    }
    if mode == "update" && (update_message_id.is_empty() || !reply_to_message_id.is_empty()) {
        return Err(GatewayError::validation(
            "delivery.mode=update requires update_message_id and forbids reply_to_message_id",
        )
        .with_trace(trace_id.to_string()));
    }
    Ok(())
}

fn notification_id(payload: &Value) -> String {
    payload
        .pointer("/notification/notification_id")
        .and_then(Value::as_str)
        .or_else(|| payload.get("notification_id").and_then(Value::as_str))
        .unwrap_or("")
        .trim()
        .to_string()
}

fn notification_trace_id(payload: &Value) -> String {
    payload
        .pointer("/notification/trace_id")
        .and_then(Value::as_str)
        .or_else(|| payload.get("trace_id").and_then(Value::as_str))
        .unwrap_or("")
        .trim()
        .to_string()
}

fn delivery_content(payload: &Value) -> Value {
    if let Some(content) = payload.get("content").filter(|value| value.is_object()) {
        return content.clone();
    }
    let reply = payload.get("reply").and_then(Value::as_object);
    json!({
        "title": reply.and_then(|reply| reply.get("title")).and_then(Value::as_str).unwrap_or(""),
        "body": reply.and_then(|reply| reply.get("text")).and_then(Value::as_str).unwrap_or(""),
        "attachments": payload.get("artifacts").cloned().unwrap_or_else(|| json!([])),
        "delivery_hints": payload.get("delivery_hints").cloned().unwrap_or_else(|| json!([])),
        "payload_format": reply.and_then(|reply| reply.get("payload_format")).and_then(Value::as_str).unwrap_or("plain_text"),
        "structured_payload": reply.and_then(|reply| reply.get("structured_payload")).cloned().unwrap_or_else(|| json!({})),
    })
}

fn notification_text(content: &Value) -> String {
    let title = content
        .get("title")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let body = content
        .get("body")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if !title.is_empty() && !body.is_empty() {
        format!("{title}\n\n{body}")
    } else if !body.is_empty() {
        body.to_string()
    } else {
        title.to_string()
    }
}

fn outbound_delivery_metadata(
    notification_id: &str,
    trace_id: &str,
    mode: &str,
    route_key: &str,
    reply_to_message_id: &str,
    update_message_id: &str,
    route: &Value,
    content: &Value,
) -> serde_json::Map<String, Value> {
    let mut metadata = serde_json::Map::new();
    metadata.insert("source".into(), json!("notification_delivery"));
    metadata.insert("notification_id".into(), json!(notification_id));
    metadata.insert("trace_id".into(), json!(trace_id));
    metadata.insert("delivery_mode".into(), json!(mode));
    metadata.insert("route_key".into(), json!(route_key));
    metadata.insert(
        "route_mode".into(),
        route
            .get("route_mode")
            .cloned()
            .unwrap_or_else(|| json!("unknown")),
    );
    metadata.insert(
        "route_source".into(),
        route
            .get("route_source")
            .cloned()
            .unwrap_or_else(|| json!("unknown")),
    );
    metadata.insert("reply_to_message_id".into(), json!(reply_to_message_id));
    metadata.insert("update_message_id".into(), json!(update_message_id));
    metadata.insert(
        "payload_format".into(),
        content
            .get("payload_format")
            .cloned()
            .unwrap_or_else(|| json!("plain_text")),
    );
    metadata.insert(
        "structured_payload".into(),
        content
            .get("structured_payload")
            .cloned()
            .unwrap_or_else(|| json!({})),
    );
    metadata
}

fn fingerprint(payload: &Value) -> String {
    let encoded = crate::harborbeacon::canonical_json(payload);
    let mut hasher = Sha256::new();
    hasher.update(encoded.as_bytes());
    format!("{:x}", hasher.finalize())
}

fn map_delivery_failure(message: &str) -> (&'static str, bool) {
    let lower = message.to_lowercase();
    if lower.contains("context_token") {
        ("INVALID_RECIPIENT", false)
    } else if lower.contains("not configured")
        || lower.contains("authorization")
        || lower.contains("auth")
    {
        ("PROVIDER_AUTH_FAILED", false)
    } else if lower.contains("unsupported") {
        ("UNSUPPORTED_CONTENT", false)
    } else {
        ("PLATFORM_UNAVAILABLE", true)
    }
}

fn classify_delivery_attempt(route: &Value, response_payload: &Value) -> Value {
    let ok = response_payload
        .get("ok")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let retryable = response_payload
        .get("retryable")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let failure_class = if ok {
        ""
    } else {
        response_payload
            .pointer("/error/code")
            .and_then(Value::as_str)
            .unwrap_or("INTERNAL_ERROR")
    };
    json!({
        "route_mode": route.get("route_mode").and_then(Value::as_str).unwrap_or("unknown"),
        "route_source": route.get("route_source").and_then(Value::as_str).unwrap_or("unknown"),
        "outcome": if ok { "sent" } else { "failed" },
        "failure_class": failure_class,
        "queue_state": if ok { "complete" } else if retryable { "retry_queue" } else { "terminal_failure" },
        "retryable": retryable,
    })
}

trait IfEmptyThen {
    fn if_empty_then(self, producer: impl FnOnce() -> String) -> String;
}

impl IfEmptyThen for String {
    fn if_empty_then(self, producer: impl FnOnce() -> String) -> String {
        if self.trim().is_empty() {
            producer()
        } else {
            self
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[tokio::test]
    async fn webhook_inbound_registers_route_and_replies() {
        let dir = tempdir().unwrap();
        let mut config = AppConfig::from_env();
        config.data_dir = dir.path().to_path_buf();
        config.harborbeacon_base_url.clear();
        let gateway = GatewayService::from_config(&config).unwrap();
        let response = gateway
            .handle_inbound(
                "webhook",
                json!({"chat_id": "chat1", "user_id": "user1", "text": "hello", "message_id": "msg1"}),
            )
            .await
            .unwrap();
        assert_eq!(response["delivery"], "webhook");
        let routes: Value = serde_json::from_str(
            &std::fs::read_to_string(dir.path().join("_routes.json")).unwrap(),
        )
        .unwrap();
        assert!(routes
            .as_object()
            .unwrap()
            .keys()
            .next()
            .unwrap()
            .starts_with("gw_route_"));
    }

    #[tokio::test]
    async fn notification_delivery_is_idempotent() {
        let dir = tempdir().unwrap();
        let mut config = AppConfig::from_env();
        config.data_dir = dir.path().to_path_buf();
        config.harborbeacon_base_url.clear();
        let gateway = GatewayService::from_config(&config).unwrap();
        gateway
            .handle_inbound(
                "webhook",
                json!({"chat_id": "chat1", "user_id": "user1", "text": "hello", "message_id": "msg1"}),
            )
            .await
            .unwrap();
        let route_key = serde_json::from_str::<Value>(
            &std::fs::read_to_string(dir.path().join("_routes.json")).unwrap(),
        )
        .unwrap()
        .as_object()
        .unwrap()
        .keys()
        .next()
        .unwrap()
        .to_string();
        let payload = json!({
            "notification": {"notification_id": "notif_1", "trace_id": "trace_1"},
            "destination": {"route_key": route_key},
            "reply": {"kind": "tool_result", "text": "done"},
            "delivery": {"mode": "send", "idempotency_key": "idem_1", "reply_to_message_id": null, "update_message_id": null}
        });
        let first = gateway
            .handle_notification_delivery(payload.clone())
            .await
            .unwrap();
        let second = gateway.handle_notification_delivery(payload).await.unwrap();
        assert_eq!(first, second);
    }

    #[test]
    fn gateway_turn_normalizes_android_payload_without_forwarding_push_secret() {
        let payload = json!({
            "turn": {"turn_id": "turn-android-1", "trace_id": "trace-android-1"},
            "actor": {"user_id": "user-1", "workspace_id": "home-1"},
            "conversation": {"channel": "android", "surface": "android", "thread_id": "device-1"},
            "transport": {
                "message_id": "msg-1",
                "metadata": {
                    "client_version": "1.0",
                    "push_token": "secret-token"
                }
            },
            "input": {"text": "hello", "parts": []}
        });

        let inbound = gateway_turn_to_inbound(&payload).unwrap();

        assert_eq!(inbound.platform, "android");
        assert_eq!(inbound.chat_id, "device-1");
        assert_eq!(inbound.user_id, "user-1");
        assert_eq!(inbound.text, "hello");
        assert_eq!(inbound.message_id, "msg-1");
        assert_eq!(
            inbound.raw_payload["transport"]["metadata"]["push_token"],
            "secret-token"
        );
        let turn_payload =
            build_channel_turn_request(&inbound, &payload, Some("conv-android-1"), None);
        assert_eq!(turn_payload["conversation"]["channel"], "android");
        assert_eq!(turn_payload["conversation"]["handle"], "conv-android-1");
        assert!(turn_payload["transport"]["metadata"]["push_token"].is_null());
    }
}

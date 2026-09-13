use super::*;
use crate::cloud_relay::CloudRelayClient;
use async_trait::async_trait;
use axum::{
    body::Bytes,
    extract::{OriginalUri, State},
    http::Method,
    response::{IntoResponse, Response},
    routing::any,
    Router,
};
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Mutex,
};
use tempfile::tempdir;

#[derive(Default)]
struct CloudState {
    owners: BTreeMap<String, bool>,
    requests: BTreeMap<String, Value>,
    turns: Vec<(String, Value)>,
    route_updates: Vec<(String, Value)>,
    notification: Option<Value>,
    notification_receipts: Vec<Value>,
    notification_denied: bool,
    permanently_denied_text: Option<String>,
    lose_notification_receipt: bool,
    media_bytes: Vec<u8>,
    media_denied: bool,
    media_reads: usize,
    ownership_revoked: bool,
}

struct CloudFixture {
    state: Arc<Mutex<CloudState>>,
    relay: CloudRelayClient,
    task: tokio::task::JoinHandle<()>,
}
impl Drop for CloudFixture {
    fn drop(&mut self) {
        self.task.abort();
    }
}

async fn cloud_http(
    State(state): State<Arc<Mutex<CloudState>>>,
    method: Method,
    OriginalUri(uri): OriginalUri,
    bytes: Bytes,
) -> Response {
    let segments: Vec<_> = uri.path().split('/').collect();
    assert_eq!(&segments[1..4], &["v1", "internal", "beacon-exchanges"]);
    let hub = segments[4];
    let identity = if hub == "navi-a" { "a" } else { "b" }.repeat(64);
    let mut state = state.lock().unwrap();
    if state.ownership_revoked {
        return (
            StatusCode::CONFLICT,
            axum::Json(json!({"error":{"code":"HUB_IDENTITY_CHANGED"}})),
        )
            .into_response();
    }
    if method == Method::POST {
        let request: Value = serde_json::from_slice(&bytes).unwrap();
        let operation = request["operation"].as_str().unwrap();
        if operation != "bindingProof" {
            assert_eq!(request["hubIdentity"], identity);
        }
        let id = request["requestId"].as_str().unwrap();
        let body = &request["body"];
        let mut http_status = 200;
        let response = match operation {
            "bindingProof" => {
                assert_eq!(body["recipient"], "15555550101");
                assert!(body["route_key"].as_str().unwrap().starts_with("gw_route_"));
                assert_eq!(body["token"].as_str().unwrap().len(), 64);
                let bound = state.owners.get(hub) == Some(&true);
                let session = if hub == "navi-a" { "a" } else { "b" }.repeat(32);
                json!({"status":if bound {"bound"} else {"awaiting_owner_confirmation"},
                    "session_id":session,"binding_id":session,"confirmed_at":chrono::Utc::now().timestamp(),
                    "expires_at":chrono::Utc::now().timestamp()+300})
            }
            "turn" => {
                state.turns.push((hub.into(), body.clone()));
                json!({"turn":{"turn_id":body["turn"]["turn_id"],"status":"completed"},
                    "conversation":{"handle":format!("opaque-{hub}")},
                    "active_frame":{"frame_id":format!("frame-{hub}"),"continuation_token":format!("token-{hub}")},
                    "reply":{"kind":"conversation","text":format!("Reply from {hub}")},"artifacts":[]})
            }
            "deliveryAuthorization" => {
                assert_eq!(body["conversation_handle"], format!("opaque-{hub}"));
                if state.notification_denied
                    || state.permanently_denied_text.as_deref() == body["text"].as_str()
                {
                    return (
                        StatusCode::FORBIDDEN,
                        axum::Json(json!({"error":{"code":"IM_DELIVERY_NOT_ALLOWED"}})),
                    )
                        .into_response();
                }
                json!({"allowed":true})
            }
            "notificationOutbox" => {
                let items=state.notification.clone().map(|payload|json!({"queue_id":payload["notification"]["notification_id"],"payload":payload})).into_iter().collect::<Vec<_>>();
                json!({"items":items})
            }
            "notificationReceipt" => {
                state.notification_receipts.push(body["receipt"].clone());
                if state.lose_notification_receipt {
                    return (
                        StatusCode::SERVICE_UNAVAILABLE,
                        axum::Json(json!({"error":{"code":"FIXTURE_ACK_LOST"}})),
                    )
                        .into_response();
                }
                json!({"acknowledged":true})
            }
            "mediaArtifact" => {
                state.media_reads += 1;
                assert_eq!(body["artifact_id"], "motion-frame.jpg");
                if state.media_denied {
                    http_status = 403;
                    json!({"error":{"code":"MEDIA_NOT_AVAILABLE"}})
                } else {
                    use base64::Engine as _;
                    let range = body["range"]
                        .as_str()
                        .unwrap()
                        .strip_prefix("bytes=")
                        .unwrap();
                    let (start, end) = range.split_once('-').unwrap();
                    let start = start.parse::<usize>().unwrap();
                    let end = end.parse::<usize>().unwrap();
                    let data = &state.media_bytes[start..(end + 1).min(state.media_bytes.len())];
                    http_status = 206;
                    json!({"dataBase64":base64::engine::general_purpose::STANDARD.encode(data),"bytes":data.len(),
                        "artifactOffset":start,"totalBytes":state.media_bytes.len(),"contentType":"image/jpeg","sha256":format!("{:x}",Sha256::digest(data))})
                }
            }
            "bindingRoute" => {
                state.route_updates.push((hub.into(), body.clone()));
                json!({"binding_id":body["binding_id"],"generation":body["generation"],"revision":body["revision"],"status":body["status"],"applied":true})
            }
            _ => panic!("unexpected operation"),
        };
        let mut receipt = json!({"requestId":id, "hubIdentity":identity,"status":"complete",
            "deadlineUnixMs":chrono::Utc::now().timestamp_millis()+request["ttlMs"].as_i64().unwrap(),
            "result":{"status":"complete","httpStatus":http_status,"body":response}});
        assert!(state
            .requests
            .insert(format!("{hub}/{id}"), receipt.clone())
            .is_none());
        receipt["status"] = json!("pending");
        receipt.as_object_mut().unwrap().remove("result");
        (StatusCode::ACCEPTED, axum::Json(receipt)).into_response()
    } else {
        assert_eq!(method, Method::GET);
        (
            StatusCode::OK,
            axum::Json(state.requests[&format!("{hub}/{}", segments[5])].clone()),
        )
            .into_response()
    }
}

impl CloudFixture {
    async fn start() -> Self {
        let state = Arc::new(Mutex::new(CloudState::default()));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let relay =
            CloudRelayClient::for_fixture(&format!("http://{}", listener.local_addr().unwrap()));
        let app = Router::new()
            .fallback(any(cloud_http))
            .with_state(state.clone());
        let task = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        Self { state, relay, task }
    }
}

struct PhoneFixture {
    normalizer: WhatsAppAdapter,
    attempts: AtomicUsize,
    sent: Mutex<Vec<OutboundMessage>>,
}
#[async_trait]
impl PlatformAdapter for PhoneFixture {
    fn name(&self) -> &str {
        "whatsapp"
    }
    fn normalize_inbound(&self, payload: Value) -> Result<InboundMessage, GatewayError> {
        self.normalizer.normalize_inbound(payload)
    }
    async fn send_outbound(&self, mut outbound: OutboundMessage) -> Result<Value, GatewayError> {
        if self.attempts.fetch_add(1, Ordering::SeqCst) == 0 {
            return Err(GatewayError::infrastructure("temporary provider timeout"));
        }
        if let Some(attachment) = outbound.attachments.first() {
            let path = attachment["path"]
                .as_str()
                .expect("remote media must be materialized in Gate's cache");
            let bytes = std::fs::read(path).expect("provider can open the authorized cache file");
            outbound.metadata.insert(
                "fixture_media_sha256".into(),
                json!(format!("{:x}", Sha256::digest(&bytes))),
            );
        }
        self.sent.lock().unwrap().push(outbound);
        Ok(json!({"provider_message_id":"fixture-sent"}))
    }
    fn profile(&self) -> Value {
        json!({"adapter_name":"whatsapp"})
    }
}

fn incoming(id: &str, text: &str) -> Value {
    json!({"phone_number_id":"15555550100", "message":{"from":"15555550101", "id":id,
        "timestamp":chrono::Utc::now().timestamp().to_string(),"text":{"body":text}}})
}
async fn next_second() {
    tokio::time::sleep(std::time::Duration::from_millis(1100)).await;
}

fn notification_poll_due(config: &AppConfig) {
    let path = config.state_dir.join("navi-routes/routes.json");
    let mut value: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    for route in value["routes"].as_object_mut().unwrap().values_mut() {
        route["notification_next_poll"] = json!(0);
    }
    std::fs::write(path, serde_json::to_vec(&value).unwrap()).unwrap();
}

#[tokio::test]
async fn remote_notification_retries_provider_and_lost_receipt_without_resending_after_restart() {
    let directory = tempdir().unwrap();
    let mut config = AppConfig::from_env();
    config.data_dir = directory.path().join("sessions");
    config.state_dir = directory.path().join("state");
    config.cloud_relay_url.clear();
    config.cloud_relay_region.clear();
    let cloud = CloudFixture::start().await;
    let adapter = Arc::new(PhoneFixture {
        normalizer: WhatsAppAdapter::new(
            WhatsAppConfig {
                phone_number_id: "15555550100".into(),
                business_number: "15555550100".into(),
                app_secret: "fixture-app-secret".into(),
                verify_token: "v".repeat(32),
                access_token: "fixture".into(),
                graph_version: "v23.0".into(),
            },
            directory.path().join("phone"),
        ),
        attempts: AtomicUsize::new(0),
        sent: Mutex::new(vec![]),
    });
    let gateway = || {
        let mut service = GatewayService::from_config(&config).unwrap();
        service.fleet = Some(NaviFleet::new(
            cloud.relay.clone(),
            config.state_dir.join("navi-routes"),
        ));
        service.adapters.insert("whatsapp".into(), adapter.clone());
        service
    };
    let first = gateway();
    cloud
        .state
        .lock()
        .unwrap()
        .owners
        .insert("navi-a".into(), true);
    first
        .handle_inbound(
            "whatsapp",
            incoming(
                "notification-proof",
                &format!("NAVI navi-a.{}", "a".repeat(64)),
            ),
        )
        .await
        .unwrap();
    first.refresh_navi_binding().await.unwrap();
    let route = adapter
        .normalizer
        .normalize_inbound(incoming("route-coordinate", "hello"))
        .unwrap()
        .route_key;
    let payload = json!({"notification":{"notification_id":"notif_fixture","trace_id":"trace_notif_fixture","event_type":"camera.motion"},
        "conversation":{"handle":"opaque-navi-a"},"destination":{"route_key":route},"reply":{"kind":"tool_result","text":"Motion detected"},
        "artifacts":[],"delivery_hints":[],"delivery":{"mode":"send","idempotency_key":"idem_notif_fixture","reply_to_message_id":"","update_message_id":""}});
    cloud.state.lock().unwrap().notification = Some(payload.clone());
    assert_eq!(
        first
            .handle_notification_delivery(payload)
            .await
            .unwrap_err()
            .code,
        "NAVI_REMOTE_NOTIFICATION_REQUIRED"
    );
    assert_eq!(first.poll_navi_notifications().await.unwrap(), 1);
    assert_eq!(adapter.attempts.load(Ordering::SeqCst), 1);
    assert_eq!(
        cloud.state.lock().unwrap().notification_receipts[0]["retryable"],
        true
    );
    drop(first);
    let restarted = gateway();
    restarted.retry_pending_deliveries().await.unwrap();
    assert_eq!(adapter.attempts.load(Ordering::SeqCst), 2);
    assert_eq!(adapter.sent.lock().unwrap().len(), 1);
    cloud.state.lock().unwrap().lose_notification_receipt = true;
    notification_poll_due(&config);
    assert!(restarted.poll_navi_notifications().await.is_err());
    cloud.state.lock().unwrap().lose_notification_receipt = false;
    drop(restarted);
    let restarted = gateway();
    notification_poll_due(&config);
    assert_eq!(restarted.poll_navi_notifications().await.unwrap(), 1);
    assert_eq!(adapter.attempts.load(Ordering::SeqCst), 2);
    assert_eq!(adapter.sent.lock().unwrap().len(), 1);
    assert_eq!(
        cloud
            .state
            .lock()
            .unwrap()
            .notification_receipts
            .last()
            .unwrap()["ok"],
        true
    );
    // A queued retry cannot send after its live notification permission is revoked.
    let mut next = cloud.state.lock().unwrap().notification.clone().unwrap();
    next["notification"]["notification_id"] = json!("notif_revoked");
    next["delivery"]["idempotency_key"] = json!("idem_revoked");
    next["reply"]["text"] = json!("Revoked notice fixture");
    {
        let mut state = cloud.state.lock().unwrap();
        state.notification = Some(next);
        state.notification_denied = true;
        state.permanently_denied_text = Some("Revoked notice fixture".into());
    }
    notification_poll_due(&config);
    let _ = restarted.poll_navi_notifications().await;
    restarted.retry_pending_deliveries().await.unwrap();
    assert_eq!(adapter.attempts.load(Ordering::SeqCst), 2);
    // A finalized event's standard media hint uses the same authenticated transfer
    // and receives a single delivery receipt for text + media.
    let mut media = cloud.state.lock().unwrap().notification.clone().unwrap();
    media["notification"]["notification_id"] = json!("notif_media");
    media["delivery"]["idempotency_key"] = json!("idem_media");
    media["reply"]["text"] = json!("Media notice fixture");
    media["artifacts"] = json!([{"artifact_id":"motion-frame.jpg","kind":"image","mime_type":"image/jpeg", "url":"/api/cameras/recordings/artifacts/motion-frame.jpg?media_context=chat"}]);
    media["delivery_hints"] =
        json!([{"kind":"native_image","artifact_id":"motion-frame.jpg","max_items":1}]);
    let bytes = vec![0xff, 0xd8, 0xff, 0xe0, 1, 2, 3, 4, 0xff, 0xd9];
    {
        let mut state = cloud.state.lock().unwrap();
        state.notification_denied = false;
        state.media_bytes = bytes.clone();
        state.notification = Some(media.clone());
    }
    notification_poll_due(&config);
    assert_eq!(restarted.poll_navi_notifications().await.unwrap(), 1);
    let attempts = adapter.attempts.load(Ordering::SeqCst);
    assert_eq!(attempts, 4);
    assert_eq!(
        adapter.sent.lock().unwrap().last().unwrap().metadata["fixture_media_sha256"],
        format!("{:x}", Sha256::digest(&bytes))
    );
    assert!(
        cloud.state.lock().unwrap().media_reads > 1,
        "transfer and provider-stage permission probes both run"
    );
    notification_poll_due(&config);
    assert_eq!(restarted.poll_navi_notifications().await.unwrap(), 1);
    assert_eq!(
        adapter.attempts.load(Ordering::SeqCst),
        attempts,
        "completed media is never resent on receipt replay"
    );
    media["notification"]["notification_id"] = json!("notif_media_revoked");
    media["delivery"]["idempotency_key"] = json!("idem_media_revoked");
    {
        let mut state = cloud.state.lock().unwrap();
        state.media_denied = true;
        state.notification = Some(media);
    }
    notification_poll_due(&config);
    let _ = restarted.poll_navi_notifications().await;
    restarted.retry_pending_deliveries().await.unwrap();
    assert_eq!(
        adapter
            .sent
            .lock()
            .unwrap()
            .iter()
            .filter(|message| !message.attachments.is_empty())
            .count(),
        1,
        "a separate text grant cannot authorize the revoked media at its provider stage"
    );
    // A provider retry remains on disk when ownership changes. A fresh Cloud
    // identity rejection clears the persisted fleet route, including on restart.
    {
        let mut state = cloud.state.lock().unwrap();
        let mut queued = state.notification.clone().unwrap();
        queued["notification"]["notification_id"] = json!("notif_ownership_reset");
        queued["delivery"]["idempotency_key"] = json!("idem_ownership_reset");
        queued["reply"]["text"] = json!("Previous household queued text");
        queued["artifacts"] = json!([]);
        queued["delivery_hints"] = json!([]);
        state.notification = Some(queued);
        state.media_denied = false;
    }
    adapter.attempts.store(0, Ordering::SeqCst);
    notification_poll_due(&config);
    assert_eq!(restarted.poll_navi_notifications().await.unwrap(), 1);
    assert_eq!(adapter.attempts.load(Ordering::SeqCst), 1);
    cloud.state.lock().unwrap().ownership_revoked = true;
    notification_poll_due(&config);
    assert_eq!(
        restarted.poll_navi_notifications().await.unwrap_err().code,
        "NAVI_IDENTITY_CHANGED"
    );
    drop(restarted);
    let restarted = gateway();
    cloud.state.lock().unwrap().ownership_revoked = false;
    restarted.retry_pending_deliveries().await.unwrap();
    assert_eq!(restarted.poll_navi_notifications().await.unwrap(), 0);
    assert_eq!(adapter.attempts.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn phone_and_owner_select_remote_navi_without_replaying_old_household_work_after_restart() {
    let directory = tempdir().unwrap();
    let mut config = AppConfig::from_env();
    config.data_dir = directory.path().join("sessions");
    config.state_dir = directory.path().join("state");
    config.cloud_relay_url.clear();
    config.cloud_relay_region.clear();
    let cloud = CloudFixture::start().await;
    let adapter = Arc::new(PhoneFixture {
        normalizer: WhatsAppAdapter::new(
            crate::adapters::whatsapp::WhatsAppConfig {
                phone_number_id: "15555550100".into(),
                business_number: "15555550100".into(),
                app_secret: "fixture-app-secret".into(),
                verify_token: "v".repeat(32),
                access_token: "fixture".into(),
                graph_version: "v23.0".into(),
            },
            directory.path().join("phone"),
        ),
        attempts: AtomicUsize::new(0),
        sent: Mutex::new(vec![]),
    });
    let gateway = || {
        let mut service = GatewayService::from_config(&config).unwrap();
        service.fleet = Some(NaviFleet::new(
            cloud.relay.clone(),
            config.state_dir.join("navi-routes"),
        ));
        service.adapters.insert("whatsapp".into(), adapter.clone());
        service
    };
    let first = gateway();
    let proof_a = incoming("proof-a", &format!("NAVI navi-a.{}", "a".repeat(64)));
    assert_eq!(
        first
            .handle_inbound("whatsapp", proof_a.clone())
            .await
            .unwrap()["accepted"],
        true
    );
    assert!(first.refresh_navi_binding().await.unwrap());
    assert!(first
        .handle_inbound("whatsapp", incoming("before-owner", "My devices"))
        .await
        .is_err());
    assert!(cloud.state.lock().unwrap().turns.is_empty());
    cloud
        .state
        .lock()
        .unwrap()
        .owners
        .insert("navi-a".into(), true);
    next_second().await;
    assert!(first.refresh_navi_binding().await.unwrap());
    next_second().await;
    let old_message = incoming("home-a-queued", "My devices");
    // The real delivery planner persists the failed send for recovery.
    let _ = first.handle_inbound("whatsapp", old_message.clone()).await;
    assert_eq!(adapter.attempts.load(Ordering::SeqCst), 1);
    assert_eq!(first.store.retryable_delivery_plans().unwrap().len(), 1);
    first
        .handle_inbound(
            "whatsapp",
            incoming("proof-b", &format!("NAVI navi-b.{}", "b".repeat(64))),
        )
        .await
        .unwrap();
    first.retry_pending_deliveries().await.unwrap();
    assert_eq!(adapter.attempts.load(Ordering::SeqCst), 1); // Pending switch pauses the old home.
    assert!(first.refresh_navi_binding().await.unwrap());
    cloud
        .state
        .lock()
        .unwrap()
        .owners
        .insert("navi-b".into(), true);
    next_second().await;
    assert!(first.refresh_navi_binding().await.unwrap());
    drop(first);
    let restarted = gateway();
    restarted.retry_pending_deliveries().await.unwrap();
    assert_eq!(adapter.attempts.load(Ordering::SeqCst), 1);
    // A consumed phone proof cannot restore an old device selection.
    restarted.handle_inbound("whatsapp", proof_a).await.unwrap();
    assert!(restarted.refresh_navi_binding().await.unwrap()); // The other home's receipt survived restart.
    assert!(!restarted.refresh_navi_binding().await.unwrap());
    assert!(restarted
        .handle_inbound("whatsapp", old_message)
        .await
        .is_err());
    next_second().await;
    restarted
        .handle_inbound("whatsapp", incoming("home-b-current", "My devices"))
        .await
        .unwrap();
    next_second().await;
    restarted
        .handle_inbound("whatsapp", incoming("home-b-next", "And now?"))
        .await
        .unwrap();
    let state = cloud.state.lock().unwrap();
    assert_eq!(state.turns.len(), 3);
    assert_eq!(state.route_updates.len(), 4);
    assert!(state
        .route_updates
        .iter()
        .any(|(hub, update)| hub == "navi-a" && update["status"] == "retired"));
    assert!(state
        .route_updates
        .iter()
        .any(|(hub, update)| hub == "navi-b" && update["status"] == "active"));
    assert_eq!(state.turns[0].0, "navi-a");
    assert_eq!(state.turns[1].0, "navi-b");
    assert!(state.turns[1].1["conversation"]["handle"].is_null());
    assert!(state.turns[1].1["continuation"].is_null());
    assert_eq!(state.turns[2].1["conversation"]["handle"], "opaque-navi-b");
    assert_eq!(state.turns[2].1["continuation"]["token"], "token-navi-b");
    let sent = adapter.sent.lock().unwrap();
    assert_eq!(sent.len(), 2);
    for message in sent.iter() {
        assert_eq!(message.text, "Reply from navi-b");
    }
}

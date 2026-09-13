use super::*;
use crate::{harborbeacon::HarborBeaconTaskClient, models::OutboundMessage};
use axum::{
    body::Bytes,
    extract::{OriginalUri, State},
    http::HeaderMap,
    response::{IntoResponse, Response},
    routing::any,
    Router,
};
use std::collections::BTreeMap;
use tempfile::tempdir;

fn credentials() -> SharedCredentialsProvider {
    SharedCredentialsProvider::new(Credentials::new(
        "ASIAFIXTUREEXAMPLE",
        "fixture-secret-only",
        Some("fixture-session-token".into()),
        Some(SystemTime::now() + Duration::from_secs(3600)),
        "RelayFixture",
    ))
}

#[derive(Default)]
struct StateData {
    posts: Vec<(String, Vec<u8>)>,
    gets: usize,
    redirects: usize,
    requests: BTreeMap<String, (Value, i64)>,
    behavior: &'static str,
    auth_status: u16,
    media: Vec<u8>,
    media_denied: bool,
    media_fault: &'static str,
}

struct Fixture {
    client: CloudRelayClient,
    state: Arc<Mutex<StateData>>,
    server: tokio::task::JoinHandle<()>,
}
impl Drop for Fixture {
    fn drop(&mut self) {
        self.server.abort();
    }
}

async fn handler(
    State(state): State<Arc<Mutex<StateData>>>,
    method: Method,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
    bytes: Bytes,
) -> Response {
    let mut state = state.lock().await;
    if uri.path() == "/should-never-follow" {
        state.redirects += 1;
        return StatusCode::OK.into_response();
    }
    let auth = headers["authorization"].to_str().unwrap();
    assert!(auth.starts_with("AWS4-HMAC-SHA256 Credential=ASIAFIXTUREEXAMPLE/"));
    assert!(auth.contains("/us-east-1/execute-api/aws4_request"));
    assert_eq!(headers["x-amz-security-token"], "fixture-session-token");
    assert!(headers["x-amz-date"].to_str().unwrap().ends_with('Z'));
    assert!(!headers.contains_key("x-contract-version")); // v2 stays inside the device HTTP seam.
    assert!(!auth.contains("fixture-secret-only"));
    let segments: Vec<_> = uri.path().split('/').collect();
    assert_eq!(&segments[1..4], &["v1", "internal", "beacon-exchanges"]);
    let hub = segments[4];
    let (id, request, deadline) = if method == Method::POST {
        let request: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(
            request.as_object().unwrap().len(),
            if request.get("hubIdentity").is_some() {
                5
            } else {
                4
            }
        );
        if request
            .get("hubIdentity")
            .is_some_and(|identity| identity.as_str() != Some("a".repeat(64).as_str()))
        {
            return (
                StatusCode::CONFLICT,
                axum::Json(json!({"error":{"code":"HUB_IDENTITY_CHANGED"}})),
            )
                .into_response();
        }
        let id = request["requestId"].as_str().unwrap().to_owned();
        let deadline = Utc::now().timestamp_millis() + request["ttlMs"].as_i64().unwrap();
        let (stored, deadline) = state
            .requests
            .entry(format!("{hub}/{id}"))
            .or_insert((request.clone(), deadline))
            .clone();
        assert_eq!(request, stored);
        state.posts.push((hub.into(), bytes.to_vec()));
        (id, request, deadline)
    } else {
        assert_eq!(method, Method::GET);
        assert!(bytes.is_empty());
        state.gets += 1;
        let id = segments[5].to_owned();
        let (request, deadline) = state.requests.get(&format!("{hub}/{id}")).unwrap().clone();
        (id, request, deadline)
    };
    if state.behavior == "redirect" {
        return (
            StatusCode::TEMPORARY_REDIRECT,
            [("location", "/should-never-follow")],
        )
            .into_response();
    }
    if state.behavior == "oversized" {
        return "x".repeat(MAX_RESPONSE + 1).into_response();
    }
    if state.behavior == "invalid-json" {
        return "provider-private-response".into_response();
    }
    if state.behavior == "service-forbidden" {
        return StatusCode::FORBIDDEN.into_response();
    }
    if state.behavior == "recovery" && state.posts.len() == 1 {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    let pending = method == Method::POST
        || state.behavior == "pending"
        || (state.behavior == "recovery" && state.posts.len() < 3);
    let status = if pending {
        StatusCode::ACCEPTED
    } else {
        StatusCode::OK
    };
    let mut payload = json!({"requestId":id,"hubIdentity":"a".repeat(64),"deadlineUnixMs":deadline,"status":if pending {"pending"} else {"complete"}});
    if !pending {
        let authorization = request["operation"] == "deliveryAuthorization";
        let mut status = if authorization {
            state.auth_status
        } else {
            200
        };
        let mut body = if authorization {
            json!({"allowed":status == 200})
        } else {
            json!({"hub":hub,"echo":request["body"],
            "turn":{"turn_id":request["body"]["turn"]["turn_id"],"status":"completed"},
            "conversation":{"handle":format!("opaque-{hub}")}, "active_frame":{"frame_id":format!("frame-{hub}"),"continuation_token":format!("opaque-token-{hub}")},
            "reply":{"kind":"conversation","text":"actual remote reply"},"artifacts":[]})
        };
        if request["operation"] == "mediaArtifact" {
            use base64::Engine as _;
            use sha2::{Digest, Sha256};
            assert_eq!(request["hubIdentity"], "a".repeat(64));
            let range = request["body"]["range"]
                .as_str()
                .unwrap()
                .strip_prefix("bytes=")
                .unwrap();
            let (start, end) = range.split_once('-').unwrap();
            let (start, end) = (
                start.parse::<usize>().unwrap(),
                end.parse::<usize>().unwrap(),
            );
            if state.media_denied || (state.media_fault == "revoke-second" && start > 0) {
                status = 403;
                body = json!({"error":{"code":"MEDIA_NOT_AVAILABLE"}});
            } else {
                assert!(
                    start < state.media.len(),
                    "client must not probe beyond the declared end"
                );
                let data = &state.media[start..(end + 1).min(state.media.len())];
                status = 206;
                body = json!({"dataBase64":base64::engine::general_purpose::STANDARD.encode(data),
                    "bytes":data.len(),"artifactOffset":start,"totalBytes":state.media.len(),
                    "contentType":"image/jpeg","sha256":format!("{:x}",Sha256::digest(data))});
                match state.media_fault {
                    "truncated" => {
                        body["dataBase64"] = json!(base64::engine::general_purpose::STANDARD
                            .encode(&data[..data.len() - 1]))
                    }
                    "wrong-offset" => body["artifactOffset"] = json!(start + 1),
                    "wrong-hash" => body["sha256"] = json!("0".repeat(64)),
                    "wrong-mime" => body["contentType"] = json!("text/html"),
                    "wrong-total" if start > 0 => body["totalBytes"] = json!(state.media.len() + 1),
                    _ => {}
                }
            }
        }
        payload["result"] = json!({"status":"complete","httpStatus":status,"body":body});
    }
    match state.behavior {
        "wrong-id" => payload["requestId"] = json!("someone-elses-result"),
        "changed-deadline" if !pending => payload["deadlineUnixMs"] = json!(deadline + 5000),
        "changed-identity" if !pending => payload["hubIdentity"] = json!("b".repeat(64)),
        "expired" => payload["deadlineUnixMs"] = json!(Utc::now().timestamp_millis() - 1),
        "pending-result" => payload["result"] = json!({"body":{"allowed":true}}),
        "redirect-result" if !pending => payload["result"]["httpStatus"] = json!(302),
        "failed" if !pending => {
            payload["result"] = json!({"status":"failed","error":{"code":"RELAY_BUSY"}})
        }
        _ => {}
    }
    (status, axum::Json(payload)).into_response()
}

async fn fixture(behavior: &'static str) -> Fixture {
    let state = Arc::new(Mutex::new(StateData {
        behavior,
        auth_status: 200,
        ..Default::default()
    }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = Url::parse(&format!("http://{}/", listener.local_addr().unwrap())).unwrap();
    let app = Router::new()
        .fallback(any(handler))
        .with_state(state.clone());
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    Fixture {
        client: CloudRelayClient::create(endpoint, "us-east-1", credentials()).unwrap(),
        state,
        server,
    }
}

fn turn() -> Value {
    json!({"turn":{"turn_id":"turn-original", "trace_id":"trace-original", "occurred_at":"2026-09-12T11:00:00Z"},
        "actor":{"user_id":"15555550101","workspace_id":"untrusted-home-hint"},
        "conversation":{"channel":"whatsapp","handle":"opaque-original"},
        "transport":{"route_key":"whatsapp:official-number:15555550101","message_id":"original-provider-id"},
        "input":{"text":"我家有哪些设备？","parts":[]},"continuation":{"token":"opaque-original-token"}})
}

#[tokio::test]
async fn relay_validates_destination_scope_before_any_network_call() {
    for endpoint in [
        "http://abc.execute-api.us-east-1.amazonaws.com",
        "https://localhost",
        "https://abc.execute-api.us-east-1.amazonaws.com.evil.test",
        "https://abc.execute-api.us-west-2.amazonaws.com",
        "https://abc.execute-api.us-east-1.amazonaws.com/admin",
        "https://abc.execute-api.us-east-1.amazonaws.com/?url=elsewhere",
        "https://user@abc.execute-api.us-east-1.amazonaws.com",
        "https://abc.execute-api.us-east-1.amazonaws.com:444",
    ] {
        assert!(
            CloudRelayClient::new(endpoint, "us-east-1", credentials()).is_err(),
            "{endpoint}"
        );
    }
    assert!(CloudRelayClient::new(
        "https://abc.execute-api.us-east-1.amazonaws.com/",
        "us-east-1",
        credentials()
    )
    .is_ok());
    let f = fixture("").await;
    for hub in ["..", "a/b", "a?b", "a#b", "a%2fb"] {
        assert!(f.client.turn(hub, &turn()).await.is_err());
    }
    let mut body = turn();
    body["conversation"]["channel"] = json!("web");
    assert!(f.client.turn("navi-a", &body).await.is_err());
    body = turn();
    body["input"]["text"] = json!("x".repeat(70_000));
    assert!(f.client.turn("navi-a", &body).await.is_err());
    assert!(f.state.lock().await.posts.is_empty());
}

#[tokio::test]
async fn two_navi_clients_keep_v2_requests_results_and_opaque_continuations_separate() {
    let f = fixture("").await;
    let a = HarborBeaconTaskClient::from_cloud_relay(f.client.clone(), "navi-a").unwrap();
    let b = HarborBeaconTaskClient::from_cloud_relay(f.client.clone(), "navi-b").unwrap();
    let (a, b) = tokio::join!(a.submit_turn_payload(turn()), b.submit_turn_payload(turn()));
    let a = a.unwrap();
    let b = b.unwrap();
    assert_eq!(a.response_payload["hub"], "navi-a");
    assert_eq!(b.response_payload["hub"], "navi-b");
    assert_eq!(a.response_payload["echo"], turn());
    assert_eq!(b.response_payload["echo"], turn());
    assert_eq!(a.conversation_handle.as_deref(), Some("opaque-navi-a"));
    assert_eq!(b.conversation_handle.as_deref(), Some("opaque-navi-b"));
    assert_eq!(a.text, "actual remote reply");
    assert_eq!(b.text, "actual remote reply");
    assert_ne!(a.continuation, b.continuation);
    let state = f.state.lock().await;
    assert_eq!(state.posts.len(), 2);
    assert_eq!(state.requests.len(), 2);
}

#[tokio::test]
async fn lost_publish_and_receipt_retry_identical_exchange_without_extending_deadline() {
    let f = fixture("recovery").await;
    let result = f.client.turn("navi-a", &turn()).await.unwrap();
    assert_eq!(result.body["hub"], "navi-a");
    let state = f.state.lock().await;
    assert_eq!(state.posts.len(), 3);
    assert!(state.posts.iter().all(|post| post == &state.posts[0]));
    assert_eq!(state.requests.len(), 1);
    assert!(state.gets >= 2);
}

#[tokio::test]
async fn selected_device_identity_is_sent_on_new_exchanges_and_replacement_is_terminal() {
    let f = fixture("").await;
    let selected = f.client.with_hub_identity(&"a".repeat(64)).unwrap();
    assert_eq!(
        selected.turn("navi-a", &turn()).await.unwrap().hub_identity,
        "a".repeat(64)
    );
    let changed = f.client.with_hub_identity(&"b".repeat(64)).unwrap();
    let error = changed.turn("navi-a", &turn()).await.err().unwrap();
    assert_eq!(error.code, "NAVI_IDENTITY_CHANGED");
    assert_eq!(error.status, StatusCode::FORBIDDEN);
    let state = f.state.lock().await;
    assert_eq!(state.posts.len(), 1);
    assert_eq!(
        serde_json::from_slice::<Value>(&state.posts[0].1).unwrap()["hubIdentity"],
        "a".repeat(64)
    );
}

#[tokio::test]
async fn each_delivery_check_observes_new_permission_in_a_new_exchange() {
    let f = fixture("").await;
    let client = HarborBeaconTaskClient::from_cloud_relay(f.client.clone(), "navi-a").unwrap();
    let outbound = OutboundMessage {
        platform: "whatsapp".into(),
        chat_id: "15555550101".into(),
        text: "Private home answer".into(),
        attachments: vec![],
        timestamp: crate::models::utc_now_iso(),
        metadata: json!({"conversation_handle":"opaque-original","route_key":"original-route"})
            .as_object()
            .unwrap()
            .clone(),
    };
    client.authorize_whatsapp_delivery(&outbound).await.unwrap();
    f.state.lock().await.auth_status = 403;
    assert_eq!(
        client
            .authorize_whatsapp_delivery(&outbound)
            .await
            .unwrap_err()
            .code,
        "IM_DELIVERY_NOT_ALLOWED"
    );
    let state = f.state.lock().await;
    assert_eq!(state.requests.len(), 2);
    for (request, _) in state.requests.values() {
        assert_eq!(request["ttlMs"], 5000);
        assert_eq!(request["operation"], "deliveryAuthorization");
        assert_eq!(request["body"]["conversation_handle"], "opaque-original");
    }
}

#[tokio::test]
async fn malformed_mixed_expired_and_redirected_receipts_never_authorize_or_leak_raw_errors() {
    for behavior in [
        "wrong-id",
        "changed-deadline",
        "changed-identity",
        "expired",
        "pending-result",
        "redirect-result",
        "redirect",
        "oversized",
        "invalid-json",
        "service-forbidden",
        "failed",
    ] {
        let f = fixture(behavior).await;
        let start = Instant::now();
        let error = f
            .client
            .authorize_delivery("navi-a", &json!({"conversation_handle":"private-handle"}))
            .await
            .err()
            .expect(behavior);
        assert!(start.elapsed() < Duration::from_secs(2), "{behavior}");
        assert!(!error.to_string().contains("private-handle"));
        assert!(!error.to_string().contains("provider-private-response"));
        assert_eq!(f.state.lock().await.redirects, 0);
    }
}

#[tokio::test]
async fn authorization_wait_expires_within_its_total_five_second_budget() {
    let f = fixture("pending").await;
    let start = Instant::now();
    assert!(f
        .client
        .authorize_delivery("navi-a", &json!({"recipient":"15555550101"}))
        .await
        .is_err());
    assert!(start.elapsed() >= Duration::from_secs(5) && start.elapsed() < Duration::from_secs(6));
    let state = f.state.lock().await;
    assert!(state.posts.len() >= 2);
    assert!(state.posts.iter().all(|post| post == &state.posts[0]));
}

#[tokio::test]
async fn remote_artifacts_require_a_trusted_artifact_id_and_never_use_gate_loopback() {
    let f = fixture("").await;
    let client = HarborBeaconTaskClient::from_cloud_relay(f.client.clone(), "navi-a").unwrap();
    let artifacts =
        vec![json!({"url":"http://127.0.0.1:8788/api/media/private.jpg","mime_type":"image/jpeg"})];
    assert_eq!(
        client
            .authorize_camera_attachments(&artifacts)
            .await
            .unwrap_err()
            .code,
        "VALIDATION_ERROR"
    );
    let temp = tempdir().unwrap();
    let batch = client
        .materialize_attachments(artifacts, &temp.path().join("cache"), "turn-one")
        .await;
    assert_eq!(batch.failed_count, 1);
    assert!(batch.attachments.is_empty());
    assert!(f.state.lock().await.posts.is_empty());
}

fn media_artifact() -> Value {
    json!({"artifact_id":"snapshot~camera.jpg","url":"/api/cameras/recordings/artifacts/snapshot~camera.jpg?media_context=chat","mime_type":"image/jpeg","kind":"image"})
}

#[tokio::test]
async fn remote_media_reassembles_exact_chunks_and_final_short_chunk_with_fresh_permission_checks()
{
    for size in [1, 49152, 49153, 98304] {
        let f = fixture("").await;
        let bytes: Vec<u8> = (0..size).map(|i| (i % 251) as u8).collect();
        f.state.lock().await.media = bytes.clone();
        let client = HarborBeaconTaskClient::from_cloud_relay(
            f.client.with_hub_identity(&"a".repeat(64)).unwrap(),
            "navi-a",
        )
        .unwrap();
        let artifacts = vec![media_artifact()];
        client
            .authorize_camera_attachments(&artifacts)
            .await
            .unwrap();
        let temp = tempdir().unwrap();
        let batch = client
            .materialize_attachments(artifacts.clone(), &temp.path().join("cache"), "media-turn")
            .await;
        assert_eq!(batch.failed_count, 0);
        assert_eq!(batch.cache_files.len(), 1);
        assert_eq!(std::fs::read(&batch.cache_files[0]).unwrap(), bytes);
        f.state.lock().await.media_denied = true;
        assert_eq!(
            client
                .authorize_camera_attachments(&artifacts)
                .await
                .unwrap_err()
                .code,
            "CAMERA_MEDIA_DELIVERY_NOT_ALLOWED"
        );
    }
}

#[tokio::test]
async fn remote_media_corruption_or_revocation_discards_the_whole_cache_batch() {
    for fault in [
        "truncated",
        "wrong-offset",
        "wrong-hash",
        "wrong-mime",
        "wrong-total",
        "revoke-second",
    ] {
        let f = fixture("").await;
        {
            let mut state = f.state.lock().await;
            state.media = vec![7; 49153];
            state.media_fault = fault;
        }
        let client = HarborBeaconTaskClient::from_cloud_relay(
            f.client.with_hub_identity(&"a".repeat(64)).unwrap(),
            "navi-a",
        )
        .unwrap();
        let temp = tempdir().unwrap();
        let cache = temp.path().join("cache");
        let batch = client
            .materialize_attachments(vec![media_artifact()], &cache, "media-turn")
            .await;
        assert_eq!(batch.failed_count, 1, "{fault}");
        assert!(batch.attachments.is_empty());
        assert!(batch.cache_files.is_empty());
        assert_eq!(std::fs::read_dir(cache).unwrap().count(), 0, "{fault}");
    }
}

#[tokio::test]
async fn media_and_notifications_require_pinned_device_identity() {
    let f = fixture("").await;
    assert!(f
        .client
        .media_artifact(
            "navi-a",
            &json!({"artifact_id":"photo.jpg","range":"bytes=0-0"})
        )
        .await
        .is_err());
    assert!(f
        .client
        .notification_outbox("navi-a", &json!({}))
        .await
        .is_err());
    assert!(f
        .client
        .notification_receipt("navi-a", &json!({}))
        .await
        .is_err());
    assert!(f.state.lock().await.posts.is_empty());
}

#[tokio::test]
#[ignore = "requires the isolated Cloud and actual Link media runner"]
async fn actual_cloud_link_media_roundtrip() {
    use sha2::{Digest, Sha256};
    let endpoint =
        Url::parse(&std::env::var("HARBORGATE_MEDIA_FIXTURE_URL").expect("fixture URL")).unwrap();
    assert_eq!(endpoint.scheme(), "http");
    assert_eq!(endpoint.host_str(), Some("127.0.0.1"));
    let relay = CloudRelayClient::create(endpoint, "us-east-1", credentials())
        .unwrap()
        .with_hub_identity(&format!("{:x}", Sha256::digest(b"cert-navi-a")))
        .unwrap();
    let client = HarborBeaconTaskClient::from_cloud_relay(relay, "navi-a").unwrap();
    let artifact = |id: &str| {
        json!({"artifact_id":id,"kind":"image","mime_type":"image/jpeg",
        "url":format!("/api/cameras/recordings/artifacts/{id}?media_context=chat")})
    };
    for (id, size) in [("exact.jpg", 98304), ("tail.jpg", 49153)] {
        let artifacts = vec![artifact(id)];
        client
            .authorize_camera_attachments(&artifacts)
            .await
            .unwrap();
        let temp = tempdir().unwrap();
        let batch = client
            .materialize_attachments(artifacts, &temp.path().join("cache"), id)
            .await;
        assert_eq!(batch.failed_count, 0);
        assert_eq!(batch.cache_files.len(), 1);
        let expected: Vec<u8> = (0..size).map(|i| (i % 251) as u8).collect();
        assert_eq!(std::fs::read(&batch.cache_files[0]).unwrap(), expected);
    }
    for id in ["revoke.jpg", "truncated.jpg", "wrong-range.jpg"] {
        let temp = tempdir().unwrap();
        let cache = temp.path().join("cache");
        let batch = client
            .materialize_attachments(vec![artifact(id)], &cache, id)
            .await;
        assert_eq!(batch.failed_count, 1, "{id}");
        assert!(batch.attachments.is_empty());
        assert_eq!(std::fs::read_dir(cache).unwrap().count(), 0);
    }
    assert_eq!(
        client
            .authorize_camera_attachments(&[artifact("denied.jpg")])
            .await
            .unwrap_err()
            .code,
        "CAMERA_MEDIA_DELIVERY_NOT_ALLOWED"
    );
}

#[tokio::test]
async fn task_credentials_refresh_without_redirects_static_keys_or_secret_errors() {
    for relative in [
        "https://other.test",
        "//other.test",
        "/v2/credentials/../other",
        "/v2/credentials/id?token=secret",
        "/v2/credentials/",
    ] {
        assert!(EcsTaskCredentials::new(relative).is_err());
    }
    let state = Arc::new(Mutex::new(0usize));
    let counter = state.clone();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = Url::parse(&format!("http://{}/", listener.local_addr().unwrap())).unwrap();
    let server = tokio::spawn(async move {
        let app = Router::new().route("/", axum::routing::get(move || {
            let counter = counter.clone();
            async move {
                let mut count = counter.lock().await; *count += 1;
                if *count == 3 {return (StatusCode::TEMPORARY_REDIRECT, [("location", "/private-key")]).into_response();}
                axum::Json(json!({"AccessKeyId":format!("fixture-key-{}", *count), "SecretAccessKey":"credential-secret", "Token":"credential-token",
                    "Expiration":(Utc::now()+chrono::Duration::hours(1)).to_rfc3339()})).into_response()
            }
        }));
        axum::serve(listener, app).await.unwrap();
    });
    let mut provider = EcsTaskCredentials::new("/v2/credentials/fixture-id").unwrap();
    provider.url = endpoint;
    let first = provider.load().await.unwrap();
    assert_eq!(
        provider.load().await.unwrap().access_key_id(),
        first.access_key_id()
    );
    assert_eq!(*state.lock().await, 1);
    *provider.cached.lock().await = None;
    assert_ne!(
        provider.load().await.unwrap().access_key_id(),
        first.access_key_id()
    );
    *provider.cached.lock().await = None;
    let error = provider.load().await.unwrap_err().to_string();
    assert!(!error.contains("credential-secret"));
    assert!(!error.contains("credential-token"));
    assert_eq!(*state.lock().await, 3);
    server.abort();
}

// Run explicitly by the local Cloud bundle integration runner. The URL is
// available only to this test, and must be a literal loopback fixture.
#[tokio::test]
#[ignore = "requires the actual Cloud Lambda bundle fixture"]
async fn actual_cloud_bundle_roundtrip() {
    let endpoint =
        Url::parse(&std::env::var("HARBORGATE_RELAY_FIXTURE_URL").expect("fixture URL")).unwrap();
    assert_eq!(endpoint.host_str(), Some("127.0.0.1"));
    assert_eq!(endpoint.scheme(), "http");
    let relay = CloudRelayClient::create(endpoint, "us-east-1", credentials()).unwrap();
    for hub in ["navi-a", "navi-b"] {
        let identity = format!(
            "{:x}",
            <sha2::Sha256 as sha2::Digest>::digest(format!("cert-{hub}").as_bytes())
        );
        let client = HarborBeaconTaskClient::from_cloud_relay(
            relay.with_hub_identity(&identity).unwrap(),
            hub,
        )
        .unwrap();
        let result = client.submit_turn_payload(turn()).await.unwrap();
        assert_eq!(result.response_payload["fixture_hub"], hub);
        assert_eq!(result.task_id, "turn-original");
        assert_eq!(result.text, "Navi device inventory fixture");
        assert_eq!(
            result.conversation_handle.as_deref(),
            Some(format!("opaque-{hub}").as_str())
        );
        let outbound = OutboundMessage {platform:"whatsapp".into(),chat_id:"15555550101".into(),text:result.text,attachments:vec![],
            timestamp:crate::models::utc_now_iso(),metadata:json!({"conversation_handle":result.conversation_handle,"route_key":"original-route"}).as_object().unwrap().clone()};
        client.authorize_whatsapp_delivery(&outbound).await.unwrap();
        assert_eq!(
            client
                .authorize_whatsapp_delivery(&outbound)
                .await
                .unwrap_err()
                .code,
            "IM_DELIVERY_NOT_ALLOWED"
        );
        let receipt=relay.with_hub_identity(&identity).unwrap().binding_route(hub,&json!({"binding_id":"a".repeat(32),
            "recipient":"15555550101","route_key":"original-route","generation":"b".repeat(32),"revision":3,"status":"active",
            "issued_at":Utc::now().timestamp()})).await.unwrap();
        assert_eq!(
            receipt.body,
            json!({"binding_id":"a".repeat(32),"generation":"b".repeat(32),"revision":3,"status":"active","applied":true})
        );
    }
}

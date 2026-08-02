use async_trait::async_trait;
use axum::body::{to_bytes, Body, Bytes};
use axum::extract::{OriginalUri, State};
use axum::http::{HeaderMap, Method, Request, StatusCode};
use axum::response::IntoResponse;
use axum::routing::any;
use axum::{Json, Router};
use harborgate::config::AppConfig;
use harborgate::gateway::GatewayService;
use harborgate::harboros_auth::{HarborOsAuthFailure, HarborOsAuthenticator, HarborOsPrincipal};
use harborgate::server::{router, AppState};
use harborgate::setup::SetupPortalService;
use serde_json::json;
use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use tempfile::TempDir;
use tokio::net::TcpListener;
use tokio::sync::Mutex;
use tower::ServiceExt;

#[derive(Clone)]
struct FakeAuthenticator {
    result: Result<HarborOsPrincipal, HarborOsAuthFailure>,
    tokens: Arc<Mutex<Vec<String>>>,
}

impl FakeAuthenticator {
    fn successful() -> Self {
        Self::new(Ok(HarborOsPrincipal {
            principal_id: "harboros:uid:42".to_string(),
            roles: vec!["FULL_ADMIN".to_string(), "SYSTEM_READ".to_string()],
        }))
    }

    fn new(result: Result<HarborOsPrincipal, HarborOsAuthFailure>) -> Self {
        Self {
            result,
            tokens: Arc::new(Mutex::new(Vec::new())),
        }
    }

    async fn tokens(&self) -> Vec<String> {
        self.tokens.lock().await.clone()
    }
}

#[async_trait]
impl HarborOsAuthenticator for FakeAuthenticator {
    async fn authenticate(&self, token: &str) -> Result<HarborOsPrincipal, HarborOsAuthFailure> {
        self.tokens.lock().await.push(token.to_string());
        self.result.clone()
    }
}

#[derive(Clone, Debug)]
struct CapturedRequest {
    method: Method,
    path_and_query: String,
    headers: HeaderMap,
    body: Bytes,
}

async fn capture_request(
    State(captured): State<Arc<Mutex<Vec<CapturedRequest>>>>,
    OriginalUri(uri): OriginalUri,
    method: Method,
    headers: HeaderMap,
    body: Bytes,
) -> axum::response::Response {
    captured.lock().await.push(CapturedRequest {
        method,
        path_and_query: uri.to_string(),
        headers,
        body,
    });
    if uri.path() == "/api/knowledge/preview" {
        let mut response =
            (StatusCode::PARTIAL_CONTENT, Bytes::from_static(b"clip")).into_response();
        response
            .headers_mut()
            .insert("content-type", "video/mp4".parse().unwrap());
        response
            .headers_mut()
            .insert("accept-ranges", "bytes".parse().unwrap());
        response
            .headers_mut()
            .insert("content-range", "bytes 10-13/100".parse().unwrap());
        response
            .headers_mut()
            .insert("content-length", "4".parse().unwrap());
        response
            .headers_mut()
            .insert("etag", "\"preview-v1\"".parse().unwrap());
        response.headers_mut().insert(
            "last-modified",
            "Sat, 02 Aug 2026 00:00:00 GMT".parse().unwrap(),
        );
        return response;
    }
    Json(json!({"ok": true})).into_response()
}

async fn mock_beacon() -> (String, Arc<Mutex<Vec<CapturedRequest>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let captured = Arc::new(Mutex::new(Vec::new()));
    let app = Router::new()
        .fallback(any(capture_request))
        .with_state(captured.clone());
    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    (format!("http://{address}"), captured)
}

fn test_state(
    beacon_url: &str,
    beacon_web_api_token: &str,
    authenticator: Arc<dyn HarborOsAuthenticator>,
) -> (AppState, TempDir) {
    let temp_dir = tempfile::tempdir().unwrap();
    let mut config = AppConfig::from_env();
    config.data_dir = temp_dir.path().join("sessions");
    config.state_dir = temp_dir.path().join("state");
    config.harborbeacon_base_url = beacon_url.to_string();
    config.harborbeacon_token = "legacy-task-secret".to_string();
    config.harborbeacon_web_api_token = beacon_web_api_token.to_string();
    config.harbor_workspace_id = "home-1".to_string();
    config.enable_feishu_websocket = false;
    config.enable_weixin_runtime = false;
    let gateway = Arc::new(GatewayService::from_config(&config).unwrap());
    let state = AppState {
        config: config.clone(),
        setup: Arc::new(SetupPortalService::new(config, gateway.clone())),
        gateway,
        feishu_websocket_started: Arc::new(AtomicBool::new(false)),
        harboros_authenticator: authenticator,
    };
    (state, temp_dir)
}

async fn response_body(response: axum::response::Response) -> String {
    String::from_utf8(
        to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap()
            .to_vec(),
    )
    .unwrap()
}

#[tokio::test]
async fn protected_prefixed_search_replaces_all_client_identity() {
    let (beacon_url, captured) = mock_beacon().await;
    let authenticator = FakeAuthenticator::successful();
    let (state, _temp_dir) = test_state(
        &beacon_url,
        "beacon-service-secret",
        Arc::new(authenticator.clone()),
    );
    let request_body = r#"{"query":"front door"}"#;
    let request = Request::builder()
        .method(Method::POST)
        .uri("/api/harbor-gate/api/beacon/knowledge/search?user_id=spoof&open_id=spoof&workspace_id=evil&limit=4")
        .header("content-type", "application/json")
        .header("authorization", "Bearer browser-controlled")
        .header("x-harboros-auth-token", "one-time-secret")
        .header("x-harbor-user-id", "spoof")
        .header("x-harbor-open-id", "spoof")
        .header("x-harboros-user", "spoof")
        .header("x-harbor-os-user", "spoof")
        .header("x-harbor-principal-source", "client")
        .header("x-harbor-principal-id", "client:spoof")
        .header("x-harbor-principal-roles", "SUPERUSER")
        .header("x-harbor-workspace-id", "evil")
        .body(Body::from(request_body))
        .unwrap();

    let response = router(state).oneshot(request).await.unwrap();

    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response
            .headers()
            .get("x-harbor-beacon-proxy-prefix")
            .unwrap(),
        "/api/harbor-gate/api/beacon"
    );
    let requests = captured.lock().await;
    assert_eq!(requests.len(), 1);
    let upstream = &requests[0];
    assert_eq!(upstream.method, Method::POST);
    assert_eq!(upstream.path_and_query, "/api/knowledge/search?limit=4");
    assert_eq!(upstream.body, request_body);
    assert_eq!(
        upstream.headers.get("authorization").unwrap(),
        "Bearer beacon-service-secret"
    );
    assert_eq!(
        upstream.headers.get("x-harbor-principal-source").unwrap(),
        "harboros"
    );
    assert_eq!(
        upstream.headers.get("x-harbor-principal-id").unwrap(),
        "harboros:uid:42"
    );
    assert_eq!(
        upstream.headers.get("x-harbor-principal-roles").unwrap(),
        "FULL_ADMIN,SYSTEM_READ"
    );
    assert_eq!(
        upstream.headers.get("x-harbor-workspace-id").unwrap(),
        "home-1"
    );
    for name in [
        "x-harboros-auth-token",
        "x-harbor-user-id",
        "x-harbor-open-id",
        "x-harboros-user",
        "x-harbor-os-user",
    ] {
        assert!(upstream.headers.get(name).is_none(), "forwarded {name}");
    }
    drop(requests);
    assert_eq!(authenticator.tokens().await, vec!["one-time-secret"]);
}

#[tokio::test]
async fn every_proxy_alias_requires_authentication_for_conversation_json() {
    let (beacon_url, captured) = mock_beacon().await;
    let authenticator = FakeAuthenticator::successful();
    let (state, _temp_dir) = test_state(
        &beacon_url,
        "beacon-service-secret",
        Arc::new(authenticator.clone()),
    );
    let app = router(state);

    for path in [
        "/api/beacon/knowledge/conversations",
        "/api/harbor-gate/api/beacon/knowledge/conversations",
        "/api/harbor-assistant/knowledge/conversations",
    ] {
        let response = app
            .clone()
            .oneshot(Request::get(path).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED, "{path}");
    }

    assert!(captured.lock().await.is_empty());
    assert!(authenticator.tokens().await.is_empty());
}

#[tokio::test]
async fn authentication_failures_have_stable_statuses_and_redact_tokens() {
    let (beacon_url, captured) = mock_beacon().await;
    let cases = [
        (
            HarborOsAuthFailure::InvalidToken,
            StatusCode::UNAUTHORIZED,
            "HARBOROS_AUTH_FAILED",
        ),
        (
            HarborOsAuthFailure::AccessDenied,
            StatusCode::FORBIDDEN,
            "HARBOROS_ACCESS_DENIED",
        ),
        (
            HarborOsAuthFailure::WebUiAccessRequired,
            StatusCode::FORBIDDEN,
            "HARBOROS_WEBUI_ACCESS_REQUIRED",
        ),
        (
            HarborOsAuthFailure::FullAdminRequired,
            StatusCode::FORBIDDEN,
            "HARBOROS_FULL_ADMIN_REQUIRED",
        ),
        (
            HarborOsAuthFailure::Unavailable,
            StatusCode::SERVICE_UNAVAILABLE,
            "HARBOROS_AUTH_UNAVAILABLE",
        ),
    ];

    for (failure, expected_status, expected_code) in cases {
        let authenticator = FakeAuthenticator::new(Err(failure));
        let (state, _temp_dir) = test_state(
            &beacon_url,
            "beacon-service-secret",
            Arc::new(authenticator),
        );
        let token = format!("secret-token-{expected_code}");
        let request = Request::post("/api/beacon/knowledge/search")
            .header("x-harboros-auth-token", &token)
            .body(Body::from("{}"))
            .unwrap();

        let response = router(state).oneshot(request).await.unwrap();
        assert_eq!(response.status(), expected_status);
        let body = response_body(response).await;
        assert!(body.contains(expected_code));
        assert!(!body.contains(&token));
    }

    assert!(captured.lock().await.is_empty());
}

#[tokio::test]
async fn ordinary_beacon_routes_strip_identity_without_harboros_login() {
    let (beacon_url, captured) = mock_beacon().await;
    let authenticator = FakeAuthenticator::successful();
    let (state, _temp_dir) = test_state(
        &beacon_url,
        "beacon-service-secret",
        Arc::new(authenticator.clone()),
    );
    let request = Request::get("/api/beacon/state?user_id=spoof&refresh=1")
        .header("authorization", "Bearer browser-controlled")
        .header("x-harbor-principal-id", "client:spoof")
        .header("x-harbor-workspace-id", "evil")
        .body(Body::empty())
        .unwrap();

    let response = router(state).oneshot(request).await.unwrap();

    assert_eq!(response.status(), StatusCode::OK);
    let requests = captured.lock().await;
    assert_eq!(requests.len(), 1);
    let upstream = &requests[0];
    assert_eq!(upstream.path_and_query, "/api/state?refresh=1");
    assert_eq!(
        upstream.headers.get("authorization").unwrap(),
        "Bearer beacon-service-secret"
    );
    assert!(upstream.headers.get("x-harbor-principal-id").is_none());
    assert!(upstream.headers.get("x-harbor-workspace-id").is_none());
    drop(requests);
    assert!(authenticator.tokens().await.is_empty());
}

#[tokio::test]
async fn media_preview_preserves_range_semantics_without_forwarding_identity() {
    let (beacon_url, captured) = mock_beacon().await;
    let authenticator = FakeAuthenticator::successful();
    let (state, _temp_dir) = test_state(
        &beacon_url,
        "beacon-service-secret",
        Arc::new(authenticator.clone()),
    );
    let request = Request::get("/api/beacon/knowledge/preview?user_id=spoof&file=clip")
        .header("range", "bytes=10-13")
        .header("if-range", "\"client-preview-v1\"")
        .header("authorization", "Bearer browser-controlled")
        .header("x-harboros-auth-token", "unused-one-time-secret")
        .header("x-harbor-principal-id", "client:spoof")
        .header("x-harbor-workspace-id", "evil")
        .body(Body::empty())
        .unwrap();

    let response = router(state).oneshot(request).await.unwrap();

    assert_eq!(response.status(), StatusCode::PARTIAL_CONTENT);
    for (name, expected) in [
        ("accept-ranges", "bytes"),
        ("content-range", "bytes 10-13/100"),
        ("content-length", "4"),
        ("etag", "\"preview-v1\""),
        ("last-modified", "Sat, 02 Aug 2026 00:00:00 GMT"),
    ] {
        assert_eq!(response.headers().get(name).unwrap(), expected, "{name}");
    }
    assert_eq!(
        to_bytes(response.into_body(), usize::MAX).await.unwrap(),
        Bytes::from_static(b"clip")
    );

    let requests = captured.lock().await;
    assert_eq!(requests.len(), 1);
    let upstream = &requests[0];
    assert_eq!(upstream.path_and_query, "/api/knowledge/preview?file=clip");
    assert_eq!(upstream.headers.get("range").unwrap(), "bytes=10-13");
    assert_eq!(
        upstream.headers.get("if-range").unwrap(),
        "\"client-preview-v1\""
    );
    assert_eq!(
        upstream.headers.get("authorization").unwrap(),
        "Bearer beacon-service-secret"
    );
    for name in [
        "x-harboros-auth-token",
        "x-harbor-principal-id",
        "x-harbor-workspace-id",
    ] {
        assert!(upstream.headers.get(name).is_none(), "forwarded {name}");
    }
    drop(requests);
    assert!(authenticator.tokens().await.is_empty());
}

#[tokio::test]
async fn legacy_task_token_cannot_replace_missing_web_api_token() {
    let (beacon_url, captured) = mock_beacon().await;
    let authenticator = FakeAuthenticator::successful();
    let (state, _temp_dir) = test_state(&beacon_url, "", Arc::new(authenticator.clone()));
    assert_eq!(state.config.harborbeacon_token, "legacy-task-secret");
    assert!(state.config.harborbeacon_web_api_token.is_empty());
    let request = Request::post("/api/beacon/knowledge/search")
        .header("authorization", "Bearer browser-controlled")
        .header("x-harboros-auth-token", "one-time-secret")
        .body(Body::from("{}"))
        .unwrap();

    let response = router(state).oneshot(request).await.unwrap();

    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    let body = response_body(response).await;
    assert!(body.contains("HARBORBEACON_SERVICE_AUTH_UNAVAILABLE"));
    assert!(!body.contains("one-time-secret"));
    assert!(!body.contains("browser-controlled"));
    assert!(captured.lock().await.is_empty());
    assert!(authenticator.tokens().await.is_empty());
}

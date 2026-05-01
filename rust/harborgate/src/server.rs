use crate::config::AppConfig;
use crate::error::GatewayError;
use crate::gateway::GatewayService;
use crate::runtime::{maybe_start_feishu_websocket_runtime, maybe_start_weixin_poll_runtime};
use crate::setup::SetupPortalService;
use axum::extract::{Path, Query, State};
use axum::http::{header::CONTENT_TYPE, HeaderMap, StatusCode};
use axum::response::{Html, IntoResponse, Redirect};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tokio::net::TcpListener;
use tracing::info;

#[derive(Clone)]
pub struct AppState {
    pub config: AppConfig,
    pub gateway: Arc<GatewayService>,
    pub setup: Arc<SetupPortalService>,
    pub feishu_websocket_started: Arc<AtomicBool>,
}

pub async fn serve(config: AppConfig) -> anyhow::Result<()> {
    let gateway = Arc::new(GatewayService::from_config(&config)?);
    let feishu_websocket_started = Arc::new(AtomicBool::new(false));
    maybe_start_configured_feishu_runtime(
        gateway.clone(),
        config.feishu.clone(),
        config.enable_feishu_websocket,
        feishu_websocket_started.clone(),
    );
    maybe_start_weixin_poll_runtime(gateway.clone(), config.enable_weixin_runtime);
    let state = AppState {
        config: config.clone(),
        setup: Arc::new(SetupPortalService::new(config.clone(), gateway.clone())),
        gateway,
        feishu_websocket_started,
    };
    let app = router(state);
    let addr: SocketAddr = format!("{}:{}", config.host, config.port).parse()?;
    let listener = TcpListener::bind(addr).await?;
    info!("HarborGate Rust listening on http://{}", addr);
    axum::serve(listener, app).await?;
    Ok(())
}

pub fn router(state: AppState) -> Router {
    let feishu_path = state.config.feishu.webhook_path.clone();
    Router::new()
        .route("/health", get(health))
        .route("/", get(root))
        .route("/api/setup/status", get(setup_status))
        .route("/api/gateway/status", get(gateway_status))
        .route("/api/notifications/deliveries", post(notification_delivery))
        .route("/setup", get(feishu_setup_page))
        .route("/setup/feishu", get(feishu_setup_page))
        .route("/setup/qr", get(feishu_qr_page))
        .route("/setup/feishu/qr", get(feishu_qr_page))
        .route("/setup/qr.svg", get(feishu_qr_svg))
        .route("/setup/feishu/qr.svg", get(feishu_qr_svg))
        .route("/setup/weixin", get(weixin_setup_page))
        .route("/setup/weixin/qr", get(weixin_setup_page))
        .route("/setup/weixin/qr.svg", get(weixin_qr_svg))
        .route("/admin/im", get(admin_im))
        .route("/admin/im/feishu", get(feishu_setup_page))
        .route("/admin/im/weixin", get(weixin_setup_page))
        .route("/api/setup/feishu/configure", post(configure_feishu))
        .route("/api/setup/weixin/login/start", post(weixin_login_start))
        .route("/api/setup/weixin/login/status", get(weixin_login_status))
        .route("/api/setup/weixin/unbind", post(weixin_unbind))
        .route("/messages/{platform}", post(message))
        .route(&feishu_path, post(feishu_webhook))
        .with_state(state)
}

async fn health(State(state): State<AppState>) -> impl IntoResponse {
    Json(json!({
        "status": "ok",
        "runtime": "rust",
        "runtime_supervisor": {
            "runtime": "rust",
            "status": "running",
            "adapters": state.gateway.status()["adapters"].clone(),
        }
    }))
}

async fn root() -> impl IntoResponse {
    Json(json!({
        "name": "harborgate",
        "runtime": "rust",
        "message": "Rust HarborGate is active for IM setup, Feishu, Weixin, webhook, delivery, and runtime supervision."
    }))
}

async fn setup_status(State(state): State<AppState>, headers: HeaderMap) -> impl IntoResponse {
    Json(state.setup.status_payload(host_header(&headers)))
}

async fn gateway_status(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<Value>, GatewayError> {
    require_service_contract(&state.config, &headers)?;
    require_service_auth(&state.config, &headers)?;
    Ok(Json(
        state.setup.gateway_status_payload(host_header(&headers)),
    ))
}

async fn notification_delivery(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(payload): Json<Value>,
) -> Result<Json<Value>, GatewayError> {
    require_service_contract(&state.config, &headers)?;
    require_service_auth(&state.config, &headers)?;
    Ok(Json(
        state.gateway.handle_notification_delivery(payload).await?,
    ))
}

async fn message(
    State(state): State<AppState>,
    Path(platform): Path<String>,
    Json(payload): Json<Value>,
) -> Result<Json<Value>, GatewayError> {
    Ok(Json(
        state.gateway.handle_inbound(&platform, payload).await?,
    ))
}

async fn feishu_setup_page(State(state): State<AppState>, headers: HeaderMap) -> impl IntoResponse {
    Html(state.setup.build_feishu_setup_page(host_header(&headers)))
}

async fn feishu_qr_page(State(state): State<AppState>) -> impl IntoResponse {
    Html(state.setup.build_qr_page())
}

async fn feishu_qr_svg(State(state): State<AppState>, headers: HeaderMap) -> impl IntoResponse {
    (
        [(CONTENT_TYPE, "image/svg+xml; charset=utf-8")],
        state.setup.build_feishu_qr_svg(host_header(&headers)),
    )
}

async fn weixin_setup_page(
    State(state): State<AppState>,
    Query(query): Query<HashMap<String, String>>,
    headers: HeaderMap,
) -> impl IntoResponse {
    Html(
        state
            .setup
            .build_weixin_setup_page(host_header(&headers), query_flag(&query, "unbound")),
    )
}

async fn weixin_qr_svg(State(state): State<AppState>) -> impl IntoResponse {
    (
        [(CONTENT_TYPE, "image/svg+xml; charset=utf-8")],
        state.setup.build_weixin_qr_svg(),
    )
}

async fn admin_im(
    State(state): State<AppState>,
    Query(query): Query<HashMap<String, String>>,
    headers: HeaderMap,
) -> impl IntoResponse {
    let platform = query
        .get("platform")
        .map(|value| value.trim().to_lowercase())
        .unwrap_or_else(|| "feishu".into());
    if platform == "weixin" {
        return Html(
            state
                .setup
                .build_weixin_setup_page(host_header(&headers), query_flag(&query, "unbound")),
        );
    }
    Html(state.setup.build_feishu_setup_page(host_header(&headers)))
}

async fn configure_feishu(
    State(state): State<AppState>,
    Json(payload): Json<Value>,
) -> Result<impl IntoResponse, GatewayError> {
    let (status, payload) = state.setup.configure_feishu(payload).await?;
    if status.is_success() {
        maybe_start_configured_feishu_runtime(
            state.gateway.clone(),
            state.gateway.feishu_adapter().settings(),
            state.config.enable_feishu_websocket,
            state.feishu_websocket_started.clone(),
        );
    }
    Ok((status, Json(payload)))
}

async fn weixin_login_start(
    State(state): State<AppState>,
) -> Result<impl IntoResponse, GatewayError> {
    let (status, payload) = state.setup.start_weixin_login().await?;
    Ok((status, Json(payload)))
}

async fn weixin_login_status(
    State(state): State<AppState>,
) -> Result<impl IntoResponse, GatewayError> {
    let (status, payload) = state.setup.poll_weixin_login().await?;
    Ok((status, Json(payload)))
}

async fn weixin_unbind(State(state): State<AppState>, headers: HeaderMap) -> impl IntoResponse {
    let payload = state.setup.unbind_weixin();
    let accept = headers
        .get("Accept")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("");
    if accept.contains("text/html") {
        return Redirect::to("/setup/weixin?unbound=1").into_response();
    }
    Json(payload).into_response()
}

async fn feishu_webhook(
    State(state): State<AppState>,
    Json(payload): Json<Value>,
) -> Result<Json<Value>, GatewayError> {
    let adapter = state.gateway.feishu_adapter();
    if adapter.is_url_verification(&payload) {
        return Ok(Json(adapter.build_url_verification_response(&payload)?));
    }
    Ok(Json(state.gateway.handle_inbound("feishu", payload).await?))
}

fn require_service_contract(config: &AppConfig, headers: &HeaderMap) -> Result<(), GatewayError> {
    let received = headers
        .get("X-Contract-Version")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .trim();
    if received != config.contract_version {
        return Err(GatewayError::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "CONTRACT_VERSION_MISMATCH",
            format!("X-Contract-Version must be {}", config.contract_version),
        ));
    }
    Ok(())
}

fn require_service_auth(config: &AppConfig, headers: &HeaderMap) -> Result<(), GatewayError> {
    if config.service_token.trim().is_empty() {
        return Ok(());
    }
    let authorization = headers
        .get("Authorization")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .trim();
    if authorization != format!("Bearer {}", config.service_token) {
        return Err(GatewayError::new(
            StatusCode::UNAUTHORIZED,
            "SERVICE_AUTH_FAILED",
            "Missing or invalid service token",
        ));
    }
    Ok(())
}

fn host_header(headers: &HeaderMap) -> &str {
    headers
        .get("Host")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
}

fn query_flag(query: &HashMap<String, String>, key: &str) -> bool {
    query
        .get(key)
        .map(|value| {
            matches!(
                value.trim().to_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
        .unwrap_or(false)
}

fn maybe_start_configured_feishu_runtime(
    gateway: Arc<GatewayService>,
    config: crate::config::FeishuConfig,
    enabled: bool,
    started: Arc<AtomicBool>,
) {
    if !enabled || !config.configured() || config.connection_mode != "websocket" {
        return;
    }
    if started.swap(true, Ordering::SeqCst) {
        return;
    }
    maybe_start_feishu_websocket_runtime(gateway, config, true);
}

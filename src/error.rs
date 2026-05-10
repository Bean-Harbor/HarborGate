use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Value};
use thiserror::Error;

#[derive(Debug, Error)]
#[error("{code}: {message}")]
pub struct GatewayError {
    pub status: StatusCode,
    pub code: String,
    pub message: String,
    pub trace_id: Option<String>,
}

impl GatewayError {
    pub fn new(status: StatusCode, code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            status,
            code: code.into(),
            message: message.into(),
            trace_id: None,
        }
    }

    pub fn with_trace(mut self, trace_id: impl Into<String>) -> Self {
        let trace_id = trace_id.into();
        if !trace_id.trim().is_empty() {
            self.trace_id = Some(trace_id);
        }
        self
    }

    pub fn validation(message: impl Into<String>) -> Self {
        Self::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "VALIDATION_ERROR",
            message,
        )
    }

    pub fn infrastructure(message: impl Into<String>) -> Self {
        Self::new(
            StatusCode::INTERNAL_SERVER_ERROR,
            "INFRASTRUCTURE_ERROR",
            message,
        )
    }

    pub fn response_payload(&self) -> Value {
        json!({
            "ok": false,
            "error": {
                "code": self.code,
                "message": self.message,
            },
            "trace_id": self.trace_id.clone().unwrap_or_default(),
        })
    }
}

impl IntoResponse for GatewayError {
    fn into_response(self) -> Response {
        (self.status, Json(self.response_payload())).into_response()
    }
}

impl From<anyhow::Error> for GatewayError {
    fn from(value: anyhow::Error) -> Self {
        Self::infrastructure(value.to_string())
    }
}

impl From<std::io::Error> for GatewayError {
    fn from(value: std::io::Error) -> Self {
        Self::infrastructure(value.to_string())
    }
}

impl From<serde_json::Error> for GatewayError {
    fn from(value: serde_json::Error) -> Self {
        Self::infrastructure(value.to_string())
    }
}

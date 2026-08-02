use async_trait::async_trait;
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use serde_json::{json, Value};
use std::time::Duration;
use tokio::time::timeout;
use tokio_tungstenite::{connect_async, tungstenite::Message};

const DEFAULT_AUTH_TIMEOUT: Duration = Duration::from_secs(5);
const AUTH_REQUEST_ID: &str = "harborgate-harboros-auth";
const FULL_ADMIN_ROLE: &str = "FULL_ADMIN";
const HARBOROS_AUTH_WEBSOCKET_URL: &str = "ws://127.0.0.1:6000/api/current";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HarborOsPrincipal {
    pub principal_id: String,
    pub roles: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HarborOsAuthFailure {
    InvalidToken,
    AccessDenied,
    WebUiAccessRequired,
    FullAdminRequired,
    Unavailable,
}

impl std::fmt::Display for HarborOsAuthFailure {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let message = match self {
            Self::InvalidToken => "HarborOS authentication failed",
            Self::AccessDenied => "HarborOS access was denied",
            Self::WebUiAccessRequired => "HarborOS WebUI access is required",
            Self::FullAdminRequired => "HarborOS FULL_ADMIN role is required",
            Self::Unavailable => "HarborOS authentication service is unavailable",
        };
        formatter.write_str(message)
    }
}

#[async_trait]
pub trait HarborOsAuthenticator: Send + Sync {
    async fn authenticate(&self, token: &str) -> Result<HarborOsPrincipal, HarborOsAuthFailure>;
}

#[derive(Debug, Clone)]
pub struct MiddlewareHarborOsAuthenticator {
    websocket_url: String,
    timeout: Duration,
}

impl MiddlewareHarborOsAuthenticator {
    pub fn new(websocket_url: impl Into<String>) -> Self {
        Self {
            websocket_url: websocket_url.into(),
            timeout: DEFAULT_AUTH_TIMEOUT,
        }
    }

    #[cfg(test)]
    fn with_timeout(websocket_url: impl Into<String>, timeout: Duration) -> Self {
        Self {
            websocket_url: websocket_url.into(),
            timeout,
        }
    }

    async fn authenticate_inner(
        &self,
        token: &str,
    ) -> Result<HarborOsPrincipal, HarborOsAuthFailure> {
        let (mut socket, _) = connect_async(self.websocket_url.as_str())
            .await
            .map_err(|_| HarborOsAuthFailure::Unavailable)?;
        let request = auth_login_request(token);
        socket
            .send(Message::Text(request.to_string()))
            .await
            .map_err(|_| HarborOsAuthFailure::Unavailable)?;

        while let Some(message) = socket.next().await {
            let message = message.map_err(|_| HarborOsAuthFailure::Unavailable)?;
            let payload = match message {
                Message::Text(payload) => payload.to_string(),
                Message::Binary(payload) => String::from_utf8(payload.to_vec())
                    .map_err(|_| HarborOsAuthFailure::Unavailable)?,
                Message::Close(_) => return Err(HarborOsAuthFailure::Unavailable),
                Message::Ping(_) | Message::Pong(_) | Message::Frame(_) => continue,
            };
            let response: RpcResponse =
                serde_json::from_str(&payload).map_err(|_| HarborOsAuthFailure::Unavailable)?;
            if response.id.as_deref() != Some(AUTH_REQUEST_ID) {
                continue;
            }
            if response.error.is_some() {
                return Err(HarborOsAuthFailure::Unavailable);
            }
            return principal_from_login_response(
                response.result.ok_or(HarborOsAuthFailure::Unavailable)?,
            );
        }

        Err(HarborOsAuthFailure::Unavailable)
    }
}

impl Default for MiddlewareHarborOsAuthenticator {
    fn default() -> Self {
        Self::new(HARBOROS_AUTH_WEBSOCKET_URL)
    }
}

#[async_trait]
impl HarborOsAuthenticator for MiddlewareHarborOsAuthenticator {
    async fn authenticate(&self, token: &str) -> Result<HarborOsPrincipal, HarborOsAuthFailure> {
        timeout(self.timeout, self.authenticate_inner(token))
            .await
            .map_err(|_| HarborOsAuthFailure::Unavailable)?
    }
}

fn auth_login_request(token: &str) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": AUTH_REQUEST_ID,
        "method": "auth.login_ex",
        "params": [{
            "mechanism": "TOKEN_PLAIN",
            "token": token,
            "login_options": {
                "user_info": true,
                "reconnect_token": false
            }
        }]
    })
}

fn principal_from_login_response(
    response: LoginExResponse,
) -> Result<HarborOsPrincipal, HarborOsAuthFailure> {
    match response.response_type.as_str() {
        "SUCCESS" => {}
        "AUTH_ERR" | "EXPIRED" => return Err(HarborOsAuthFailure::InvalidToken),
        "DENIED" => return Err(HarborOsAuthFailure::AccessDenied),
        _ => return Err(HarborOsAuthFailure::Unavailable),
    }
    let user_info = response.user_info.ok_or(HarborOsAuthFailure::Unavailable)?;
    if !user_info.privilege.webui_access {
        return Err(HarborOsAuthFailure::WebUiAccessRequired);
    }
    let mut roles = user_info.privilege.roles.values;
    roles.sort();
    roles.dedup();
    if !roles.iter().any(|role| role == FULL_ADMIN_ROLE) {
        return Err(HarborOsAuthFailure::FullAdminRequired);
    }
    Ok(HarborOsPrincipal {
        principal_id: format!("harboros:uid:{}", user_info.pw_uid),
        roles,
    })
}

#[derive(Debug, Deserialize)]
struct RpcResponse {
    id: Option<String>,
    result: Option<LoginExResponse>,
    error: Option<Value>,
}

#[derive(Debug, Deserialize)]
struct LoginExResponse {
    response_type: String,
    user_info: Option<HarborOsUserInfo>,
}

#[derive(Debug, Deserialize)]
struct HarborOsUserInfo {
    pw_uid: u64,
    privilege: HarborOsPrivilege,
}

#[derive(Debug, Deserialize)]
struct HarborOsPrivilege {
    webui_access: bool,
    roles: HarborOsRoles,
}

#[derive(Debug, Deserialize)]
struct HarborOsRoles {
    #[serde(rename = "$set")]
    values: Vec<String>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use futures_util::{SinkExt, StreamExt};
    use std::sync::Arc;
    use tokio::net::TcpListener;
    use tokio::sync::Mutex;
    use tokio_tungstenite::accept_async;

    async fn mock_middleware(
        response: Option<Value>,
        delay: Duration,
    ) -> (String, Arc<Mutex<Option<Value>>>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let captured = Arc::new(Mutex::new(None));
        let captured_for_server = captured.clone();
        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut socket = accept_async(stream).await.unwrap();
            if let Some(Ok(Message::Text(payload))) = socket.next().await {
                *captured_for_server.lock().await = serde_json::from_str(&payload).ok();
            }
            tokio::time::sleep(delay).await;
            if let Some(response) = response {
                socket
                    .send(Message::Text(response.to_string()))
                    .await
                    .unwrap();
            }
        });
        (format!("ws://{address}"), captured)
    }

    fn login_response(response_type: &str, webui_access: bool, roles: &[&str]) -> Value {
        json!({
            "jsonrpc": "2.0",
            "id": AUTH_REQUEST_ID,
            "result": {
                "response_type": response_type,
                "user_info": {
                    "pw_uid": 42,
                    "privilege": {
                        "webui_access": webui_access,
                        "roles": {"$set": roles}
                    }
                }
            }
        })
    }

    #[tokio::test]
    async fn authenticates_full_admin_and_preserves_token_as_json_data() {
        let malicious_token = "secret-\"}],\"method\":\"system.reboot";
        let (url, captured) = mock_middleware(
            Some(login_response(
                "SUCCESS",
                true,
                &["FULL_ADMIN", "SYSTEM_READ"],
            )),
            Duration::ZERO,
        )
        .await;
        let authenticator = MiddlewareHarborOsAuthenticator::new(url);

        let principal = authenticator.authenticate(malicious_token).await.unwrap();

        assert_eq!(principal.principal_id, "harboros:uid:42");
        assert_eq!(principal.roles, vec!["FULL_ADMIN", "SYSTEM_READ"]);
        let request = captured.lock().await.clone().unwrap();
        assert_eq!(request["method"], "auth.login_ex");
        assert_eq!(request["params"][0]["mechanism"], "TOKEN_PLAIN");
        assert_eq!(request["params"][0]["token"], malicious_token);
        assert_eq!(request["params"][0]["login_options"]["user_info"], true);
        assert_eq!(
            request["params"][0]["login_options"]["reconnect_token"],
            false
        );
    }

    #[tokio::test]
    async fn rejects_expired_or_invalid_token_without_echoing_it() {
        let token = "expired-super-secret";
        let (url, _) = mock_middleware(
            Some(login_response("EXPIRED", true, &["FULL_ADMIN"])),
            Duration::ZERO,
        )
        .await;
        let authenticator = MiddlewareHarborOsAuthenticator::new(url);

        let error = authenticator.authenticate(token).await.unwrap_err();

        assert_eq!(error, HarborOsAuthFailure::InvalidToken);
        assert!(!error.to_string().contains(token));
        assert!(!format!("{error:?}").contains(token));
    }

    #[tokio::test]
    async fn maps_middleware_denied_to_access_denied() {
        let (url, _) = mock_middleware(
            Some(login_response("DENIED", true, &["FULL_ADMIN"])),
            Duration::ZERO,
        )
        .await;
        let authenticator = MiddlewareHarborOsAuthenticator::new(url);

        assert_eq!(
            authenticator.authenticate("token").await.unwrap_err(),
            HarborOsAuthFailure::AccessDenied
        );
    }

    #[tokio::test]
    async fn requires_webui_access_and_full_admin_role() {
        let (url, _) = mock_middleware(
            Some(login_response("SUCCESS", false, &["FULL_ADMIN"])),
            Duration::ZERO,
        )
        .await;
        let authenticator = MiddlewareHarborOsAuthenticator::new(url);
        assert_eq!(
            authenticator.authenticate("token-1").await.unwrap_err(),
            HarborOsAuthFailure::WebUiAccessRequired
        );

        let (url, _) = mock_middleware(
            Some(login_response("SUCCESS", true, &["READONLY_ADMIN"])),
            Duration::ZERO,
        )
        .await;
        let authenticator = MiddlewareHarborOsAuthenticator::new(url);
        assert_eq!(
            authenticator.authenticate("token-2").await.unwrap_err(),
            HarborOsAuthFailure::FullAdminRequired
        );
    }

    #[tokio::test]
    async fn maps_timeout_and_unreachable_middleware_to_unavailable() {
        let (url, _) = mock_middleware(
            Some(login_response("SUCCESS", true, &["FULL_ADMIN"])),
            Duration::from_millis(200),
        )
        .await;
        let authenticator =
            MiddlewareHarborOsAuthenticator::with_timeout(url, Duration::from_millis(20));
        assert_eq!(
            authenticator.authenticate("token").await.unwrap_err(),
            HarborOsAuthFailure::Unavailable
        );

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        drop(listener);
        let authenticator = MiddlewareHarborOsAuthenticator::with_timeout(
            format!("ws://{address}"),
            Duration::from_millis(100),
        );
        assert_eq!(
            authenticator.authenticate("token").await.unwrap_err(),
            HarborOsAuthFailure::Unavailable
        );
    }
}

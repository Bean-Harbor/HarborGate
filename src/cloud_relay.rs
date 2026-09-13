//! Gate's authenticated HTTP client. Cloud/Link own the device connection;
//! this module neither publishes MQTT nor decides the user's selected Navi.
use crate::error::GatewayError;
use aws_credential_types::{
    provider::{self, ProvideCredentials, SharedCredentialsProvider},
    Credentials,
};
use aws_sigv4::{
    http_request::{sign, SignableBody, SignableRequest, SigningSettings},
    sign::v4,
};
use chrono::{DateTime, Utc};
use reqwest::{redirect::Policy, Client, Method, StatusCode};
use serde_json::{json, Value};
use std::{
    sync::Arc,
    time::{Duration, SystemTime},
};
use tokio::{
    sync::Mutex,
    time::{sleep, timeout_at, Instant},
};
use url::Url;
use uuid::Uuid;

const MAX_RESPONSE: usize = 74_096;
const AUTH_TTL: Duration = Duration::from_secs(5);
const TURN_TTL: Duration = Duration::from_secs(180);
const MEDIA_TTL: Duration = Duration::from_secs(45);

#[derive(Clone)]
pub struct CloudRelayClient {
    expected_hub_identity: Option<String>,
    endpoint: Url,
    region: String,
    credentials: SharedCredentialsProvider,
    http: Client,
}

pub(crate) struct RelayResponse {
    pub hub_identity: String,
    pub status: StatusCode,
    pub body: Value,
}

fn unavailable() -> GatewayError {
    GatewayError::new(
        StatusCode::SERVICE_UNAVAILABLE,
        "NAVI_RELAY_UNAVAILABLE",
        "Navi connection is temporarily unavailable",
    )
}

fn invalid() -> GatewayError {
    GatewayError::validation("Invalid Navi relay request")
}

fn invalid_response() -> GatewayError {
    GatewayError::new(
        StatusCode::BAD_GATEWAY,
        "NAVI_RELAY_INVALID_RESPONSE",
        "Navi connection returned an invalid response",
    )
}

pub(crate) fn valid_hub_id(hub: &str) -> bool {
    (3..=64).contains(&hub.len())
        && hub
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

impl CloudRelayClient {
    pub(crate) fn with_hub_identity(&self, identity: &str) -> Result<Self, GatewayError> {
        if identity.len() != 64 || !identity.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(invalid());
        }
        let mut client = self.clone();
        client.expected_hub_identity = Some(identity.into());
        Ok(client)
    }
    /// Deployment uses the CDK HTTP API endpoint and a short-lived role provider.
    /// The hub is chosen separately by an authenticated binding directory.
    pub fn new(
        endpoint: &str,
        region: &str,
        credentials: SharedCredentialsProvider,
    ) -> Result<Self, GatewayError> {
        let endpoint = Url::parse(endpoint).map_err(|_| invalid())?;
        let suffix = if region.starts_with("cn-") {
            "amazonaws.com.cn"
        } else {
            "amazonaws.com"
        };
        let host_suffix = format!(".execute-api.{region}.{suffix}");
        let api_id = endpoint
            .host_str()
            .and_then(|host| host.strip_suffix(&host_suffix))
            .unwrap_or("");
        if region.is_empty()
            || !region
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
            || api_id.is_empty()
            || !api_id
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
            || endpoint.scheme() != "https"
            || endpoint.port().is_some()
            || !endpoint.username().is_empty()
            || endpoint.password().is_some()
            || endpoint.path() != "/"
            || endpoint.query().is_some()
            || endpoint.fragment().is_some()
        {
            return Err(invalid());
        }
        Self::create(endpoint, region, credentials)
    }

    pub fn from_ecs(endpoint: &str, region: &str) -> Result<Self, GatewayError> {
        let relative =
            std::env::var("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI").map_err(|_| unavailable())?;
        Self::new(
            endpoint,
            region,
            SharedCredentialsProvider::new(EcsTaskCredentials::new(&relative)?),
        )
    }

    fn create(
        endpoint: Url,
        region: &str,
        credentials: SharedCredentialsProvider,
    ) -> Result<Self, GatewayError> {
        Ok(Self {
            expected_hub_identity: None,
            endpoint,
            region: region.into(),
            credentials,
            http: Client::builder()
                .redirect(Policy::none())
                .connect_timeout(Duration::from_secs(2))
                .timeout(Duration::from_secs(10))
                .build()
                .map_err(|_| unavailable())?,
        })
    }

    pub(crate) async fn binding_proof(
        &self,
        hub: &str,
        body: &Value,
    ) -> Result<RelayResponse, GatewayError> {
        self.exchange(hub, "bindingProof", body, AUTH_TTL).await
    }

    pub(crate) async fn binding_route(
        &self,
        hub: &str,
        body: &Value,
    ) -> Result<RelayResponse, GatewayError> {
        self.exchange(hub, "bindingRoute", body, AUTH_TTL).await
    }

    pub(crate) async fn turn(
        &self,
        hub: &str,
        body: &Value,
    ) -> Result<RelayResponse, GatewayError> {
        if body["conversation"]["channel"] != "whatsapp" {
            return Err(invalid());
        }
        self.exchange(hub, "turn", body, TURN_TTL).await
    }

    pub(crate) async fn authorize_delivery(
        &self,
        hub: &str,
        body: &Value,
    ) -> Result<RelayResponse, GatewayError> {
        // New exchange per check; an earlier allowed result must never be reused.
        self.exchange(hub, "deliveryAuthorization", body, AUTH_TTL)
            .await
    }

    pub(crate) async fn media_artifact(
        &self,
        hub: &str,
        body: &Value,
    ) -> Result<RelayResponse, GatewayError> {
        if self.expected_hub_identity.is_none() {
            return Err(invalid());
        }
        self.exchange(hub, "mediaArtifact", body, MEDIA_TTL).await
    }

    pub async fn notification_outbox(
        &self,
        hub: &str,
        body: &Value,
    ) -> Result<Value, GatewayError> {
        self.notification_exchange(hub, "notificationOutbox", body)
            .await
    }

    pub async fn notification_receipt(
        &self,
        hub: &str,
        body: &Value,
    ) -> Result<Value, GatewayError> {
        self.notification_exchange(hub, "notificationReceipt", body)
            .await
    }

    async fn notification_exchange(
        &self,
        hub: &str,
        operation: &str,
        body: &Value,
    ) -> Result<Value, GatewayError> {
        if self.expected_hub_identity.is_none() {
            return Err(invalid());
        }
        let response = self
            .exchange(hub, operation, body, Duration::from_secs(10))
            .await?;
        if !response.status.is_success() {
            return Err(unavailable());
        }
        Ok(response.body)
    }

    async fn exchange(
        &self,
        hub: &str,
        operation: &str,
        body: &Value,
        ttl: Duration,
    ) -> Result<RelayResponse, GatewayError> {
        if !valid_hub_id(hub) || !body.is_object() {
            return Err(invalid());
        }
        let request_json = serde_json::to_string(&json!({"operation":operation, "body":body}))
            .map_err(|_| invalid())?;
        if request_json.len() > 70_000
            || serde_json::to_vec(&json!({"requestJson":request_json}))
                .map_err(|_| invalid())?
                .len()
                + 1024
                > 120_000
        {
            return Err(invalid());
        }
        let id = format!("gate_{}", Uuid::new_v4().simple());
        let mut request = json!({"requestId":id, "operation":operation, "body":body, "ttlMs":ttl.as_millis() as u64});
        if let Some(identity) = &self.expected_hub_identity {
            request["hubIdentity"] = json!(identity);
        }
        let encoded = serde_json::to_vec(&request).map_err(|_| invalid())?;
        if encoded.len() > MAX_RESPONSE {
            return Err(invalid());
        }
        let post_url = self
            .endpoint
            .join(&format!("v1/internal/beacon-exchanges/{hub}"))
            .map_err(|_| invalid())?;
        let get_url = self
            .endpoint
            .join(&format!("v1/internal/beacon-exchanges/{hub}/{id}"))
            .map_err(|_| invalid())?;
        let end = Instant::now() + ttl;
        timeout_at(end, async {
            let mut accepted = false;
            let mut cloud_deadline = None;
            let mut cloud_identity = self.expected_hub_identity.clone();
            let mut last_publish = Instant::now();
            loop {
                // A repeated POST republishes the original durable exchange,
                // recovering a lost MQTT publish or reply without a new turn.
                let publish = !accepted || last_publish.elapsed() >= Duration::from_secs(2);
                let (method, url, bytes) = if publish {
                    (Method::POST, &post_url, encoded.as_slice())
                } else {
                    (Method::GET, &get_url, &[][..])
                };
                let response = self.request(method, url, bytes).await;
                match response {
                    Ok((status, _))
                        if status == StatusCode::TOO_MANY_REQUESTS || status.is_server_error() => {}
                    Err(error) if error.code == "NAVI_RELAY_INVALID_RESPONSE" => return Err(error),
                    Err(_) => {} // Retry transport/credential failures within the original total deadline.
                    Ok((status, bytes)) => {
                        if status == StatusCode::CONFLICT
                            && serde_json::from_slice::<Value>(&bytes)
                                .ok()
                                .is_some_and(|body| body["error"]["code"] == "HUB_IDENTITY_CHANGED")
                        {
                            return Err(GatewayError::new(
                                StatusCode::FORBIDDEN,
                                "NAVI_IDENTITY_CHANGED",
                                "Navi identity permission changed; reconnect WhatsApp",
                            ));
                        }
                        if status != StatusCode::OK && status != StatusCode::ACCEPTED {
                            return Err(unavailable());
                        }
                        let payload: Value =
                            serde_json::from_slice(&bytes).map_err(|_| unavailable())?;
                        let deadline =
                            payload["deadlineUnixMs"].as_i64().ok_or_else(unavailable)?;
                        let identity = payload["hubIdentity"]
                            .as_str()
                            .filter(|id| {
                                id.len() == 64 && id.bytes().all(|b| b.is_ascii_hexdigit())
                            })
                            .ok_or_else(unavailable)?;
                        if cloud_identity
                            .as_deref()
                            .is_some_and(|expected| expected != identity)
                        {
                            return Err(unavailable());
                        }
                        cloud_identity = Some(identity.into());
                        if payload["requestId"] != id
                            || deadline <= Utc::now().timestamp_millis()
                            || cloud_deadline.is_some_and(|original| original != deadline)
                        {
                            return Err(unavailable());
                        }
                        cloud_deadline = Some(deadline);
                        if status == StatusCode::ACCEPTED {
                            if payload["status"] != "pending" || payload.get("result").is_some() {
                                return Err(unavailable());
                            }
                            accepted = true;
                            if publish {
                                last_publish = Instant::now();
                            }
                        } else {
                            let result = &payload["result"];
                            if payload["status"] != "complete" || result["status"] != "complete" {
                                return Err(unavailable());
                            }
                            let code = result["httpStatus"]
                                .as_u64()
                                .filter(|code| {
                                    (200..=599).contains(code) && !(300..400).contains(code)
                                })
                                .ok_or_else(unavailable)?;
                            let body = result.get("body").ok_or_else(unavailable)?.clone();
                            if serde_json::to_vec(&body).map_err(|_| unavailable())?.len() > 70_000
                            {
                                return Err(unavailable());
                            }
                            return Ok(RelayResponse {
                                hub_identity: identity.into(),
                                status: StatusCode::from_u16(code as u16)
                                    .map_err(|_| unavailable())?,
                                body,
                            });
                        }
                    }
                }
                sleep(Duration::from_millis(250)).await;
            }
        })
        .await
        .map_err(|_| unavailable())?
    }

    #[cfg(test)]
    pub(crate) fn for_fixture(endpoint: &str) -> Self {
        let endpoint = Url::parse(endpoint).unwrap();
        assert_eq!(endpoint.host_str(), Some("127.0.0.1"));
        let credentials = Credentials::new(
            "ASIAFIXTUREEXAMPLE",
            "fixture-secret-only",
            Some("fixture-session-token".into()),
            Some(SystemTime::now() + Duration::from_secs(3600)),
            "RelayFixture",
        );
        Self::create(
            endpoint,
            "us-east-1",
            SharedCredentialsProvider::new(credentials),
        )
        .unwrap()
    }

    async fn request(
        &self,
        method: Method,
        url: &Url,
        body: &[u8],
    ) -> Result<(StatusCode, Vec<u8>), GatewayError> {
        let credentials = self
            .credentials
            .provide_credentials()
            .await
            .map_err(|_| unavailable())?;
        if credentials.session_token().is_none_or(str::is_empty)
            || credentials
                .expiry()
                .is_none_or(|expiry| expiry <= SystemTime::now() + Duration::from_secs(5))
        {
            return Err(unavailable());
        }
        let identity = credentials.into();
        let params = v4::SigningParams::builder()
            .identity(&identity)
            .region(&self.region)
            .name("execute-api")
            .time(SystemTime::now())
            .settings(SigningSettings::default())
            .build()
            .map_err(|_| unavailable())?
            .into();
        let headers = [("content-type", "application/json")];
        let signable = SignableRequest::new(
            method.as_str(),
            url.as_str(),
            headers.into_iter(),
            SignableBody::Bytes(body),
        )
        .map_err(|_| unavailable())?;
        let (instructions, _) = sign(signable, &params)
            .map_err(|_| unavailable())?
            .into_parts();
        let mut request = self
            .http
            .request(method, url.clone())
            .header("content-type", "application/json")
            .body(body.to_vec());
        for (name, value) in instructions.headers() {
            request = request.header(name, value);
        }
        let mut response = request.send().await.map_err(|_| unavailable())?;
        let status = response.status();
        if response
            .content_length()
            .is_some_and(|length| length > MAX_RESPONSE as u64)
        {
            return Err(invalid_response());
        }
        // A redirect is a protocol error and must not carry the signature onwards.
        if status.is_redirection() {
            return Ok((status, Vec::new()));
        }
        let mut bytes = Vec::new();
        while let Some(chunk) = response.chunk().await.map_err(|_| unavailable())? {
            if bytes.len() + chunk.len() > MAX_RESPONSE {
                return Err(invalid_response());
            }
            bytes.extend_from_slice(&chunk);
        }
        Ok((status, bytes))
    }
}

/// ECS task-role credentials. Never accepts a caller-supplied metadata host,
/// follows redirects, or falls back to an instance role/static user key.
#[derive(Debug, Clone)]
struct EcsTaskCredentials {
    url: Url,
    http: Client,
    cached: Arc<Mutex<Option<Credentials>>>,
}

impl EcsTaskCredentials {
    fn new(relative: &str) -> Result<Self, GatewayError> {
        let id = relative.strip_prefix("/v2/credentials/").unwrap_or("");
        if id.is_empty()
            || id.len() > 128
            || !id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        {
            return Err(invalid());
        }
        Ok(Self {
            url: Url::parse(&format!("http://169.254.170.2{relative}")).map_err(|_| invalid())?,
            http: Client::builder()
                .no_proxy()
                .redirect(Policy::none())
                .timeout(Duration::from_secs(2))
                .build()
                .map_err(|_| unavailable())?,
            cached: Arc::new(Mutex::new(None)),
        })
    }

    async fn load(&self) -> Result<Credentials, provider::error::CredentialsError> {
        let error = || {
            provider::error::CredentialsError::provider_error("Gate task credentials unavailable")
        };
        let mut cached = self.cached.lock().await;
        if let Some(value) = cached.as_ref().filter(|value| {
            value
                .expiry()
                .is_some_and(|expiry| expiry > SystemTime::now() + Duration::from_secs(60))
        }) {
            return Ok(value.clone());
        }
        let mut response = self
            .http
            .get(self.url.clone())
            .send()
            .await
            .map_err(|_| error())?;
        if response.status() != StatusCode::OK {
            return Err(error());
        }
        let mut bytes = Vec::new();
        while let Some(chunk) = response.chunk().await.map_err(|_| error())? {
            if bytes.len() + chunk.len() > 16_384 {
                return Err(error());
            }
            bytes.extend_from_slice(&chunk);
        }
        let value: Value = serde_json::from_slice(&bytes).map_err(|_| error())?;
        let field = |name: &str| {
            value[name]
                .as_str()
                .filter(|text| !text.is_empty())
                .ok_or_else(error)
        };
        let expiry: SystemTime = DateTime::parse_from_rfc3339(field("Expiration")?)
            .map_err(|_| error())?
            .into();
        if expiry <= SystemTime::now() + Duration::from_secs(5) {
            return Err(error());
        }
        let credentials = Credentials::new(
            field("AccessKeyId")?,
            field("SecretAccessKey")?,
            Some(field("Token")?.into()),
            Some(expiry),
            "GateEcsTaskRole",
        );
        *cached = Some(credentials.clone());
        Ok(credentials)
    }
}

impl ProvideCredentials for EcsTaskCredentials {
    fn provide_credentials<'a>(&'a self) -> provider::future::ProvideCredentials<'a>
    where
        Self: 'a,
    {
        provider::future::ProvideCredentials::new(self.load())
    }
}

#[cfg(test)]
#[path = "cloud_relay_tests.rs"]
mod tests;

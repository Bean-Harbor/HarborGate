use harborgate::config::AppConfig;
use harborgate::server;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env().add_directive("harborgate=info".parse()?))
        .init();
    let config = AppConfig::from_env();
    server::serve(config).await
}

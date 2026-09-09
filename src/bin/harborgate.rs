use harborgate::config::AppConfig;
use harborgate::device_session::DeviceSessionStore;
use harborgate::server;
use std::env;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let arguments = env::args().skip(1).collect::<Vec<_>>();
    if arguments
        .first()
        .is_some_and(|argument| argument == "device-pair")
    {
        return issue_device_pairing(&arguments[1..]);
    }
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env().add_directive("harborgate=info".parse()?))
        .init();
    let config = AppConfig::from_env();
    server::serve(config).await
}

fn issue_device_pairing(arguments: &[String]) -> anyhow::Result<()> {
    let camera_id = required_argument(arguments, "--camera")?;
    let ttl_seconds = optional_argument(arguments, "--ttl-seconds")
        .map(|value| value.parse::<u64>())
        .transpose()?
        .unwrap_or(300);
    let state_dir = optional_argument(arguments, "--state-dir")
        .map(Into::into)
        .unwrap_or_else(|| AppConfig::from_env().device_session_state_dir);
    let pairing = DeviceSessionStore::new(state_dir).issue_pairing(camera_id, ttl_seconds)?;
    println!("pairing_code={}", pairing.code);
    println!("camera_id={}", pairing.camera_id);
    println!(
        "expires_at_epoch_seconds={}",
        pairing.expires_at_epoch_seconds
    );
    Ok(())
}

fn required_argument<'a>(arguments: &'a [String], name: &str) -> anyhow::Result<&'a str> {
    optional_argument(arguments, name)
        .ok_or_else(|| anyhow::anyhow!("missing required argument {name}"))
}

fn optional_argument<'a>(arguments: &'a [String], name: &str) -> Option<&'a str> {
    arguments
        .windows(2)
        .find(|pair| pair[0] == name)
        .map(|pair| pair[1].as_str())
}

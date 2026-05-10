# Default target
default:
    just --list

# Format the code
fmt:
    cargo fmt --check

# Run tests
test:
    cargo test

# Run the HarborGate service
start:
    cargo run --bin harboros-im-gate

# Build the release binary
build:
    cargo build --release --bin harboros-im-gate

# Build the portable Linux release using zigbuild
build-linux:
    cargo zigbuild --release --bin harboros-im-gate --target x86_64-unknown-linux-musl

# Stop any running HarborGate instances
stop:
    pkill -f "harboros-im-gate" || echo "No harboros-im-gate process found."

# Multi-stage build: compile with cargo, ship runtime-only image.
# Build on Debian 12 (bookworm) so the glibc-linked binary runs on the
# debian:bookworm-slim runtime image (rust:1-slim currently tracks trixie).
# Target linux/amd64 (OrbStack Rosetta emulation):
#   docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
#     -t localhost:32000/langperf/rust:v1 --load apps/rust
FROM rust:1-slim-bookworm AS build
WORKDIR /app

# Dependency cache layer: build a stub main against the full dep graph first.
COPY Cargo.toml Cargo.lock ./
RUN mkdir -p src && printf 'fn main() {}\n' > src/main.rs && cargo build --release && rm -rf src

COPY src ./src
RUN touch src/main.rs && cargo build --release

FROM debian:bookworm-slim
COPY --from=build /app/target/release/langperf-rust /usr/local/bin/langperf-rust
ENV PORT=8080
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/langperf-rust"]

#!/bin/sh
# Production process tree: Go control, Rust worker, Rust public listener.
set -eu
mkdir -p /data/go-control /data/worker
export DEEPSEEK_INFRA_ROOT="${DEEPSEEK_INFRA_ROOT:-/data}"
export DEEPSEEK_INFRA_STATIC_DIR="${DEEPSEEK_INFRA_STATIC_DIR:-/app/static}"
export DEEPSEEK_RUNTIME_MODE="${DEEPSEEK_RUNTIME_MODE:-python_disabled}"
export DEEPSEEKD_MODE="${DEEPSEEKD_MODE:-authoritative}"
export DEEPSEEKD_LISTEN="${DEEPSEEKD_LISTEN:-127.0.0.1:8090}"
export DEEPSEEKD_PRODUCTION_STORE="${DEEPSEEKD_PRODUCTION_STORE:-/data/go-control}"
export DEEPSEEKD_SHADOW_STORE=
export DEEPSEEKD_OWNER="${DEEPSEEKD_OWNER:-deepseekd}"
export GO_CONTROL_ADDR="${GO_CONTROL_ADDR:-http://127.0.0.1:8090}"
export DEEPSEEK_WORKER_LISTEN="${DEEPSEEK_WORKER_LISTEN:-127.0.0.1:50052}"
export GATEWAY_BIND_ADDR="${GATEWAY_BIND_ADDR:-0.0.0.0:8000}"

export DEEPSEEK_NATIVE_BIN=/usr/local/bin
exec deepseek-launch --server

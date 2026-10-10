"""Dedicated process entry point for one sovereign Federation node."""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from pathlib import Path
from typing import Sequence

import uvicorn

from deepseek_infra.infra.workspace import backup_recovery_credential, federation_node
from deepseek_infra.web.federation_app import create_federation_app

OPERATOR_TOKEN_ENV = "DEEPSEEK_FEDERATION_OPERATOR_TOKEN"
SIGNER_PASSPHRASE_ENV = "DEEPSEEK_FEDERATION_SIGNER_PASSPHRASE"  # pragma: allowlist secret
MAX_RECOVERY_IDENTITY_BYTES = 64 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one DeepSeek Infra Federation node")
    parser.add_argument("--config", type=Path, required=True, help="Public node config and encrypted online signer references")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--allow-non-loopback", action="store_true")
    parser.add_argument("--recovery-identity-stdin", action="store_true")
    parser.add_argument("--ssl-certfile", type=Path)
    parser.add_argument("--ssl-keyfile", type=Path)
    return parser


def _validate_bind(host: str, *, allow_non_loopback: bool) -> None:
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError as exc:
        raise federation_node.FederationNodeError("FEDERATION_NODE_BIND_HOST_INVALID") from exc
    if not loopback and not allow_non_loopback:
        raise federation_node.FederationNodeError("FEDERATION_NODE_NON_LOOPBACK_REQUIRES_OPT_IN")


def _read_recovery_identity(enabled: bool) -> bytearray | None:
    if not enabled:
        return None
    raw = sys.stdin.buffer.readline(MAX_RECOVERY_IDENTITY_BYTES + 1)
    if not raw or len(raw) > MAX_RECOVERY_IDENTITY_BYTES:
        raise federation_node.FederationNodeError("FEDERATION_RECOVERY_IDENTITY_BINDING_REQUIRED")
    return bytearray(raw.rstrip(b"\r\n"))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    _validate_bind(str(arguments.host), allow_non_loopback=bool(arguments.allow_non_loopback))
    if not (1 <= int(arguments.port) <= 65535):
        raise federation_node.FederationNodeError("FEDERATION_NODE_PORT_INVALID")
    if bool(arguments.ssl_certfile) != bool(arguments.ssl_keyfile):
        raise federation_node.FederationNodeError("FEDERATION_NODE_TLS_CONFIG_INVALID")
    operator_token = os.environ.get(OPERATOR_TOKEN_ENV, "")
    signer_text = os.environ.pop(SIGNER_PASSPHRASE_ENV, "")
    if len(operator_token) < 16:
        raise federation_node.FederationNodeError("FEDERATION_OPERATOR_TOKEN_INVALID")
    if not signer_text:
        raise federation_node.FederationNodeError("FEDERATION_SIGNER_PASSPHRASE_REQUIRED")
    signer_passphrase = bytearray(signer_text.encode("utf-8"))
    recovery_identity = _read_recovery_identity(bool(arguments.recovery_identity_stdin))
    try:
        node = federation_node.load_federation_node(
            arguments.config,
            signer_passphrase=signer_passphrase,
            recovery_age_identity=recovery_identity,
        )
    finally:
        backup_recovery_credential.zeroize(signer_passphrase)
        backup_recovery_credential.zeroize(recovery_identity)
    app = create_federation_app(node=node, operator_token=operator_token)
    uvicorn.run(
        app,
        host=str(arguments.host),
        port=int(arguments.port),
        log_level="info",
        access_log=False,
        ssl_certfile=None if arguments.ssl_certfile is None else str(arguments.ssl_certfile),
        ssl_keyfile=None if arguments.ssl_keyfile is None else str(arguments.ssl_keyfile),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by real process evidence
    try:
        raise SystemExit(main())
    except federation_node.FederationNodeError as exc:
        raise SystemExit(exc.code) from None

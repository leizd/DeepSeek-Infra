"""Production AuthorityReplicaProvider — bootstrap replicas before Startup Verdict.

4.6.5 Gate A: fresh processes must rebuild replica handles from operator bootstrap
configuration and credential references. Never rely on inherited process globals alone.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from deepseek_infra.core.errors import AppError, ErrorCode

# Bootstrap env: JSON list of replica descriptors (locator + credentialReference only).
ENV_AUTHORITY_REPLICAS = "DEEPSEEK_CONTROL_AUTHORITY_REPLICAS"
ENV_AUTHORITY_BOOTSTRAP_PATH = "DEEPSEEK_CONTROL_AUTHORITY_BOOTSTRAP"


@dataclass(frozen=True)
class AuthorityReplicaLocator:
    """Secretless replica locator (never carries access keys or Age identities)."""

    replica_id: str
    kind: str  # "filesystem" | "s3"
    root: str | None = None
    endpoint: str | None = None
    bucket: str | None = None
    prefix: str | None = None
    region: str | None = None
    credential_reference: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "replicaId": self.replica_id,
            "kind": self.kind,
        }
        if self.root is not None:
            out["root"] = self.root
        if self.endpoint is not None:
            out["endpoint"] = self.endpoint
        if self.bucket is not None:
            out["bucket"] = self.bucket
        if self.prefix is not None:
            out["prefix"] = self.prefix
        if self.region is not None:
            out["region"] = self.region
        if self.credential_reference is not None:
            out["credentialReference"] = self.credential_reference
        return out


@dataclass
class AuthorityReplica:
    """Resolved replica handle for discover / write / anti-entropy."""

    locator: AuthorityReplicaLocator
    root: Path | None = None
    store: Any | None = None

    @property
    def replica_id(self) -> str:
        return self.locator.replica_id


class AuthorityReplicaProvider(Protocol):
    def discover(self) -> list[AuthorityReplica]:
        ...

    def locators(self) -> list[AuthorityReplicaLocator]:
        ...

    def configured(self) -> bool:
        ...


@dataclass
class StaticAuthorityReplicaProvider:
    """Provider backed by already-resolved roots/stores or bootstrap locators."""

    _locators: list[AuthorityReplicaLocator] = field(default_factory=list)
    _replicas: list[AuthorityReplica] = field(default_factory=list)
    _store_factory: Any | None = None

    def locators(self) -> list[AuthorityReplicaLocator]:
        return list(self._locators)

    def configured(self) -> bool:
        return bool(self._locators or self._replicas)

    def discover(self) -> list[AuthorityReplica]:
        if self._replicas:
            return list(self._replicas)
        resolved: list[AuthorityReplica] = []
        for locator in self._locators:
            if locator.kind == "filesystem":
                if not locator.root:
                    continue
                resolved.append(AuthorityReplica(locator=locator, root=Path(locator.root)))
            elif locator.kind == "s3":
                store = self._resolve_s3_store(locator)
                if store is not None:
                    resolved.append(AuthorityReplica(locator=locator, store=store))
        self._replicas = resolved
        return list(self._replicas)

    def _resolve_s3_store(self, locator: AuthorityReplicaLocator) -> Any | None:
        if self._store_factory is not None:
            return self._store_factory(locator)
        return None


# Process-wide provider installed before Startup Verdict (tests + production bootstrap).
_PROVIDER: AuthorityReplicaProvider | None = None


def configure_authority_replica_provider(provider: AuthorityReplicaProvider | None) -> None:
    global _PROVIDER
    _PROVIDER = provider


def get_authority_replica_provider() -> AuthorityReplicaProvider | None:
    return _PROVIDER


def authority_provider_configured() -> bool:
    if _PROVIDER is not None and _PROVIDER.configured():
        return True
    return False


def parse_replica_locators(raw: Any) -> list[AuthorityReplicaLocator]:
    """Parse bootstrap JSON/list into secretless locators."""
    if raw is None:
        return []
    items = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            items = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AppError(
                "control-authority-bootstrap-invalid-json",
                code=ErrorCode.INVALID_REQUEST,
                status=400,
            ) from exc
    if not isinstance(items, list):
        raise AppError(
            "control-authority-bootstrap-must-be-list",
            code=ErrorCode.INVALID_REQUEST,
            status=400,
        )
    out: list[AuthorityReplicaLocator] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        target = item.get("target") if isinstance(item.get("target"), dict) else item
        assert isinstance(target, dict)
        kind = str(target.get("kind") or item.get("kind") or "").strip().casefold()
        replica_id = str(item.get("replicaId") or item.get("id") or f"replica-{index}")
        if kind in {"fs", "filesystem", "file", "path"}:
            root = target.get("root") or target.get("path") or item.get("root")
            if not root:
                continue
            out.append(
                AuthorityReplicaLocator(
                    replica_id=replica_id,
                    kind="filesystem",
                    root=str(root),
                )
            )
            continue
        if kind in {"s3", "minio", "object", "object-store"}:
            out.append(
                AuthorityReplicaLocator(
                    replica_id=replica_id,
                    kind="s3",
                    endpoint=str(target.get("endpoint") or target.get("endpointUrl") or "") or None,
                    bucket=str(target.get("bucket") or "") or None,
                    prefix=str(target.get("prefix") or "") or None,
                    region=str(target.get("region") or "") or None,
                    credential_reference=str(
                        target.get("credentialReference")
                        or item.get("credentialReference")
                        or ""
                    )
                    or None,
                )
            )
    return out


def load_bootstrap_locators(
    *,
    env: dict[str, str] | None = None,
    bootstrap_path: Path | str | None = None,
) -> list[AuthorityReplicaLocator]:
    """Load locators from env JSON and/or bootstrap file (no secrets)."""
    environ = env if env is not None else dict(os.environ)
    locators: list[AuthorityReplicaLocator] = []
    path_raw = bootstrap_path or environ.get(ENV_AUTHORITY_BOOTSTRAP_PATH)
    if path_raw:
        path = Path(str(path_raw))
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AppError(
                    f"control-authority-bootstrap-unreadable:{path}",
                    code=ErrorCode.INVALID_REQUEST,
                    status=400,
                ) from exc
            block = payload.get("controlAuthority") if isinstance(payload, dict) else None
            replicas = None
            if isinstance(block, dict):
                replicas = block.get("replicas")
            elif isinstance(payload, dict):
                replicas = payload.get("replicas")
            locators.extend(parse_replica_locators(replicas if replicas is not None else payload))
    env_json = environ.get(ENV_AUTHORITY_REPLICAS)
    if env_json:
        locators.extend(parse_replica_locators(env_json))
    # De-dupe by replica_id preserving order.
    seen: set[str] = set()
    unique: list[AuthorityReplicaLocator] = []
    for item in locators:
        if item.replica_id in seen:
            continue
        seen.add(item.replica_id)
        unique.append(item)
    return unique


def provider_from_roots_and_stores(
    roots: list[Path | str] | None = None,
    stores: list[Any] | None = None,
) -> StaticAuthorityReplicaProvider:
    """Build a provider from legacy process-local root/store lists."""
    locators: list[AuthorityReplicaLocator] = []
    replicas: list[AuthorityReplica] = []
    for index, root in enumerate(roots or []):
        path = Path(root)
        locator = AuthorityReplicaLocator(
            replica_id=f"fs-{index}:{path.name}",
            kind="filesystem",
            root=str(path),
        )
        locators.append(locator)
        replicas.append(AuthorityReplica(locator=locator, root=path))
    for index, store in enumerate(stores or []):
        locator = AuthorityReplicaLocator(replica_id=f"store-{index}", kind="s3")
        locators.append(locator)
        replicas.append(AuthorityReplica(locator=locator, store=store))
    return StaticAuthorityReplicaProvider(_locators=locators, _replicas=replicas)


def install_provider_from_bootstrap(
    *,
    env: dict[str, str] | None = None,
    bootstrap_path: Path | str | None = None,
    store_factory: Any | None = None,
    extra_roots: list[Path | str] | None = None,
    extra_stores: list[Any] | None = None,
) -> StaticAuthorityReplicaProvider:
    """Parse bootstrap, merge with any already-open handles, install as process provider."""
    locators = load_bootstrap_locators(env=env, bootstrap_path=bootstrap_path)
    provider = StaticAuthorityReplicaProvider(_locators=list(locators), _store_factory=store_factory)
    # Merge explicit handles (tests / in-process configuration).
    merged = provider_from_roots_and_stores(extra_roots, extra_stores)
    all_locators = list(provider.locators()) + list(merged.locators())
    all_replicas = list(merged.discover())
    # Resolve filesystem locators without factory.
    for locator in locators:
        if locator.kind == "filesystem" and locator.root:
            all_replicas.append(AuthorityReplica(locator=locator, root=Path(locator.root)))
        elif locator.kind == "s3" and store_factory is not None:
            store = store_factory(locator)
            if store is not None:
                all_replicas.append(AuthorityReplica(locator=locator, store=store))
    final = StaticAuthorityReplicaProvider(_locators=all_locators, _replicas=all_replicas, _store_factory=store_factory)
    configure_authority_replica_provider(final)
    # Mirror into legacy globals so existing anchor paths keep working.
    from deepseek_infra.infra.workspace import backup_control_authority

    roots_out: list[Path | str] = [r.root for r in all_replicas if r.root is not None]
    stores_out = [r.store for r in all_replicas if r.store is not None]
    backup_control_authority.configure_authority_anchor_roots(roots_out or None)
    backup_control_authority.configure_authority_anchor_stores(stores_out or None)
    return final


def sync_provider_from_legacy_globals() -> StaticAuthorityReplicaProvider | None:
    """If legacy roots/stores are set but no provider, install a wrapping provider."""
    from deepseek_infra.infra.workspace import backup_control_authority

    roots: list[Path | str] = list(backup_control_authority.get_authority_anchor_roots())
    stores = backup_control_authority.get_authority_anchor_stores()
    if not roots and not stores:
        return None
    provider = provider_from_roots_and_stores(roots, stores)
    configure_authority_replica_provider(provider)
    return provider


def reset_authority_replica_provider() -> None:
    """Test helper: clear process provider."""
    configure_authority_replica_provider(None)

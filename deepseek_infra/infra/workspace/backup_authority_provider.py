"""Production AuthorityReplicaProvider — bootstrap replicas before Startup Verdict.

4.6.6: production S3 store factory from credentialReference; configured≠resolved
fail-closed; never degrade configured S3 replicas to local-only ACTIVE genesis.
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
ENV_AUTHORITY_MODE = "DEEPSEEK_CONTROL_AUTHORITY_MODE"
ENV_AUTHORITY_MIN_DURABLE = "DEEPSEEK_CONTROL_AUTHORITY_MIN_DURABLE_REPLICAS"
MODE_LOCAL_ONLY = "local-only"
MODE_REPLICATED = "replicated"


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

    def configured_count(self) -> int:
        ...

    def resolved_count(self) -> int:
        ...


def credential_provider_from_reference(reference: str | None) -> dict[str, Any]:
    """Map secretless credentialReference → S3 credentialProvider descriptor (no secrets)."""
    ref = str(reference or "").strip()
    if not ref or ref in {"aws-default", "aws-default-chain", "default"}:
        return {"type": "aws-default-chain"}
    if ref.startswith("profile:") or ref.startswith("aws-profile:"):
        profile = ref.split(":", 1)[1].strip()
        if not profile:
            raise AppError(
                "control-authority-credential-reference-invalid-profile",
                code=ErrorCode.INVALID_REQUEST,
                status=400,
            )
        return {"type": "aws-profile", "profile": profile}
    if ref in {"environment", "instance-role", "workload-identity"}:
        return {"type": ref}
    # Named profile shorthand.
    return {"type": "aws-profile", "profile": ref}


def record_from_authority_locator(locator: AuthorityReplicaLocator) -> dict[str, Any]:
    """Build secret-free S3 target record for open_s3_store."""
    if locator.kind != "s3":
        raise AppError(
            f"control-authority-locator-not-s3:{locator.replica_id}",
            code=ErrorCode.INVALID_REQUEST,
            status=400,
        )
    if not locator.bucket:
        raise AppError(
            f"control-authority-s3-bucket-required:{locator.replica_id}",
            code=ErrorCode.INVALID_REQUEST,
            status=400,
        )
    return {
        "targetId": f"authority-replica:{locator.replica_id}",
        "kind": "s3",
        "bucket": locator.bucket,
        "prefix": locator.prefix or "",
        "region": locator.region,
        "endpointUrl": locator.endpoint,
        "credentialProvider": credential_provider_from_reference(locator.credential_reference),
        "credentialReference": locator.credential_reference,
    }


def production_authority_store_factory(locator: AuthorityReplicaLocator, *, client: Any | None = None) -> Any | None:
    """Production AuthorityStoreFactory: locator → real S3TargetStore via credentialReference."""
    if locator.kind != "s3":
        return None
    from deepseek_infra.infra.workspace import backup_target_s3

    record = record_from_authority_locator(locator)
    return backup_target_s3.open_s3_store(record, client=client)


def authority_mode(
    *,
    env: dict[str, str] | None = None,
    bootstrap_path: Path | str | None = None,
) -> str:
    environ = env if env is not None else dict(os.environ)
    mode = str(environ.get(ENV_AUTHORITY_MODE) or "").strip().casefold()
    if not mode:
        path_raw = bootstrap_path or environ.get(ENV_AUTHORITY_BOOTSTRAP_PATH)
        if path_raw and Path(str(path_raw)).is_file():
            try:
                payload = json.loads(Path(str(path_raw)).read_text(encoding="utf-8"))
                block = payload.get("controlAuthority") if isinstance(payload, dict) else None
                if isinstance(block, dict) and block.get("mode") is not None:
                    mode = str(block.get("mode") or "").strip().casefold()
            except (OSError, json.JSONDecodeError):
                mode = ""
    if not mode:
        mode = MODE_REPLICATED
    if mode in {MODE_LOCAL_ONLY, "local", "localonly"}:
        return MODE_LOCAL_ONLY
    return MODE_REPLICATED


def min_durable_replicas(*, env: dict[str, str] | None = None, default: int = 1) -> int:
    environ = env if env is not None else dict(os.environ)
    raw = environ.get(ENV_AUTHORITY_MIN_DURABLE)
    if raw is None or str(raw).strip() == "":
        return max(1, int(default))
    try:
        return max(1, int(raw))
    except ValueError as exc:
        raise AppError(
            "control-authority-min-durable-invalid",
            code=ErrorCode.INVALID_REQUEST,
            status=400,
        ) from exc


@dataclass
class StaticAuthorityReplicaProvider:
    """Provider backed by already-resolved roots/stores or bootstrap locators."""

    _locators: list[AuthorityReplicaLocator] = field(default_factory=list)
    _replicas: list[AuthorityReplica] = field(default_factory=list)
    _store_factory: Any | None = None
    _resolve_errors: list[str] = field(default_factory=list)

    def locators(self) -> list[AuthorityReplicaLocator]:
        return list(self._locators)

    def configured(self) -> bool:
        return self.configured_count() > 0

    def configured_count(self) -> int:
        if self._locators:
            return len(self._locators)
        return len(self._replicas)

    def resolved_count(self) -> int:
        return len(self.discover())

    def resolve_errors(self) -> list[str]:
        return list(self._resolve_errors)

    def discover(self) -> list[AuthorityReplica]:
        if self._replicas:
            return list(self._replicas)
        resolved: list[AuthorityReplica] = []
        errors: list[str] = []
        for locator in self._locators:
            if locator.kind == "filesystem":
                if not locator.root:
                    errors.append(f"{locator.replica_id}:filesystem-root-missing")
                    continue
                resolved.append(AuthorityReplica(locator=locator, root=Path(locator.root)))
            elif locator.kind == "s3":
                try:
                    store = self._resolve_s3_store(locator)
                except Exception as exc:  # noqa: BLE001 — surface per-replica resolve failure
                    errors.append(f"{locator.replica_id}:{exc}")
                    continue
                if store is not None:
                    resolved.append(AuthorityReplica(locator=locator, store=store))
                else:
                    errors.append(f"{locator.replica_id}:s3-store-unresolved")
        self._resolve_errors = errors
        self._replicas = resolved
        return list(self._replicas)

    def _resolve_s3_store(self, locator: AuthorityReplicaLocator) -> Any | None:
        factory = self._store_factory if self._store_factory is not None else production_authority_store_factory
        return factory(locator)


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
    """Parse bootstrap, merge with any already-open handles, install as process provider.

    Production default ``store_factory`` is ``production_authority_store_factory`` so S3
    locators resolve via credentialReference without inherited process handles.
    """
    factory = store_factory if store_factory is not None else production_authority_store_factory
    locators = load_bootstrap_locators(env=env, bootstrap_path=bootstrap_path)
    # Merge explicit handles (tests / in-process configuration).
    merged = provider_from_roots_and_stores(extra_roots, extra_stores)
    all_locators = list(locators) + list(merged.locators())
    all_replicas = list(merged.discover())
    resolve_errors: list[str] = []
    for locator in locators:
        if locator.kind == "filesystem" and locator.root:
            all_replicas.append(AuthorityReplica(locator=locator, root=Path(locator.root)))
        elif locator.kind == "s3":
            try:
                store = factory(locator)
            except Exception as exc:  # noqa: BLE001
                resolve_errors.append(f"{locator.replica_id}:{exc}")
                continue
            if store is not None:
                all_replicas.append(AuthorityReplica(locator=locator, store=store))
            else:
                resolve_errors.append(f"{locator.replica_id}:s3-store-unresolved")
    final = StaticAuthorityReplicaProvider(
        _locators=all_locators,
        _replicas=all_replicas,
        _store_factory=factory,
        _resolve_errors=resolve_errors,
    )
    configure_authority_replica_provider(final)
    # Mirror into legacy globals so existing anchor paths keep working.
    from deepseek_infra.infra.workspace import backup_control_authority

    roots_out: list[Path | str] = [r.root for r in all_replicas if r.root is not None]
    stores_out = [r.store for r in all_replicas if r.store is not None]
    backup_control_authority.configure_authority_anchor_roots(roots_out or None)
    backup_control_authority.configure_authority_anchor_stores(stores_out or None)
    return final


def provider_status() -> dict[str, Any]:
    """Operator-facing configured vs resolved snapshot."""
    provider = get_authority_replica_provider()
    mode = authority_mode()
    if provider is None:
        return {
            "mode": mode,
            "configuredReplicaCount": 0,
            "resolvedReplicaCount": 0,
            "resolveErrors": [],
            "replicaIds": [],
            "locatorIds": [],
            "minDurableReplicas": min_durable_replicas(),
        }
    locators = list(provider.locators())
    replicas = list(provider.discover())
    errors: list[str] = []
    if isinstance(provider, StaticAuthorityReplicaProvider):
        errors = list(provider.resolve_errors())
        configured = int(provider.configured_count())
    else:
        configured = len(locators)
    return {
        "mode": mode,
        "configuredReplicaCount": int(configured),
        "resolvedReplicaCount": len(replicas),
        "resolveErrors": list(errors),
        "replicaIds": [r.replica_id for r in replicas],
        "locatorIds": [loc.replica_id for loc in locators],
        "minDurableReplicas": min_durable_replicas(),
    }


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

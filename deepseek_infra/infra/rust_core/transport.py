"""Persistent, credential-free HTTP transport for the optional Rust sidecar.

The sidecar clients intentionally keep their public ``urllib``-style failure
contract.  This module only replaces the per-call connection lifecycle with a
small stdlib pool that is safe to reset in tests and after a fork.
"""

from __future__ import annotations

import atexit
import http.client
import io
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from email.message import Message
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

DEFAULT_MAX_CONNECTIONS = 32
DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class ResponseTooLargeError(OSError):
    """Raised when a sidecar response exceeds the bounded read budget."""


@dataclass(frozen=True)
class TransportStats:
    pid: int
    pools: int
    connections_created: int
    idle_connections: int
    in_use_connections: int


class BufferedResponse:
    """Fully buffered response with the subset used by the sidecar clients."""

    def __init__(
        self,
        *,
        status: int,
        body: bytes,
        headers: Message,
        transport_us: int,
        connection_reused: bool,
        connection_count: int,
    ) -> None:
        self.status = status
        self._body = body
        self.headers = headers
        self.transport_us = transport_us
        self.connection_reused = connection_reused
        self.connection_count = connection_count

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "BufferedResponse":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback


class _ConnectionPool:
    def __init__(self, scheme: str, host: str, port: int, max_connections: int) -> None:
        self.scheme = scheme
        self.host = host
        self.port = port
        self.max_connections = max_connections
        self._condition = threading.Condition()
        self._idle: deque[http.client.HTTPConnection] = deque()
        self._open = 0
        self._in_use = 0
        self._created = 0
        self._closed = False

    def _new_connection(self, timeout: float) -> http.client.HTTPConnection:
        if self.scheme == "https":
            return http.client.HTTPSConnection(
                self.host,
                self.port,
                timeout=timeout,
                context=ssl.create_default_context(),
            )
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def acquire(self, timeout: float) -> tuple[http.client.HTTPConnection, bool, int]:
        deadline = time.monotonic() + max(0.001, timeout)
        with self._condition:
            while True:
                if self._closed:
                    raise OSError("Rust sidecar connection pool is closed")
                if self._idle:
                    connection = self._idle.pop()
                    self._in_use += 1
                    return connection, True, self._created
                if self._open < self.max_connections:
                    connection = self._new_connection(timeout)
                    self._open += 1
                    self._in_use += 1
                    self._created += 1
                    return connection, False, self._created
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out waiting for a Rust sidecar connection")
                self._condition.wait(remaining)

    def release(self, connection: http.client.HTTPConnection, *, reusable: bool) -> None:
        with self._condition:
            self._in_use = max(0, self._in_use - 1)
            if reusable and not self._closed:
                self._idle.append(connection)
            else:
                self._open = max(0, self._open - 1)
                try:
                    connection.close()
                except OSError:
                    pass
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            idle = list(self._idle)
            self._idle.clear()
            self._open = max(0, self._open - len(idle))
            self._condition.notify_all()
        for connection in idle:
            try:
                connection.close()
            except OSError:
                pass

    def stats(self) -> tuple[int, int, int]:
        with self._condition:
            return self._created, len(self._idle), self._in_use


class _PoolManager:
    def __init__(self) -> None:
        self.pid = os.getpid()
        self._lock = threading.Lock()
        self._pools: dict[tuple[str, str, int], _ConnectionPool] = {}

    @staticmethod
    def _max_connections() -> int:
        try:
            value = int(os.environ.get("DEEPSEEK_RUST_SIDECAR_MAX_CONNECTIONS", DEFAULT_MAX_CONNECTIONS))
        except ValueError:
            value = DEFAULT_MAX_CONNECTIONS
        return max(1, min(value, 128))

    def pool(self, scheme: str, host: str, port: int) -> _ConnectionPool:
        key = (scheme, host, port)
        with self._lock:
            pool = self._pools.get(key)
            if pool is None:
                pool = _ConnectionPool(scheme, host, port, self._max_connections())
                self._pools[key] = pool
            return pool

    def close(self) -> None:
        with self._lock:
            pools = list(self._pools.values())
            self._pools.clear()
        for pool in pools:
            pool.close()

    def stats(self) -> TransportStats:
        with self._lock:
            pools = list(self._pools.values())
        values = [pool.stats() for pool in pools]
        return TransportStats(
            pid=self.pid,
            pools=len(pools),
            connections_created=sum(value[0] for value in values),
            idle_connections=sum(value[1] for value in values),
            in_use_connections=sum(value[2] for value in values),
        )


_manager_lock = threading.Lock()
_manager = _PoolManager()


def _current_manager() -> _PoolManager:
    global _manager
    pid = os.getpid()
    if _manager.pid == pid:
        return _manager
    with _manager_lock:
        if _manager.pid != pid:
            old = _manager
            _manager = _PoolManager()
            old.close()
        return _manager


def reset_persistent_clients() -> None:
    """Close all idle connections and replace the manager without blocking exit."""
    global _manager
    with _manager_lock:
        old = _manager
        _manager = _PoolManager()
    old.close()


def _after_fork_child() -> None:
    # Never acquire a lock inherited from another thread in the child.
    global _manager, _manager_lock
    _manager_lock = threading.Lock()
    _manager = _PoolManager()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)


def transport_stats() -> TransportStats:
    return _current_manager().stats()


def new_correlation_id() -> str:
    """Return a system-generated, log-safe request correlation identifier."""
    return uuid.uuid4().hex


def response_header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except (AttributeError, TypeError):
        return None
    return str(value) if value is not None else None


def _response_limit() -> int:
    try:
        value = int(os.environ.get("DEEPSEEK_RUST_SIDECAR_MAX_RESPONSE_BYTES", DEFAULT_MAX_RESPONSE_BYTES))
    except ValueError:
        value = DEFAULT_MAX_RESPONSE_BYTES
    return max(1024, min(value, 64 * 1024 * 1024))


def urlopen(request: urllib.request.Request, timeout: float) -> BufferedResponse:
    """Open a sidecar request through a persistent pool.

    The return value and HTTP-error behavior intentionally mirror the small
    subset of ``urllib.request.urlopen`` used by the four sidecar clients.
    """
    parsed = urlsplit(request.full_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise urllib.error.URLError("Rust sidecar URL must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise urllib.error.URLError("Rust sidecar URL must not contain credentials")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    manager = _current_manager()
    pool = manager.pool(parsed.scheme, parsed.hostname, port)
    started_ns = time.perf_counter_ns()
    connection, reused, connection_count = pool.acquire(timeout)
    reusable = False
    try:
        connection.timeout = timeout
        if connection.sock is not None:
            connection.sock.settimeout(timeout)
        body = request.data
        headers = {key: value for key, value in request.header_items()}
        connection.request(request.get_method(), path, body=body, headers=headers)
        response = connection.getresponse()
        limit = _response_limit()
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ResponseTooLargeError("Rust sidecar response exceeds the configured limit")
        reusable = not response.will_close
        message = Message()
        for key, value in response.getheaders():
            message[key] = value
        transport_us = max(0, (time.perf_counter_ns() - started_ns) // 1000)
        buffered = BufferedResponse(
            status=response.status,
            body=raw,
            headers=message,
            transport_us=transport_us,
            connection_reused=reused,
            connection_count=connection_count,
        )
        pool.release(connection, reusable=reusable)
        if response.status >= 400:
            raise urllib.error.HTTPError(
                request.full_url,
                response.status,
                response.reason,
                message,
                io.BytesIO(raw),
            )
        return buffered
    except urllib.error.HTTPError:
        raise
    except (OSError, http.client.HTTPException, socket.timeout):
        pool.release(connection, reusable=False)
        raise
    except Exception:
        pool.release(connection, reusable=False)
        raise


atexit.register(reset_persistent_clients)

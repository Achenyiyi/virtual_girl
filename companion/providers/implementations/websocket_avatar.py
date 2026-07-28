"""Versioned WebSocket transport for an external avatar stage.

The external stage remains presentation-only. The companion runtime owns
identity, affect, memory, and policy and sends complete visual state snapshots.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

from websockets.asyncio.client import ClientConnection, connect

from companion.providers.avatar import AvatarModel, AvatarProvider, AvatarState
from companion.providers.base import (
    ProviderCapability,
    ProviderHealth,
    ProviderInfo,
)

logger = logging.getLogger(__name__)

PROTOCOL_NAME = "companion-avatar"
PROTOCOL_VERSION = 1


class AvatarBridgeError(RuntimeError):
    """The avatar bridge rejected a request or violated the protocol."""


@dataclass(frozen=True)
class WebSocketAvatarConfig:
    """Connection settings for a trusted external avatar bridge."""

    url: str = "ws://127.0.0.1:6121/ws"
    auth_token_env: str = "COMPANION_AVATAR_TOKEN"
    connect_timeout_seconds: float = 3.0
    request_timeout_seconds: float = 3.0
    max_message_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError("avatar bridge URL must use ws:// or wss:// and include a host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("avatar bridge URL must not contain credentials, query, or fragment")
        if parsed.scheme == "ws" and not _is_loopback_host(parsed.hostname):
            raise ValueError("non-loopback avatar bridge URLs must use wss://")
        if self.connect_timeout_seconds <= 0 or self.request_timeout_seconds <= 0:
            raise ValueError("avatar bridge timeouts must be positive")
        if not 1024 <= self.max_message_bytes <= 16 * 1024 * 1024:
            raise ValueError("avatar bridge max_message_bytes must be between 1 KiB and 16 MiB")

    def get_auth_token(self) -> str:
        """Read the bearer secret at use time without storing it in config or logs."""
        return os.environ.get(self.auth_token_env, "") if self.auth_token_env else ""


class WebSocketAvatarProvider(AvatarProvider):
    """RPC client for the documented companion avatar bridge protocol."""

    def __init__(self, config: WebSocketAvatarConfig) -> None:
        self._config = config
        self._connection: ClientConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._request_sequence = 0
        self._closed = False
        self._last_health_check = 0.0
        self._health = ProviderHealth.UNKNOWN

    def provider_info(self) -> ProviderInfo:
        return ProviderInfo(
            name="websocket-avatar-bridge",
            version="1.0.0",
            capabilities=[
                ProviderCapability.REAL_TIME,
                ProviderCapability.EMOTION_AWARE,
            ],
            health=self._health,
            last_health_check=self._last_health_check,
        )

    async def health_check(self) -> ProviderHealth:
        self._last_health_check = time.time()
        try:
            result = await self._request("health", {})
            self._health = (
                ProviderHealth.HEALTHY
                if result.get("status") == "healthy"
                else ProviderHealth.UNHEALTHY
            )
        except Exception:
            self._health = ProviderHealth.UNHEALTHY
            await self._disconnect()
        return self._health

    async def load_model(self, model_id: str) -> bool:
        if not model_id:
            raise ValueError("model_id must not be empty")
        result = await self._request("model.load", {"model_id": model_id})
        return result.get("loaded") is True

    async def update_state(self, state: AvatarState) -> None:
        await self._request("state.update", {"state": asdict(state)})

    async def trigger_expression(
        self, expression_id: str, intensity: float = 0.5, duration_ms: int = 2000
    ) -> None:
        await self._request(
            "expression.trigger",
            {
                "expression_id": expression_id,
                "intensity": _unit_interval(intensity, "intensity"),
                "duration_ms": duration_ms,
            },
        )

    async def trigger_gesture(self, gesture_id: str, intensity: float = 0.5) -> None:
        await self._request(
            "gesture.trigger",
            {"gesture_id": gesture_id, "intensity": _unit_interval(intensity, "intensity")},
        )

    async def set_proactive_level(self, level: int) -> None:
        if not 0 <= level <= 4:
            raise ValueError("proactive level must be between 0 and 4")
        await self._request("proactive.set_level", {"level": level})

    async def list_available_models(self) -> list[AvatarModel]:
        result = await self._request("model.list", {})
        raw_models = result.get("models")
        if not isinstance(raw_models, list):
            raise AvatarBridgeError("model.list response must contain a models array")
        models: list[AvatarModel] = []
        for raw in raw_models:
            if not isinstance(raw, dict):
                raise AvatarBridgeError("model.list returned an invalid model entry")
            try:
                models.append(
                    AvatarModel(
                        model_id=str(raw["model_id"]),
                        name=str(raw["name"]),
                        type=str(raw["type"]),
                        path=str(raw.get("path", "")),
                        thumbnail_path=_optional_string(raw.get("thumbnail_path")),
                        expressions=_string_list(raw.get("expressions", [])),
                        validation_errors=_string_list(raw.get("validation_errors", [])),
                    )
                )
            except KeyError as exc:
                raise AvatarBridgeError(f"model.list entry is missing {exc.args[0]}") from exc
        return models

    async def validate_model(self, model_id: str) -> list[str]:
        result = await self._request("model.validate", {"model_id": model_id})
        return _string_list(result.get("errors", []))

    async def shutdown(self) -> None:
        self._closed = True
        await self._disconnect()

    async def _ensure_connected(self) -> ClientConnection:
        if self._closed:
            raise AvatarBridgeError("avatar bridge provider is shut down")
        async with self._connect_lock:
            if self._connection is not None:
                return self._connection
            try:
                auth_token = self._config.get_auth_token()
                if not auth_token:
                    raise AvatarBridgeError("avatar bridge authentication token is not configured")
                connection = await asyncio.wait_for(
                    connect(
                        self._config.url,
                        max_size=self._config.max_message_bytes,
                        open_timeout=self._config.connect_timeout_seconds,
                        ping_interval=20,
                        ping_timeout=20,
                    ),
                    timeout=self._config.connect_timeout_seconds,
                )
            except Exception:
                logger.warning("Avatar bridge connection failed for %s", self._config.url)
                raise

            self._connection = connection
            self._reader_task = asyncio.create_task(self._reader_loop(connection))
            try:
                handshake = await self._request_on_connection(
                    connection,
                    "handshake",
                    {
                        "supported_versions": [PROTOCOL_VERSION],
                        "client": "virtual-companion",
                        "auth_token": auth_token,
                    },
                )
                if handshake.get("version") != PROTOCOL_VERSION:
                    raise AvatarBridgeError("avatar bridge did not negotiate protocol version 1")
            except Exception:
                await self._disconnect()
                raise
            return connection

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        connection = await self._ensure_connected()
        return await self._request_on_connection(connection, method, params)

    async def _request_on_connection(
        self, connection: ClientConnection, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        self._request_sequence += 1
        request_id = f"req-{self._request_sequence}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        payload = {
            "protocol": PROTOCOL_NAME,
            "version": PROTOCOL_VERSION,
            "type": "request",
            "id": request_id,
            "method": method,
            "params": params,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > self._config.max_message_bytes:
            self._pending.pop(request_id, None)
            raise AvatarBridgeError("avatar bridge request exceeds configured message limit")
        try:
            await connection.send(encoded)
            return await asyncio.wait_for(
                future, timeout=self._config.request_timeout_seconds
            )
        except TimeoutError as exc:
            raise AvatarBridgeError(f"avatar bridge request timed out: {method}") from exc
        finally:
            self._pending.pop(request_id, None)

    async def _reader_loop(self, connection: ClientConnection) -> None:
        failure: BaseException = AvatarBridgeError("avatar bridge disconnected")
        try:
            async for message in connection:
                if not isinstance(message, str):
                    raise AvatarBridgeError("avatar bridge responses must be JSON text")
                response = _decode_response(message)
                request_id = response["id"]
                future = self._pending.get(request_id)
                if future is None or future.done():
                    continue
                if response["ok"]:
                    future.set_result(response["result"])
                else:
                    future.set_exception(AvatarBridgeError(response["error"]))
        except asyncio.CancelledError:
            failure = AvatarBridgeError("avatar bridge reader stopped")
            raise
        except Exception as exc:
            failure = exc
        finally:
            if self._connection is connection:
                self._connection = None
            for future in tuple(self._pending.values()):
                if not future.done():
                    future.set_exception(failure)
            await connection.close()

    async def _disconnect(self) -> None:
        connection, self._connection = self._connection, None
        reader, self._reader_task = self._reader_task, None
        if connection is not None:
            await connection.close()
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader


def _decode_response(message: str) -> dict[str, Any]:
    try:
        raw = json.loads(message)
    except json.JSONDecodeError as exc:
        raise AvatarBridgeError("avatar bridge returned invalid JSON") from exc
    if not isinstance(raw, dict):
        raise AvatarBridgeError("avatar bridge response must be an object")
    if (
        raw.get("protocol") != PROTOCOL_NAME
        or raw.get("version") != PROTOCOL_VERSION
        or raw.get("type") != "response"
        or not isinstance(raw.get("id"), str)
        or not isinstance(raw.get("ok"), bool)
    ):
        raise AvatarBridgeError("avatar bridge returned an invalid response envelope")
    if raw["ok"]:
        result = raw.get("result", {})
        if not isinstance(result, dict):
            raise AvatarBridgeError("avatar bridge result must be an object")
        raw["result"] = result
    else:
        error = raw.get("error")
        if not isinstance(error, str) or not error:
            raise AvatarBridgeError("avatar bridge error response must include an error message")
    return raw


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _unit_interval(value: float, name: str) -> float:
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return value


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AvatarBridgeError("avatar bridge returned an invalid string array")
    return value

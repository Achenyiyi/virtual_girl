"""Authenticated loopback WebSocket server for the AIRI Electron main process."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from companion.desktop.control_protocol import (
    CONTROL_VERSION,
    DEFAULT_RPC_TIMEOUT_SECONDS,
    MAX_CONCURRENT_REQUESTS,
    MAX_MESSAGE_BYTES,
    ControlError,
    ControlRequest,
    decode_request,
    error_envelope,
    event_envelope,
    response_envelope,
)

logger = logging.getLogger(__name__)
_DEFAULT_HANDSHAKE_TIMEOUT_SECONDS = 2.0
type RequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
type DisconnectHandler = Callable[[], Awaitable[None]]
type ConnectedHandler = Callable[[], Awaitable[None]]


class ControlServer:
    """Serve a single authenticated AIRI client on a random loopback port."""

    def __init__(
        self,
        token: str,
        handler: RequestHandler,
        *,
        host: str = "127.0.0.1",
        request_timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS,
        handshake_timeout_seconds: float = _DEFAULT_HANDSHAKE_TIMEOUT_SECONDS,
        on_connected: ConnectedHandler | None = None,
        on_disconnected: DisconnectHandler | None = None,
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError("control server must bind to 127.0.0.1")
        if not token or token != token.strip():
            raise ValueError("control token is invalid")
        if request_timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        if handshake_timeout_seconds <= 0:
            raise ValueError("handshake timeout must be positive")
        self._token = token
        self._handler = handler
        self._host = host
        self._request_timeout_seconds = request_timeout_seconds
        self._handshake_timeout_seconds = handshake_timeout_seconds
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._server: Server | None = None
        self._client: ServerConnection | None = None
        self._client_authenticated = False
        self._send_lock = asyncio.Lock()
        self._client_lock = asyncio.Lock()
        self._handshake_lock = asyncio.Lock()
        self._sequence = 0
        self._connected = asyncio.Event()

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("control server is not running")
        sockets = tuple(self._server.sockets)
        if not sockets:
            raise RuntimeError("control server is not running")
        port = int(sockets[0].getsockname()[1])
        return f"ws://{self._host}:{port}"

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client_authenticated

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await serve(
            self._handle_connection,
            self._host,
            0,
            max_size=MAX_MESSAGE_BYTES,
            max_queue=MAX_CONCURRENT_REQUESTS * 2,
            compression=None,
            ping_interval=20,
            ping_timeout=20,
        )

    async def wait_until_connected(self, timeout_seconds: float) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout=timeout_seconds)

    async def publish(self, event: str, payload: dict[str, Any]) -> bool:
        client = self._client
        if client is None or not self._client_authenticated:
            return False
        envelope: dict[str, Any]
        try:
            async with self._send_lock:
                self._sequence += 1
                envelope = event_envelope(self._sequence, event, payload)
                await self._send_locked(client, envelope)
        except ConnectionClosed:
            return False
        except ControlError as exc:
            if exc.code != "message_too_large":
                raise
            logger.warning("Control event exceeded the outbound message size limit")
            return False
        return True

    async def stop(self) -> None:
        server, self._server = self._server, None
        client, self._client = self._client, None
        self._client_authenticated = False
        self._connected.clear()
        if client is not None:
            await client.close(code=1001, reason="Control server stopped")
        if server is not None:
            server.close()
            await server.wait_closed()

    async def _handle_connection(self, websocket: ServerConnection) -> None:
        async with self._client_lock:
            if self._client is not None:
                await websocket.close(code=1008, reason="Control client already connected")
                return
            self._client = websocket
            self._client_authenticated = False
        tasks: set[asyncio.Task[None]] = set()
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        handshake_timeout_task = asyncio.create_task(
            self._close_after_handshake_timeout(websocket)
        )
        try:
            async for raw_message in websocket:
                if len(tasks) >= MAX_CONCURRENT_REQUESTS:
                    request_id = self._safe_request_id(raw_message)
                    await self._send(
                        websocket,
                        error_envelope(
                            request_id,
                            ControlError(
                                "too_many_requests",
                                "Too many concurrent requests.",
                                retryable=True,
                            ),
                        ),
                    )
                    continue
                task = asyncio.create_task(
                    self._process_message(websocket, raw_message, semaphore)
                )
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except ConnectionClosed:
            pass
        finally:
            handshake_timeout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await handshake_timeout_task
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            was_authenticated = self._client_authenticated
            async with self._client_lock:
                if self._client is websocket:
                    self._client = None
                    self._client_authenticated = False
                    self._connected.clear()
            if was_authenticated and self._on_disconnected is not None:
                await self._call_lifecycle(self._on_disconnected)

    async def _process_message(
        self,
        websocket: ServerConnection,
        raw_message: str | bytes,
        semaphore: asyncio.Semaphore,
    ) -> None:
        request_id = self._safe_request_id(raw_message)
        async with semaphore:
            try:
                request = decode_request(raw_message)
                request_id = request.request_id
                authenticated_now = False
                if not self._client_authenticated:
                    async with self._handshake_lock:
                        if self._client_authenticated:
                            raise ControlError(
                                "authentication_required", "Handshake is required."
                            )
                        result = await self._handle_handshake(request)
                        # The connection signal is released only after the handshake
                        # response is on the wire. Host snapshots may publish as soon as
                        # wait_until_connected() returns, so earlier authentication would
                        # allow an event to overtake the response.
                        await self._send(
                            websocket, response_envelope(request_id, result)
                        )
                        self._client_authenticated = True
                        self._connected.set()
                        authenticated_now = True
                elif request.method == "handshake":
                    raise ControlError("already_authenticated", "Client is already authenticated.")
                else:
                    result = await asyncio.wait_for(
                        self._handler(request.method, request.params),
                        timeout=self._request_timeout_seconds,
                    )
                if not authenticated_now:
                    await self._send(websocket, response_envelope(request_id, result))
                if authenticated_now and self._on_connected is not None:
                    await self._call_lifecycle(self._on_connected)
            except TimeoutError:
                await self._send(
                    websocket,
                    error_envelope(
                        request_id,
                        ControlError("timeout", "Request timed out.", retryable=True),
                    ),
                )
            except ControlError as exc:
                await self._send(websocket, error_envelope(request_id, exc))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Control request failed")
                await self._send(
                    websocket,
                    error_envelope(
                        request_id,
                        ControlError(
                            "internal_error",
                            "Request could not be completed.",
                            retryable=True,
                        ),
                    ),
                )

    async def _handle_handshake(self, request: ControlRequest) -> dict[str, Any]:
        if request.method != "handshake":
            raise ControlError("authentication_required", "Handshake is required.")
        token = request.params.get("token")
        version = request.params.get("version")
        if not isinstance(token, str) or not hmac.compare_digest(token, self._token):
            raise ControlError("authentication_failed", "Authentication failed.")
        if version != CONTROL_VERSION:
            raise ControlError("unsupported_protocol", "Unsupported control protocol version.")
        return {"protocol": "companion-control", "version": CONTROL_VERSION}

    async def _close_after_handshake_timeout(
        self, websocket: ServerConnection
    ) -> None:
        await asyncio.sleep(self._handshake_timeout_seconds)
        async with self._handshake_lock:
            if self._client is websocket and not self._client_authenticated:
                await websocket.close(code=1008, reason="Handshake timed out")

    async def _send(self, websocket: ServerConnection, value: dict[str, Any]) -> None:
        async with self._send_lock:
            await self._send_locked(websocket, value)

    @staticmethod
    async def _send_locked(
        websocket: ServerConnection, value: dict[str, Any]
    ) -> None:
        # Send text (not bytes): the websockets library encodes str as a TEXT
        # frame, which the AIRI client requires; bytes would go out as BINARY.
        message = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ControlError("message_too_large", "Response exceeds the size limit.")
        await websocket.send(message)

    @staticmethod
    def _safe_request_id(message: str | bytes) -> str:
        try:
            if isinstance(message, bytes):
                value = json.loads(message.decode("utf-8"))
            else:
                value = json.loads(message)
            request_id = value.get("id") if isinstance(value, dict) else None
            if isinstance(request_id, str) and 0 < len(request_id) <= 128:
                return request_id
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        return "invalid"

    @staticmethod
    async def _call_lifecycle(callback: Callable[[], object]) -> None:
        result = callback()
        if inspect.isawaitable(result):
            await result

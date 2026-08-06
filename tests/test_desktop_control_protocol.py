from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from companion.desktop.control_protocol import (
    CONTROL_PROTOCOL,
    CONTROL_VERSION,
    MAX_MESSAGE_BYTES,
    ControlError,
    decode_request,
)
from companion.desktop.control_server import ControlServer


def _request(
    request_id: str,
    method: str,
    params: dict[str, object] | None = None,
) -> str:
    return json.dumps(
        {
            "protocol": CONTROL_PROTOCOL,
            "version": CONTROL_VERSION,
            "type": "request",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
    )


async def _receive_json(websocket) -> dict[str, object]:
    return json.loads(await websocket.recv())


def test_decode_request_validates_envelope_and_bounds() -> None:
    decoded = decode_request(_request("req-1", "runtime.snapshot"))
    assert decoded.request_id == "req-1"
    assert decoded.method == "runtime.snapshot"

    with pytest.raises(ControlError, match="Request id"):
        decode_request(_request("x" * 129, "runtime.snapshot"))
    with pytest.raises(ControlError, match="size limit"):
        decode_request(" " * (MAX_MESSAGE_BYTES + 1))
    with pytest.raises(ControlError, match="Unsupported"):
        value = json.loads(_request("req-2", "runtime.snapshot"))
        value["version"] = 99
        decode_request(json.dumps(value))


@pytest.mark.asyncio
async def test_server_requires_handshake_and_sanitizes_handler_failures() -> None:
    async def handler(method: str, _params: dict[str, object]) -> dict[str, object]:
        if method == "explode":
            raise RuntimeError("SECRET C:/private/provider-response")
        return {"ok": True}

    server = ControlServer("token", handler)
    await server.start()
    try:
        async with connect(server.url) as websocket:
            await websocket.send(_request("req-1", "runtime.snapshot"))
            error = await _receive_json(websocket)
            assert error["error"] == {
                "code": "authentication_required",
                "message": "Handshake is required.",
                "retryable": False,
            }

            await websocket.send(
                _request("req-2", "handshake", {"token": "token", "version": 1})
            )
            response = await _receive_json(websocket)
            assert response["result"] == {
                "protocol": CONTROL_PROTOCOL,
                "version": CONTROL_VERSION,
            }

            await websocket.send(_request("req-3", "explode"))
            failure = await _receive_json(websocket)
            serialized = json.dumps(failure)
            assert failure["error"] == {
                "code": "internal_error",
                "message": "Request could not be completed.",
                "retryable": True,
            }
            assert "SECRET" not in serialized
            assert "private" not in serialized
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_connected_callback_runs_after_handshake_response() -> None:
    callback_completed = asyncio.Event()

    async def handler(_method: str, _params: dict[str, object]) -> dict[str, object]:
        return {}

    async def on_connected() -> None:
        callback_completed.set()

    server = ControlServer("token", handler, on_connected=on_connected)
    await server.start()
    try:
        async with connect(server.url) as websocket:
            await websocket.send(
                _request("hello", "handshake", {"token": "token", "version": 1})
            )
            response = await _receive_json(websocket)
            assert response["type"] == "response"
            await asyncio.wait_for(callback_completed.wait(), timeout=1)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_handshake_response_precedes_connected_snapshot() -> None:
    async def handler(_method: str, _params: dict[str, object]) -> dict[str, object]:
        return {}

    server = ControlServer("token", handler)
    response_send_started = asyncio.Event()
    allow_response = asyncio.Event()
    original_send = server._send

    async def delay_handshake_response(websocket, value) -> None:
        if value.get("type") == "response" and value.get("id") == "hello":
            response_send_started.set()
            await allow_response.wait()
        await original_send(websocket, value)

    server._send = delay_handshake_response  # type: ignore[method-assign]
    await server.start()
    publish_task = asyncio.create_task(_publish_snapshot_after_connection(server))
    try:
        async with connect(server.url) as websocket:
            await websocket.send(
                _request("hello", "handshake", {"token": "token", "version": 1})
            )
            await asyncio.wait_for(response_send_started.wait(), timeout=1)
            await asyncio.sleep(0)
            allow_response.set()

            handshake = await _receive_json(websocket)
            snapshot = await _receive_json(websocket)

            assert handshake["type"] == "response"
            assert handshake["id"] == "hello"
            assert snapshot["type"] == "event"
            assert snapshot["event"] == "runtime.snapshot"
            await asyncio.wait_for(publish_task, timeout=1)
    finally:
        allow_response.set()
        publish_task.cancel()
        await asyncio.gather(publish_task, return_exceptions=True)
        await server.stop()


async def _publish_snapshot_after_connection(server: ControlServer) -> None:
    await server.wait_until_connected(timeout_seconds=1)
    assert await server.publish("runtime.snapshot", {"phase": "starting"})


@pytest.mark.asyncio
async def test_unauthenticated_client_is_closed_after_handshake_timeout() -> None:
    async def handler(_method: str, _params: dict[str, object]) -> dict[str, object]:
        return {}

    server = ControlServer("token", handler, handshake_timeout_seconds=0.05)
    await server.start()
    first = await connect(server.url)
    try:
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(first.recv(), timeout=1)

        async with connect(server.url) as second:
            await second.send(
                _request("hello", "handshake", {"token": "token", "version": 1})
            )
            response = await _receive_json(second)
            assert response["type"] == "response"
    finally:
        await first.close()
        await server.stop()


@pytest.mark.asyncio
async def test_only_one_parallel_handshake_can_authenticate() -> None:
    async def handler(_method: str, _params: dict[str, object]) -> dict[str, object]:
        return {}

    server = ControlServer("token", handler)
    await server.start()
    try:
        async with connect(server.url) as websocket:
            await websocket.send(
                _request("hello-1", "handshake", {"token": "token", "version": 1})
            )
            await websocket.send(
                _request("hello-2", "handshake", {"token": "wrong", "version": 1})
            )
            messages = [await _receive_json(websocket), await _receive_json(websocket)]
            assert sum(message["type"] == "response" for message in messages) == 1
            assert sum(message["type"] == "error" for message in messages) == 1
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_server_rejects_second_client_and_stops_voice_on_disconnect() -> None:
    disconnected = asyncio.Event()

    async def handler(_method: str, _params: dict[str, object]) -> dict[str, object]:
        return {}

    async def on_disconnected() -> None:
        disconnected.set()

    server = ControlServer("token", handler, on_disconnected=on_disconnected)
    await server.start()
    try:
        async with connect(server.url) as first:
            await first.send(
                _request("req-1", "handshake", {"token": "token", "version": 1})
            )
            await _receive_json(first)
            second = await connect(server.url)
            try:
                with pytest.raises(ConnectionClosed):
                    await second.recv()
            finally:
                await second.close()
        await asyncio.wait_for(disconnected.wait(), timeout=1)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_unauthenticated_disconnect_does_not_run_disconnect_callback() -> None:
    disconnected = asyncio.Event()

    async def handler(_method: str, _params: dict[str, object]) -> dict[str, object]:
        return {}

    async def on_disconnected() -> None:
        disconnected.set()

    server = ControlServer("token", handler, on_disconnected=on_disconnected)
    await server.start()
    try:
        websocket = await connect(server.url)
        await websocket.close()
        await asyncio.sleep(0)
        assert not disconnected.is_set()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_failed_handshake_disconnect_does_not_run_disconnect_callback() -> None:
    disconnected = asyncio.Event()

    async def handler(_method: str, _params: dict[str, object]) -> dict[str, object]:
        return {}

    async def on_disconnected() -> None:
        disconnected.set()

    server = ControlServer("token", handler, on_disconnected=on_disconnected)
    await server.start()
    try:
        websocket = await connect(server.url)
        await websocket.send(
            _request("hello", "handshake", {"token": "wrong", "version": 1})
        )
        failure = await _receive_json(websocket)
        assert failure["error"]["code"] == "authentication_failed"
        await websocket.close()
        await asyncio.sleep(0)
        assert not disconnected.is_set()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_outbound_messages_are_bounded() -> None:
    async def handler(_method: str, _params: dict[str, object]) -> dict[str, object]:
        return {"text": "界" * MAX_MESSAGE_BYTES}

    server = ControlServer("token", handler)
    await server.start()
    try:
        async with connect(server.url) as websocket:
            await websocket.send(
                _request("hello", "handshake", {"token": "token", "version": 1})
            )
            await _receive_json(websocket)

            await websocket.send(_request("too-large", "runtime.snapshot"))
            response = await _receive_json(websocket)
            assert response["error"] == {
                "code": "message_too_large",
                "message": "Response exceeds the size limit.",
                "retryable": False,
            }

            assert not await server.publish(
                "runtime.snapshot", {"text": "界" * MAX_MESSAGE_BYTES}
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(websocket.recv(), timeout=0.05)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_server_limits_concurrent_requests() -> None:
    release = asyncio.Event()
    entered = 0

    async def handler(_method: str, _params: dict[str, object]) -> dict[str, object]:
        nonlocal entered
        entered += 1
        await release.wait()
        return {"ok": True}

    server = ControlServer("token", handler, request_timeout_seconds=1)
    await server.start()
    try:
        async with connect(server.url) as websocket:
            await websocket.send(
                _request("hello", "handshake", {"token": "token", "version": 1})
            )
            await _receive_json(websocket)
            for index in range(9):
                await websocket.send(_request(f"req-{index}", "wait"))
            messages = []
            while not any(
                message.get("error", {}).get("code") == "too_many_requests"
                for message in messages
            ):
                messages.append(await asyncio.wait_for(_receive_json(websocket), timeout=1))
            assert entered == 8
            release.set()
    finally:
        release.set()
        await server.stop()


@pytest.mark.asyncio
async def test_parallel_events_keep_unique_increasing_sequences() -> None:
    async def handler(_method: str, _params: dict[str, object]) -> dict[str, object]:
        return {}

    server = ControlServer("token", handler)
    await server.start()
    try:
        async with connect(server.url) as websocket:
            await websocket.send(
                _request("hello", "handshake", {"token": "token", "version": 1})
            )
            await _receive_json(websocket)

            results = await asyncio.gather(
                *(server.publish("runtime.snapshot", {"index": index}) for index in range(12))
            )
            messages = [await _receive_json(websocket) for _ in range(12)]

            assert all(results)
            assert [message["sequence"] for message in messages] == list(range(1, 13))
    finally:
        await server.stop()

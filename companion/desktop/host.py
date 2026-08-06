"""Python-owned lifecycle and RPC semantics for the Windows desktop client."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
from typing import Any

from companion.config_loader import RuntimeConfig
from companion.desktop.control_protocol import ControlError
from companion.desktop.control_server import ControlServer
from companion.desktop.history import ConversationHistoryProjector, HistoryCursorError
from companion.desktop.voice import DesktopVoiceController
from companion.events.base import BaseEvent, generate_ulid
from companion.security.windows_credentials import (
    provision_avatar_bridge_credential,
    read_windows_credential,
    write_windows_credential,
)

logger = logging.getLogger(__name__)
_FORWARDED_EVENTS = frozenset(
    {
        "conversation.turn.started",
        "conversation.turn.interrupted",
        "conversation.turn.completed",
        "conversation.turn.failed",
    }
)


class DesktopHost:
    """Keep AIRI available while providers transition through setup and degradation."""

    def __init__(self, app: Any, config: RuntimeConfig) -> None:
        self._app = app
        self._config = config
        self._token = secrets.token_urlsafe(32)
        self._server = ControlServer(
            self._token,
            self._handle_request,
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
        )
        self._history = ConversationHistoryProjector(app.memory)
        self._voice = DesktopVoiceController(
            config.microphone_config,
            app.voice_pipeline,
            self._on_voice_state_changed,
        )
        self._quit = asyncio.Event()
        self._phase = "starting"
        self._provider_status: dict[str, str] = {}
        self._last_error: dict[str, Any] | None = None
        self._voice_state: dict[str, Any] = {"enabled": False, "state": "off"}
        self._readiness_task: asyncio.Task[None] | None = None
        self._turn_task: asyncio.Task[str] | None = None
        self._active_turn_id = ""
        self._pending_user_text: dict[str, str] = {}
        self._subscribed = False

    async def run(self) -> bool:
        """Run until AIRI requests an application-level quit."""
        if self._config.avatar_stage_launch_config is None:
            logger.error("Desktop mode requires managed AIRI launch configuration")
            return False
        self._ensure_avatar_credential()
        self._subscribe_events()
        self._app.voice_pipeline.set_assistant_delta_callback(self._on_assistant_delta)
        await self._server.start()
        try:
            if not await self._app.start_desktop_runtime(
                control_url=self._server.url,
                control_token=self._token,
            ):
                self._phase = "error"
                self._last_error = {
                    "code": "airi_start_failed",
                    "message": "Desktop interface could not be started.",
                    "retryable": False,
                }
                return False
            try:
                await self._server.wait_until_connected(
                    self._config.avatar_stage_launch_config.startup_timeout_seconds
                )
            except TimeoutError:
                self._phase = "error"
                self._last_error = {
                    "code": "control_connection_timeout",
                    "message": "Desktop interface did not connect in time.",
                    "retryable": False,
                }
                return False
            await self._publish_snapshot()
            self._readiness_task = asyncio.create_task(self._run_readiness())
            await self._readiness_task
            await self._quit.wait()
            return True
        finally:
            self._phase = "stopping"
            if self._readiness_task is not None and not self._readiness_task.done():
                self._readiness_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._readiness_task
            if self._turn_task is not None and not self._turn_task.done():
                self._turn_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._turn_task
            try:
                await self._voice.stop()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Desktop voice cleanup failed")
            finally:
                try:
                    self._app.voice_pipeline.set_assistant_delta_callback(None)
                except Exception:
                    logger.exception("Desktop delta callback cleanup failed")
                try:
                    await self._server.stop()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Desktop control server cleanup failed")

    async def _handle_request(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            if method == "runtime.snapshot":
                self._require_empty_params(params)
                return self._snapshot()
            if method == "runtime.retry":
                self._require_empty_params(params)
                return self._retry_readiness()
            if method == "conversation.sessions.list":
                return await self._history.list_sessions(
                    cursor=self._string(params, "cursor", default=""),
                    limit=self._integer(params, "limit", default=20),
                )
            if method == "conversation.history":
                return await self._history.history(
                    self._string(params, "session_id"),
                    cursor=self._string(params, "cursor", default=""),
                    limit=self._integer(params, "limit", default=50),
                )
            if method == "conversation.send":
                return self._send_conversation(params)
            if method == "conversation.cancel":
                return await self._cancel_conversation(params)
            if method == "voice.start":
                self._require_empty_params(params)
                return await self._start_voice()
            if method == "voice.stop":
                self._require_empty_params(params)
                await self._voice.stop()
                return {"stopped": True}
            if method == "credential.set":
                return self._set_credential(params)
            if method == "application.quit":
                self._require_empty_params(params)
                asyncio.get_running_loop().call_later(0.05, self._quit.set)
                return {"accepted": True}
        except (ValueError, HistoryCursorError) as exc:
            raise ControlError("invalid_params", "Request parameters are invalid.") from exc
        raise ControlError("method_not_found", "Control method is not available.")

    def _retry_readiness(self) -> dict[str, Any]:
        if self._active_turn_id:
            raise ControlError(
                "runtime_busy", "Readiness cannot be retried during an active turn.", retryable=True
            )
        if self._readiness_task is not None and not self._readiness_task.done():
            return {"accepted": True}
        self._readiness_task = asyncio.create_task(self._run_readiness())
        return {"accepted": True}

    def _send_conversation(self, params: dict[str, Any]) -> dict[str, Any]:
        text = self._string(params, "text").strip()
        speak = self._boolean(params, "speak", default=False)
        if not 1 <= len(text) <= 8000:
            raise ValueError("text length is invalid")
        if self._active_turn_id or (self._turn_task and not self._turn_task.done()):
            raise ControlError(
                "conversation_busy",
                "A conversation turn is already active.",
                retryable=True,
            )
        if self._provider_status.get("runtime") != "healthy":
            raise ControlError(
                "setup_required",
                "The language provider is not ready.",
                retryable=True,
            )
        if speak and self._provider_status.get("tts") != "healthy":
            raise ControlError("voice_unavailable", "Speech output is not ready.", retryable=True)
        turn_id = f"turn_{generate_ulid()}"
        self._active_turn_id = turn_id
        self._pending_user_text[turn_id] = text
        task = asyncio.create_task(
            self._app.voice_pipeline.process_text_input(
                text, speak=speak, turn_id=turn_id
            )
        )
        self._turn_task = task
        task.add_done_callback(lambda completed: self._turn_done(turn_id, completed))
        return {"turn_id": turn_id}

    async def _cancel_conversation(self, params: dict[str, Any]) -> dict[str, Any]:
        turn_id = self._string(params, "turn_id")
        if not turn_id or len(turn_id) > 128:
            raise ValueError("turn id is invalid")
        if turn_id != self._active_turn_id:
            return {"cancelled": False}
        cancelled = await self._app.voice_pipeline.cancel(turn_id)
        return {"cancelled": cancelled}

    async def _start_voice(self) -> dict[str, Any]:
        if self._active_turn_id:
            raise ControlError(
                "conversation_busy",
                "A conversation turn is already active.",
                retryable=True,
            )
        if self._provider_status.get("runtime") != "healthy":
            raise ControlError(
                "setup_required",
                "The language provider is not ready.",
                retryable=True,
            )
        if not await self._app.prepare_desktop_voice():
            raise ControlError(
                "voice_unavailable",
                "Voice providers are not ready.",
                retryable=True,
            )
        self._provider_status.update({"asr": "healthy", "tts": "healthy"})
        self._update_phase_from_provider_status()
        await self._publish_snapshot()
        if not await self._voice.start():
            raise ControlError(
                "microphone_unavailable",
                "The default microphone is unavailable.",
                retryable=True,
            )
        return {"started": True}

    def _set_credential(self, params: dict[str, Any]) -> dict[str, Any]:
        kind = self._string(params, "kind")
        value = self._string(params, "value")
        overwrite = self._boolean(params, "overwrite", default=False)
        if kind not in {"llm", "tts"}:
            raise ValueError("credential kind is invalid")
        if not value.strip():
            raise ValueError("credential value is empty")
        provider_config = (
            self._config.llm_config if kind == "llm" else self._config.tts_config
        )
        if provider_config is None or not provider_config.credential_target:
            raise ControlError("credential_unavailable", "Credential storage is not configured.")
        if provider_config.api_key_env and os.environ.get(provider_config.api_key_env, ""):
            raise ControlError(
                "environment_override",
                "Credential is controlled by the process environment.",
            )
        try:
            write_windows_credential(
                provider_config.credential_target,
                value,
                overwrite=overwrite,
            )
        except FileExistsError as exc:
            raise ControlError(
                "credential_exists",
                "Credential already exists; explicit overwrite is required.",
            ) from exc
        if self._active_turn_id:
            # The credential is already persisted. Scheduling readiness during an
            # active turn would raise runtime_busy and misreport a successful
            # write as failed; the UI snapshot reflects the new state immediately
            # and readiness resumes after the turn ends.
            asyncio.create_task(self._publish_snapshot())
        else:
            self._retry_readiness()
        return {"kind": kind, "status": "windows_credential"}

    async def _run_readiness(self) -> None:
        self._phase = "checking"
        self._last_error = None
        await self._publish_snapshot()
        try:
            self._provider_status = await self._app.retry_desktop_readiness()
            self._update_phase_from_provider_status()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Desktop provider readiness failed")
            self._phase = "error"
            self._last_error = {
                "code": "readiness_failed",
                "message": "Provider readiness could not be completed.",
                "retryable": True,
            }
        await self._publish_snapshot()

    def _update_phase_from_provider_status(self) -> None:
        if self._provider_status.get("llm") != "healthy":
            self._phase = "setup_required"
        elif self._provider_status.get("runtime") != "healthy":
            self._phase = "error"
            self._last_error = {
                "code": "runtime_unavailable",
                "message": "The companion runtime is not ready.",
                "retryable": True,
            }
        elif (
            self._provider_status.get("tts") != "healthy"
            or self._provider_status.get("asr") != "healthy"
        ):
            self._phase = "degraded"
        else:
            self._phase = "ready"

    async def _on_connected(self) -> None:
        asyncio.create_task(self._publish_snapshot())

    async def _on_disconnected(self) -> None:
        try:
            await self._voice.stop()
        finally:
            self._quit.set()

    async def _on_voice_state_changed(self, state: dict[str, Any]) -> None:
        self._voice_state = state.copy()
        await self._server.publish("voice.state.changed", self._voice_state)
        await self._publish_snapshot()

    async def _on_assistant_delta(self, turn_id: str, text: str) -> None:
        if turn_id == self._active_turn_id:
            await self._server.publish(
                "conversation.response.delta", {"turn_id": turn_id, "text": text}
            )

    async def _on_domain_event(self, event: BaseEvent) -> None:
        if event.event_type in _FORWARDED_EVENTS:
            payload = event.model_dump(mode="json", exclude={"header"})
            payload.pop("companion_full_text", None)
            if event.event_type == "conversation.turn.started":
                turn_id = str(payload.get("turn_id", ""))
                if turn_id:
                    self._active_turn_id = turn_id
                payload["user_text"] = self._pending_user_text.get(turn_id, "")
            await self._server.publish(event.event_type, payload)
            if event.event_type == "conversation.turn.started":
                await self._publish_snapshot()
            if event.event_type in {
                "conversation.turn.completed",
                "conversation.turn.failed",
            }:
                self._finish_active_turn(str(payload.get("turn_id", "")))
                await self._publish_snapshot()
        elif event.event_type.startswith("emotion."):
            await self._server.publish("emotion.changed", self._emotion_snapshot())

    def _turn_done(self, turn_id: str, task: asyncio.Future[str]) -> None:
        if not task.cancelled():
            with contextlib.suppress(Exception):
                task.result()
        if turn_id == self._active_turn_id:
            # Normal paths publish their terminal before the task completes. This
            # fallback prevents an unexpected task failure from leaving the host
            # busy and still publishes the required single failure terminal.
            asyncio.create_task(self._publish_unexpected_turn_failure(turn_id))

    async def _publish_unexpected_turn_failure(self, turn_id: str) -> None:
        await asyncio.sleep(0)
        if self._active_turn_id != turn_id:
            return
        await self._server.publish(
            "conversation.turn.failed",
            {
                "turn_id": turn_id,
                "stage": "generation",
                "error_type": "runtime_error",
                "retryable": True,
            },
        )
        self._finish_active_turn(turn_id)
        await self._publish_snapshot()

    def _finish_active_turn(self, turn_id: str) -> None:
        self._pending_user_text.pop(turn_id, None)
        if self._active_turn_id == turn_id:
            self._active_turn_id = ""
            self._turn_task = None

    def _snapshot(self) -> dict[str, Any]:
        llm_status = self._credential_status(self._config.llm_config)
        tts_status = self._credential_status(self._config.tts_config)
        return {
            "phase": self._phase,
            "capabilities": {
                "text_chat": self._provider_status.get("runtime") == "healthy",
                "voice_input": self._provider_status.get("asr") == "healthy",
                "speech_output": self._provider_status.get("tts") == "healthy",
                "avatar": self._provider_status.get("avatar") == "healthy",
            },
            "providers": self._provider_status.copy(),
            "credentials": {"llm": llm_status, "tts": tts_status},
            "voice": {
                **self._voice_state,
                "device": "default",
                "text_reply_speech_default": False,
                "continuous_voice_speech_default": True,
            },
            "emotion": self._emotion_snapshot(),
            "identity": {
                "name": self._app.state.identity.name,
                "avatar_model_id": self._app.state.identity.avatar_model_id,
            },
            "session_id": self._app.voice_pipeline.current_session_id,
            "active_turn": (
                {"turn_id": self._active_turn_id} if self._active_turn_id else None
            ),
            "error": self._last_error,
        }

    def _emotion_snapshot(self) -> dict[str, Any]:
        affect = self._app.state.affect
        return {
            "dominant": self._app.state.dominant_emotion(),
            "valence": affect.valence,
            "arousal": affect.arousal,
            "energy": affect.energy,
        }

    async def _publish_snapshot(self) -> None:
        await self._server.publish("runtime.snapshot", self._snapshot())

    def _ensure_avatar_credential(self) -> None:
        config = self._config.avatar_config
        if config is None or not config.credential_target:
            raise ValueError("desktop mode requires an Avatar Bridge credential target")
        if os.environ.get(config.auth_token_env, "") or config.get_auth_token():
            return
        with contextlib.suppress(FileExistsError):
            provision_avatar_bridge_credential(config.credential_target, overwrite=False)

    @staticmethod
    def _credential_status(config: Any | None) -> str:
        if config is None:
            return "missing"
        if config.api_key_env and os.environ.get(config.api_key_env, ""):
            return "environment"
        if getattr(config, "api_key", ""):
            return "windows_credential"
        if config.credential_target and read_windows_credential(config.credential_target):
            return "windows_credential"
        return "missing"

    def _subscribe_events(self) -> None:
        if self._subscribed:
            return
        self._app.event_bus.on_any()(self._on_domain_event)
        self._subscribed = True

    @staticmethod
    def _require_empty_params(params: dict[str, Any]) -> None:
        if params:
            raise ValueError("params must be empty")

    @staticmethod
    def _string(params: dict[str, Any], name: str, *, default: str | None = None) -> str:
        value = params.get(name, default)
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        return value

    @staticmethod
    def _integer(params: dict[str, Any], name: str, *, default: int) -> int:
        value = params.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        return value

    @staticmethod
    def _boolean(params: dict[str, Any], name: str, *, default: bool) -> bool:
        value = params.get(name, default)
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean")
        return value

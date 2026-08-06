"""Cloud LLM provider — Anthropic and OpenAI implementations.

Supports:
- Anthropic Claude (via Messages API)
- OpenAI compatible (GPT, DeepSeek, etc.)
- Streaming with cancellation
- Automatic retry with exponential backoff
- Provider health monitoring
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from companion.async_util import consume_task_result, wait_with_timeout
from companion.providers.base import ProviderCapability, ProviderHealth, ProviderInfo
from companion.providers.model import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
)
from companion.security.redaction import redact_text
from companion.security.windows_credentials import configured_secret_sources, resolve_secret

logger = logging.getLogger(__name__)


async def _iter_sse_data(resp: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Yield parsed JSON objects from SSE ``data:`` lines.

    Skips non-``data:`` lines, the ``[DONE]`` sentinel, and malformed JSON.
    """
    async for line in resp.aiter_lines():
        if not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str == "[DONE]":
            return
        try:
            yield json.loads(data_str)
        except json.JSONDecodeError:
            continue


@dataclass
class CloudLLMConfig:
    """Configuration for a cloud LLM provider."""

    provider: str = "anthropic"  # 'anthropic', 'openai', 'openai_compatible'
    model: str = "claude-sonnet-5"
    api_key: str = ""
    api_key_env: str = "ANTHROPIC_API_KEY"
    credential_target: str = ""
    base_url: str = ""
    max_retries: int = 3
    retry_delay_seconds: float = 1.0
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        configured_secret_sources(
            env_name=self.api_key_env, credential_target=self.credential_target
        )
        if self.provider not in {"anthropic", "openai", "openai_compatible"}:
            raise ValueError("unsupported cloud LLM provider")
        if not self.model.strip():
            raise ValueError("cloud LLM model must not be empty")
        if self.max_retries < 1 or self.retry_delay_seconds < 0 or self.timeout_seconds <= 0:
            raise ValueError("cloud LLM retry and timeout settings are invalid")
        endpoint = self.get_base_url()
        parsed = urlsplit(endpoint)
        if (
            not endpoint
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("cloud LLM endpoint must use https://")

    def get_api_key(self) -> str:
        """Resolve a test-injected key, environment override, or Windows credential."""
        if self.api_key:
            return self.api_key
        return resolve_secret(
            env_name=self.api_key_env, credential_target=self.credential_target
        ).value

    def credential_source(self) -> str:
        if self.api_key:
            return "in-process credential injection"
        resolved = resolve_secret(
            env_name=self.api_key_env, credential_target=self.credential_target
        )
        return resolved.source or configured_secret_sources(
            env_name=self.api_key_env, credential_target=self.credential_target
        )

    def get_base_url(self) -> str:
        """Get the API endpoint URL."""
        if self.base_url:
            return self.base_url
        if self.provider == "anthropic":
            return "https://api.anthropic.com/v1/messages"
        if self.provider == "openai":
            return "https://api.openai.com/v1/chat/completions"
        return ""


class CloudLLMProvider(LLMProvider):
    """Cloud-based LLM provider (Anthropic / OpenAI compatible)."""

    def __init__(self, config: CloudLLMConfig | None = None) -> None:
        self._config = config or CloudLLMConfig()
        self._client: httpx.AsyncClient | None = None
        self._active_cancellations: set[str] = set()
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}
        self._cancel_lock: asyncio.Lock = asyncio.Lock()
        self._last_health: ProviderHealth = ProviderHealth.UNKNOWN
        self._last_health_time: float = 0.0
        self._total_requests: int = 0
        self._failed_requests: int = 0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._config.timeout_seconds)
        return self._client

    # ── Main generate methods ─────────────────────────────────────────

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self._total_requests += 1

        api_key = self._config.get_api_key()
        if not api_key:
            return LLMResponse(
                text="[LLM not configured: no API key]",
                turn_id=request.turn_id,
                model_id=self._config.model,
                model_provider="cloud",
                finish_reason="error",
            )

        generation_task = asyncio.create_task(self._generate_with_retries(request, api_key))
        if request.turn_id:
            self._active_tasks[request.turn_id] = generation_task
        try:
            if not await wait_with_timeout(
                generation_task, self._config.timeout_seconds
            ):
                self._failed_requests += 1
                logger.error("LLM generation exceeded the configured total timeout")
                return self._error_response(request)
            return await generation_task
        except asyncio.CancelledError:
            # Stop the generation and drain its result so a failure racing the
            # cancellation is not reported as an unretrieved task exception.
            generation_task.cancel()
            generation_task.add_done_callback(consume_task_result)
            raise
        finally:
            if request.turn_id and self._active_tasks.get(request.turn_id) is generation_task:
                self._active_tasks.pop(request.turn_id, None)
            self._active_cancellations.discard(request.turn_id)

    async def _generate_with_retries(
        self, request: LLMRequest, api_key: str
    ) -> LLMResponse:
        t0 = time.time()
        last_error: Exception | None = None
        for attempt in range(self._config.max_retries):
            try:
                response_data = await self._make_api_call(request, api_key, stream=False)
                elapsed_ms = int((time.time() - t0) * 1000)
                return LLMResponse(
                    text=response_data["text"],
                    turn_id=request.turn_id,
                    model_id=response_data.get("model", self._config.model),
                    model_provider="cloud",
                    token_count=response_data.get("tokens", 0),
                    time_to_first_token_ms=elapsed_ms,
                    total_latency_ms=elapsed_ms,
                    finish_reason=response_data.get("finish_reason", "stop"),
                )
            except httpx.HTTPStatusError as e:
                last_error = e
                if (
                    self._is_retryable_status(e.response.status_code)
                    and attempt < self._config.max_retries - 1
                ):
                    delay = self._config.retry_delay_seconds * (2**attempt)
                    logger.warning(
                        "LLM API returned status %d (attempt %d/%d), retrying in %.1fs",
                        e.response.status_code,
                        attempt + 1,
                        self._config.max_retries,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    break
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                last_error = e
                if attempt < self._config.max_retries - 1:
                    await asyncio.sleep(self._config.retry_delay_seconds * (2**attempt))
                else:
                    break
            except Exception as e:
                last_error = e
                break

        self._failed_requests += 1
        logger.error(
            "LLM API call failed after %d attempts: %s",
            self._config.max_retries,
            redact_text(last_error),
        )
        return self._error_response(request)

    def _error_response(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text="抱歉，我的大脑暂时离线了...",
            turn_id=request.turn_id,
            model_id=self._config.model,
            model_provider="cloud",
            finish_reason="error",
        )

    async def generate_stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        api_key = self._config.get_api_key()
        if not api_key:
            yield LLMStreamChunk(
                text="[LLM not configured]", turn_id=request.turn_id, is_first=True, is_final=True
            )
            return

        token_index = 0
        is_first = True
        stream = self._make_streaming_call(request, api_key).__aiter__()
        stream_task: asyncio.Task[str] | None = None
        deadline = time.monotonic() + self._config.timeout_seconds
        owner_task = asyncio.current_task()
        if request.turn_id:
            if owner_task is None:
                raise RuntimeError("streaming generation requires an asyncio task")
            self._active_tasks[request.turn_id] = owner_task
        try:
            while True:
                next_chunk: Awaitable[str] = stream.__anext__()
                stream_task = asyncio.ensure_future(next_chunk)
                remaining = max(0.0, deadline - time.monotonic())
                done, _ = await asyncio.wait([stream_task], timeout=remaining)
                if not done:
                    stream_task.cancel()
                    stream_task.add_done_callback(consume_task_result)
                    raise TimeoutError("stream total timeout")
                try:
                    text_chunk = await stream_task
                except StopAsyncIteration:
                    break
                if request.turn_id in self._active_cancellations:
                    yield LLMStreamChunk(
                        text="", turn_id=request.turn_id, is_final=True, token_index=token_index
                    )
                    return
                yield LLMStreamChunk(
                    text=text_chunk,
                    turn_id=request.turn_id,
                    is_first=is_first,
                    is_final=False,
                    token_index=token_index,
                )
                is_first = False
                token_index += 1
        except asyncio.CancelledError:
            if stream_task is not None and not stream_task.done():
                stream_task.cancel()
                stream_task.add_done_callback(consume_task_result)
            raise
        except Exception as e:
            logger.error("Streaming LLM error: %s", redact_text(e))
            raise RuntimeError("Streaming LLM request failed") from None
        else:
            yield LLMStreamChunk(
                text="", turn_id=request.turn_id, is_final=True, token_index=token_index
            )
        finally:
            if request.turn_id and self._active_tasks.get(request.turn_id) is owner_task:
                self._active_tasks.pop(request.turn_id, None)
            self._active_cancellations.discard(request.turn_id)

    async def cancel(self, turn_id: str) -> bool:
        async with self._cancel_lock:
            task = self._active_tasks.get(turn_id)
            if task is not None and not task.done():
                self._active_cancellations.add(turn_id)
                task.cancel()
                task.add_done_callback(consume_task_result)
                return True
        return False

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code in {408, 409, 425, 429} or status_code >= 500

    # ── Internal API call builders ────────────────────────────────────

    async def _make_api_call(
        self, request: LLMRequest, api_key: str, stream: bool
    ) -> dict[str, Any]:
        if self._config.provider == "anthropic":
            return await self._call_anthropic(request, api_key, stream)
        else:
            return await self._call_openai(request, api_key, stream)

    async def _call_anthropic(
        self, request: LLMRequest, api_key: str, stream: bool
    ) -> dict[str, Any]:
        client = await self._get_client()
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        # Build messages from request
        system_prompt = request.system_prompt
        messages = []
        for m in request.messages:
            if m["role"] == "system":
                system_prompt = m["content"]
            else:
                messages.append({"role": m["role"], "content": m["content"]})

        body = {
            "model": request.model_hint or self._config.model,
            "max_tokens": min(request.max_tokens, 4096),
            "messages": messages,
            "stream": stream,
        }
        if system_prompt:
            body["system"] = system_prompt
        if request.temperature > 0:
            body["temperature"] = request.temperature

        url = self._config.get_base_url()
        resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        content_blocks = data.get("content", [])
        text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
        return {
            "text": text,
            "model": data.get("model", self._config.model),
            "tokens": data.get("usage", {}).get("output_tokens", 0),
            "finish_reason": data.get("stop_reason", "stop"),
        }

    async def _call_openai(self, request: LLMRequest, api_key: str, stream: bool) -> dict[str, Any]:
        client = await self._get_client()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }

        body = {
            "model": request.model_hint or self._config.model,
            "messages": request.messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": stream,
        }

        url = self._config.get_base_url()
        resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        choices = data.get("choices", [{}])
        message = choices[0].get("message", {})
        return {
            "text": message.get("content", ""),
            "model": data.get("model", self._config.model),
            "tokens": data.get("usage", {}).get("completion_tokens", 0),
            "finish_reason": choices[0].get("finish_reason", "stop"),
        }

    async def _make_streaming_call(self, request: LLMRequest, api_key: str) -> AsyncIterator[str]:
        """Yield text chunks from a streaming API call."""
        if self._config.provider == "anthropic":
            async for chunk in self._stream_anthropic(request, api_key):
                yield chunk
        else:
            async for chunk in self._stream_openai(request, api_key):
                yield chunk

    async def _stream_anthropic(self, request: LLMRequest, api_key: str) -> AsyncIterator[str]:
        """Stream from Anthropic Messages API with SSE."""
        client = await self._get_client()
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        system_prompt = request.system_prompt
        messages = [m for m in request.messages if m["role"] != "system"]
        for m in request.messages:
            if m["role"] == "system":
                system_prompt = m["content"]

        body = {
            "model": request.model_hint or self._config.model,
            "max_tokens": min(request.max_tokens, 4096),
            "messages": messages,
            "stream": True,
        }
        if system_prompt:
            body["system"] = system_prompt

        url = self._config.get_base_url()
        async with client.stream("POST", url, json=body, headers=headers) as resp:
            resp.raise_for_status()
            async for data in _iter_sse_data(resp):
                if data.get("type") == "content_block_delta":
                    delta = data.get("delta", {})
                    text = delta.get("text", "")
                    if text:
                        yield text

    async def _stream_openai(self, request: LLMRequest, api_key: str) -> AsyncIterator[str]:
        """Stream from OpenAI-compatible API with SSE."""
        client = await self._get_client()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }
        body = {
            "model": request.model_hint or self._config.model,
            "messages": request.messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": True,
        }
        url = self._config.get_base_url()
        async with client.stream("POST", url, json=body, headers=headers) as resp:
            resp.raise_for_status()
            async for data in _iter_sse_data(resp):
                choices = data.get("choices", [{}])
                delta = choices[0].get("delta", {})
                text = delta.get("content", "")
                if text:
                    yield text

    # ── Provider lifecycle ────────────────────────────────────────────

    def provider_info(self) -> ProviderInfo:
        return ProviderInfo(
            name=f"cloud-llm-{self._config.provider}",
            version="0.1.0",
            capabilities=[
                ProviderCapability.CLOUD,
                ProviderCapability.STREAMING,
                ProviderCapability.HIGH_QUALITY,
                ProviderCapability.CHINESE,
                ProviderCapability.ENGLISH,
            ],
            health=self._last_health,
            last_health_check=self._last_health_time,
        )

    async def health_check(self) -> ProviderHealth:
        api_key = self._config.get_api_key()
        if not api_key:
            self._last_health = ProviderHealth.UNHEALTHY
            return self._last_health
        try:
            # Connectivity alone is not readiness: rejected credentials and an
            # invalid route must block startup.
            client = await self._get_client()
            url = self._config.get_base_url()
            headers = (
                {"x-api-key": api_key}
                if self._config.provider == "anthropic"
                else {"Authorization": f"Bearer {api_key}"}
            )
            if self._config.provider == "anthropic":
                headers["anthropic-version"] = "2023-06-01"
            parsed = urlsplit(url)
            api_root = parsed.path.split("/chat/completions", 1)[0]
            api_root = api_root.split("/messages", 1)[0]
            health_url = urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc,
                    f"{api_root.rstrip('/')}/models",
                    "",
                    "",
                )
            )
            resp = await client.get(health_url, headers=headers, timeout=5.0)
            if 200 <= resp.status_code < 300:
                self._last_health = ProviderHealth.HEALTHY
            elif resp.status_code == 429 or 500 <= resp.status_code < 600:
                self._last_health = ProviderHealth.DEGRADED
            else:
                self._last_health = ProviderHealth.UNHEALTHY
        except httpx.ConnectError:
            self._last_health = ProviderHealth.UNHEALTHY
        except httpx.TimeoutException:
            self._last_health = ProviderHealth.DEGRADED
        except Exception:
            self._last_health = ProviderHealth.UNHEALTHY
        self._last_health_time = time.time()
        return self._last_health

    async def shutdown(self) -> None:
        active_tasks = tuple(self._active_tasks.values())
        self._active_tasks.clear()
        for task in active_tasks:
            task.cancel()
            task.add_done_callback(consume_task_result)
        self._active_cancellations.clear()
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def error_rate(self) -> float:
        if self._total_requests == 0:
            return 0.0
        return self._failed_requests / self._total_requests

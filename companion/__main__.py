"""Entry point for the Virtual Companion CLI.

Usage:
    python -m companion              # Interactive chat mode
    python -m companion --once "你好"  # Single message
    python -m companion --config path/to/config.yaml  # Custom config
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from companion.audio.microphone import MicrophoneCapture, VoiceChatMode
from companion.audio.player import SoundDeviceAudioOutput, SystemAudioOutput
from companion.avatar_acceptance import (
    failed_avatar_acceptance_report,
    render_avatar_acceptance_report,
    run_avatar_acceptance,
    shutdown_avatar_acceptance_provider,
)
from companion.config_loader import RuntimeConfig
from companion.core.event_bus import EventBus
from companion.core.expression_mapper import ExpressionMapper
from companion.core.orchestrator import CompanionOrchestrator
from companion.core.policy_gate import PolicyGate
from companion.core.state_manager import StateManager
from companion.diagnostics import render_diagnostic_report, run_diagnostics
from companion.memory.memory_service import MemoryService
from companion.providers.base import ProviderHealth
from companion.providers.implementations.cloud_llm import CloudLLMProvider
from companion.providers.implementations.cloud_tts import CloudTTSProvider
from companion.providers.implementations.faster_whisper_asr import (
    FasterWhisperASRProvider,
)
from companion.providers.implementations.websocket_avatar import WebSocketAvatarProvider
from companion.providers.implementations.windows_readonly_action import (
    WindowsReadOnlyActionProvider,
)
from companion.security.action_audit import SQLiteActionAuditStore
from companion.security.redaction import RedactingFormatter
from companion.services.action_service import ActionService
from companion.services.proactive_scheduler import ProactiveScheduler, SchedulerConfig
from companion.services.voice_pipeline import VoicePipeline
from companion.voice_acceptance import (
    failed_voice_acceptance_report,
    render_voice_acceptance_report,
    run_voice_acceptance,
)

_SHUTDOWN_STEP_TIMEOUT_SECONDS = 5.0

# ── Logging setup ──────────────────────────────────────────────────────


def setup_logging(
    level: str = "INFO",
    log_file: str = "",
    *,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Configure logging for the companion runtime."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
        )

    formatter = RedactingFormatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )


# ── Output formatting ─────────────────────────────────────────────────


class Colors:
    """ANSI color codes for terminal output."""

    CYAN = "\033[36m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"


def print_companion_name(name: str) -> None:
    """Print the companion's name as a header."""
    print(f"\n{Colors.CYAN}{Colors.BOLD}═══ {name} ═══{Colors.RESET}")
    print(f"{Colors.DIM}输入消息开始对话，输入 /quit 退出，/help 查看帮助{Colors.RESET}\n")


def print_response(name: str, text: str, emotion: str = "", latency_ms: int = 0) -> None:
    """Print the companion's response with formatting."""
    tag = f"[{emotion}] " if emotion else ""
    timing = f" {Colors.DIM}({latency_ms}ms){Colors.RESET}" if latency_ms else ""
    print(f"{Colors.CYAN}{name}{Colors.RESET}: {tag}{text}{timing}")


def print_help() -> None:
    """Print available commands."""
    print(f"""{Colors.YELLOW}可用命令:{Colors.RESET}
  {Colors.BOLD}/quit{Colors.RESET}    退出对话
  {Colors.BOLD}/help{Colors.RESET}    显示此帮助
  {Colors.BOLD}/emotion{Colors.RESET}  查看当前情绪状态
  {Colors.BOLD}/memory{Colors.RESET}   查看记忆中的事实
  {Colors.BOLD}/history{Colors.RESET}  查看最近对话
  {Colors.BOLD}/state{Colors.RESET}    查看完整状态快照
  {Colors.BOLD}/action{Colors.RESET}   运行只读诊断: status/window/app
""")


# ── Application ───────────────────────────────────────────────────────


class CompanionApp:
    """Manages the companion application lifecycle."""

    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        self._stop_task: asyncio.Task[None] | None = None

        # Core components
        self._state_mgr = StateManager()
        self._bus = EventBus(name="main", max_log_size=config.event_log_retention)
        self._policy = PolicyGate(config.policy_config)

        # Apply identity from config
        if config.identity:
            with contextlib.suppress(ValueError):
                self._state_mgr.update_identity(config.identity)

        # Provider configuration
        self._memory = MemoryService(config.effective_memory_config())
        self._bus.set_persistence_handler(self._memory.append_domain_event)

        llm_config = config.llm_config
        self._llm = CloudLLMProvider(llm_config) if llm_config else None
        if self._llm:
            assert llm_config is not None
            api_key = llm_config.get_api_key()
            if api_key:
                logger = logging.getLogger(__name__)
                logger.info("LLM API key found for %s", llm_config.provider)
            else:
                print(
                    f"{Colors.YELLOW}⚠ 未找到 LLM API Key。请设置环境变量 "
                    f"{llm_config.api_key_env}{Colors.RESET}"
                )
                print(
                    f"{Colors.YELLOW}  例如: export {llm_config.api_key_env}=your-key{Colors.RESET}"
                )

        self._tts = CloudTTSProvider(config.tts_config) if config.tts_config else None
        self._asr = FasterWhisperASRProvider(config.asr_config) if config.asr_config else None
        self._avatar = (
            WebSocketAvatarProvider(config.avatar_config) if config.avatar_config else None
        )
        self._action_provider = (
            WindowsReadOnlyActionProvider(config.action_provider_config)
            if config.action_provider_config
            else None
        )
        self._action_audit = (
            SQLiteActionAuditStore(config.action_audit_db_path)
            if config.action_service_config and config.action_audit_db_path
            else None
        )
        self._action_service = (
            ActionService(
                provider=self._action_provider,
                bus=self._bus,
                config=config.action_service_config,
                policy_gate=self._policy,
                audit_store=self._action_audit,
            )
            if config.action_service_config
            else None
        )

        # Expression mapper (emotion → facial/voice params)
        self._expression_mapper = ExpressionMapper()

        # Proactive scheduler (controls when companion initiates)
        self._proactive = ProactiveScheduler(
            policy_gate=self._policy,
            bus=self._bus,
            config=SchedulerConfig(periodic_check_interval_seconds=60.0),
        )

        # Orchestrator
        self._orchestrator = CompanionOrchestrator(
            state_manager=self._state_mgr,
            event_bus=self._bus,
            policy_gate=self._policy,
            llm_provider=self._llm,
            tts_provider=self._tts,
            asr_provider=self._asr,
            memory_provider=self._memory,
            avatar_provider=self._avatar,
            action_provider=self._action_provider,
        )
        self._audio_output = SystemAudioOutput()
        self._voice_audio_output = SoundDeviceAudioOutput()
        self._voice_pipeline = VoicePipeline(
            state=self._state_mgr,
            bus=self._bus,
            policy=self._policy,
            asr=self._asr,
            tts=self._tts,
            audio_output=self._voice_audio_output,
            runtime=self._orchestrator,
            config=config.voice_pipeline_config,
        )

    @property
    def orchestrator(self) -> CompanionOrchestrator:
        return self._orchestrator

    @property
    def state(self) -> StateManager:
        return self._state_mgr

    @property
    def memory(self) -> MemoryService:
        return self._memory

    @property
    def voice_pipeline(self) -> VoicePipeline:
        return self._voice_pipeline

    @property
    def action_service(self) -> ActionService | None:
        return self._action_service

    @property
    def event_bus(self) -> EventBus:
        return self._bus

    async def start(self) -> bool:
        """Start the companion. Returns True if LLM is available."""
        print(f"{Colors.BLUE}正在启动虚拟伴侣…{Colors.RESET}")
        if self._action_audit:
            try:
                if not await self._action_audit.verify_chain():
                    print(f"{Colors.RED}✗ 行动审计链校验失败，启动已中止。{Colors.RESET}")
                    return False
            except Exception:
                logging.getLogger(__name__).exception("Action audit readiness check failed")
                print(f"{Colors.RED}✗ 行动审计存储不可用，启动已中止。{Colors.RESET}")
                return False
        if not await self._orchestrator.startup():
            print(f"{Colors.RED}✗ 必需服务未就绪，启动已中止。{Colors.RESET}")
            return False

        if not self._llm:
            print(f"{Colors.YELLOW}⚠ LLM 未配置。请设置 API Key 后重试。{Colors.RESET}")
            return False

        llm_config = self._config.llm_config
        if llm_config is None:
            print(f"{Colors.YELLOW}⚠ LLM 未配置。请设置 API Key 后重试。{Colors.RESET}")
            return False
        api_key = llm_config.get_api_key() if llm_config else ""
        if not api_key:
            key_source = llm_config.api_key_file or llm_config.api_key_env or "API Key"
            print(f"{Colors.YELLOW}⚠ 未检测到 API Key（来源: {key_source}）。{Colors.RESET}")
            return False

        # Validate API key format (basic check)
        if len(api_key) < 20:
            print(
                f"{Colors.YELLOW}⚠ API Key 似乎不合法（长度太短）。请检查环境变量。{Colors.RESET}"
            )

        print(f"{Colors.GREEN}✓ 虚拟伴侣已就绪{Colors.RESET}")
        if self._llm:
            provider = self._config.llm_config.provider if self._config.llm_config else "unknown"
            model = self._config.llm_config.model if self._config.llm_config else "unknown"
            print(f"{Colors.DIM}  模型: {provider}/{model}{Colors.RESET}")
        print()
        return True

    async def chat(self, message: str, speak: bool = False) -> dict[str, Any]:
        """Send a message and get a response.

        If speak=True and TTS is available, speak the response aloud.
        """
        result = await self._orchestrator.process_user_input(message)

        # Text is already visible, so this optional voice rendering does not
        # control whether the conversation turn is committed.
        if speak and self._tts and result.get("response_text", "").startswith("[") is False:
            try:
                from companion.providers.tts import TTSRequest

                tts_req = TTSRequest(
                    text=result["response_text"],
                    turn_id=result.get("turn_id", "tts"),
                    sample_rate=(
                        self._config.tts_config.sample_rate
                        if self._config.tts_config
                        else 24000
                    ),
                )
                tts_chunk = await self._tts.synthesize(tts_req)
                if tts_chunk.audio_bytes:
                    await self._audio_output.play(tts_chunk.audio_bytes, tts_chunk.sample_rate)
            except Exception:
                logging.getLogger(__name__).exception("Optional TTS playback failed")

        return result

    async def start_voice_mode(self) -> bool:
        """Validate and preload the required local voice components."""
        if not self._asr or not self._tts:
            print(f"{Colors.RED}✗ 语音模式缺少 ASR 或 TTS 配置。{Colors.RESET}")
            return False
        try:
            await self._asr.preload()
        except Exception as exc:
            logging.getLogger(__name__).error("ASR preload failed: %s", exc)
            print(f"{Colors.RED}✗ ASR 无法启动；请安装 virtual-companion[voice]。{Colors.RESET}")
            return False
        if await self._asr.health_check() != ProviderHealth.HEALTHY:
            print(f"{Colors.RED}✗ ASR 未就绪。{Colors.RESET}")
            return False
        if await self._tts.health_check() != ProviderHealth.HEALTHY:
            print(f"{Colors.RED}✗ TTS 未就绪，请检查 Azure Speech 配置。{Colors.RESET}")
            return False
        await self._voice_pipeline.start_session()
        return True

    async def stop(self) -> None:
        """Release every runtime resource once, even if the caller is cancelled."""
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop_components())
        try:
            await asyncio.shield(self._stop_task)
        except asyncio.CancelledError:
            await self._stop_task
            raise

    async def _stop_components(self) -> None:
        await asyncio.gather(
            *(
                self._stop_component(name, operation)
                for name, operation in [
                    ("system audio", self._audio_output.stop()),
                    ("streaming audio", self._voice_audio_output.stop()),
                    ("voice pipeline", self._voice_pipeline.shutdown()),
                ]
            )
        )
        # Drain accepted events before closing the memory provider they persist to.
        await self._stop_component("event bus", self._bus.shutdown())
        components: list[tuple[str, Any]] = [
            ("orchestrator", self._orchestrator.shutdown()),
        ]
        if self._action_audit:
            components.append(("action audit", self._action_audit.shutdown()))
        await asyncio.gather(
            *(self._stop_component(name, operation) for name, operation in components)
        )

    @staticmethod
    async def _stop_component(name: str, operation: Any) -> None:
        task = asyncio.ensure_future(operation)
        try:
            done, _ = await asyncio.wait(
                [task], timeout=_SHUTDOWN_STEP_TIMEOUT_SECONDS
            )
            if not done:
                task.cancel()
                task.add_done_callback(CompanionApp._consume_task_result)
                logging.getLogger(__name__).error(
                    "Timed out after %.1fs while shutting down %s",
                    _SHUTDOWN_STEP_TIMEOUT_SECONDS,
                    name,
                )
                return
            await task
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(CompanionApp._consume_task_result)
            logging.getLogger(__name__).warning("Shutdown of %s was cancelled", name)
        except Exception:
            logging.getLogger(__name__).exception("Error shutting down %s", name)

    @staticmethod
    def _consume_task_result(task: asyncio.Future[Any]) -> None:
        if task.cancelled():
            return
        with contextlib.suppress(Exception):
            task.exception()


async def async_main(args: argparse.Namespace) -> int:
    """Async main entry point."""
    if args.verify_memory_backup:
        version, legacy = MemoryService.verify_backup(args.verify_memory_backup)
        suffix = " (legacy v0; it will be registered on first startup)" if legacy else ""
        print(
            f"Memory backup schema v{version} is valid: "
            f"{args.verify_memory_backup.resolve()}{suffix}"
        )
        return 0
    # Load config for every operation that uses runtime settings.
    config = RuntimeConfig.from_yaml(args.config)
    if args.doctor or args.doctor_online or args.doctor_json:
        report = await run_diagnostics(
            config,
            require_voice=args.voice_input or args.voice,
            online=args.doctor_online,
            voice_hardware=args.doctor_voice_hardware,
        )
        print(report.to_json() if args.doctor_json else render_diagnostic_report(report))
        return report.exit_code

    accept_avatar = bool(
        getattr(args, "accept_avatar", False)
        or getattr(args, "accept_avatar_json", False)
    )
    if accept_avatar:
        json_output = bool(getattr(args, "accept_avatar_json", False))
        if not config.avatar_config:
            avatar_report = failed_avatar_acceptance_report(
                "avatar.config", "Avatar bridge is disabled or not configured."
            )
        elif not config.identity or not config.identity.avatar_model_id:
            avatar_report = failed_avatar_acceptance_report(
                "avatar.model_config", "Configured identity.avatar_model_id is empty."
            )
        elif not config.avatar_config.get_auth_token():
            avatar_report = failed_avatar_acceptance_report(
                "avatar.credential",
                f"Avatar token environment variable is unset: "
                f"{config.avatar_config.auth_token_env}",
            )
        else:
            provider = WebSocketAvatarProvider(config.avatar_config)
            try:
                print(
                    "Observe the stage now: confirm the intended model, happy expression, "
                    "and nod gesture are visibly correct.",
                    file=sys.stderr,
                )
                avatar_report = await run_avatar_acceptance(
                    provider,
                    model_id=config.identity.avatar_model_id,
                    visual_hold_seconds=3.0,
                )
            finally:
                await shutdown_avatar_acceptance_provider(provider)
        print(
            avatar_report.to_json()
            if json_output
            else render_avatar_acceptance_report(avatar_report)
        )
        return avatar_report.exit_code

    if args.backup_memory:
        memory = MemoryService(config.effective_memory_config())
        try:
            backup = await memory.backup_to(args.backup_memory, overwrite=args.overwrite_backup)
        finally:
            await memory.shutdown()
        print(f"Memory backup created and verified: {backup}")
        return 0

    setup_logging(
        args.log_level or config.log_level,
        config.log_file,
        max_bytes=config.log_max_bytes,
        backup_count=config.log_backup_count,
    )

    accept_voice = bool(
        getattr(args, "accept_voice", False)
        or getattr(args, "accept_voice_json", False)
    )
    quiet_output = bool(getattr(args, "accept_voice_json", False))
    if quiet_output:
        with contextlib.redirect_stdout(io.StringIO()):
            app = CompanionApp(config)
    else:
        app = CompanionApp(config)
    try:
        if quiet_output:
            with contextlib.redirect_stdout(io.StringIO()):
                ready = await app.start()
        else:
            ready = await app.start()
        if not ready:
            if accept_voice:
                setup_report = failed_voice_acceptance_report(
                    "voice.runtime_ready",
                    "Required runtime providers did not pass startup readiness.",
                )
                print(
                    setup_report.to_json()
                    if getattr(args, "accept_voice_json", False)
                    else render_voice_acceptance_report(setup_report)
                )
            return 1

        if accept_voice:
            if quiet_output:
                with contextlib.redirect_stdout(io.StringIO()):
                    voice_ready = await app.start_voice_mode()
            else:
                voice_ready = await app.start_voice_mode()
            if not voice_ready:
                provider_report = failed_voice_acceptance_report(
                    "voice.provider_ready",
                    "ASR or Azure TTS did not pass voice readiness.",
                )
                print(
                    provider_report.to_json()
                    if quiet_output
                    else render_voice_acceptance_report(provider_report)
                )
                return 1
            microphone = MicrophoneCapture(config.microphone_config)
            if not await microphone.start():
                microphone_report = failed_voice_acceptance_report(
                    "voice.microphone_ready",
                    "Voice acceptance could not open the microphone.",
                )
                print(
                    microphone_report.to_json()
                    if quiet_output
                    else render_voice_acceptance_report(microphone_report)
                )
                return 1
            try:
                acceptance_report = await run_voice_acceptance(
                    microphone=microphone,
                    pipeline=app.voice_pipeline,
                    event_bus=app.event_bus,
                    sample_rate=config.voice_pipeline_config.sample_rate,
                    utterance_timeout_seconds=(
                        config.microphone_config.max_speech_duration_ms / 1000 + 10.0
                    ),
                    turn_timeout_seconds=(
                        config.voice_pipeline_config.max_turn_duration_ms / 1000 + 5.0
                    ),
                    target_e2e_latency_ms=(
                        config.voice_pipeline_config.target_e2e_latency_ms
                    ),
                    target_interrupt_latency_ms=(
                        config.voice_pipeline_config.target_interrupt_latency_ms
                    ),
                    announce=(
                        (lambda message: print(message, file=sys.stderr))
                        if quiet_output
                        else print
                    ),
                )
            finally:
                await microphone.stop()
            print(
                acceptance_report.to_json()
                if getattr(args, "accept_voice_json", False)
                else render_voice_acceptance_report(acceptance_report)
            )
            return acceptance_report.exit_code

        companion_name = app.state.identity.name

        if args.voice_input:
            if not await app.start_voice_mode():
                return 1
            microphone = MicrophoneCapture(config.microphone_config)
            if not await microphone.start():
                print(f"{Colors.RED}✗ 麦克风不可用。{Colors.RESET}")
                return 1
            try:
                await VoiceChatMode(microphone, app.voice_pipeline, companion_name).run()
            finally:
                try:
                    await microphone.stop()
                finally:
                    await app.stop()
            return 0

        if args.once:
            result = await app.chat(args.once, speak=args.voice)
            print_response(
                companion_name,
                result["response_text"],
                emotion=result.get("emotion", ""),
                latency_ms=result.get("latency_ms", 0),
            )
            return 0

        print_companion_name(companion_name)
        while True:
            try:
                user_input = input(f"{Colors.GREEN}你{Colors.RESET}: ").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{Colors.YELLOW}再见！{Colors.RESET}")
                break

            if not user_input:
                continue

            # Commands
            if user_input == "/quit":
                print(f"{Colors.YELLOW}再见！{Colors.RESET}")
                break
            elif user_input == "/help":
                print_help()
                continue
            elif user_input == "/emotion":
                affect = app.state.affect
                print(f"""  {Colors.BLUE}当前情绪:{Colors.RESET}
    愉悦度: {affect.valence:+.2f}  唤醒度: {affect.arousal:.2f}
    信任: {affect.trust:.2f}  亲近: {affect.closeness:.2f}
    精力: {affect.energy:.2f}  不确定: {affect.uncertainty:.2f}
    主导情绪: {Colors.MAGENTA}{affect.dominant_emotion()}{Colors.RESET}""")
                continue
            elif user_input == "/memory":
                print(f"  {Colors.BLUE}对话轮数: {app.orchestrator.turn_count}{Colors.RESET}")
                print(
                    f"  {Colors.BLUE}最近摘要: "
                    f"{app.orchestrator.get_conversation_summary()}{Colors.RESET}"
                )
                continue
            elif user_input == "/history":
                history = app.orchestrator.get_conversation_history()
                for i, turn in enumerate(history.get_recent_turns(10)):
                    print(
                        f"  {Colors.DIM}#{i + 1}{Colors.RESET} "
                        f"{Colors.GREEN}你{Colors.RESET}: {turn.user_text[:60]}"
                    )
                    print(
                        f"     {Colors.CYAN}{companion_name}{Colors.RESET}: "
                        f"{turn.companion_text[:60]}"
                    )
                continue
            elif user_input == "/state":
                state_snapshot = app.state.get_state_snapshot()
                print(
                    f"  {Colors.BLUE}"
                    f"{json.dumps(state_snapshot, indent=2, ensure_ascii=False)}"
                    f"{Colors.RESET}"
                )
                continue
            elif user_input == "/action" or user_input.startswith("/action "):
                action_names = {
                    "status": "check_system_status",
                    "window": "read_window_title",
                    "app": "read_active_app",
                }
                selection = user_input.removeprefix("/action").strip()
                action_type = action_names.get(selection)
                if not app.action_service:
                    print(f"{Colors.YELLOW}只读行动 Provider 未启用。{Colors.RESET}")
                elif not action_type:
                    print(f"{Colors.YELLOW}用法: /action status|window|app{Colors.RESET}")
                else:
                    _record, action_result = await app.action_service.request(action_type)
                    if action_result and action_result.success:
                        print(
                            f"{Colors.BLUE}"
                            f"{json.dumps(action_result.result_data, ensure_ascii=False)}"
                            f"{Colors.RESET}"
                        )
                    else:
                        error = action_result.error_message if action_result else "行动等待确认"
                        print(f"{Colors.RED}行动失败: {error}{Colors.RESET}")
                continue
            elif user_input == "/face":
                affect = app.state.affect
                expression_snapshot = app._expression_mapper.map(affect)
                facial = expression_snapshot.facial
                voice = expression_snapshot.voice
                print(f"""  {Colors.MAGENTA}当前表情映射:{Colors.RESET}
    表情: {facial.expression_id} (强度: {facial.expression_intensity:.2f})
    语音风格: {voice.style} (速率: {voice.rate})
    手势建议: {len(expression_snapshot.gestures)} 个
    主动级别建议: {expression_snapshot.proactive_level_hint}
    眼睛: {facial.eye_open:.2f} 微笑: {facial.mouth_smile:.2f}
    皱眉: {facial.mouth_frown:.2f}""")
                continue
            elif user_input == "/proactive" or user_input == "/p":
                stats = app._proactive.get_stats()
                print(f"""  {Colors.YELLOW}主动行为统计:{Colors.RESET}
    今日主动: {stats["total_proactives_today"]} 次
    最近一小时: {stats["proactives_last_hour"]} 次
    接受率: {stats["acceptance_rate"]:.0%}
    按级别: {stats.get("by_level", {})}""")
                continue

            # Normal message
            result = await app.chat(user_input, speak=args.voice)
            print_response(
                companion_name,
                result["response_text"],
                emotion=result.get("emotion", ""),
                latency_ms=result.get("latency_ms", 0),
            )

    finally:
        await app.stop()

    return 0


def main() -> None:
    """Parse arguments and run the companion."""
    parser = argparse.ArgumentParser(
        description="二次元虚拟伴侣 — Virtual Companion Runtime",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m companion                    # 交互对话模式
  python -m companion --once "你好！"    # 单次消息
  python -m companion --config my.yaml   # 自定义配置
        """,
    )
    parser.add_argument(
        "--config", "-c", type=Path, default=None, help="配置文件路径 (默认: 包内置配置)"
    )
    parser.add_argument("--once", "-1", type=str, default=None, help="发送一条消息后退出")
    parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别 (默认: INFO)",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="运行本地上线前自检，不启动伴侣",
    )
    parser.add_argument(
        "--doctor-online",
        action="store_true",
        help="在 doctor 中额外验证已启用的远程 Provider",
    )
    parser.add_argument(
        "--doctor-json",
        action="store_true",
        help="以 JSON 输出 doctor 结果，便于自动化验收",
    )
    parser.add_argument(
        "--doctor-voice-hardware",
        action="store_true",
        help="深度检查 Whisper 模型与真实音频流（可能下载模型并短暂占用设备）",
    )
    parser.add_argument(
        "--accept-voice",
        action="store_true",
        help="交互验收真实语音全链路和打断延迟",
    )
    parser.add_argument(
        "--accept-voice-json",
        action="store_true",
        help="交互验收真实语音链路并仅输出结构化 JSON 结果",
    )
    parser.add_argument(
        "--accept-avatar",
        action="store_true",
        help="验收真实 Live2D/VRM 舞台、模型加载和状态渲染",
    )
    parser.add_argument(
        "--accept-avatar-json",
        action="store_true",
        help="验收真实形象舞台并仅输出结构化 JSON 结果",
    )
    parser.add_argument(
        "--backup-memory",
        type=Path,
        default=None,
        help="创建经过完整性校验的在线 SQLite 记忆备份后退出",
    )
    parser.add_argument(
        "--verify-memory-backup",
        type=Path,
        default=None,
        help="只读校验记忆备份的完整性和结构后退出",
    )
    parser.add_argument(
        "--overwrite-backup",
        action="store_true",
        help="允许 --backup-memory 原子替换已有目标文件",
    )
    parser.add_argument(
        "--voice",
        action="store_true",
        default=False,
        help="启用 TTS 语音输出 (需要配置 AZURE_SPEECH_KEY)",
    )
    parser.add_argument(
        "--voice-input",
        action="store_true",
        default=False,
        help="启用语音输入模式 (需要 pyaudio 或 sounddevice)",
    )

    args = parser.parse_args()
    maintenance_modes = sum(
        (
            any(
                (
                    args.doctor,
                    args.doctor_online,
                    args.doctor_json,
                    args.doctor_voice_hardware,
                )
            ),
            bool(args.accept_voice or args.accept_voice_json),
            bool(args.accept_avatar or args.accept_avatar_json),
            bool(args.backup_memory),
            bool(args.verify_memory_backup),
        )
    )
    if maintenance_modes > 1:
        parser.error(
            "doctor、accept-voice、accept-avatar、backup-memory 和 "
            "verify-memory-backup 模式不能组合使用"
        )
    if args.accept_voice and args.accept_voice_json:
        parser.error("--accept-voice 和 --accept-voice-json 只能选择一个")
    if args.accept_avatar and args.accept_avatar_json:
        parser.error("--accept-avatar 和 --accept-avatar-json 只能选择一个")
    if args.overwrite_backup and not args.backup_memory:
        parser.error("--overwrite-backup 只能与 --backup-memory 一起使用")
    if args.doctor_voice_hardware:
        args.doctor = True
        args.voice_input = True
    try:
        exit_code = asyncio.run(async_main(args))
    except (OSError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        exit_code = 2
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

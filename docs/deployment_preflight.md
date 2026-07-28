# Deployment Preflight

## Configuration contract

The packaged and repository YAML templates contain only capabilities implemented by the current
runtime. The loader rejects unsupported provider types, enabled perception/telemetry export, YAML
credentials, invalid cloud endpoints, invalid audio settings, and unsafe action configuration.

The following settings are now authoritative at runtime:

- memory SQLite path, WAL, and FTS configuration;
- `COMPANION_DB_PATH` as the explicit memory-path environment override;
- LLM retries, retry delay, timeout, model, endpoint, and credential environment name;
- Azure TTS region, voice, timeout, credential environment name, and 24 kHz PCM format;
- microphone/ASR sample rate, language, pre-roll, speech/silence limits, and turn timeout;
- quiet hours, hourly proactive budgets, and per-level cooldowns;
- event-bus retention, avatar bridge, and Windows read-only action settings.

Relative data/log/audit paths in the packaged default resolve under
`%LOCALAPPDATA%\VirtualCompanion`, independent of the launch directory. Set
`COMPANION_RUNTIME_DIR` to override that root. Relative paths in an explicitly supplied YAML file
resolve from that file's directory. An explicit missing config file is an error and never silently
falls back to defaults.

## Doctor commands

Run from the exact Python environment used to start the release:

```powershell
python -m companion --doctor
python -m companion --doctor --voice-input
python -m companion --doctor-online --voice-input
python -m companion --doctor-json --voice-input
```

`--doctor` performs no LLM/TTS network calls. `--doctor-online` validates configured remote
providers. The Azure check uses the read-only voices-list endpoint and doesn't synthesize audio.
No diagnostic message or JSON field includes credential values.

Exit codes:

- `0`: every required requested check passed; warnings and disabled optional providers are allowed;
- `1`: a required runtime, credential, database, module, device, or provider check failed;
- `2`: configuration could not be parsed or validated.

When `--voice-input` is included, doctor requires faster-whisper, NumPy, sounddevice, a usable
default input device, a usable default output device, and the Azure Speech credential. It does not
download or load the configured Whisper model.

`--doctor-voice-hardware` is an explicit deeper check. It may download/load the configured model,
runs one in-memory silence inference, opens the production microphone stream long enough to
capture several in-memory frames, and opens the production playback stream with about 20 ms of
silence. It doesn't persist captured audio or make cloud requests. On Windows without Developer
Mode, the Hugging Face cache may warn that symlink deduplication is unavailable; this increases
cache disk use but does not invalidate the model/inference check.

## Target-machine release sequence

1. Rotate any credential that was ever stored outside the environment/credential manager.
2. Install `requirements.lock` with hashes and install the wheel with `--no-deps`.
3. Verify the published wheel and sdist against the accompanying `SHA256SUMS` before installation.
4. Run `pip check` in that environment.
5. Run local doctor with `--voice-input`.
6. Inject the rotated DeepSeek and Azure credentials and run online doctor.
7. Run a real spoken-turn test, including microphone capture, model load, streaming playback, and
   barge-in latency measurement.
8. Enable the avatar only when its bridge extension is running and test Live2D/VRM state sync.
9. Keep mutating computer actions disabled; the shipped Windows provider remains read-only.

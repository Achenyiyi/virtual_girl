"""Audio processing utilities.

Handles:
- Voice Activity Detection (VAD)
- Pre-roll buffer management
- Echo cancellation (AEC) stub
- Audio format conversion
- Silence detection
- Audio output playback (TTS → speakers)
"""

from companion.audio.microphone import MicConfig, MicrophoneCapture, VoiceChatMode
from companion.audio.player import (
    AudioPlayer,
    PlaybackResult,
    SoundDeviceAudioOutput,
    SystemAudioOutput,
)
from companion.audio.vad import VADConfig, VADResult, VoiceActivityDetector

__all__ = [
    "VADConfig",
    "VADResult",
    "VoiceActivityDetector",
    "AudioPlayer",
    "PlaybackResult",
    "SystemAudioOutput",
    "SoundDeviceAudioOutput",
    "MicrophoneCapture",
    "MicConfig",
    "VoiceChatMode",
]

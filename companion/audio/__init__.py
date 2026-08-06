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
    PlaybackResult,
    SoundDeviceAudioOutput,
    SystemAudioOutput,
    pcm_to_wav,
)
from companion.audio.vad import VADConfig, VADResult, VoiceActivityDetector

__all__ = [
    "VADConfig",
    "VADResult",
    "VoiceActivityDetector",
    "pcm_to_wav",
    "PlaybackResult",
    "SystemAudioOutput",
    "SoundDeviceAudioOutput",
    "MicrophoneCapture",
    "MicConfig",
    "VoiceChatMode",
]

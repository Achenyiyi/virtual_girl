"""Communication protocols — turn management, cancellation, and audio confirmation.

These protocols enforce the invariants described in the PLAN:
- turn_id: every interaction is uniquely identified and traceable
- Cancellation: turns can be interrupted with guaranteed cleanup
- Audio confirmation: only actually-played audio enters shared history
"""

from companion.protocols.audio import AudioConfirmationProtocol, AudioPlaybackRecord
from companion.protocols.turn import TurnManager, TurnProtocol, TurnState

__all__ = [
    "TurnManager",
    "TurnProtocol",
    "TurnState",
    "AudioConfirmationProtocol",
    "AudioPlaybackRecord",
]

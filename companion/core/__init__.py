"""Core orchestration — the brain of the virtual companion.

Key components:
- CompanionOrchestrator: the central coordinator for dialogue and behavior
- EventBus: typed event distribution between components
- PolicyGate: safety/permission gate for actions and proactive behavior
- StateManager: manages identity, affect, and relationship state
"""

from companion.core.event_bus import EventBus, EventHandler
from companion.core.orchestrator import CompanionOrchestrator
from companion.core.policy_gate import PolicyGate, ProactiveDecision
from companion.core.state_manager import StateManager

__all__ = [
    "CompanionOrchestrator",
    "EventBus",
    "EventHandler",
    "PolicyGate",
    "ProactiveDecision",
    "StateManager",
]

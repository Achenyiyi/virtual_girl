"""Memory Service — five-layer memory system implementation facade."""

from companion.memory.episode_segmenter import EpisodeSegmenter, SegmentationResult
from companion.memory.fact_extractor import FactExtractionResult, FactExtractor
from companion.memory.memory_service import MemoryService, MemoryServiceConfig
from companion.memory.reflection_engine import ReflectionConfig, ReflectionEngine

__all__ = [
    "MemoryService",
    "MemoryServiceConfig",
    "FactExtractor",
    "FactExtractionResult",
    "EpisodeSegmenter",
    "SegmentationResult",
    "ReflectionEngine",
    "ReflectionConfig",
]

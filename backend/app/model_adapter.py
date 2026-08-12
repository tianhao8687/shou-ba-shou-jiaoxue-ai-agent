"""Compatibility imports for callers migrating to ``app.model`` modules."""

from .model import (
    CompactDraftProposal,
    CompactDraftStep,
    CoordinatedModelAdapter,
    DeterministicModelAdapter,
    HeuristicModelAdapter,
    ModelAdapter,
    OpenAICompatibleModelAdapter,
    ResilientModelRouter,
    SafeDisabledModelAdapter,
    _extract_json_object,
    create_model_adapter,
)

__all__ = [name for name in globals() if not name.startswith("__")]

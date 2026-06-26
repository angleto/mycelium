"""Adjudication framework: pluggable multi-agent convergence.

See docs/adr/0027-adjudication-framework.md for the design. This
package is opt-in: callers reach it through ``services.adjudication``
or compose strategies directly in code. The orchestration pieces
(``services/agent_runtime``, ``services/coordination``, ...) are
unaware of it.
"""

from __future__ import annotations

from mycelium_core.adjudication.base import (
    AdjudicationContext,
    AdjudicationOutcome,
    AdjudicationStrategy,
    CostModel,
    DissentNote,
    StepRecord,
    StepStore,
    StrategyRequirements,
)
from mycelium_core.adjudication.composition import Cap, FallbackChain, Filter, Race
from mycelium_core.adjudication.policy import PolicyRouter, PolicyRule
from mycelium_core.adjudication.registry import StrategyRegistry, get_registry
from mycelium_core.adjudication.store import DBStepStore, InMemoryStepStore

__all__ = [
    "AdjudicationContext",
    "AdjudicationOutcome",
    "AdjudicationStrategy",
    "Cap",
    "CostModel",
    "DBStepStore",
    "DissentNote",
    "FallbackChain",
    "Filter",
    "InMemoryStepStore",
    "PolicyRouter",
    "PolicyRule",
    "Race",
    "StepRecord",
    "StepStore",
    "StrategyRegistry",
    "StrategyRequirements",
    "get_registry",
]

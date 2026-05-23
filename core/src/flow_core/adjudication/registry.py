"""Strategy registry with capability ranking and composition validation.

ADR-0027 §D2: strategies declare ``id``, ``requires``, ``cost_model``,
``composable_with``, ``mutually_exclusive_with``. The registry holds
instances; out-of-tree strategies plug in via the ``flow.adjudication.
strategies`` entry-point group.

The registry is a process-global because strategy id is a free-form
string and we want one canonical instance per id (testing overrides
re-register). ``get_registry`` exposes it for explicit access.
"""

from __future__ import annotations

import importlib.metadata
import threading

from flow_core.adjudication.base import (
    AdjudicationContext,
    AdjudicationStrategy,
)


class StrategyRegistry:
    """Holds the active set of registered strategies, keyed by id."""

    def __init__(self) -> None:
        self._strategies: dict[str, AdjudicationStrategy] = {}
        self._lock = threading.Lock()
        # Set to True after the first ``load_entry_points`` call to keep
        # subsequent imports idempotent. Tests can flip it back via
        # ``reset_entry_points_loaded``.
        self._entry_points_loaded = False

    def register(self, strategy: AdjudicationStrategy) -> None:
        """Idempotent register-by-id.

        Re-registering the same id (e.g. a test replacing the
        production debate with a fake) wins.
        """
        sid = strategy.id
        with self._lock:
            self._strategies[sid] = strategy

    def unregister(self, strategy_id: str) -> None:
        with self._lock:
            self._strategies.pop(strategy_id, None)

    def get(self, strategy_id: str) -> AdjudicationStrategy:
        try:
            return self._strategies[strategy_id]
        except KeyError as e:
            raise KeyError(f"Unknown adjudication strategy: {strategy_id!r}") from e

    def list_strategies(self) -> list[AdjudicationStrategy]:
        return list(self._strategies.values())

    def rank(self, ctx: AdjudicationContext) -> list[tuple[float, AdjudicationStrategy]]:
        """Return registered strategies ordered by ``applicable(ctx)``
        descending. Strategies that return 0 are filtered out; ties
        broken by lower ex-ante cost estimate."""
        scored: list[tuple[float, AdjudicationStrategy]] = []
        for s in self._strategies.values():
            score = s.applicable(ctx)
            if score > 0:
                scored.append((score, s))
        scored.sort(
            key=lambda item: (-item[0], item[1].cost_model.estimate()),
        )
        return scored

    def validate_composition(
        self,
        *,
        outer: AdjudicationStrategy,
        children: list[AdjudicationStrategy],
    ) -> None:
        """Raise ``ValueError`` if any child violates outer's
        compatibility metadata, or if two children declare each other
        mutually exclusive.

        Outer is typically a composition primitive (``FallbackChain``,
        ``Race``, ``Cap``, ``Filter``); children are the wrapped
        strategies.
        """
        if outer.composable_with:
            for child in children:
                if child.id not in outer.composable_with:
                    raise ValueError(
                        f"strategy {child.id!r} is not composable inside "
                        f"{outer.id!r} (composable_with does not list it)"
                    )
        for i, child_i in enumerate(children):
            for child_j in children[i + 1 :]:
                if (
                    child_j.id in child_i.mutually_exclusive_with
                    or child_i.id in child_j.mutually_exclusive_with
                ):
                    raise ValueError(
                        f"strategies {child_i.id!r} and {child_j.id!r} "
                        "are mutually exclusive and cannot share an "
                        "adjudication composition"
                    )

    def load_entry_points(self, *, force: bool = False) -> None:
        """Discover strategies advertised via the ``flow.adjudication.
        strategies`` entry-point group.

        Each entry point must yield an ``AdjudicationStrategy``
        instance (callable factory or module attribute). Failures are
        not silenced: a broken plugin must surface during boot.
        """
        if self._entry_points_loaded and not force:
            return
        eps = importlib.metadata.entry_points(group="flow.adjudication.strategies")
        for ep in eps:
            obj = ep.load()
            strategy = obj() if callable(obj) else obj
            self.register(strategy)
        self._entry_points_loaded = True

    def reset_entry_points_loaded(self) -> None:
        """Test helper: forget that entry-points were loaded so a
        following ``load_entry_points`` runs again."""
        self._entry_points_loaded = False

    def clear(self) -> None:
        """Test helper: drop every registered strategy."""
        with self._lock:
            self._strategies.clear()
            self._entry_points_loaded = False


_registry = StrategyRegistry()


def get_registry() -> StrategyRegistry:
    return _registry

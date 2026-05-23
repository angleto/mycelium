"""Declarative policy router (ADR-0027 §D3).

Selection precedence:

1. Explicit override at call site (``select(ctx, override='debate')``).
2. Declarative rules: first matching enabled rule wins, sorted by
   ``priority`` ascending (lower runs first).
3. Applicability auto-rank: the highest ``applicable(ctx)`` among
   registered strategies; ties broken by lower cost estimate.

The router does selection + capability gating only. It never executes
domain logic: merit logic stays in strategies. M1 keeps rules in
Python (callable predicates); a persisted ``adjudication_policy_rule``
table is a P4 add-on.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from flow_core.adjudication.base import AdjudicationContext, AdjudicationStrategy
from flow_core.adjudication.registry import StrategyRegistry, get_registry


@dataclass(frozen=True)
class PolicyRule:
    """One declarative routing rule.

    ``when`` is a pure predicate over the context: it must not touch
    the DB, the network, or shared state. It can mutate nothing. The
    router calls it possibly many times per selection.
    """

    name: str
    when: Callable[[AdjudicationContext], bool]
    strategy_id: str
    # Per-rule config layered on top of the caller's config (caller
    # wins on key conflict). Used to encode policy decisions like
    # ``n_agents=3, max_rounds=2`` for a specific task type without
    # forcing every call site to pass them.
    config: dict[str, Any] = field(default_factory=dict)
    priority: int = 100
    enabled: bool = True


class PolicyRouter:
    def __init__(
        self,
        rules: list[PolicyRule] | None = None,
        *,
        registry: StrategyRegistry | None = None,
    ) -> None:
        self._rules: list[PolicyRule] = list(rules or [])
        self._registry = registry or get_registry()

    def add_rule(self, rule: PolicyRule) -> None:
        self._rules.append(rule)

    def remove_rule(self, name: str) -> None:
        self._rules = [r for r in self._rules if r.name != name]

    def rules(self) -> list[PolicyRule]:
        return list(self._rules)

    def select(
        self,
        ctx: AdjudicationContext,
        *,
        override: str | None = None,
    ) -> tuple[AdjudicationStrategy, dict[str, Any]]:
        """Return ``(strategy, effective_config)``.

        - If ``override`` is set, the named strategy is returned with
          ``ctx.config`` unchanged.
        - Else, the first matching enabled rule (sorted by priority
          ascending) wins; effective config is the rule's config
          merged with ``ctx.config`` (caller wins on conflict).
        - Else, the registry's auto-rank yields the top strategy, with
          ``ctx.config`` unchanged.

        Raises ``LookupError`` when no strategy can be selected
        (registry empty, all ``applicable`` zero, or no enabled rule
        matches and the registry has no positive-applicability
        strategies).
        """
        if override is not None:
            return self._registry.get(override), dict(ctx.config)

        for rule in sorted(self._rules, key=lambda r: r.priority):
            if not rule.enabled:
                continue
            try:
                matched = rule.when(ctx)
            except Exception as e:
                raise RuntimeError(f"policy rule {rule.name!r} predicate raised: {e!r}") from e
            if matched:
                effective = {**rule.config, **ctx.config}
                return self._registry.get(rule.strategy_id), effective

        ranked = self._registry.rank(ctx)
        if not ranked:
            raise LookupError("no adjudication strategy is applicable to the supplied context")
        return ranked[0][1], dict(ctx.config)

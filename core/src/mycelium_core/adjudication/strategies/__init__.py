"""Built-in strategies, registered with the process registry at import.

Out-of-tree strategies plug in via the ``flow.adjudication.strategies``
entry-point group (see ``registry.load_entry_points``).
"""

from __future__ import annotations

from mycelium_core.adjudication.registry import get_registry
from mycelium_core.adjudication.strategies.debate import DebateConfig, DebateStrategy
from mycelium_core.adjudication.strategies.human_in_loop import HumanInLoopStrategy
from mycelium_core.adjudication.strategies.single_shot import SingleShotStrategy


def register_builtins() -> None:
    """Idempotent registration of the in-tree strategies.

    Called from the boot sequence; tests can also call it explicitly
    after ``registry.clear()`` to restore the default set.
    """
    reg = get_registry()
    reg.register(SingleShotStrategy())
    reg.register(HumanInLoopStrategy())
    reg.register(DebateStrategy())


__all__ = [
    "DebateConfig",
    "DebateStrategy",
    "HumanInLoopStrategy",
    "SingleShotStrategy",
    "register_builtins",
]

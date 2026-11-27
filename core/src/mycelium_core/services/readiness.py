"""When a dependency still holds its successor back: the one readiness rule.

A task can be started when none of its incoming dependencies holds it back.
By type (docs/adr/0004):

- FS (finish to start): held back until the predecessor is finished, that
  is in a terminal workflow state.
- SS (start to start): held back until the predecessor has started: it has
  an ``actual_start``, or it has left its workflow's initial state.
- FF (finish to finish) and SF (start to finish) constrain when the
  successor may FINISH, never when it may start, so they never hold a start
  back.

The lag is not part of readiness. It is working time between two dates,
which is what the scheduler computes; readiness answers "may this be
started now", and a predecessor that is finished is finished whatever lag
the scheduler then places after it.

Every reader of "blocked" or "ready" goes through this module: the blocked
overlay and the advisory layer (through ``dependencies.blocked_task_ids``),
``task_pull``, the dispatch loop's admitted set, and the Connections panel.
Before 2026-11-26 the overlay held a task back on any non-terminal
predecessor whatever the type, while ``task_pull`` and the dispatch loop
did not look at dependencies at all.

What this does not cover: whether an FF or SF successor may be moved to a
finishing state while its predecessor is not there yet. That is a rule about
closing, not about starting, and nothing enforces it today.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, and_, exists, or_, select
from sqlalchemy.orm import aliased

from mycelium_core.models.dependency import DependencyType, TaskDependency
from mycelium_core.models.task import Task
from mycelium_core.models.workflow import WorkflowState


def holds_back(dependency: Any, predecessor: Any, predecessor_state: Any) -> ColumnElement[bool]:
    """SQL truth of "this dependency still holds its successor back".

    ``dependency`` is ``TaskDependency`` (or an alias of it), ``predecessor``
    an alias of ``Task`` joined on the predecessor id, and
    ``predecessor_state`` an alias of ``WorkflowState`` joined on the
    predecessor's state. The caller does the joins, so the same expression
    serves an EXISTS filter and a selected column.
    """
    return or_(
        and_(
            dependency.type == DependencyType.FS,
            predecessor_state.is_terminal.is_(False),
        ),
        and_(
            dependency.type == DependencyType.SS,
            predecessor.actual_start.is_(None),
            predecessor_state.is_initial.is_(True),
            predecessor_state.is_terminal.is_(False),
        ),
    )


def held_back(task_id: Any) -> ColumnElement[bool]:
    """SQL truth of "some incoming dependency holds this task back".

    ``task_id`` is the column (or value) naming the successor, typically
    ``Task.id`` of the outer query. A predecessor that is soft-deleted holds
    nothing back: it is gone from every view, and a task waiting on it could
    never be started.
    """
    pred = aliased(Task)
    pred_state = aliased(WorkflowState)
    return exists(
        select(TaskDependency.id)
        .join(pred, pred.id == TaskDependency.predecessor_id)
        .join(pred_state, pred_state.id == pred.state_id)
        .where(
            TaskDependency.successor_id == task_id,
            pred.deleted_at.is_(None),
            holds_back(TaskDependency, pred, pred_state),
        )
    )

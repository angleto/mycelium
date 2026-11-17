"""What kind of credential is calling: a person, an agent, a person's words
carried out by a model, or the system.

One function answers it, and every guard that depends on the answer asks it
here. Four places answered it on their own before, and they disagreed: an
agent token over REST is published as ``human_api`` with the token's id as
subject (``deps._tenant_scope``), and the lease holder, the bus and the work
digest all read the kind alone, so that agent was a person to each of them --
it skipped the lease every other agent must take, and took the owner's
exemption. The annotations guard was the only one that read the subject too.

The answer comes from the two session settings ``tenant_session`` arms and
the activity log records, ``app.current_actor_kind`` and
``app.current_actor_subject``, so it can be asked of a live session and of a
logged row alike.

- **person**: ``human_api`` with no subject: a person's own session, from the
  web app or stdio. What the person typed or clicked is what reaches the
  service. The CLI is not one: ``mycelium auth login`` stores an agent token
  in a file every process of that user can read, so a call made with it may
  come from any agent on the machine.
- **agent**: everything with a credential that is not a person's: an agent
  token (over MCP or REST), a capability, a dispatched run, an issuer key, a
  payment connector. A kind this module does not know is an agent too: the
  question is asked to decide what may be skipped, and an unknown caller
  skips nothing.
- **mediated**: ``human_telegram``. The instruction is a person's, but a model
  turns it into the calls the server receives, so a call can be something
  the person did not ask for. It is not a person for the acts reserved to a
  person until the person confirms the act through a channel the model does
  not write (task 04d8b905).
- **system**: ``system``, the worker and the scheduler acting on their own;
  and ``human_direct``, which is not a person despite its name. It is the
  default kind of ``tenant_session``, so it is what internal code gets when
  it opens a session for a user it acts for without that user presenting
  anything: the worker's dispatch loop, the SdI pipeline, maintenance
  scripts. No credential was presented, so it is the server acting. The
  annotations guard already refused it as a person for that reason.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_PERSON_KIND = "human_api"
_SYSTEM_KINDS = frozenset({"system", "human_direct"})
# The kinds whose subject is the id of a token an agent presents: an agent
# token or a capability. A dispatched run's subject is the run, which is not
# a credential a session carries from one call to the next.
_TOKEN_KINDS = frozenset({"mcp_token", "human_api"})


class CallerClass(enum.StrEnum):
    person = "person"
    agent = "agent"
    mediated = "mediated"
    system = "system"


@dataclass(frozen=True, slots=True)
class Caller:
    cls: CallerClass
    kind: str
    subject_id: uuid.UUID | None

    @property
    def token_id(self) -> uuid.UUID | None:
        """The token an agent presented, when it presented one."""
        return self.subject_id if self.kind in _TOKEN_KINDS else None

    @property
    def is_person(self) -> bool:
        return self.cls is CallerClass.person

    @property
    def is_model(self) -> bool:
        """A model chose this call: an agent, or a person's words turned
        into calls by one. The acts reserved to a person are refused to it."""
        return self.cls in (CallerClass.agent, CallerClass.mediated)


def _subject(raw: object) -> uuid.UUID | None:
    if isinstance(raw, uuid.UUID):
        return raw
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


def classify(kind: str | None, subject: object = None) -> Caller:
    """The class of a caller from its recorded kind and subject.

    A subject that does not parse is kept as "present": it still says a
    credential other than the person's own session was used."""
    k = (kind or "").strip()
    subject_id = _subject(subject)
    has_subject = subject_id is not None or bool(subject)
    if k == _PERSON_KIND and not has_subject:
        cls = CallerClass.person
    elif k == "human_telegram":
        cls = CallerClass.mediated
    elif k in _SYSTEM_KINDS and not has_subject:
        cls = CallerClass.system
    else:
        cls = CallerClass.agent
    return Caller(cls=cls, kind=k, subject_id=subject_id)


async def current(session: AsyncSession) -> Caller:
    """The class of the credential this session was opened with."""
    row = (
        await session.execute(
            text(
                "SELECT current_setting('app.current_actor_kind', true),"
                "       current_setting('app.current_actor_subject', true)"
            )
        )
    ).one()
    return classify(row[0], row[1])


__all__ = ["Caller", "CallerClass", "classify", "current"]

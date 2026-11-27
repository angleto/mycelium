"""Notifications on the operation registry (docs/adr/0068).

One operation so far: a message to oneself. It is on both surfaces on
purpose. A laptop reporting that its charger was pulled reaches it through
the CLI; an agent that is blocked and wants its person to know reaches it
over MCP. Neither can choose where the message goes: it reaches the caller,
on the caller's own channels, whose destinations the caller proved
(docs/adr/0082).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from mycelium_core.models.notification import NotificationChannelKind
from mycelium_core.operations import Kind, OpCtx, operation
from mycelium_core.services import notifications as svc

NOTIFICATIONS = "notifications"


class NotifySelfIn(BaseModel):
    # Extra keys are refused, not dropped: a caller that sends a recipient or
    # a destination must learn there is none, not believe it chose one.
    model_config = ConfigDict(extra="forbid")

    # Telegram caps a message at 4096 characters and a phone's lock screen
    # shows about one line of title: the bounds keep a message readable
    # where it lands, and below the provider's limit with room for the title.
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(default="", max_length=2000)
    key: str | None = Field(default=None, min_length=1, max_length=120)


class NotifySelfOut(BaseModel):
    queued: list[NotificationChannelKind]
    already_queued: list[NotificationChannelKind]


@operation(
    "notifications.notify_self",
    domain=NOTIFICATIONS,
    kind=Kind.WRITE,
    access="notifications:notify_self",
    params=NotifySelfIn,
    result=NotifySelfOut,
)
async def notify_self(ctx: OpCtx, p: NotifySelfIn) -> NotifySelfOut:
    """Send a message to yourself, on every channel of yours that can reach
    you here (e-mail, linked Telegram, subscribed browsers). It goes out
    with the next dispatch, within about a minute. There is no recipient to
    choose: it only ever reaches the person the credential acts for. Pass
    ``key`` to make a repeat a no-op on channels it already went to. Refused
    with ``notification.no_reachable_channel`` when nothing would arrive,
    and past 30 messages per channel per hour."""
    r = await svc.notify_self(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.actor_id,
        title=p.title,
        body=p.body,
        key=p.key,
    )
    return NotifySelfOut(queued=r.queued, already_queued=r.already_queued)

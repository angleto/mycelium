"""Migration 0030: a new workspace is seeded with an enabled email
notification channel (target = owner's email) inside
``provision_organization``, so reminders reach the owner without manual
setup. Without a channel pref, ``scan_reminders`` skips the user and
``dispatch_pending`` fails closed -- the workspace would silently never
deliver a reminder.
"""

from __future__ import annotations

import uuid

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.notification import NotificationChannelKind
from mycelium_core.services.auth import signup
from mycelium_core.services.notifications import list_prefs


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_signup_seeds_enabled_email_pref() -> None:
    email = _email()
    async with admin_session() as s:
        a = await signup(s, email=email, password="pw-strong-123", org_name="WS")
    async with tenant_session(str(a.org_id), str(a.user_id)) as s:
        prefs = await list_prefs(s, org_id=a.org_id, user_id=a.user_id)
    email_prefs = [p for p in prefs if p.channel == NotificationChannelKind.email]
    assert len(email_prefs) == 1
    pref = email_prefs[0]
    assert pref.enabled is True
    assert pref.target == email  # signup lower-cases; _email() is already lower

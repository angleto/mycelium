# ADR-0082: A notification reaches only a destination its person proved

Status: Accepted (2026-11-27, Angelo). Relates to: ADR-0026 (Telegram channel),
ADR-0072 (caller classes), ADR-0068 (operation registry).

This record decides who may write a notification preference and where its
destination comes from. It records the defect that forced the question, the
decision (caller-only writes, destination derived from a proved identity),
what changes for the person, the data migration, and what it does not cover.

## Context

Found on 2.4 at d56b97d7, 2026-11-27, while designing a self-notification
for a laptop power alert:

| what | measured |
|---|---|
| `PUT /notifications/prefs` | took `user_id` from the body; the service checked only that the actor was a member |
| MCP `set_notification_pref` | took `user_id` and `target` as arguments, same check |
| `target` | free text up to 320 characters, written by the request |
| effect | a member could point a colleague's reminders at their own address; any caller holding `notifications:write`, an agent token included, could send the person's notifications to an unverified chat or mailbox |

The second half is the one that matters beyond this workspace. A
notification "to the caller's own channel" is delivery to that person only
while nobody else, and nothing unattended, can choose where the channel
points. Otherwise it is publication outside the system by whoever wrote the
target last.

## Decision

1. **A preference is written only by the person it reaches.** The service
   `set_pref` takes no `user_id`: it writes the actor's row. The REST body
   and the MCP tool carry only the channel and the on/off switch, and the
   REST schema refuses extra keys (422), so a client still sending `user_id`
   or `target` learns it no longer chooses them.
2. **The destination is derived from an identity the person proved.**
   E-mail: the account address, proved at signup and changed by no route.
   Telegram: the chat bound by the link flow, which proves control by
   redeeming a one-time code from inside the chat. Web push: none, its
   targets are the device subscriptions.
3. **Enabling a channel with no proved destination is refused**
   (`notification.channel_not_linked`, 422) rather than stored enabled and
   empty, which dispatch would skip in silence.
4. **Rows stored under the old rule are rewritten** by migration 0040:
   e-mail to the account address, Telegram to the linked chat or switched
   off when there is no link, web push emptied. Rows already right are not
   touched.

5. **A message to oneself is an operation of its own**,
   `notifications.notify_self` on the registry (REST `POST
   /notifications/self`, MCP `notify_self`, CLI `mycelium notif send`), with
   its own flat scope `notifications:notify_self`. It queues one row per
   reachable channel of the caller and never dispatches: the worker sends it
   with the next tick. It takes no recipient and no destination, refuses
   extra keys, refuses when nothing would arrive
   (`notification.no_reachable_channel`), deduplicates by an optional key,
   and stops at 30 messages per channel per rolling hour
   (`notification.self_quota`, 429). It is open to agent tokens on purpose:
   points 1 and 2 are what make "the caller's own channels" mean the person,
   so there is nothing for a person-only gate to protect.

## Consequences

- A person can no longer route e-mail notifications to an address other
  than their account's. Measured on production for Angelo's account: the
  stored e-mail target already equalled the account address.
- The web page shows each channel with its destination and a switch; the
  free-text field is gone. Telegram is linked from Settings, Account.
- A person linked to Telegram can enable it in any workspace, because the
  link belongs to the person; before, the pref row existed only in the
  workspace that issued the link code.
- A self-notification operation can now deliver "to the caller's own
  channels" without a person-only gate on the send, because no caller can
  aim those channels at a third party.

## Alternatives rejected

- **Keep free text, require a person principal to change it.** It closes
  the agent path but leaves a typo or a hijacked session one write away from
  sending reminders to a stranger, and it still needs a verification flow
  for e-mail to mean anything. Deriving needs none.
- **Ignore `user_id` instead of refusing it.** A stale client naming
  another user would silently write its own row and believe it wrote the
  other's.
- **Resolve the destination at dispatch time instead of storing it.** One
  source of truth with no copy, which is better in principle; it changes the
  dispatch path and drops a column, a larger change than the defect needed.
  The stored copy is written only from the proved source, and the account
  address never changes, so the copy cannot drift today. Revisit if an
  e-mail change route is ever added.

## What this does not cover

- A compromised mailbox or a linked chat handed to someone else: those are
  failures of the identity provider, invisible from here.
- Who may *read* a preference: unchanged, a person reads their own.
- Delivery health (whether a channel actually works): a separate defect,
  Mycelium task 70fc3547.

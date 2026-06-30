# ADR-0022 Hands-free activation (headphone button): native/OS-assistant

Status: accepted. Platform reality, not a preference. Ties to
decision #10 (mobile/native later), ADR-0020 (capture), ADR-0021
(intent).

## Context

The user wants to trigger capture / a voice command from the headphone
button without touching the phone (screen off, in pocket), and asks
whether an app is needed.

## Decision

- A pure responsive web PWA **cannot** do screen-off, browser-closed,
  headphone-button cold activation. The Web Media Session API
  intercepts headphone keys only while an audio/capture session is
  active and the page is alive (limited foreground control only).
  Stated, not over-promised.
- True hands-free requires a **native companion app** (or a native
  App Intents / Siri Shortcuts / Android App Actions extension) that
  maps the headphone media button and/or registers an OS assistant
  intent ("Hey Siri/Google, new Mycelium note"). Feasibility depends on
  the button emitting standard media-key/HID events or triggering the
  OS assistant; a vendor-proprietary button usable only by that
  vendor's app may not be interceptable -- a hardware dependency,
  not guaranteed blindly.
- Therefore hands-free activation is **post web-v1**, delivered by the
  native companion app phase (consistent with decision #10 and
  ADR-0020's "always-on capture needs native app"). Web v1 = limited
  Media Session control while the app is active.
- The trigger only activates the existing pipeline (ADR-0020 capture
  offline-first, ADR-0021 intent layer). No new downstream design.

## Consequences

- Roadmap gains a **Native companion app (post-v1)** phase delivering
  headphone-button + OS-assistant hands-free; web v1 documents the
  limitation explicitly.

## Alternatives rejected

- Claiming a web PWA can do screen-off headphone activation: false.
- Requiring the browser to stay foregrounded while running: defeats
  the purpose.

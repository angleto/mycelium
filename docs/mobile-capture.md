# Mobile capture: notes from your phone

Three channels, each landing in the SAME Flow `/notes`:

1. **PWA install** (browser → home-screen icon)
2. **Apple Shortcut** (Siri "Hey Siri, nuova nota Flow…")
3. **Telegram bot** (voice messages → notes; revived in v1.2.29)

All three reuse the same backend; the difference is the front door.

## 1. PWA install

Mobile Safari (iOS) / Chrome (Android) → open `https://flow.leto.blue` →
share menu → **Add to Home Screen**. From v1.2.28 Flow registers a
real manifest + service worker, so the icon opens fullscreen (no
browser chrome), shows the right title (`Flow`), and has two PWA
shortcuts the OS surfaces on long-press of the icon: **New note**
and **Tasks**. The service worker caches the SPA shell so the icon
opens to a usable screen even with flaky network; the API still goes
straight to the server (no offline writes).

Voice from PWA today: `/notes` → New → kind = voice → ● Record. The
audio uploads as a note attachment. **Transcription is not yet
wired** in prod (no STT provider deployed); you'll have the audio
but the transcript stays empty until v1.2.29's STT setup. For
voice-first capture without that wait, use the Telegram bot or
Apple Shortcut paths below.

## 2. Apple Shortcut

Single endpoint: `POST https://flow.leto.blue/api/notes/quick-create`
with `Authorization: Bearer flow_at_…`. Body:

```json
{
  "project": "Kiwiprocess",
  "text": "punto da rivedere sulla pipeline X",
  "kind": "text"
}
```

`project` is a project name (case-insensitive partial — `kiwi` finds
`Kiwiprocess`) or a UUID. Omit it to land in the default `General`
project.

### Setup, one-time

1. Open Flow on desktop → **Settings → AI assistants** → "+ New
   assistant". Label it "iOS Shortcut". Permissions: leave the
   defaults (everything except `danger:*`). Save. Copy the **client
   secret** that appears in the Credentials card — it's shown only
   once.
2. On your iPhone, open the **Shortcuts** app → "+" → name it
   `Nota Flow`.
3. Add the action **Dictate Text** (Siri transcribes on-device).
   Optional: set Stop Listening → "On Tap" so dictation ends when
   you press, not on the first pause.
4. Add **Text** action with content `"Kiwiprocess"` (or whichever
   project — change this each time you want a different one, or
   make it an `Ask for Input` prompt).
5. Add **Dictionary**:
   - key `project` → from the Text step above
   - key `text` → from `Dictated Text`
   - key `kind` → `text`
6. Add **Get Contents of URL**:
   - URL: `https://flow.leto.blue/api/notes/quick-create`
   - Method: `POST`
   - Headers:
     - `Authorization` = `Bearer flow_at_xxxxxxxxxxxxxxxxxxxxxxxxxxx`
   - Request Body: JSON, Source = the Dictionary above
7. (Optional) **Show Notification** with the response so you see the
   note id when it works.
8. Bottom of the shortcut: "Add to Siri" — choose a trigger phrase
   like *"Nuova nota Flow"*. From now on:
   - **Siri** (iPhone, AirPods, Apple Watch, CarPlay): "Hey Siri,
     Nuova nota Flow" → speaks → done.
   - **Lock screen widget** / **Action Button**: assign the shortcut
     for one-tap dictation.

### Security note

The bearer lives **in the Shortcut**. If you lose your phone,
revoke the assistant from Flow's Settings → AI assistants → Revoke.
The bound bearer dies immediately and the Shortcut stops working.

## 3. Telegram bot (voice-first)

**Code shipped in v1.2.29**; awaits a one-time prod deploy step
(bot token + webhook registration). Once that's done the UX is:

- One-time bind: open `/settings → Telegram link` → copy the deep
  link → opens the bot → `/start <code>` runs automatically →
  account bound.
- Send **text** → text note in your default project.
- Send a **voice message** (Telegram's mic icon) → voice note: the
  bot downloads the `.ogg` via `getFile`, stores it as a note
  attachment, sets `audio_ref = attachment:<id>`, and best-effort
  triggers transcription. The audio is always playable from
  `/notes`; the transcript appears once an STT provider responds
  (see "STT provider" below).
- `/task <title>` → creates a task instead.

The bot routes EVERYTHING into your workspace's default project. To
land a note in a specific project, use the web SPA or move it via
drag-drop from `/notes`.

### Deploy step (one-time, ops)

To enable the bot in prod:

1. Mint the bot via BotFather, get the token.
2. Add to the Flow secret store:
   - `FLOW_TELEGRAM_BOT_TOKEN` = the BotFather token
   - `FLOW_TELEGRAM_BOT_USERNAME` = e.g. `flow_leto_bot`
   - `FLOW_TELEGRAM_WEBHOOK_SECRET` = a long random string
3. Restart the backend; the `/telegram/webhook/{secret}` route comes
   alive (it 404s when the bot is unconfigured).
4. Register the webhook with Telegram:

   ```bash
   curl -s "https://api.telegram.org/bot$TOKEN/setWebhook" \
     -d "url=https://flow.leto.blue/api/telegram/webhook/$SECRET" \
     -d "secret_token=$SECRET"
   ```

### STT provider

Voice notes land as audio + empty transcript until an STT provider
is wired. The `transcribe` service calls `get_stt().transcribe(...)`;
the default `LocalSTT` raises `RuntimeError` unless the
`faster-whisper` extra is installed. Two prod options:

- **faster-whisper on Ollama / GPU pod**: install the extra,
  configure the model id, point Flow at it. Self-hosted.
- **External Whisper API**: implement an `OpenAIWhisperProvider` (or
  similar) that returns the same `TranscriptResult` shape. Set the
  override via `set_stt_override`.

Until one of these is configured, the voice notes are functionally
complete (capture + playback) and the transcript stays empty.

## Picking between them

- "I want it on my home screen and capture in the SPA": **PWA**.
- "Hands-free dictation while running": **Apple Shortcut** (today,
  on iPhone / Watch) or **Telegram bot** (once live, works on iOS
  and Android).
- "Already in Telegram all day": **Telegram bot** as soon as
  v1.2.29 ships.

All three end up in the same `/notes` table; mix freely.

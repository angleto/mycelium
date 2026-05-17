# ADR-0020 Voice notes and conversational capture

Status: accepted. Reuses ADR-0012 (provider abstraction), ADR-0016
(hierarchical memory), ADR-0007 (isolation), ADR-0018/0019 (S3 +
metering). Refines ADR-0019 (metering unit).

## Context

The user wants low-friction capture of ideas/notes by voice and a
text/voice brainstorming mode with an LLM, transcribed into a note.
Primary scenario: capturing while running, with intermittent/no
connectivity.

## Decision

- **Separate capture from processing.** Capture is offline-first and
  not a metered operation; processing (STT, LLM, memory) is async,
  idempotent, server-side, and metered. Same pipeline pattern as
  email->task and memory; reused, not duplicated.
- **Capture**: PWA with `MediaRecorder`, IndexedDB queue, Service
  Worker background sync, resumable presigned multipart upload to
  **S3**. Raw audio is heavy media -> S3 (ADR-0018/0019), never the
  DB; DB holds transcript + metadata. Known limit: reliable background
  capture on web (locked screen, iOS Safari) is constrained; v1 =
  foreground capture + upload queue; always-on capture needs the
  native app (mobile later, decision #10). Stated, not over-promised.
- **`Note` entity** (distinct from Task): kind = voice | text |
  conversation; fields: transcript, `audio_ref` (S3), optional LLM
  outputs (title, summary, action items), tags, (org, project) scope
  (ADR-0007), provenance. Action items may spawn Tasks (reuse the
  email->task flow). Transcript/summary feeds hierarchical memory
  (ADR-0016) so captured ideas resurface when relevant.
- **`TranscriptionProvider`** pluggable (Protocol + DB-driven rate
  card + neutral DTO), same pattern as ADR-0012. Default = **local
  STT** (Whisper/faster-whisper, small/distil multilingual on
  CPU/ARM, swappable to large on GPU or to an API). Audio is personal
  data: local default is consistent with the privacy posture
  (ADR-0012/0016); external STT only with per-Org opt-in + audit. No
  single "best" model fixed; chosen at implementation.
- **`TtsProvider`** pluggable (same pattern as ADR-0012), in v1:
  spoken LLM replies. Local default consistent with the privacy
  posture; external opt-in. Metered per characters/seconds (ADR-0019,
  unit already generalized). Online; offline answers are delivered as
  text (notified) and may be spoken on reconnect.
- **Conversation/brainstorming**: a `conversation` Note with
  user/LLM turns; reuses the LLM provider (ADR-0012), metered
  (ADR-0019); the dialog is saved as the note and summarized into
  memory. Text and voice are the same flow with STT in front and TTS
  for spoken replies (in v1).
- **Interaction model (the LLM does reply)**: online = a live loop
  (speak -> STT -> LLM -> text reply, spoken back via TTS, v1).
  Offline (typical while running, no signal): STT/LLM are
  server-side, metered, online; the question is captured offline (not
  metered, never lost) and answered as soon as connectivity returns --
  the worker runs the LLM, appends the answer turn to the conversation
  Note, and notifies the user (FR-12). On-device LLM for true offline
  replies is out of scope (infeasible on a phone while running).
- **Metering unit generalized (refines ADR-0019)**: the rate card /
  usage unit is first-class: tokens | audio-minutes | tts-chars |
  GB-month. STT billed per audio-minute, TTS per chars/seconds. BYOK
  and zero-credit gating apply to processing. **Capture/recording is
  not metered** and works at zero credits and offline (do not lose the
  idea); only STT/LLM processing is gated.
- **Audio retention**: configurable per Org; default = delete audio
  after confirmed transcription (GDPR minimization). Erasure cascades
  to S3 audio + transcript + memory blobs + spawned-task links
  (provenance, ADR-0005/0016).

## Consequences

- New `Note` domain + capture UI (PWA recorder) + worker STT/LLM
  pipeline; rate card grows an audio-minute unit.
- Phasing: capture + `Note` (no AI) is a contained increment;
  STT/LLM/memory processing is gated behind F5b (metering) and F6
  (memory). Dedicated phase **F6b**.
- TTS (voice-out) is in v1 via a pluggable `TtsProvider`, metered.

## Alternatives rejected

- Browser Web Speech API as primary STT: online-only, inconsistent,
  privacy-poor.
- Audio stored in the DB: wrong storage tier; heavy.
- Tokens-only metering: cannot price STT/TTS; unit must generalize.
- Blocking capture at zero credits: would lose the idea; only
  processing is metered (consistent with ADR-0019).

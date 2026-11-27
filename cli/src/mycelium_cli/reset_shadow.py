"""The reset shadow: at each prompt of a session in an allowed project,
record what `reset_rule` recommends beside what the session then did
(docs/adr/0081). It never acts on a session and never sends anything to the
server.

What is written, to ``<state dir>/reset_shadow/log.jsonl``, is numbers and
event names: context length, call and prompt counts, whether HEAD moved,
the recommendation and its margin, which hook fired. No prompt text, no file
content, no path. The project is named as ``agent_host.project_name`` names
it. Nothing is recorded for a directory that is not under a root the person
allowed with ``mycelium session reset-shadow allow``. A session's history
belongs to the project it ran in, and some projects are a client's.

It runs inside the hook, so it holds to the hook's two properties
(docs/adr/0078). Every error goes to the hook log and is swallowed. When
nothing is allowed it costs one ``stat`` and nothing else.

Standard library only.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mycelium_cli import agent_host
from mycelium_cli.config import state_dir
from mycelium_cli.reset_rule import PARAMETERS, Observation, recommend

#: The log is rotated once it passes this size, keeping one previous file.
#: A prompt record is about 400 bytes, so the bound keeps roughly the last
#: 100,000 prompts. That is months of work, more than one analysis reads.
LOG_LIMIT_BYTES = 40_000_000

#: Tools whose call hands work to another agent.
_DELEGATING_TOOLS = frozenset({"Agent", "Task"})


def _dir() -> Path:
    return state_dir() / "reset_shadow"


def allow_path() -> Path:
    return _dir() / "allow.json"


def log_path() -> Path:
    return _dir() / "log.jsonl"


def _session_path(host_ref: str) -> Path:
    safe = "".join(c for c in host_ref if c.isalnum() or c in "-_")[:128] or "unnamed"
    return _dir() / "sessions" / f"{safe}.json"


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    """Two hooks of one session can run at once. A half-written file would
    lose the transcript offset and count the whole transcript again."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Which projects are allowed
# ---------------------------------------------------------------------------


def allowed_roots() -> list[str]:
    try:
        data = json.loads(allow_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    roots = data.get("roots") if isinstance(data, dict) else None
    return [str(r) for r in roots] if isinstance(roots, list) else []


def _set_roots(roots: list[str]) -> None:
    _write_atomic(allow_path(), {"roots": sorted(set(roots))})


def allow(path: str) -> str:
    root = str(Path(path).expanduser().resolve())
    _set_roots([*allowed_roots(), root])
    return root


def deny(path: str) -> bool:
    root = str(Path(path).expanduser().resolve())
    roots = allowed_roots()
    if root not in roots:
        return False
    _set_roots([r for r in roots if r != root])
    return True


def is_allowed(cwd: str, roots: list[str]) -> bool:
    if not cwd:
        return False
    here = Path(cwd).resolve()
    return any(here == Path(r) or Path(r) in here.parents for r in roots)


# ---------------------------------------------------------------------------
# What one session has shown so far
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Progress:
    """How far the transcript has been read, and what it said. Read
    incrementally from ``offset``, so a prompt costs the lines written since
    the previous one and never the whole transcript."""

    offset: int = 0
    calls: int = 0
    last_message: str | None = None
    context: int = 0
    model: str | None = None
    prompts: int = 0
    head: str | None = None
    failed: bool = False


def _load(host_ref: str) -> Progress:
    try:
        data = json.loads(_session_path(host_ref).read_text(encoding="utf-8"))
        return Progress(**{k: data[k] for k in Progress.__dataclass_fields__ if k in data})
    except (OSError, json.JSONDecodeError, TypeError):
        return Progress()


def _save(host_ref: str, progress: Progress) -> None:
    _write_atomic(_session_path(host_ref), asdict(progress))


def read_transcript(path: str, progress: Progress) -> None:
    """Advance ``progress`` over the main thread's model calls written since
    ``offset``.

    A call's content blocks are written as several records with one message
    id, so a call is counted once per id. A last line without its newline is
    still being written: it is left for the next read. A transcript shorter
    than the offset was replaced, and is read from the start."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size < progress.offset:
        progress.offset, progress.calls, progress.last_message = 0, 0, None
    with open(path, "rb") as f:
        f.seek(progress.offset)
        chunk = f.read()
    end = chunk.rfind(b"\n")
    if end < 0:
        return
    for line in chunk[: end + 1].splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict) or r.get("type") != "assistant" or r.get("isSidechain"):
            continue
        msg = r.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        mid = str(msg.get("id") or "")
        if mid and mid == progress.last_message:
            continue
        progress.last_message = mid or None
        progress.calls += 1
        progress.context = sum(
            int(usage.get(k) or 0)
            for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        if msg.get("model"):
            progress.model = str(msg["model"])
    progress.offset += end + 1


# ---------------------------------------------------------------------------
# The log
# ---------------------------------------------------------------------------


def _append(record: dict[str, Any]) -> None:
    p = log_path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if p.exists() and p.stat().st_size > LOG_LIMIT_BYTES:
        os.replace(p, p.with_suffix(".jsonl.1"))
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def observe(event: str, payload: dict[str, Any]) -> None:
    """Record what this hook event means for the shadow, if anything."""
    if not allow_path().exists():
        return
    cwd = str(payload.get("cwd") or "")
    if not is_allowed(cwd, allowed_roots()):
        return
    ref = str(payload.get("session_id") or "")
    if not ref:
        return
    base = {"ts": _now(), "session": ref, "project": agent_host.project_name(cwd)}

    if event == "UserPromptSubmit":
        progress = _load(ref)
        transcript = str(payload.get("transcript_path") or "")
        if transcript:
            read_transcript(transcript, progress)
        head = agent_host._git(cwd, "rev-parse", "HEAD")
        head_moved = progress.head is not None and head is not None and head != progress.head
        progress.prompts += 1
        rec = recommend(
            Observation(
                model=progress.model,
                context=progress.context,
                failed_since_last_prompt=progress.failed,
            )
        )
        _append(
            {
                **base,
                "event": "prompt",
                "model": progress.model,
                "context": progress.context,
                "calls": progress.calls,
                "prompts": progress.prompts,
                "head_moved": head_moved,
                "after_failure": progress.failed,
                "recommended": rec.action.value if rec else None,
                "basis": rec.basis.value if rec else None,
                "compact_margin_usd": rec.compact_margin_usd if rec else None,
                "parameters": PARAMETERS.version,
            }
        )
        progress.head = head or progress.head
        progress.failed = False
        _save(ref, progress)
    elif event in ("PostToolUseFailure", "StopFailure"):
        progress = _load(ref)
        if not progress.failed:
            progress.failed = True
            _save(ref, progress)
    elif event == "PostToolUse":
        if str(payload.get("tool_name") or "") in _DELEGATING_TOOLS:
            _append({**base, "event": "delegated"})
    elif event == "PreCompact":
        _append({**base, "event": "compact", "trigger": str(payload.get("trigger") or "")})
    elif event == "SessionStart":
        _append({**base, "event": "session_start", "source": str(payload.get("source") or "")})
    elif event == "SessionEnd":
        _append({**base, "event": "session_end"})
        _session_path(ref).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Reading it back
# ---------------------------------------------------------------------------


def _done_after(events: list[dict[str, Any]]) -> str:
    """What the session did between one prompt and the next.

    A compaction or a clear ends what the history was. Of several things
    done in one interval, those win over a delegation, which leaves the
    session's own history in place."""
    done = "continue"
    for e in events:
        kind = e.get("event")
        if kind == "compact":
            return f"compact_{e.get('trigger') or 'unknown'}"
        if kind == "session_start" and e.get("source") == "clear":
            return "restart_clean"
        if kind == "session_end":
            return "ended"
        if kind == "delegated":
            done = "delegate"
    return done


def summary(lines: list[str]) -> dict[str, Any]:
    """Recommended against done, per basis, over the prompts of the log.

    The last prompt of a session still open has no "done" yet, and is
    counted as pending rather than as continued."""
    by_session: dict[str, list[dict[str, Any]]] = {}
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("session"):
            by_session.setdefault(str(r["session"]), []).append(r)
    pairs: Counter[tuple[str, str, str]] = Counter()
    pending = 0
    prompts = 0
    for records in by_session.values():
        idx = [i for i, r in enumerate(records) if r.get("event") == "prompt"]
        for n, i in enumerate(idx):
            prompts += 1
            rec = records[i]
            nxt = idx[n + 1] if n + 1 < len(idx) else None
            tail = records[i + 1 : nxt] if nxt is not None else records[i + 1 :]
            if nxt is None and not any(e.get("event") == "session_end" for e in tail):
                pending += 1
                continue
            pairs[(str(rec.get("basis")), str(rec.get("recommended")), _done_after(tail))] += 1
    return {
        "prompts": prompts,
        "pending": pending,
        "sessions": len(by_session),
        "pairs": [
            {"basis": b, "recommended": r, "done": d, "count": c}
            for (b, r, d), c in sorted(pairs.items())
        ],
        "quality": {
            model: {
                "null": q.null,
                "anchoring_fail_upper": q.anchoring_fail_upper,
                "dilution_fail_upper": q.dilution_fail_upper,
                "source": q.source,
            }
            for model, q in PARAMETERS.quality.items()
        },
    }


def read_log() -> list[str]:
    out: list[str] = []
    for p in (log_path().with_suffix(".jsonl.1"), log_path()):
        try:
            out.extend(p.read_text(encoding="utf-8").splitlines())
        except FileNotFoundError:
            continue
    return out

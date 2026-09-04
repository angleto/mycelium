// Reading the backend's error envelope, once, for every browser client.
//
// The SPA and the browser extension talk to the same FastAPI adapter
// (api/src/mycelium_api/app.py), so turning a failure into something a
// person can read is a property of the SERVER's contract and not of a
// rendering library. That is why it lives here rather than beside either
// client's transport.
//
// Two shapes arrive on the wire and a caller has to survive both:
//
//   our domain errors   {"code": "conflict.stale_version", "detail": "..."}
//   FastAPI's own 422   {"detail": [{"loc": [...], "msg": "...", ...}]}
//
// The second one is an ARRAY, it bypasses the domain handler entirely,
// and rendering it raw white-screens React with "Objects are not valid
// as a React child". That incident is the whole reason this module is
// not three lines at a call site.
//
// Branch on ``code``, never on the prose: ``detail`` is written for a
// person and is localized from Accept-Language, so a caller that parses
// it breaks on a copy edit in a language nobody on the team reads.
//
// Pure by contract: this directory is compiled into more than one
// package and must import nothing -- which is why the fallback sentence
// is a parameter here. The caller resolves it from its own catalogue, so
// no user-facing text is written at the point of use.

export type ApiError = { code?: string; detail?: unknown }

/** The stable domain code, or undefined when the failure carried none
 *  (a transport error, or FastAPI's own validation envelope). */
export function errCode(e: unknown): string | undefined {
  return (e as ApiError | undefined)?.code
}

/** One entry of FastAPI's validation array, as a line a person can act
 *  on: ``field.subfield: message``. ``body`` is dropped from the path
 *  because it names the envelope, not the field the user got wrong. */
function validationLine(x: unknown): string | null {
  if (x && typeof x === 'object' && 'msg' in x) {
    const o = x as { msg?: unknown; loc?: unknown }
    const msg = typeof o.msg === 'string' ? o.msg : ''
    const loc = Array.isArray(o.loc) ? o.loc.filter((p) => p !== 'body').join('.') : ''
    return loc && msg ? `${loc}: ${msg}` : msg || null
  }
  return typeof x === 'string' ? x : null
}

/** Always a string: a non-string ``detail`` must never reach the DOM.
 *  ``fallback`` is the caller's own catalogue sentence, used only when
 *  the server said nothing usable and there is not even a code to show. */
export function errMessage(e: unknown, fallback: string): string {
  const d = (e as ApiError | undefined)?.detail
  if (typeof d === 'string' && d) return d
  if (Array.isArray(d)) {
    const msgs = d.map(validationLine).filter((m): m is string => !!m)
    if (msgs.length) return msgs.join('; ')
  }
  if (d && typeof d === 'object') {
    const m = (d as { msg?: unknown }).msg
    if (typeof m === 'string' && m) return m
  }
  return (e as ApiError | undefined)?.code ?? fallback
}

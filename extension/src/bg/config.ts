// The deployment this package was compiled against, frozen at build time.
//
// Not configurable at run time, and that is the point: the origin here,
// the host permission in the manifest and the origin allowed to hand over
// a credential are all derived from ONE build variable, so a package
// cannot be permitted to reach one deployment while its code talks to
// another. An options field would reintroduce exactly that gap.

declare const __MYC_ORIGIN__: string
declare const __MYC_VERSION_NAME__: string
declare const __MYC_CAN_CONNECT__: boolean

function build(): Readonly<{
  origin: string
  apiUrl: string
  versionName: string
  /** False for a build against a development server. Chrome refuses an
   *  externally_connectable pattern whose host has no second-level
   *  domain, so such a package cannot receive the credential handover --
   *  a platform rule, and the panel says so instead of offering a button
   *  that can never work. */
  canConnect: boolean
}> {
  const origin = __MYC_ORIGIN__
  if (!origin || !/^https?:\/\/[^/]+$/.test(origin)) {
    // A build that inlined `undefined` must fail loudly at load rather
    // than produce requests to "undefined/api".
    throw new Error(`built with an unusable origin: ${String(origin)}`)
  }
  return Object.freeze({
    origin,
    apiUrl: `${origin}/api`,
    versionName: __MYC_VERSION_NAME__,
    canConnect: __MYC_CAN_CONNECT__,
  })
}

export const config = build()

/** Where the app shows an entity. Built from the 8-hex code rather than
 *  the full id: the short routes exist, and a code is what a person
 *  recognises in the address bar. */
export function entityRoute(kind: 'task' | 'note', code: string): string {
  return `${config.origin}/${kind === 'task' ? 't' : 'n'}/${encodeURIComponent(code)}`
}

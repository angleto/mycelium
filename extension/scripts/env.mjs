// The build environment, derived once and validated.
//
// Everything a package can reach is decided here: which deployment it
// talks to, which origin it may fetch from, and which origin may hand it
// a credential. All three come from ONE value, so a package cannot end up
// permitted to reach one deployment while its code talks to another --
// the failure that ships when the host permission and the base URL are
// maintained separately.
//
// There is NO DEFAULT ORIGIN, and that is the important line in this
// file. The prior art this borrows from defaulted to its production host
// and had a Dockerfile that never passed the variable, so every image it
// built shipped an extension pointing at production regardless of which
// deployment it was built for. A missing variable must stop the build,
// not pick somewhere.

import { execFileSync } from 'node:child_process'

/** @typedef {{ baseUrl: string, hostPermission: string, connectMatch: string | null, version: string, versionName: string }} BuildEnv */

/** Chrome accepts up to four dot-separated integers below 65536 and
 *  nothing else: no prefix, no suffix, no fifth part. `git describe`
 *  gives `v2.3.9-12-gabc1234-dirty`, so the numeric core is extracted and
 *  each part clamped. The full string survives in `version_name`, which
 *  is what chrome://extensions actually shows a person.
 *  @param {string | undefined} raw
 *  @returns {string} */
export function toChromeVersion(raw) {
  const match = /(\d+(?:\.\d+){0,3})/.exec(raw ?? '')
  const core = match?.[1]
  if (!core) return '0.0.0'
  return core
    .split('.')
    .slice(0, 4)
    .map((part) => String(Math.min(Number(part), 65535)))
    .join('.')
}

/** @param {string} baseUrl @returns {string} */
export function hostPermissionFor(baseUrl) {
  const url = new URL(baseUrl)
  return `${url.protocol}//${url.host}/*`
}

/** The origin allowed to hand this extension a credential: the app
 *  itself, and nothing else.
 *
 *  Chrome refuses an ``externally_connectable`` pattern whose host has no
 *  second-level domain, so `localhost` and a bare IP cannot appear in one
 *  -- which means a build against a development server CANNOT receive the
 *  handshake at all. That is a platform rule, not something to work
 *  around, so the entry is omitted and the panel says the build cannot
 *  connect. Emitting an invalid pattern instead would make Chrome reject
 *  the whole package with an error about a line nobody wrote by hand.
 *  @param {URL} url @returns {string | null} */
export function connectMatchFor(url) {
  const labels = url.hostname.split('.')
  const hasSecondLevelDomain = labels.length >= 2 && labels.every((part) => part.length > 0)
  const isIp = /^[\d.]+$/.test(url.hostname) || url.hostname.includes(':')
  if (!hasSecondLevelDomain || isIp) return null
  return hostPermissionFor(url.origin)
}

/** @param {string[]} args @returns {string} */
function git(args) {
  try {
    return execFileSync('git', args, { encoding: 'utf8' }).trim()
  } catch {
    return ''
  }
}

/** @param {NodeJS.ProcessEnv} [env] @returns {BuildEnv} */
export function readBuildEnv(env = process.env) {
  const raw = env.MYCELIUM_EXTENSION_ORIGIN
  if (!raw) {
    throw new Error(
      'MYCELIUM_EXTENSION_ORIGIN is required and has no default.\n' +
        'It decides which deployment the package talks to, which origin it\n' +
        'may fetch from, and which origin may hand it a credential.\n' +
        'Example: MYCELIUM_EXTENSION_ORIGIN=https://mycelium.xeno.garden pnpm build',
    )
  }

  let url
  try {
    url = new URL(raw)
  } catch {
    throw new Error(`MYCELIUM_EXTENSION_ORIGIN is not a URL: ${raw}`)
  }

  const local = url.hostname === 'localhost' || url.hostname === '127.0.0.1'
  if (url.protocol !== 'https:' && !local) {
    // A bearer token travels on this origin on every request. Plain HTTP
    // is tolerated only where the traffic cannot leave the machine.
    throw new Error(`MYCELIUM_EXTENSION_ORIGIN must be https outside localhost: ${raw}`)
  }
  if (url.pathname !== '/' || url.search || url.hash) {
    throw new Error(`MYCELIUM_EXTENSION_ORIGIN must be a bare origin, with no path: ${raw}`)
  }

  const versionName =
    env.MYCELIUM_VERSION || git(['describe', '--tags', '--always', '--dirty']) || 'dev'

  return {
    baseUrl: url.origin,
    hostPermission: hostPermissionFor(url.origin),
    connectMatch: connectMatchFor(url),
    version: toChromeVersion(versionName),
    versionName,
  }
}

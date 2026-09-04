// The package Chrome is asked to accept.
//
// Every assertion here is about something that fails LATE and quietly:
// an icon that is not the size it claims, a version Chrome will not
// parse, an origin that ships a package talking to the wrong deployment.
// None of them is visible in a bundle that built successfully.

import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { connectMatchFor, hostPermissionFor, readBuildEnv, toChromeVersion } from '../scripts/env.mjs'
import { manifestFor } from '../scripts/manifest.mjs'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')

const env = readBuildEnv({ MYCELIUM_EXTENSION_ORIGIN: 'https://mycelium.test', MYCELIUM_VERSION: 'v2.3.9-1-gabc' })

describe('the build environment', () => {
  it('has NO default origin: a missing variable stops the build', () => {
    // The prior art defaulted to its production host and had a build
    // that never passed the variable, so every image it produced shipped
    // an extension pointing at production whatever it was built for.
    expect(() => readBuildEnv({})).toThrow(/required and has no default/)
  })

  it('refuses plain http outside localhost, where a bearer token would travel in clear', () => {
    expect(() => readBuildEnv({ MYCELIUM_EXTENSION_ORIGIN: 'http://mycelium.test' })).toThrow(/https/)
    expect(() => readBuildEnv({ MYCELIUM_EXTENSION_ORIGIN: 'http://localhost:5173' })).not.toThrow()
  })

  it('refuses anything but a bare origin', () => {
    for (const bad of ['https://mycelium.test/app', 'https://mycelium.test/?x=1', 'not a url']) {
      expect(() => readBuildEnv({ MYCELIUM_EXTENSION_ORIGIN: bad })).toThrow()
    }
  })

  it('derives the host permission from the same value as the base url', () => {
    expect(env.hostPermission).toBe(hostPermissionFor(env.baseUrl))
    expect(env.baseUrl).toBe('https://mycelium.test')
  })

  it('reduces a git description to something Chrome will parse', () => {
    expect(toChromeVersion('v2.3.9-1-gabc1234-dirty')).toBe('2.3.9')
    expect(toChromeVersion('v2.3')).toBe('2.3')
    // Four parts maximum, each below 65536.
    expect(toChromeVersion('1.2.3.4.5')).toBe('1.2.3.4')
    expect(toChromeVersion('99999.1')).toBe('65535.1')
    expect(toChromeVersion('nothing-numeric')).toBe('0.0.0')
  })

  it('omits the connect origin where Chrome would refuse the pattern', () => {
    // externally_connectable needs a second-level domain, so a build
    // against a development server cannot receive the handover at all.
    // Emitting an invalid pattern would make Chrome reject the whole
    // package with an error about a line nobody wrote.
    expect(connectMatchFor(new URL('http://localhost:5173'))).toBeNull()
    expect(connectMatchFor(new URL('http://127.0.0.1:8000'))).toBeNull()
    expect(connectMatchFor(new URL('https://mycelium.test'))).toBe('https://mycelium.test/*')
  })
})

describe('the manifest', () => {
  const manifest = manifestFor(env)

  it('asks for the short permission set, and nothing that reads every page', () => {
    expect(manifest.permissions).toEqual([
      'storage',
      'sidePanel',
      'contextMenus',
      'activeTab',
      'scripting',
    ])
    expect(JSON.stringify(manifest)).not.toContain('<all_urls>')
    expect(manifest.permissions).not.toContain('tabs')
    expect(manifest.permissions).not.toContain('alarms')
    expect(manifest.permissions).not.toContain('unlimitedStorage')
  })

  it('lets exactly one origin hand it a credential, and it is the app', () => {
    expect(manifest.externally_connectable).toEqual({ matches: ['https://mycelium.test/*'] })
    expect(manifest.host_permissions).toEqual(['https://mycelium.test/*'])
  })

  it('declares no content script at all', () => {
    // Not even on the app's own origin: a script there could read the
    // human's session out of the page's storage.
    expect(manifest).not.toHaveProperty('content_scripts')
  })

  it('carries the readable version as well as the numeric one', () => {
    expect(manifest.version).toBe('2.3.9')
    expect(manifest.version_name).toBe('v2.3.9-1-gabc')
  })

  it('names four shortcuts, which is what Chrome grants', () => {
    expect(Object.keys(manifest.commands)).toHaveLength(4)
  })

  it('declares every icon at a size the file actually is', () => {
    // A PNG says its own dimensions in the IHDR chunk. Declaring 128 and
    // shipping 64 produces a blurred icon and no error anywhere.
    for (const [declared, path] of Object.entries(manifest.icons)) {
      const bytes = readFileSync(join(ROOT, path))
      expect(bytes.subarray(1, 4).toString('ascii'), path).toBe('PNG')
      const width = bytes.readUInt32BE(16)
      const height = bytes.readUInt32BE(20)
      expect(width, path).toBe(Number(declared))
      expect(height, path).toBe(Number(declared))
    }
  })

  it('references only message keys the catalogue defines', () => {
    const catalogue = JSON.parse(
      readFileSync(join(ROOT, '_locales', 'en', 'messages.json'), 'utf8'),
    ) as Record<string, unknown>
    for (const [, key] of JSON.stringify(manifest).matchAll(/__MSG_([A-Za-z0-9_]+)__/g)) {
      expect(catalogue, key).toHaveProperty(key)
    }
  })
})

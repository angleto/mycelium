// Single source of truth for "what kind of attachment is this" — the one
// place the wide format set lives. Both the Attachments panel (which
// preview affordance to show) and the markdown renderer (how to embed a
// `![]()` reference) classify through here so the two never drift.
//
// Classification is mime-first, extension-second. The stored mime_type is
// authoritative WHEN it is specific; when it is missing, empty, or the
// generic octet-stream (the server could not sniff and the client sent
// nothing usable), we fall back to the filename extension. This matters
// for the long tail of code/text files whose extension the server's small
// fallback map does not cover — `report.py` may arrive as octet-stream but
// is still text to a human.

export type AttachmentKind = 'image' | 'pdf' | 'audio' | 'video' | 'text' | 'other'

// application/* mimes that are really text. text/* is handled by prefix.
const TEXT_MIMES = new Set<string>([
  'application/json',
  'application/ld+json',
  'application/x-ndjson',
  'application/xml',
  'application/xhtml+xml',
  'application/javascript',
  'application/ecmascript',
  'application/x-javascript',
  'application/x-yaml',
  'application/yaml',
  'application/toml',
  'application/sql',
  'application/graphql',
  'application/x-sh',
  'application/x-shellscript',
  'application/x-python',
  'application/x-httpd-php',
  'application/csv',
])

// Extension → kind. Deliberately broad: this is the fallback that gives
// the feature its reach over the octet-stream long tail.
const IMAGE_EXT = new Set([
  'png', 'jpg', 'jpeg', 'jfif', 'pjpeg', 'gif', 'webp', 'svg', 'bmp', 'ico',
  'tif', 'tiff', 'avif', 'heic', 'heif', 'apng',
])
const AUDIO_EXT = new Set([
  'mp3', 'wav', 'wave', 'ogg', 'oga', 'opus', 'm4a', 'm4b', 'aac', 'flac',
  'weba', 'aiff', 'aif', 'aifc', 'mid', 'midi', 'amr', '3ga', 'wma',
])
// NB: `.ts`/`.mts` (MPEG transport stream / AVCHD) are deliberately NOT
// here — in this codebase they are far more often TypeScript / TS-module
// source, so they live in TEXT_EXT. A real MPEG-TS upload still classifies
// as video when its mime is specific (video/mp2t wins over the extension).
const VIDEO_EXT = new Set([
  'mp4', 'm4v', 'webm', 'mov', 'ogv', 'mkv', 'avi', '3gp', '3g2', 'mpeg',
  'mpg', 'mpe', 'wmv', 'flv',
])
const TEXT_EXT = new Set([
  // plain / data
  'txt', 'text', 'log', 'csv', 'tsv', 'json', 'json5', 'jsonl', 'ndjson',
  'xml', 'yaml', 'yml', 'toml', 'ini', 'cfg', 'conf', 'config', 'properties',
  'env', 'rst', 'tex', 'bib', 'srt', 'vtt', 'diff', 'patch',
  // markdown
  'md', 'markdown', 'mdown', 'mkd', 'mdx',
  // markup / style
  'html', 'htm', 'xhtml', 'css', 'scss', 'sass', 'less', 'svg',
  // code
  'js', 'mjs', 'cjs', 'jsx', 'ts', 'tsx', 'mts', 'cts', 'py', 'pyi', 'rb',
  'go', 'rs', 'java', 'kt', 'kts', 'scala', 'sc', 'c', 'h', 'cpp', 'cxx',
  'cc', 'hpp', 'hh', 'hxx', 'm', 'mm', 'sh', 'bash', 'zsh', 'fish', 'ps1',
  'bat', 'cmd', 'sql', 'graphql', 'gql', 'proto', 'lua', 'pl', 'pm', 'php',
  'swift', 'dart', 'r', 'jl', 'ex', 'exs', 'erl', 'hs', 'clj', 'cljs', 'cljc',
  'edn', 'vue', 'svelte', 'astro', 'tf', 'hcl', 'gradle', 'groovy', 'cmake',
])
// Extensionless filenames that are conventionally plain text.
const TEXT_BASENAMES = new Set([
  'dockerfile', 'makefile', 'rakefile', 'gemfile', 'procfile', 'license',
  'readme', 'changelog', 'authors', 'notice', 'copying', '.gitignore',
  '.dockerignore', '.editorconfig', '.env',
])

const MARKDOWN_EXT = new Set(['md', 'markdown', 'mdown', 'mkd'])

// Reduce a reference / filename to a comparable basename: drop query/hash,
// take the last path segment, percent-decode, lowercase. Mirrors
// attachmentManifest.attachmentBasename so name resolution and kind
// detection agree on what the filename is.
function basename(name: string): string {
  const noFragment = name.split(/[?#]/, 1)[0]
  const seg = noFragment.split('/').pop() ?? noFragment
  let decoded = seg
  try {
    decoded = decodeURIComponent(seg)
  } catch {
    // malformed escape: keep raw
  }
  return decoded.trim().toLowerCase()
}

function extOf(name: string): string {
  const base = basename(name)
  const dot = base.lastIndexOf('.')
  return dot > 0 ? base.slice(dot + 1) : ''
}

function normalizeMime(mime: string | null | undefined): string {
  return (mime ?? '').toLowerCase().split(';', 1)[0].trim()
}

// Concrete kind from a specific mime, or null when the mime has no
// opinion (empty / octet-stream) so the extension can decide.
function kindFromMime(m: string): AttachmentKind | null {
  if (!m || m === 'application/octet-stream' || m === 'binary/octet-stream') {
    return null
  }
  // svg is XML but renders as an image (and an <img> context disables any
  // embedded script), so classify it as image despite being in TEXT_MIMES.
  if (m === 'image/svg+xml') return 'image'
  if (m.startsWith('image/')) return 'image'
  if (m.startsWith('audio/')) return 'audio'
  if (m.startsWith('video/')) return 'video'
  if (m === 'application/pdf') return 'pdf'
  if (m.startsWith('text/')) return 'text'
  if (TEXT_MIMES.has(m)) return 'text'
  return null
}

function kindFromName(name: string): AttachmentKind | null {
  const ext = extOf(name)
  if (ext) {
    if (IMAGE_EXT.has(ext)) return 'image'
    if (AUDIO_EXT.has(ext)) return 'audio'
    if (VIDEO_EXT.has(ext)) return 'video'
    if (ext === 'pdf') return 'pdf'
    if (TEXT_EXT.has(ext)) return 'text'
    return null
  }
  if (TEXT_BASENAMES.has(basename(name))) return 'text'
  return null
}

/**
 * Classify an attachment by its (mime, filename). Mime wins when specific;
 * the filename breaks ties for the octet-stream long tail. Returns 'other'
 * when neither yields a previewable kind.
 */
export function attachmentKind(
  mime: string | null | undefined,
  filename?: string | null,
): AttachmentKind {
  const byMime = kindFromMime(normalizeMime(mime))
  if (byMime) return byMime
  if (filename) {
    const byName = kindFromName(filename)
    if (byName) return byName
  }
  return 'other'
}

/** True when the attachment should render as markdown (a `.md` file or a
 * `text/markdown` mime), as opposed to raw monospace text. */
export function isMarkdown(
  mime: string | null | undefined,
  filename?: string | null,
): boolean {
  if (normalizeMime(mime) === 'text/markdown') return true
  return !!filename && MARKDOWN_EXT.has(extOf(filename))
}

/** Whether a kind has an in-app preview (panel lightbox / markdown embed).
 * 'other' (zip, binaries, office docs) stays download-only. */
export function isPreviewableKind(kind: AttachmentKind): boolean {
  return kind !== 'other'
}

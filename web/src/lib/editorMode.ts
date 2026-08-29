import { useSyncExternalStore } from 'react'
import { DEFAULT_MODE, type MarkdownMode } from './markdownSource/mode'

// Which of the two views every markdown editor on the page is showing.
//
// One preference for the whole app, with subscribers, rather than the
// per-instance `useState(read)` the toolbar-collapse switch uses. A note
// mounts one editor per part, plus the annotation composers, and a setting
// that changes what the caret does cannot be allowed to disagree between two
// editors that are on screen at the same time.
//
// Persisted, like the theme: it is a way of working, not a per-document
// property, and nothing about it reaches the server or the body.

const KEY = 'mycelium.rte.mode'

const listeners = new Set<() => void>()

function read(): MarkdownMode {
  try {
    const v = localStorage.getItem(KEY)
    return v === 'source' || v === 'visual' ? v : DEFAULT_MODE
  } catch {
    // Private mode / storage disabled.
    return DEFAULT_MODE
  }
}

let current: MarkdownMode = read()

export function getEditorMode(): MarkdownMode {
  return current
}

export function setEditorMode(mode: MarkdownMode): void {
  if (mode === current) return
  current = mode
  try {
    localStorage.setItem(KEY, mode)
  } catch {
    // The switch still works for this session, it just is not remembered.
  }
  for (const fn of listeners) fn()
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

export function useEditorMode(): MarkdownMode {
  return useSyncExternalStore(subscribe, getEditorMode, getEditorMode)
}

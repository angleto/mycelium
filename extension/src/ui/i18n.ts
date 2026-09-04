// Every string a person reads, resolved by key.
//
// Typed against the catalogue Chrome itself reads, so a key that does not
// exist fails to COMPILE rather than rendering as an empty string at run
// time -- which is what chrome.i18n does with an unknown key, silently.
//
// The language is the browser's, and the same choice is sent as
// Accept-Language on every request, so the panel's own words and the
// server's error sentence are in one language. The app is fixed to
// English and gets that wrong in the other direction: an Italian `detail`
// arriving inside English chrome.

import type catalogue from '../../_locales/en/messages.json'

export type MessageKey = keyof typeof catalogue

export function m(key: MessageKey, ...substitutions: string[]): string {
  const resolved = chrome.i18n.getMessage(key, substitutions)
  // Only reachable if a catalogue entry was removed without its call
  // site, which the message gate refuses. Showing the key beats showing
  // a blank: a blank looks like a rendering bug rather than a missing
  // translation.
  return resolved || key
}

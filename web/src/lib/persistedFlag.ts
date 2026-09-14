import { useEffect, useState } from 'react'

// A boolean the user set, remembered across reloads.
//
// Two disclosures now want the same thing: a section that opens and closes,
// whose state belongs to the person and not to the session. The first one
// (the recent-tasks widget) carried its own copy of read + write + the
// try/catch around a localStorage that can throw; the second would have been
// the moment that copy became a convention nobody checks, so it is a hook
// instead.
//
// `fallback` is what a profile that has never chosen gets. It is a real
// decision, not a default: both call sites open a panel that costs vertical
// space on a page where the thing the user came for was already below the
// fold, so both pass `false`. Whoever opens one keeps it open.
//
// Every access is guarded: localStorage throws in a private window, and the
// value can be anything a previous version wrote. A stored value that is
// neither '0' nor '1' is not a state, so it is ignored rather than coerced.
export function usePersistedFlag(key: string, fallback: boolean): [boolean, (v: boolean) => void] {
  const [value, setValue] = useState<boolean>(() => {
    try {
      const v = localStorage.getItem(key)
      if (v === '0') return false
      if (v === '1') return true
    } catch {
      /* private mode / quota: fall through to the fallback */
    }
    return fallback
  })
  useEffect(() => {
    try {
      localStorage.setItem(key, value ? '1' : '0')
    } catch {
      /* the preference is a convenience; losing it must not break the page */
    }
  }, [key, value])
  return [value, setValue]
}

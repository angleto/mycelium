// Shared elapsed/time formatting (timer chips, running indicator).
export function hms(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  return `${Math.floor(s / 3600)}:${String(
    Math.floor((s % 3600) / 60),
  ).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

export function elapsedSec(startedAtIso: string, nowMs: number): number {
  return (nowMs - new Date(startedAtIso).getTime()) / 1000
}

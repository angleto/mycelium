// Foreground for text sitting on an arbitrary user-chosen color (tag
// colors): WCAG relative luminance decides between white and a
// near-black. 0.21 is where the two contrast ratios cross, so each
// side gets the better of the two — light tag colors (yellows,
// pastels) flip to dark text instead of a fixed white that was
// unreadable on them in both themes.
export function readableOn(bg: string): '#fff' | '#1c2420' {
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(bg.trim())
  if (!m) return '#fff'
  let hex = m[1]
  if (hex.length === 3) hex = hex.replace(/./g, (c) => c + c)
  const n = parseInt(hex, 16)
  const lin = (c: number) => {
    const s = c / 255
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  }
  const L =
    0.2126 * lin((n >> 16) & 255) +
    0.7152 * lin((n >> 8) & 255) +
    0.0722 * lin(n & 255)
  return L > 0.21 ? '#1c2420' : '#fff'
}

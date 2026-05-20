// Kind → glyph mapping for tag chips. Lifted out of TagChip.tsx so
// the GraphRoute filter chips can reuse it without a circular import
// (and so Vite fast-refresh stays happy — a .tsx file can only export
// components, not constants/functions, under react-refresh).
//
// Glyphs encode the tag's kind so two tags with the same name on
// different clients/projects are distinguishable at a glance:
//   client         -> ▲ triangle
//   project        -> ■ square
//   memory_channel -> ◆ diamond
//   generic / else -> ● circle
export function kindGlyph(kind: string | undefined): string {
  switch (kind) {
    case 'client':
      return '▲'
    case 'project':
      return '■'
    case 'memory_channel':
      return '◆'
    default:
      return '●'
  }
}

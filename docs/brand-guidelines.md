# Flow brand guidelines

## Concept

Flow's mark is the **Mycelium Trio**: a tree, two mushrooms, and the
mycelium network that connects them under the soil. It anchors the
product vision of "the forest of memory" (note `3747eaac`) and the
fungal decomposition metaphor that differentiates Flow from
note-as-rigid-document tools.

## Files

| Use case | File | Notes |
|---|---|---|
| App favicon / PWA | `web/public/favicon.svg`, `icon-192.png`, `icon-512.png`, `apple-touch-icon.png` | bake-in `#4a6b3e` background |
| App topbar mark | `web/src/components/Logo.tsx` | uses `var(--brand)` / `var(--brand-fg)`; adapts to theme |
| Standalone full mark | `assets/flow-logo.svg` | 128×128, brand background |
| Compact / very small | `assets/flow-monogram.svg` | drops soil dashes and curving roots; readable down to 16px |
| Horizontal lockup | `assets/flow-logo-horizontal.svg` | mark + serif wordmark |
| Monochrome, dark on light | `assets/flow-logo-mono-dark.svg` | print, light overlays |
| Monochrome, light on dark | `assets/flow-logo-mono-light.svg` | dark overlays |
| Social card / OG image | `assets/flow-og.svg`, `assets/flow-og.png` | 1200×630, dark forest gradient |

## Palette

| Token | Light | Dark | Role |
|---|---|---|---|
| `--bg` | `#f7f5ef` (avorio) | `#0f1612` (verde-nero sottobosco) | page background |
| `--surface` | `#fdfcf7` | `#161e18` | cards, panels |
| `--accent` / `--brand` / `--moss` | `#4a6b3e` (verde musco) | `#7fa56e` (musco luna) | identity + primary actions |
| `--accent-weak` / `--brand-weak` | `#e2ecde` | `#1d2c1f` | subtle accent backgrounds |
| `--accent-fg` / `--brand-fg` | `#ffffff` | `#0f1612` | text on accent |
| `--bark` | `#6a4f33` (marrone humus) | `#a98963` | secondary identity, tertiary surfaces |
| `--bloom` | `#c97b9f` (ocra/fiore) | `#d99cb8` | mindmap halo, cross-pollination accents |
| `--err` | `#a13322` | `#f08a76` | error |
| `--ok` | `#2f7d3f` | `#6dc176` | success |

Verde musco on white reaches contrast ratio **6.2:1** (WCAG AA for
normal text), the dark-mode pair musco luna on `#0f1612` reaches
**6.9:1**. Avoid using the accent against `--surface-2` in light mode
without a border — it sits at ~5.3:1 against the warmer surface.

## Typography

- **Display** (`--font-display`): `ui-serif, Georgia, 'Times New Roman', serif`. Used for `h1`–`h3` and the wordmark. The system serif keeps the build free of external font requests; it carries the organic warmth required by the forest metaphor.
- **Body** (`--sans`): `system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif`. Unchanged for readability and zero-cost rendering.
- **Mono** (`--mono`): `ui-monospace, SFMono-Regular, Consolas, monospace`.

Headings use `font-weight: 600` and `letter-spacing: -0.01em` for a
tighter, editorial look that pairs with the serif.

## Clear space and minimum sizes

- **Clear space** around the mark: at least the height of one
  mushroom cap (~25% of the mark's height) on every side. For the
  horizontal lockup, the same clear space applies around the bounding
  box of mark + wordmark.
- **Minimum sizes**:
  - Full mark (`flow-logo.svg`): 24 px square. Below that, switch to
    `flow-monogram.svg` which keeps the four mycelium nodes legible
    down to 16 px.
  - Horizontal lockup: 96 px wide minimum.

## Do / don't

- **Do** use `flow-logo.svg` or `flow-monogram.svg` on solid
  backgrounds. Prefer `--surface` (avorio) in light contexts and
  `--bg` (verde-nero) in dark contexts.
- **Do** use the monochrome variants for one-color print, embossing,
  or single-channel overlays.
- **Don't** recolour the brand background to a non-palette hue.
- **Don't** stretch the mark non-uniformly or rotate it.
- **Don't** place the mark on a photographic background without an
  intermediate scrim that meets the clear-space requirement.
- **Don't** swap viola back in as the primary action colour — viola
  was the v1 mark and reads off-brand against the forest palette.

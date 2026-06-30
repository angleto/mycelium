// Garden iconography (task 56d80038): forest-metaphor glyphs for note
// link kinds, note maturity stages, and the centrality / cluster
// badges in the mindmap.
//
// Coherent with NavIcon: 24x24 viewBox, single <path> per name,
// stroke = currentColor so the dark theme inherits without a separate
// mono asset. The set is the foundation the garden / mindmap views
// pull from when they render link chips, maturity dots, or hub
// badges.
//
// Naming groups (with their forest metaphor in parens):
//   - note<->note link kinds (ADR-0040 4-verb model): hypha_of
//     (filament branching off a node), related (two nodes joined by a
//     plain line), supersedes (one shape overtaking another),
//     contradicts (broken twig)
//   - note<->task link kinds: references (pointing arc) and
//     derives_from (root spread); leaf / branch double as task-link
//     glyphs (see LinkedTasksPanel)
//   - maturity: seed (buried seed), sprout (two cotyledons),
//     branch (forked stem), leaf (full leaf), compost (decomposing
//     leaf)
//   - structural: root (deep tap-root for centrality hubs),
//     cluster (mushroom trio for community groups)

export type GardenIconName =
  // note<->note link kinds (ADR-0040)
  | 'hypha_of'
  | 'related'
  | 'supersedes'
  | 'contradicts'
  // note<->task link kinds (LinkedTasksPanel)
  | 'references'
  | 'derives_from'
  // Maturity
  | 'seed'
  | 'sprout'
  | 'branch'
  | 'leaf'
  | 'compost'
  // Structural badges
  | 'root'
  | 'cluster'

const PATH: Record<GardenIconName, string> = {
  // --- note<->note link kinds (ADR-0040 4-verb model) ---------------
  // Hypha: a filament sprouting off a parent node (derived-from /
  // grew-from). Directional, origin at the node on the left.
  hypha_of:
    'M5 12a2 2 0 1 1 0-0.01M7 12c4 0 6-3 10-5M17 7l1 3M17 7l-3 1M7 12c4 0 6 3 10 5M17 17l1-3M17 17l-3-1',
  // Related: two small nodes joined by a plain undirected line.
  related:
    'M6 12a2.2 2.2 0 1 1 0-0.01M18 12a2.2 2.2 0 1 1 0-0.01M8.2 12h7.6',
  // Supersedes: one shape overtaking another, an arrow replacing the
  // old node (the target decays toward dormant).
  supersedes:
    'M6 9a3 3 0 1 0 0 6M4 12h11M11 8l4 4-4 4M18 7v10',
  // Broken twig: a snapped stem (contradiction = severed link).
  contradicts:
    'M4 6c4 2 6 4 7 6M20 18c-4-2-6-4-7-6M11 12l-2 1M13 12l2-1M9 13l-3 1M15 11l3-1',

  // --- note<->task link kinds (LinkedTasksPanel) --------------------
  // Pointing arc: classic right-arrow trimmed to a citation gesture.
  references: 'M4 12h13M11 6l6 6-6 6',
  // Roots spreading: source-of (derives_from = origin in the soil).
  derives_from:
    'M12 3v8M12 11c-2 0-3 1-5 4M12 11c2 0 3 1 5 4M5 18v3M12 11v10M19 18v3',

  // --- Maturity ------------------------------------------------------
  // Seed: a small oval cotyledon under the soil line.
  seed: 'M12 11c1.5 0 2.5 1 2.5 2s-1 2-2.5 2-2.5-1-2.5-2 1-2 2.5-2ZM4 18h16',
  // Sprout: two cotyledons on a tiny stem.
  sprout:
    'M12 20v-7M12 13c-2.5 0-4-2-4-4 2.5 0 4 2 4 4ZM12 13c2.5 0 4-2 4-4-2.5 0-4 2-4 4ZM4 20h16',
  // Branch: forked twig with a single leaf at the tip.
  branch:
    'M5 19c2-2 4-4 6-8M11 11c2 0 4 1 6 3M11 11c1-1 1-3 0-5M11 11c-1-1-3-1-5 0',
  // Leaf: classic teardrop leaf with central vein.
  leaf: 'M5 19c1-9 7-15 14-15-1 9-7 15-14 15ZM6 18 18 6',
  // Compost: a curled leaf falling into a heap of soil.
  compost:
    'M12 4c-4 0-6 4-6 7s2 6 5 6 6-3 6-7c0-2-1-3-2-3M4 21h16M6 19c2-1 6-1 12 0',

  // --- Structural badges --------------------------------------------
  // Root: deep tap-root with two lateral spreads -- the centrality
  // hub of a sub-graph.
  root:
    'M12 3v9M12 12c-4 0-6 3-7 6M12 12c4 0 6 3 7 6M12 12c-2 2-2 5-2 8M12 12c2 2 2 5 2 8',
  // Cluster: three mushroom caps grouped (Leiden community).
  cluster:
    'M4 14a3 3 0 0 1 6 0M4 14h6M6 14v5M8 14v5M10 9a3 3 0 0 1 6 0M10 9h6M12 9v3M14 9v3M14 16a3 3 0 0 1 6 0M14 16h6M16 16v5M18 16v5',
}

// One entry per icon kept inline so a future a11y pass can swap the
// generic "aria-hidden" for a per-icon label without touching the
// callers.
export function GardenIcon({
  name,
  size = 16,
}: {
  name: GardenIconName
  size?: number
}) {
  return (
    <svg
      className="gardenicon"
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={PATH[name]} />
    </svg>
  )
}

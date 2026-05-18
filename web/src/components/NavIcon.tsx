// Compact inline-SVG nav icons (16px, stroke = currentColor so they
// inherit the link colour / active state). One <path> set per route.
export type IconName =
  | 'home'
  | 'tasks'
  | 'trash'
  | 'time'
  | 'schedule'
  | 'calendar'
  | 'workflows'
  | 'graph'
  | 'tags'
  | 'clients'
  | 'advisory'
  | 'budgets'
  | 'notes'
  | 'memory'
  | 'email'
  | 'notifications'
  | 'billing'
  | 'invoices'
  | 'settings'
  | 'shield'

const P: Record<IconName, string> = {
  home: 'M3 10.5 12 3l9 7.5M5 9.5V21h14V9.5',
  tasks: 'M9 6h12M9 12h12M9 18h12M4 6l1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2',
  trash: 'M4 7h16M9 7V4h6v3M6 7l1 14h10l1-14',
  time: 'M12 7v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
  schedule: 'M4 6h16M4 12h10M4 18h7M16 16l2 2 4-4',
  calendar: 'M4 5h16v16H4zM4 9h16M8 3v4M16 3v4',
  workflows: 'M6 5h6v4H6zM12 15h6v4h-6zM9 9v3a3 3 0 0 0 3 3',
  graph: 'M5 19V5M5 19h14M9 15l3-4 3 2 4-6',
  tags: 'M3 12 12 3h8v8l-9 9zM16.5 7.5h.01',
  clients: 'M16 19v-2a4 4 0 0 0-8 0v2M12 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z',
  advisory: 'M12 3a7 7 0 0 0-4 12.7V18h8v-2.3A7 7 0 0 0 12 3ZM9 21h6',
  budgets: 'M3 12a9 9 0 1 0 18 0 9 9 0 0 0-18 0ZM12 7v10M9.5 9.5h4a1.5 1.5 0 0 1 0 3h-3a1.5 1.5 0 0 0 0 3h4',
  notes: 'M6 3h9l5 5v13H6zM14 3v6h6M9 13h7M9 17h7',
  memory: 'M9 3h6a3 3 0 0 1 3 3v12a3 3 0 0 1-3 3H9a3 3 0 0 1-3-3V6a3 3 0 0 1 3-3ZM9 8h6M9 12h6M9 16h4',
  email: 'M3 6h18v12H3zM3 7l9 7 9-7',
  notifications: 'M18 10a6 6 0 1 0-12 0c0 6-2 7-2 7h16s-2-1-2-7M10 21h4',
  billing: 'M3 7h18v10H3zM3 11h18M7 15h3',
  invoices: 'M6 3h9l4 4v14H6zM14 3v5h5M9 12h6M9 16h6',
  settings:
    'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6ZM19 12a7 7 0 0 0-.1-1l2-1.6-2-3.4-2.4 1a7 7 0 0 0-1.7-1l-.4-2.5h-4l-.4 2.5a7 7 0 0 0-1.7 1l-2.4-1-2 3.4 2 1.6a7 7 0 0 0 0 2l-2 1.6 2 3.4 2.4-1a7 7 0 0 0 1.7 1l.4 2.5h4l.4-2.5a7 7 0 0 0 1.7-1l2.4 1 2-3.4-2-1.6c.07-.33.1-.66.1-1Z',
  shield: 'M12 3 5 6v6c0 4 3 7 7 9 4-2 7-5 7-9V6l-7-3ZM9.5 12l2 2 3.5-4',
}

export function Icon({ name }: { name: IconName }) {
  return (
    <svg
      className="nav__svg"
      viewBox="0 0 24 24"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={P[name]} />
    </svg>
  )
}

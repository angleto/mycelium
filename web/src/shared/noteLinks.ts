// The kinds of a typed link between two notes, as the API names them.
//
// The anchor-relative reading of those links (which end is the far one,
// which kinds have a direction) used to live here for the per-mechanism
// panels. Since ADR-0077 the server answers that question for every kind of
// connection at once (``connections.list`` returns each connection with its
// direction from the item asked about), so only the kind names remain.
//
// Pure by contract: this directory is compiled into both packages and
// imports nothing.

/** The four kinds of link between two notes (ADR-0040). */
export type NoteLinkKind = 'hypha_of' | 'related' | 'supersedes' | 'contradicts'

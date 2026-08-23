// A ``mailto:`` href built from an address that came from someone else's
// system.
//
// Lives here rather than beside its one caller because both of its rules are
// about untrusted input, not about the payment-connector screen: the same
// reasoning applies to any address a provider, an inbox or an import hands us.
//
// RULE 1 -- only link what is plausibly an address. A provider that put a note,
// a customer id or an empty-ish string in its email field would otherwise get
// a live-looking link that goes nowhere.
//
// RULE 2 -- encode each half and rejoin on a literal ``@`` (RFC 6068 spells the
// addr-spec that way). This is the security half: ``?`` opens the HEADER
// section of a mailto URI, so an address ending in ``?subject=…&body=…`` would
// hand the operator a pre-composed mail nobody in this org wrote, addressed to
// a customer, with the org's own mail client as the sender. ``,`` is the
// address separator and would silently turn one recipient into two.

/** ``mailto:`` for ``email``, or null when it is not plausibly an address. */
export function mailtoHref(email: string): string | null {
  if (!/^[^\s@]+@[^\s@]+$/.test(email)) return null
  const [local, domain] = email.split('@')
  return `mailto:${encodeURIComponent(local)}@${encodeURIComponent(domain)}`
}

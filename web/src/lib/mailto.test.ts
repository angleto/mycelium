import { describe, expect, it } from 'vitest'
import { mailtoHref } from './mailto'

describe('mailtoHref', () => {
  it('links an ordinary address', () => {
    expect(mailtoHref('amministrazione@acme.test')).toBe('mailto:amministrazione@acme.test')
  })

  it('refuses to link what is not plausibly an address', () => {
    // Everything a provider's "email" field turns out to hold in practice.
    for (const value of ['', ' ', 'cus_QZ8bk3Ta', 'nessuna email', 'a@b c', 'a@b@c', '@b.test']) {
      expect(mailtoHref(value)).toBeNull()
    }
  })

  it('cannot be talked into composing a mail nobody wrote', () => {
    // ``?`` opens the header section of a mailto URI. Unencoded, this address
    // would pre-fill a subject and a body in the operator's mail client, in
    // their name, to a customer. The whole thing must stay in the recipient.
    const href = mailtoHref('victim@acme.test?subject=Pagamento&body=Invia%20a%20IBAN')
    expect(href).not.toBeNull()
    expect(href).not.toContain('?')
    expect(href).toContain('%3F')
  })

  it('keeps one recipient from becoming two', () => {
    // ``,`` separates addresses in a mailto URI, so it stays inside the one
    // recipient. A value carrying a SECOND ``@`` never gets this far -- the
    // plausibility guard has already refused it.
    expect(mailtoHref('a,c@d.test')).toBe('mailto:a%2Cc@d.test')
    expect(mailtoHref('a@b.test,c@d.test')).toBeNull()
  })

  it('leaves the addr-spec @ literal', () => {
    // RFC 6068's addr-spec carries an unencoded ``@``; percent-encoding it
    // would be a different (and less well supported) URI.
    expect(mailtoHref('a@b.test')).toContain('@b.test')
  })
})

"""OAuth for MCP hosts, in three layers (ADR-0069).

``resolution``  who is this client? One contract (``ClientResolver``), a
                registry that asks its strategies in order, and the
                strategies themselves under ``strategies/``. Adding a way of
                identifying a client is a module there and one entry in
                ``resolution.default_registry``; nothing else changes.
``flow``        the authorization path, the same for every strategy: the
                endpoints hand it a client id and get back a redirect, a
                consent request or a token, and never ask which strategy
                answered.
``issuance``    what a successful exchange produces. ``CredentialIssuer`` is
                the seam a different kind of token (short-lived, refreshable)
                plugs into without touching the two layers above.

``redirects`` (where a code may go) and ``cimd`` (fetching a Client ID
Metadata Document safely) are components the strategies use.
"""

# Privacy statement: the Mycelium browser extension

This is the statement the Chrome Web Store listing points at. It describes
the extension only. The service it talks to is a Mycelium deployment, and
what that deployment does with your data is the operator's to state.

## What it sends, and where

The extension talks to **one** server: the Mycelium deployment it was built
for, named in its own manifest as the only host it may reach. It cannot send
anything anywhere else, and there is no analytics service, no error reporting
service and no third party of any kind.

It sends, to that deployment only:

- what you type into its search box, when you search;
- the identifiers of the tasks and notes you open from a search result,
  together with the query and the position you opened, which is how the
  deployment measures whether its search is finding things (this is recorded
  for ranked results only);
- the changes you make: a state, a due date, an importance, a title;
- what you choose to capture: the address and title of the page you are on,
  any text you had selected, and any screenshot or file you explicitly
  attach.

## What it reads from a page

**Nothing, until you ask it to.** The extension declares no content script,
so none of its code runs on the pages you visit.

When you invoke capture, it reads the current tab's address, title and your
current text selection — from that tab, at that moment, once. Where the
browser refuses to let an extension read a page at all, capture still works
from the address and the title, and says the selection could not be included.

A screenshot is taken only when you press the screenshot button, and only of
the tab you invoked the extension from.

## What it stores, and where

Everything is stored **locally in your browser**. Nothing is synchronised to
any server by the extension.

| Kept until the browser closes | Kept until you disconnect |
|---|---|
| cached lists and lookups, unsent capture drafts | the credential, the workspace and focus you selected, the titles of what you recently opened |

The credential is a token minted by your Mycelium deployment when you approve
a connection there. It is limited to a fixed list of permissions, shown to
you before it is created, and it cannot read your account or reach your other
workspaces.

Storage is the browser's own, in your profile directory, with no additional
encryption. Anything running as your browser profile can read it. Disconnect
in the extension to remove it from a machine; revoke it in Mycelium's
settings to end it on the server.

## What it never does

- No advertising, no tracking, no profiling, no fingerprinting.
- Nothing is sold, and nothing is shared with anyone.
- No data is sent to any server other than the Mycelium deployment it was
  built for.
- Your password is never typed into the extension and never passes through
  it.

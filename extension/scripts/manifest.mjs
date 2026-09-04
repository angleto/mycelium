// The manifest, derived rather than maintained.
//
// Every value that could disagree with the bundle -- the version, the
// origin it may reach, the origin that may talk to it -- is computed from
// the same build environment the code is compiled against, so the two
// cannot drift. What is left here is the part that is genuinely a
// decision: which permissions the extension asks for, and why.

/** @typedef {import('./env.mjs').BuildEnv} BuildEnv */

/** @param {BuildEnv} env */
export function manifestFor(env) {
  return {
    manifest_version: 3,
    name: '__MSG_extName__',
    description: '__MSG_extDescription__',
    default_locale: 'en',
    version: env.version,
    // The full `git describe` string. `version` is a number Chrome
    // compares; this is what a person reads when reporting a problem.
    version_name: env.versionName,
    minimum_chrome_version: '116',
    icons: {
      16: 'icons/icon-16.png',
      32: 'icons/icon-32.png',
      48: 'icons/icon-48.png',
      128: 'icons/icon-128.png',
    },
    action: {
      default_popup: 'popup.html',
      default_title: '__MSG_extName__',
      default_icon: { 16: 'icons/icon-16.png', 32: 'icons/icon-32.png' },
    },
    background: { service_worker: 'background.js', type: 'module' },
    side_panel: { default_path: 'sidepanel.html' },
    // Each of these is justified in the store listing, and the list is
    // deliberately short:
    //
    //   storage      the credential, the scope selection, the recents
    //   sidePanel    the persistent working surface
    //   contextMenus capture from a selection or a link
    //   activeTab    read the page ONLY at the moment capture is invoked,
    //                and only the tab it was invoked from
    //   scripting    what activeTab is exercised through
    //
    // NOT requested, and each absence is a decision:
    //
    //   <all_urls>          no in-page overlay, so no code of ours runs on
    //                       every page you visit
    //   alarms              nothing needs a background timer, and asking
    //                       for one invites a review question it cannot
    //                       answer
    //   notifications       a write that lands after the panel closed is
    //                       reported the next time the panel opens, not by
    //                       interrupting the desktop
    //   tabs                chrome.tabs.create and .query need no
    //                       permission for what this does
    //   unlimitedStorage    the caches are bounded by count on purpose
    permissions: ['storage', 'sidePanel', 'contextMenus', 'activeTab', 'scripting'],
    host_permissions: [env.hostPermission],
    // The app origin, and nothing else, may hand this extension a
    // credential. Chrome fills in the sender's origin, so the page cannot
    // claim to be somewhere it is not -- which is why this is a handshake
    // over externally_connectable rather than a content script reading
    // the page. A content script on the app origin could read the human's
    // session out of localStorage; this cannot read the page at all.
    // Omitted entirely for a development build: Chrome refuses a pattern
    // whose host has no second-level domain, so a package built against
    // localhost simply cannot receive the handover, and the panel says so
    // rather than offering a Connect button that can never succeed.
    ...(env.connectMatch ? { externally_connectable: { matches: [env.connectMatch] } } : {}),
    commands: {
      _execute_action: {
        suggested_key: { default: 'Ctrl+Shift+K', mac: 'Command+Shift+K' },
        description: '__MSG_cmdOpenPanel__',
      },
      'open-side-panel': {
        suggested_key: { default: 'Ctrl+Shift+L', mac: 'Command+Shift+L' },
        description: '__MSG_cmdOpenSidePanel__',
      },
      capture: {
        suggested_key: { default: 'Ctrl+Shift+S', mac: 'Command+Shift+S' },
        description: '__MSG_cmdCapture__',
      },
      'search-selection': {
        suggested_key: { default: 'Ctrl+Shift+F', mac: 'Command+Shift+F' },
        description: '__MSG_cmdSearchSelection__',
      },
    },
    omnibox: { keyword: 'myc' },
  }
}

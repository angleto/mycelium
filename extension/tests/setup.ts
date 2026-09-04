// jsdom gives a document; it does not give chrome.* or a fetch worth
// asserting on. Each test installs its own fake, so nothing leaks
// between them.
export {}

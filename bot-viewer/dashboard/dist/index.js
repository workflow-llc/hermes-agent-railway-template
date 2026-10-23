// Deliberately empty dashboard entry.
//
// This plugin is a standalone page (`/api/plugins/bot-viewer/page`), not a React tab in the
// dashboard SPA: the viewer needs its own document so noVNC can own the canvas and so the page
// works on its own URL, which is what gets pasted into Slack. The tab entry in manifest.json is
// `hidden` for the same reason. This file exists only so the app's plugin loader has a module to
// import instead of 404-ing on every boot.
export default function register() {}

// Minimal shim for the noVNC `core/`-only bundle.
//
// Upstream's `core/input/keyboard.js` reaches into `../../app/ui.js` for one optional read:
//
//     UI.rfb && UI.rfb.translateShortcuts && ...
//
// i.e. "is the full noVNC app driving this connection and does it want shortcut translation".
// We embed only `core/` (the RFB client) in a page of our own, so there is no UI module —
// exporting an object whose `rfb` is null keeps that branch off, which is what we want here
// (raw key events, no shortcut rewriting).
const UI = { rfb: null };

export default UI;

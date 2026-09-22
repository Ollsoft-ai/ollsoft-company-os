// Runs in the <head> of a public link's page, before anything paints.
//
// Out here there is no account and no settings, so the theme cannot be the
// sharer's `ui.theme` — it is the READER's device that gets asked. The app's
// default chassis (deep blue) is already right for a dark screen; a light one
// gets the light theme instead of a wall of navy in daylight.
try {
  if (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) {
    document.documentElement.dataset.theme = "light";
  }
} catch (e) { /* no matchMedia: the default theme is a fine answer */ }

// The terminal's dependencies, in their own chunk. xterm is a quarter of the
// bundle, a viewer account never opens a shell, and nobody needs it to see
// their documents — so app.js imports this lazily (warmTerminal) the first
// time a terminal is wanted, or at t=0 when a restore knows it will want one.
export { Terminal } from "@xterm/xterm";
export { FitAddon } from "@xterm/addon-fit";

// Undo steps back your own edits, never what arrived from elsewhere.
//
// yCollab applies what comes through the live session (the note as it loads,
// a colleague's typing, an agent's write landing in the file) as an ordinary
// CodeMirror change, and history() recorded it like one of yours. So Ctrl+Z
// could take back somebody else's words, and an edit made within half a
// second of the note loading joined the load's undo event: one Ctrl+Z then
// emptied the whole note.
//
// Every such change is dispatched while Yjs calls its observers, so the flag
// is up from "beforeObserverCalls" to "afterTransaction". Your own typing is
// never dispatched in that window: yCollab writes it into Yjs from inside the
// editor's update, and its observer skips its own transactions.
import { EditorState, Transaction } from "@codemirror/state";

export function notUndoableFromYjs(ydoc) {
  let applying = false;
  ydoc.on("beforeObserverCalls", () => { applying = true; });
  ydoc.on("afterTransaction", () => { applying = false; });
  return EditorState.transactionExtender.of((tr) =>
    applying && tr.docChanged ? { annotations: Transaction.addToHistory.of(false) } : null);
}

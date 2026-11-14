import { Extension, Prec, StateEffect, StateField } from "@codemirror/state";
import { Decoration, EditorView, WidgetType, keymap } from "@codemirror/view";
import type { Command } from "@codemirror/view";
import { isCommentLine } from "./iquery";

export interface Ghost {
  pos: number;
  text: string;
  requestId: string;
}

export interface Accepted {
  from: number;
  to: number;
  requestId: string;
}

export interface GhostHandlers {
  onCommentEnter(pos: number): void;
  onAccept(s: Accepted): void;
  onDismiss(): void;
}

export const setGhost = StateEffect.define<Ghost | null>();

/**
 * The suggestion shown in gray at the cursor. Any edit, or a cursor move off its position, clears it,
 * and an answer for a position that is no longer the cursor is ignored (a late answer must not appear).
 */
export const ghostField = StateField.define<Ghost | null>({
  create: () => null,
  update(value, tr) {
    if (value && (tr.docChanged || (tr.selection && tr.state.selection.main.head !== value.pos))) value = null;
    for (const e of tr.effects) {
      if (!e.is(setGhost)) continue;
      const g = e.value;
      const sel = tr.state.selection.main;
      value = g === null || (sel.empty && sel.head === g.pos) ? g : value;
    }
    return value;
  },
  provide: (f) =>
    EditorView.decorations.from(f, (g) =>
      g ? Decoration.set([Decoration.widget({ widget: new GhostWidget(g.text), side: 1 }).range(g.pos)]) : Decoration.none,
    ),
});

class GhostWidget extends WidgetType {
  constructor(readonly text: string) {
    super();
  }
  eq(other: GhostWidget) {
    return other.text === this.text;
  }
  toDOM() {
    const el = document.createElement("span");
    el.className = "ghost-text";
    el.textContent = this.text;
    el.setAttribute("aria-hidden", "true");
    return el;
  }
  ignoreEvent() {
    return true;
  }
}

export const ghostAccept =
  (h: Pick<GhostHandlers, "onAccept">): Command =>
  (view) => {
    const g = view.state.field(ghostField);
    if (!g) return false;
    const insert = g.text + "\n";
    const to = g.pos + insert.length;
    view.dispatch({
      changes: { from: g.pos, insert },
      selection: { anchor: to },
      effects: setGhost.of(null),
      userEvent: "input.accept",
    });
    h.onAccept({ from: g.pos, to, requestId: g.requestId });
    return true;
  };

export const ghostDismiss =
  (h: Pick<GhostHandlers, "onDismiss">): Command =>
  (view) => {
    if (!view.state.field(ghostField)) return false;
    view.dispatch({ effects: setGhost.of(null) });
    h.onDismiss();
    return true;
  };

export function ghostExtension(h: GhostHandlers): Extension {
  return [
    ghostField,
    Prec.highest(keymap.of([{ key: "Tab", run: ghostAccept(h) }, { key: "Escape", run: ghostDismiss(h) }])),
    EditorView.updateListener.of((u) => {
      if (!u.docChanged) return;
      for (const tr of u.transactions) {
        if (!tr.isUserEvent("input") || tr.isUserEvent("input.accept")) continue;
        tr.changes.iterChanges((_fa, _ta, fromB, toB, inserted) => {
          if (!inserted.toString().startsWith("\n")) return;
          const line = u.state.doc.lineAt(fromB);
          if (line.to === fromB && isCommentLine(line.text)) h.onCommentEnter(u.state.selection.main.head);
        });
      }
    }),
    EditorView.baseTheme({
      ".ghost-text": { color: "var(--text-faint)", whiteSpace: "pre-wrap", pointerEvents: "none", opacity: "0.85" },
    }),
  ];
}

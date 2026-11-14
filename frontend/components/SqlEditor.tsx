"use client";

import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { sql } from "@codemirror/lang-sql";
import { defaultHighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { EditorState, type Extension } from "@codemirror/state";
import { EditorView, keymap, lineNumbers } from "@codemirror/view";
import { useEffect, useRef } from "react";

interface Props {
  value: string;
  onChange(value: string): void;
  extensions: Extension[];
  onReady(view: EditorView): void;
}

const theme = EditorView.theme({
  "&": { background: "var(--surface-1)", color: "var(--text)", border: "1px solid var(--line)", borderRadius: "var(--radius-control)", minHeight: "220px" },
  ".cm-content": { fontFamily: "var(--font-code)", fontSize: "var(--size-code)", caretColor: "var(--disk)", padding: "var(--space-3) 0" },
  ".cm-gutters": { background: "transparent", color: "var(--text-faint)", border: "none" },
  "&.cm-focused": { outline: "2px solid var(--disk)", outlineOffset: "1px" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": { background: "var(--disk-tint) !important" },
});

/** A plain SQL editor. `value` seeds it; later edits flow out through `onChange`, and the owner drives it through the view from `onReady`. */
export function SqlEditor({ value, onChange, extensions, onReady }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const change = useRef(onChange);
  change.current = onChange;

  useEffect(() => {
    const view = new EditorView({
      parent: host.current!,
      state: EditorState.create({
        doc: value,
        extensions: [
          lineNumbers(),
          history(),
          sql(),
          syntaxHighlighting(defaultHighlightStyle),
          EditorView.lineWrapping,
          theme,
          ...extensions,
          keymap.of([...defaultKeymap, ...historyKeymap]),
          EditorView.updateListener.of((u) => {
            if (u.docChanged) change.current(u.state.doc.toString());
          }),
        ],
      }),
    });
    onReady(view);
    return () => view.destroy();
    // the editor is created once; the owner changes it through the view
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return <div ref={host} className="sql-editor" />;
}

import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { describe, expect, it, vi } from "vitest";
import { ghostAccept, ghostDismiss, ghostExtension, ghostField, setGhost } from "./ghost";

function make(doc: string, cursor = doc.length) {
  const handlers = { onCommentEnter: vi.fn(), onAccept: vi.fn(), onDismiss: vi.fn() };
  const view = new EditorView({
    state: EditorState.create({ doc, selection: { anchor: cursor }, extensions: [ghostExtension(handlers)] }),
    parent: document.body,
  });
  return { view, handlers };
}
const ghost = (v: EditorView) => v.state.field(ghostField);
const show = (v: EditorView, text = "SELECT 1", pos = v.state.selection.main.head) =>
  v.dispatch({ effects: setGhost.of({ pos, text, requestId: "r1" }) });

describe("ghost", () => {
  it("shows a ghost at its position", () => {
    const { view } = make("-- a\n");
    show(view);
    expect(ghost(view)).toEqual({ pos: 5, text: "SELECT 1", requestId: "r1" });
    expect(view.dom.querySelector(".ghost-text")?.textContent).toBe("SELECT 1");
  });

  it("Tab accepts: inserts text and a newline, reports the range, clears the ghost", () => {
    const { view, handlers } = make("-- a\n");
    show(view);
    expect(ghostAccept(handlers)(view)).toBe(true);
    expect(view.state.doc.toString()).toBe("-- a\nSELECT 1;\n");
    expect(handlers.onAccept).toHaveBeenCalledWith({ from: 5, to: 15, requestId: "r1" });
    expect(ghost(view)).toBeNull();
    expect(view.state.selection.main.head).toBe(15);
  });

  it("Tab does not double a semicolon the suggestion already has", () => {
    const { view, handlers } = make("-- a\n");
    show(view, "SELECT 1;");
    ghostAccept(handlers)(view);
    expect(view.state.doc.toString()).toBe("-- a\nSELECT 1;\n");
  });

  it("Tab and Esc do nothing without a ghost, so Tab can still indent", () => {
    const { view, handlers } = make("x");
    expect(ghostAccept(handlers)(view)).toBe(false);
    expect(ghostDismiss(handlers)(view)).toBe(false);
  });

  it("Esc dismisses", () => {
    const { view, handlers } = make("-- a\n");
    show(view);
    expect(ghostDismiss(handlers)(view)).toBe(true);
    expect(ghost(view)).toBeNull();
    expect(handlers.onDismiss).toHaveBeenCalled();
  });

  it("typing clears it", () => {
    const { view } = make("-- a\n");
    show(view);
    view.dispatch({ changes: { from: 5, insert: "s" }, userEvent: "input.type" });
    expect(ghost(view)).toBeNull();
  });

  it("moving the cursor off its position clears it", () => {
    const { view } = make("-- a\n");
    show(view);
    view.dispatch({ selection: { anchor: 0 } });
    expect(ghost(view)).toBeNull();
  });

  it("drops a late answer for a position that is not the cursor", () => {
    const { view } = make("-- a\nselect 1", 13);
    show(view, "late", 5);
    expect(ghost(view)).toBeNull();
  });

  it("Enter on a comment line fires onCommentEnter with the new cursor, a SQL line does not", () => {
    const { view, handlers } = make("-- a");
    view.dispatch({ changes: { from: 4, insert: "\n" }, selection: { anchor: 5 }, userEvent: "input" });
    expect(handlers.onCommentEnter).toHaveBeenCalledWith(5);
    handlers.onCommentEnter.mockClear();
    view.dispatch({ changes: { from: 5, insert: "select 1" }, userEvent: "input.type" });
    view.dispatch({ changes: { from: 13, insert: "\n" }, selection: { anchor: 14 }, userEvent: "input" });
    expect(handlers.onCommentEnter).not.toHaveBeenCalled();
  });

  it("does not fire for programmatic changes", () => {
    const { view, handlers } = make("-- a");
    view.dispatch({ changes: { from: 4, insert: "\n" }, selection: { anchor: 5 } });
    expect(handlers.onCommentEnter).not.toHaveBeenCalled();
  });
});

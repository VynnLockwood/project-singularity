import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SqlEditor } from "./SqlEditor";

describe("SqlEditor", () => {
  it("mounts, shows the value and hands out the view", () => {
    const onReady = vi.fn();
    const { container } = render(<SqlEditor value={"select 1"} onChange={() => {}} extensions={[]} onReady={onReady} />);
    expect(container.querySelector(".cm-content")?.textContent).toBe("select 1");
    expect(onReady).toHaveBeenCalledTimes(1);
    expect(onReady.mock.calls[0][0].state.doc.toString()).toBe("select 1");
  });

  it("reports edits", () => {
    const onChange = vi.fn();
    let view: import("@codemirror/view").EditorView | undefined;
    render(<SqlEditor value="" onChange={onChange} extensions={[]} onReady={(v) => (view = v)} />);
    view!.dispatch({ changes: { from: 0, insert: "x" } });
    expect(onChange).toHaveBeenCalledWith("x");
  });
});

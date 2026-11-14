import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { EditorView } from "@codemirror/view";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, getSchemas, type Me } from "@/lib/api";
import { learnQuery, recallQuery, runQuery, suggestQuery, type Suggestion } from "@/lib/iquery";
import { IntelligentQueryView } from "./IntelligentQueryView";

vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), getSchemas: vi.fn() }));
vi.mock("@/lib/iquery", async (orig) => ({
  ...(await orig<typeof import("@/lib/iquery")>()),
  recallQuery: vi.fn(), suggestQuery: vi.fn(), learnQuery: vi.fn(), runQuery: vi.fn(),
}));

const me: Me = { id: 1, email: "v@x.com", name: "V", role: "viewer" };
const VALUES = [{ table: "partner", column: "name", value: "Acme Ltd" }];

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}
const answer = (sql: string | null, extra: Partial<Suggestion> = {}): Suggestion =>
  ({ sql, source: "model", tables: ["partner"], values: VALUES, requestId: "r", ...extra });

async function setup(schema = "master") {
  vi.mocked(getSchemas).mockResolvedValue([{ name: "master", tables: 3, comment: null }, { name: "crm", tables: 1, comment: null }]);
  const r = render(<IntelligentQueryView me={me} route={{ view: "query", schema }} navigate={vi.fn()} />);
  await screen.findByRole("combobox", { name: "Database" });
  const view = EditorView.findFromDOM(r.container.querySelector(".cm-editor") as HTMLElement)!;
  return { view, ...r };
}
const type = (v: EditorView, text: string) => {
  const at = v.state.doc.length;
  v.dispatch({ changes: { from: at, insert: text }, selection: { anchor: at + text.length }, userEvent: "input.type" });
};
const enter = (v: EditorView) => {
  const at = v.state.doc.length;
  v.dispatch({ changes: { from: at, insert: "\n" }, selection: { anchor: at + 1 }, userEvent: "input" });
};
const ghostText = () => document.querySelector(".ghost-text")?.textContent ?? null;
const tab = (v: EditorView) => fireEvent.keyDown(v.contentDOM, { key: "Tab", keyCode: 9 });

describe("IntelligentQueryView", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.mocked(recallQuery).mockReset().mockResolvedValue(answer(null, { source: "memory" }));
    vi.mocked(suggestQuery).mockReset().mockResolvedValue(answer(null));
    vi.mocked(learnQuery).mockReset().mockResolvedValue({ id: 1, status: "candidate" });
    vi.mocked(runQuery).mockReset().mockResolvedValue({ columns: ["n"], rows: [[1]], truncated: false });
  });
  afterEach(() => vi.restoreAllMocks());

  it("asks recall and suggest together on comment Enter", async () => {
    const { view } = await setup();
    type(view, "-- contracts of Acme");
    expect(recallQuery).not.toHaveBeenCalled();
    enter(view);
    await waitFor(() => expect(suggestQuery).toHaveBeenCalledTimes(1));
    expect(recallQuery).toHaveBeenCalledTimes(1);
    const a = vi.mocked(recallQuery).mock.calls[0][0];
    const b = vi.mocked(suggestQuery).mock.calls[0][0];
    expect(a).toEqual(b);
    expect(a).toMatchObject({ schema: "master", comments: ["contracts of Acme"], sqlContext: "" });
    expect(a.requestId).toBeTruthy();
    expect(vi.mocked(suggestQuery).mock.calls[0][1]).toBeInstanceOf(AbortSignal);
  });

  it("memory answer then model answer", async () => {
    const rec = deferred<Suggestion>();
    const sug = deferred<Suggestion>();
    vi.mocked(recallQuery).mockReturnValue(rec.promise);
    vi.mocked(suggestQuery).mockReturnValue(sug.promise);
    const { view } = await setup();
    type(view, "-- c");
    enter(view);
    await act(async () => rec.resolve(answer("SELECT 'm'", { source: "memory" })));
    expect(await screen.findByText("From memory")).toBeTruthy();
    expect(ghostText()).toBe("SELECT 'm'");
    await act(async () => sug.resolve(answer("SELECT 'model'")));
    await waitFor(() => expect(ghostText()).toBe("SELECT 'model'"));
    expect(screen.queryByText("From memory")).toBeNull();
  });

  it("model answer does not replace after accept", async () => {
    const rec = deferred<Suggestion>();
    const sug = deferred<Suggestion>();
    vi.mocked(recallQuery).mockReturnValue(rec.promise);
    vi.mocked(suggestQuery).mockReturnValue(sug.promise);
    const { view } = await setup();
    type(view, "-- c");
    enter(view);
    await act(async () => rec.resolve(answer("SELECT 'm'", { source: "memory" })));
    await waitFor(() => expect(ghostText()).toBe("SELECT 'm'"));
    tab(view);
    expect(view.state.doc.toString()).toBe("-- c\nSELECT 'm';\n");
    await act(async () => sug.resolve(answer("SELECT 'model'")));
    expect(ghostText()).toBeNull();
    expect(view.state.doc.toString()).toBe("-- c\nSELECT 'm';\n");
  });

  it("Esc drops the suggestion for good: a later model answer does not bring it back", async () => {
    const rec = deferred<Suggestion>();
    const sug = deferred<Suggestion>();
    vi.mocked(recallQuery).mockReturnValue(rec.promise);
    vi.mocked(suggestQuery).mockReturnValue(sug.promise);
    const { view } = await setup();
    type(view, "-- c");
    enter(view);
    await act(async () => rec.resolve(answer("SELECT 'm'", { source: "memory" })));
    await waitFor(() => expect(ghostText()).toBe("SELECT 'm'"));
    fireEvent.keyDown(view.contentDOM, { key: "Escape", keyCode: 27 });
    expect(ghostText()).toBeNull();
    await act(async () => sug.resolve(answer("SELECT 'model'")));
    expect(ghostText()).toBeNull();
    expect((vi.mocked(suggestQuery).mock.calls[0][1] as AbortSignal).aborted).toBe(true);
  });

  it("a new Enter aborts the old requests", async () => {
    vi.mocked(recallQuery).mockReturnValue(new Promise(() => {}));
    vi.mocked(suggestQuery).mockReturnValue(new Promise(() => {}));
    const { view } = await setup();
    type(view, "-- one");
    enter(view);
    await waitFor(() => expect(suggestQuery).toHaveBeenCalledTimes(1));
    const first = vi.mocked(suggestQuery).mock.calls[0][1] as AbortSignal;
    const firstRecall = vi.mocked(recallQuery).mock.calls[0][1] as AbortSignal;
    type(view, "-- two");
    enter(view);
    await waitFor(() => expect(suggestQuery).toHaveBeenCalledTimes(2));
    expect(first.aborted).toBe(true);
    expect(firstRecall.aborted).toBe(true);
    expect((vi.mocked(suggestQuery).mock.calls[1][1] as AbortSignal).aborted).toBe(false);
  });

  it.each([
    ["rate limit", () => Promise.reject(new ApiError(429, "too many", "rate_limited")), "Slow down a moment"],
    ["no answer", () => Promise.resolve(answer(null, { reason: "nothing fits" })), "No suggestion: nothing fits"],
    ["search offline", () => Promise.resolve(answer("SELECT 1", { searchOffline: true })), "search is offline"],
    ["low confidence", () => Promise.resolve(answer("SELECT 1", { lowConfidence: true })), "Not sure which tables you mean"],
  ])("shows reasons: %s", async (_n, make, text) => {
    vi.mocked(suggestQuery).mockImplementation(make as never);
    const { view } = await setup();
    type(view, "-- c");
    enter(view);
    expect(await screen.findByText(new RegExp(text))).toBeTruthy();
  });

  it("Run uses the statement under the cursor, or the selection, and shows rows", async () => {
    const { view } = await setup();
    view.dispatch({ changes: { from: 0, insert: "select 1;\nselect 2;" }, selection: { anchor: 15 } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(runQuery).toHaveBeenCalledWith("master", "select 2"));
    expect(await screen.findByText("n")).toBeTruthy();
    expect(screen.getByRole("cell", { name: "1" })).toBeTruthy();
    view.dispatch({ selection: { anchor: 0, head: 8 } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(runQuery).toHaveBeenLastCalledWith("master", "select 1"));
  });

  it("shows the error text when a run fails", async () => {
    vi.mocked(runQuery).mockRejectedValue(new ApiError(400, 'relation "x" does not exist'));
    const { view } = await setup();
    view.dispatch({ changes: { from: 0, insert: "select * from x" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText(/relation "x" does not exist/)).toBeTruthy();
  });

  async function accepted() {
    vi.mocked(recallQuery).mockResolvedValue(answer("SELECT 'm'", { source: "memory" }));
    const s = await setup();
    type(s.view, "-- c");
    enter(s.view);
    await waitFor(() => expect(ghostText()).toBe("SELECT 'm'"));
    tab(s.view);
    return s;
  }
  const run = () => fireEvent.click(screen.getByRole("button", { name: "Run" }));

  it("learns after accept and a successful run", async () => {
    const { view } = await accepted();
    view.dispatch({ selection: { anchor: 6 } }); // inside the accepted text
    run();
    await waitFor(() => expect(learnQuery).toHaveBeenCalledTimes(1));
    expect(learnQuery).toHaveBeenCalledWith({ schema: "master", comments: ["c"], sql: "SELECT 'm'", values: VALUES });
  });

  it("learns the SQL as it was run, after an edit inside the range", async () => {
    const { view } = await accepted();
    const at = view.state.doc.toString().indexOf("'m'");
    view.dispatch({ changes: { from: at, to: at + 3, insert: "'Acme'" } });
    view.dispatch({ selection: { anchor: 6 } });
    run();
    await waitFor(() => expect(learnQuery).toHaveBeenCalledTimes(1));
    expect(vi.mocked(learnQuery).mock.calls[0][0].sql).toBe("SELECT 'Acme'");
  });

  it("does not learn from a failed run or a statement outside the range", async () => {
    const { view } = await accepted();
    vi.mocked(runQuery).mockRejectedValueOnce(new ApiError(400, "boom"));
    view.dispatch({ selection: { anchor: 6 } });
    run();
    await screen.findByText(/boom/);
    expect(learnQuery).not.toHaveBeenCalled();
    const end = view.state.doc.length;
    view.dispatch({ changes: { from: 0, insert: "select 9;\n" } }); // an unrelated statement before the range
    view.dispatch({ selection: { anchor: 3 } });
    run();
    await waitFor(() => expect(runQuery).toHaveBeenLastCalledWith("master", "select 9"));
    expect(learnQuery).not.toHaveBeenCalled();
    expect(end).toBeGreaterThan(0);
  });

  it("keeps text per schema in browser storage", async () => {
    window.localStorage.setItem("singularity.iquery.text.master", "select 42");
    const { view } = await setup();
    expect(view.state.doc.toString()).toBe("select 42");
    type(view, " -- x");
    expect(window.localStorage.getItem("singularity.iquery.text.master")).toBe("select 42 -- x");
  });

  it("works when storage throws", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("no storage"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("no storage"); });
    const { view } = await setup();
    expect(() => type(view, "select 1")).not.toThrow();
    expect(view.state.doc.toString()).toBe("select 1");
  });
});

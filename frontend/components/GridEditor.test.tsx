import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { GridColumn, Me, RowsPage } from "@/lib/api";
import { change } from "@/lib/fixtures";

const col = (name: string, type: string, o: Partial<GridColumn> = {}): GridColumn =>
  ({ name, type, nullable: false, readOnly: false, references: null, ...o });

const page = (rows: Record<string, unknown>[], total = rows.length): RowsPage => ({
  table: "orderHeader", total, offset: 0, limit: 50,
  columns: [
    col("id", "bigint", { readOnly: true }), col("note", "text", { nullable: true }), col("qty", "integer"),
    col("customerId", "bigint", { references: { table: "customer", column: "id" } }),
  ],
  rows,
});
const ROWS = [
  { id: 1, note: "first", qty: 1, customerId: 10 },
  { id: 2, note: "second", qty: 2, customerId: 11 },
];
const me = (role: Me["role"]): Me => ({ id: 1, email: "ed@example.com", name: "", role });

async function setup(role: Me["role"] = "editor", data = page(ROWS), onBusy: (b: boolean) => void = vi.fn(), extra: Record<string, unknown> = {}) {
  vi.resetModules();
  const getRows = vi.fn().mockResolvedValue(data);
  const proposeChange = vi.fn().mockResolvedValue(change({ id: 77, kind: "data", status: "pending" }));
  vi.doMock("@/lib/api", async (orig) => ({
    ...(await orig<typeof import("@/lib/api")>()),
    getRows, proposeChange,
    getOptions: vi.fn().mockResolvedValue({
      labelColumn: "name", truncated: false,
      options: [{ id: 10, label: "Acme" }, { id: 11, label: "Beta" }],
    }),
  }));
  const { GridEditor } = await import("./GridEditor");
  const onProposed = vi.fn();
  render(<GridEditor me={me(role)} table="orderHeader" live={[]} onProposed={onProposed} onBusy={onBusy} {...extra} />);
  await screen.findByText(/\d+–\d+ of \d+|^no rows$/);
  return { getRows, proposeChange, onProposed };
}

const type = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });

describe("GridEditor", () => {
  beforeEach(() => vi.resetModules());

  it("shows rows, the range, and reference columns as dropdowns with names", async () => {
    await setup();
    expect(screen.getByText("1–2 of 2")).toBeInTheDocument();
    const fk = () => screen.getByLabelText("customerId 1") as HTMLSelectElement;
    await waitFor(() => expect(within(fk()).getByText("Acme")).toBeInTheDocument(), { timeout: 4000 });
    expect(fk().value).toBe("10");
    expect(within(fk()).getByText("Beta")).toBeInTheDocument();
  });

  it("shows progress on the grid itself and keeps it usable while a page loads", async () => {
    const { getRows } = await setup();
    let finish: (p: RowsPage) => void = () => {};
    getRows.mockReturnValueOnce(new Promise<RowsPage>((r) => { finish = r; }));
    fireEvent.click(screen.getByRole("button", { name: "note" }));
    expect(await screen.findByRole("progressbar", { name: "Loading rows" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add row" })).toBeEnabled();
    finish(page(ROWS));
    await waitFor(() => expect(screen.queryByRole("progressbar")).toBeNull());
  });

  it("reports busy while a page is loading", async () => {
    const busy: boolean[] = [];
    await setup("editor", page(ROWS), (b) => busy.push(b));
    expect(busy).toContain(true);
  });

  it("read-only columns are not editable", async () => {
    await setup();
    expect(screen.queryByLabelText("id 1")).toBeNull();
    expect(screen.getByLabelText("note 1")).toBeInTheDocument();
  });

  it("edits collect as a draft and save as one proposal, grouped", async () => {
    const { proposeChange, onProposed } = await setup();
    type("qty 1", "5");
    type("qty 2", "5");
    type("note 2", "same");
    type("note 1", "same");
    expect(screen.getByTestId("draft-count")).toHaveTextContent("4 pending changes");
    fireEvent.click(screen.getByRole("button", { name: "Save for approval" }));
    await waitFor(() => expect(proposeChange).toHaveBeenCalledTimes(1));
    expect(proposeChange.mock.calls[0][0]).toEqual([
      { op: "update_rows", table: "orderHeader", where: { id: [1, 2] }, set: { qty: 5, note: "same" } },
    ]);
    await waitFor(() => expect(screen.queryByTestId("draft-count")).toBeNull());
    expect(screen.getByText(/Saved as change #77/)).toBeInTheDocument();
    expect(onProposed).toHaveBeenCalled();
  });

  it("a bad value is flagged and blocks saving", async () => {
    await setup();
    type("qty 1", "lots");
    expect(screen.getByLabelText("qty 1")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("button", { name: "Save for approval" })).toBeDisabled();
    type("qty 1", "3");
    expect(screen.getByRole("button", { name: "Save for approval" })).toBeEnabled();
  });

  it("removing a row deactivates it, and can be undone", async () => {
    const { proposeChange } = await setup();
    fireEvent.click(screen.getByRole("button", { name: "remove row 2" }));
    expect(screen.getByRole("button", { name: "restore row 2" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save for approval" }));
    await waitFor(() => expect(proposeChange).toHaveBeenCalled());
    expect(proposeChange.mock.calls[0][0]).toEqual([{ op: "deactivate_rows", table: "orderHeader", where: { id: 2 } }]);
  });

  it("new rows send only what was typed", async () => {
    const { proposeChange } = await setup();
    fireEvent.click(screen.getByRole("button", { name: "Add row" }));
    type("qty new0", "9");
    type("customerId new0", "10");
    fireEvent.click(screen.getByRole("button", { name: "Save for approval" }));
    await waitFor(() => expect(proposeChange).toHaveBeenCalled());
    expect(proposeChange.mock.calls[0][0]).toEqual([
      { op: "insert_rows", table: "orderHeader", rows: [{ qty: 9, customerId: 10 }] },
    ]);
  });

  it("discard throws the draft away without calling the server", async () => {
    const { proposeChange } = await setup();
    type("qty 1", "7");
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.queryByTestId("draft-count")).toBeNull();
    expect((screen.getByLabelText("qty 1") as HTMLInputElement).value).toBe("1");
    expect(proposeChange).not.toHaveBeenCalled();
  });

  it("clicking a header sorts, twice flips the direction; filters are sent to the server", async () => {
    const { getRows } = await setup();
    fireEvent.click(screen.getByRole("button", { name: "qty" }));
    await waitFor(() => expect(getRows).toHaveBeenLastCalledWith("orderHeader", expect.objectContaining({ sort: "qty", dir: "asc" })));
    fireEvent.click(screen.getByRole("button", { name: /qty/ }));
    await waitFor(() => expect(getRows).toHaveBeenLastCalledWith("orderHeader", expect.objectContaining({ sort: "qty", dir: "desc" })));
    fireEvent.change(screen.getByLabelText("Filter column"), { target: { value: "note" } });
    type("Filter text", "sec");
    fireEvent.click(screen.getByRole("button", { name: "Add filter" }));
    await waitFor(() => expect(getRows).toHaveBeenLastCalledWith("orderHeader", expect.objectContaining({ filters: { note: "sec" } })), { timeout: 2000 });
  });

  it("viewers get a read-only grid", async () => {
    await setup("viewer");
    expect(screen.queryByLabelText("qty 1")).toBeNull();
    expect(screen.getByText("first")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add row" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Save for approval" })).toBeNull();
  });

  it("shows a foreign key's label, not its raw id, and can toggle back", async () => {
    await setup("viewer", page(ROWS));
    // viewer => read-only cells, which is exactly where a raw id was being shown
    await waitFor(() => expect(screen.getAllByText("Acme").length).toBeGreaterThan(0));
    fireEvent.click(screen.getByRole("checkbox", { name: /show labels/i }));
    await waitFor(() => expect(screen.getByText("10")).toBeInTheDocument());
    expect(screen.queryByText("Acme")).toBeNull();
  });

  it("the labels toggle also drives editable FK cells", async () => {
    await setup("editor", page(ROWS));
    // editor => FK renders as <select>; its displayed text must obey "show labels" too
    const fk = () => screen.getByLabelText("customerId 1") as HTMLSelectElement;
    await waitFor(() => expect(fk().tagName).toBe("SELECT"), { timeout: 4000 }); // options load async
    await waitFor(() => expect(fk().options[fk().selectedIndex].textContent).toBe("Acme"));
    expect(fk().value).toBe("10"); // option VALUES stay ids, so editing still saves the FK
    fireEvent.click(screen.getByRole("checkbox", { name: /show labels/i }));
    expect(fk().options[fk().selectedIndex].textContent).toBe("10");
    expect(fk().value).toBe("10");
  });

  it("pages forward", async () => {
    const { getRows } = await setup("editor", page(ROWS, 120));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(getRows).toHaveBeenLastCalledWith("orderHeader", expect.objectContaining({ offset: 50 })));
  });

  it("fetches with the chosen page size", async () => {
    const { getRows } = await setup("viewer", page(ROWS));
    fireEvent.change(screen.getByLabelText("Rows per page"), { target: { value: "100" } });
    await waitFor(() => expect(getRows).toHaveBeenCalledWith(
      expect.anything(), expect.objectContaining({ limit: 100 })));
  });

  it("explains a gate rejection instead of pretending it worked", async () => {
    const { proposeChange } = await setup();
    proposeChange.mockResolvedValueOnce(change({ id: 80, kind: "data", status: "rejected" }));
    type("qty 1", "4");
    fireEvent.click(screen.getByRole("button", { name: "Save for approval" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("blocked this change"));
    expect(screen.getByTestId("draft-count")).toHaveTextContent("1 pending change"); // draft kept for fixing
  });

  it("has a filter bar and no per-column filter row", async () => {
    await setup();
    expect(screen.getByRole("button", { name: "Add filter" })).toBeInTheDocument();
    expect(screen.queryByLabelText("filter note")).toBeNull();
  });

  it("controlled view props reach getRows and view changes go out through onViewChange", async () => {
    const onViewChange = vi.fn();
    const view = { sort: "note", dir: "desc" as const, filters: { note: "x" }, inactive: true };
    const { getRows } = await setup("editor", page(ROWS), vi.fn(), { view, onViewChange, schema: "other" });
    expect(getRows).toHaveBeenLastCalledWith("orderHeader", expect.objectContaining({ sort: "note", dir: "desc", filters: { note: "x" }, inactive: true }), "other");
    expect(screen.getByRole("columnheader", { name: /note/ })).toHaveAttribute("aria-sort", "descending");
    fireEvent.click(screen.getByRole("button", { name: /^note/ }));
    expect(onViewChange).toHaveBeenLastCalledWith({ ...view, sort: null, dir: "asc" });
  });

  it("the third header click clears the sort", async () => {
    const { getRows } = await setup();
    const head = () => screen.getByRole("button", { name: /^qty/ });
    fireEvent.click(head()); fireEvent.click(head()); fireEvent.click(head());
    await waitFor(() => expect(getRows).toHaveBeenLastCalledWith("orderHeader", expect.objectContaining({ sort: undefined })));
    expect(screen.getByRole("columnheader", { name: /qty/ })).toHaveAttribute("aria-sort", "none");
  });

  it("a filter change keeps pending edits and goes back to page 1", async () => {
    const { getRows } = await setup("editor", page(ROWS, 120));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(getRows).toHaveBeenLastCalledWith("orderHeader", expect.objectContaining({ offset: 50 })));
    type("qty 1", "9");
    type("Filter text", "sec");
    fireEvent.click(screen.getByRole("button", { name: "Add filter" }));
    await waitFor(() => expect(getRows).toHaveBeenLastCalledWith("orderHeader", expect.objectContaining({ offset: 0, filters: { id: "sec" } })));
    expect(screen.getByTestId("draft-count")).toHaveTextContent("1 pending change");
  });

  it("an old reply never overwrites a newer one", async () => {
    let first!: (p: RowsPage) => void;
    const { getRows } = await setup();
    getRows.mockImplementationOnce(() => new Promise<RowsPage>((r) => { first = r; }));
    getRows.mockResolvedValueOnce(page([{ id: 9, note: "newest", qty: 1, customerId: 10 }]));
    fireEvent.click(screen.getByRole("button", { name: "qty" }));
    fireEvent.click(screen.getByRole("button", { name: /^qty/ }));
    await screen.findByDisplayValue("newest");
    first(page([{ id: 8, note: "stale", qty: 1, customerId: 10 }]));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByDisplayValue("stale")).toBeNull();
  });

  it("the save bar exists only with pending edits and counts cells that need fixing", async () => {
    await setup();
    expect(screen.queryByRole("button", { name: "Save for approval" })).toBeNull();
    type("qty 1", "lots");
    expect(screen.getByTestId("draft-count")).toHaveTextContent("1 pending change");
    expect(screen.getByTestId("draft-count")).toHaveTextContent("1 cell needs fixing");
    expect(screen.getByRole("button", { name: "Discard" })).toBeInTheDocument();
  });

  it("marks edited, removed, new and invalid cells", async () => {
    await setup();
    type("qty 1", "lots");
    expect(screen.getByLabelText("qty 1").closest("td")).toHaveAttribute("data-edited", "true");
    expect(screen.getByText(/Enter a whole number|not a number|whole number/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "remove row 2" }));
    expect(screen.getByRole("button", { name: "restore row 2" }).closest("tr")).toHaveAttribute("data-removed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Add row" }));
    expect(screen.getByText("new")).toBeInTheDocument();
  });

  it("empty table and empty result are told apart", async () => {
    await setup("editor", page([], 0));
    expect(screen.getByText("No rows yet.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Filter text"), { target: { value: "zz" } });
    fireEvent.click(screen.getByRole("button", { name: "Add filter" }));
    expect(await screen.findByText("No rows match these filters.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByText("No rows yet.")).toBeInTheDocument();
  });

  it("a load error shows its message and Try again reloads", async () => {
    const { getRows } = await setup();
    getRows.mockRejectedValueOnce(new Error("x"));
    fireEvent.click(screen.getByRole("button", { name: "qty" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load rows.");
    getRows.mockResolvedValueOnce(page(ROWS));
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });

  it("Export CSV is hidden from a viewer", async () => {
    await setup("viewer");
    expect(screen.queryByRole("button", { name: "Export CSV" })).toBeNull();
  });

  it("Export CSV downloads the whole view; a server refusal is shown and nothing downloads", async () => {
    const view = { sort: "note", dir: "asc" as const, filters: { note: "x" }, inactive: false };
    await setup("editor", page(ROWS), vi.fn(), { view, onViewChange: vi.fn() });
    const create = vi.fn(() => "blob:x");
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: create, revokeObjectURL: vi.fn() }));
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: "5 rows match; the most that can be exported is 3 (setting limits.maxExportRows). Narrow the filters." }), { status: 400 }))
      .mockResolvedValueOnce(new Response("\ufeffa\r\n", { status: 200, headers: { "Content-Disposition": 'attachment; filename="master-t-2026-11-11.csv"' } }));
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    fireEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("the most that can be exported is 3");
    expect(create).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls[0][0]).toContain("/export.csv?schema=master&sort=note&dir=asc&filters=");
    fireEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
  });
});

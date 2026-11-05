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

async function setup(role: Me["role"] = "editor", data = page(ROWS)) {
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
  render(<GridEditor me={me(role)} table="orderHeader" live={[]} onProposed={onProposed} />);
  await screen.findByText(/\d+–\d+ of \d+/);
  return { getRows, proposeChange, onProposed };
}

const type = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });

describe("GridEditor", () => {
  beforeEach(() => vi.resetModules());

  it("shows rows, the range, and reference columns as dropdowns with names", async () => {
    await setup();
    expect(screen.getByText("1–2 of 2")).toBeInTheDocument();
    const fk = screen.getByLabelText("customerId 1") as HTMLSelectElement;
    await waitFor(() => expect(within(fk).getByText("Acme")).toBeInTheDocument());
    expect(fk.value).toBe("10");
    expect(within(fk).getByText("Beta")).toBeInTheDocument();
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
    await waitFor(() => expect(screen.getByTestId("draft-count")).toHaveTextContent("No pending edits"));
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
    expect(screen.getByTestId("draft-count")).toHaveTextContent("No pending edits");
    expect((screen.getByLabelText("qty 1") as HTMLInputElement).value).toBe("1");
    expect(proposeChange).not.toHaveBeenCalled();
  });

  it("clicking a header sorts, twice flips the direction; filters are sent to the server", async () => {
    const { getRows } = await setup();
    fireEvent.click(screen.getByRole("button", { name: "qty" }));
    await waitFor(() => expect(getRows).toHaveBeenLastCalledWith("orderHeader", expect.objectContaining({ sort: "qty", dir: "asc" })));
    fireEvent.click(screen.getByRole("button", { name: /qty/ }));
    await waitFor(() => expect(getRows).toHaveBeenLastCalledWith("orderHeader", expect.objectContaining({ sort: "qty", dir: "desc" })));
    type("filter note", "sec");
    await waitFor(() => expect(getRows).toHaveBeenLastCalledWith("orderHeader", expect.objectContaining({ filters: { note: "sec" } })), { timeout: 2000 });
  });

  it("viewers get a read-only grid", async () => {
    await setup("viewer");
    expect(screen.queryByLabelText("qty 1")).toBeNull();
    expect(screen.getByText("first")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add row" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Save for approval" })).toBeNull();
  });

  it("pages forward", async () => {
    const { getRows } = await setup("editor", page(ROWS, 120));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(getRows).toHaveBeenLastCalledWith("orderHeader", expect.objectContaining({ offset: 50 })));
  });

  it("explains a gate rejection instead of pretending it worked", async () => {
    const { proposeChange } = await setup();
    proposeChange.mockResolvedValueOnce(change({ id: 80, kind: "data", status: "rejected" }));
    type("qty 1", "4");
    fireEvent.click(screen.getByRole("button", { name: "Save for approval" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("blocked this change"));
    expect(screen.getByTestId("draft-count")).toHaveTextContent("1 pending change"); // draft kept for fixing
  });
});

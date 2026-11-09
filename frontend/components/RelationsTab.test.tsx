import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Me, SchemaGraph, TableDetail } from "@/lib/api";

const editor: Me = { id: 1, email: "e@x.io", name: "", role: "editor" };
const viewer: Me = { ...editor, role: "viewer" };
const aChange = { id: 9, status: "pending", kind: "schema", source: "web", requester: "e@x.io", prompt: "t", createdAt: "", decidedBy: null, error: "", decisionNote: "", tables: [], diff: ["+ x"], check: {} };
const c = (name: string, type = "text") => ({ name, type, nullable: true, default: null, comment: null, identity: false });
const detail: TableDetail = {
  name: "order", comment: null, hiddenColumns: ["created_at"],
  columns: [c("id", "integer"), c("customer_id", "integer"), c("note"), c("created_at", "timestamptz")],
  constraints: [
    { name: "order_pkey", kind: "primary_key", definition: "PRIMARY KEY (id)" },
    { name: "order_note_key", kind: "unique", definition: "UNIQUE (note)" },
    { name: "order_customer_fk", kind: "foreign_key", definition: "FOREIGN KEY (customer_id) REFERENCES customer(id)" },
  ],
  indexes: [
    { name: "order_pkey", definition: "CREATE UNIQUE INDEX order_pkey ON master.order (id)" },
    { name: "order_note_idx", definition: "CREATE INDEX order_note_idx ON master.order (note)" },
  ],
};
const gc = (name: string) => ({ name, type: "integer", nullable: false, primaryKey: false });
const graph: SchemaGraph = {
  tables: [
    { name: "customer", comment: null, rowEstimate: 0, columns: [gc("id"), gc("code")] },
    { name: "order", comment: null, rowEstimate: 0, columns: [gc("id")] },
    { name: "line", comment: null, rowEstimate: 0, columns: [gc("id")] },
  ],
  relations: [
    { from: { table: "order", column: "customer_id" }, to: { table: "customer", column: "id" } },
    { from: { table: "line", column: "order_id" }, to: { table: "order", column: "id" } },
  ],
};

async function setup(opts: { me?: Me; getTable?: ReturnType<typeof vi.fn>; graph?: SchemaGraph | null; proposeChange?: ReturnType<typeof vi.fn> } = {}) {
  vi.resetModules();
  const mocks = {
    getTable: opts.getTable ?? vi.fn().mockResolvedValue(detail),
    proposeChange: opts.proposeChange ?? vi.fn().mockResolvedValue(aChange),
  };
  vi.doMock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), ...mocks }));
  const { RelationsTab } = await import("./RelationsTab");
  const onProposed = vi.fn();
  const el = (o: { schema?: string; table?: string; refreshKey?: number }) => (
    <RelationsTab me={opts.me ?? editor} schema={o.schema ?? "master"} table={o.table ?? "order"}
      graph={opts.graph === undefined ? graph : opts.graph} refreshKey={o.refreshKey ?? 0} onProposed={onProposed} />
  );
  const r = render(el({}));
  return { mocks, onProposed, rerender: (o: { schema?: string; table?: string; refreshKey?: number }) => r.rerender(el(o)) };
}
const propose = async (mocks: { proposeChange: ReturnType<typeof vi.fn> }) => {
  fireEvent.click(screen.getByRole("button", { name: "Propose" }));
  await waitFor(() => expect(mocks.proposeChange).toHaveBeenCalled());
  return mocks.proposeChange.mock.calls[0];
};
const review = () => fireEvent.click(screen.getByRole("button", { name: "Review" }));

describe("RelationsTab", () => {
  beforeEach(() => vi.resetModules());

  it("lists both directions, constraints and indexes", async () => {
    await setup();
    expect(await screen.findByText("customer_id → customer.id")).toBeInTheDocument();
    expect(screen.getByText("line.order_id → id")).toBeInTheDocument();
    expect(screen.getByText("order_note_key")).toBeInTheDocument();
    expect(screen.getByText("primary key")).toBeInTheDocument();
    expect(screen.getByText("order_note_idx")).toBeInTheDocument();
  });

  it("shows empty-state text", async () => {
    await setup({ getTable: vi.fn().mockResolvedValue({ ...detail, constraints: [], indexes: [] }), graph: { tables: graph.tables, relations: [] } });
    expect(await screen.findByText("No relations, constraints or indexes.")).toBeInTheDocument();
    expect(screen.getByText("No constraints.")).toBeInTheDocument();
    expect(screen.getByText("No indexes.")).toBeInTheDocument();
  });

  it("shows an error when the table fails to load", async () => {
    await setup({ getTable: vi.fn().mockRejectedValue(new Error("x")) });
    expect(await screen.findByText("Could not load the relations.")).toBeInTheDocument();
  });

  it("viewers see no write buttons", async () => {
    await setup({ me: viewer });
    await screen.findByText("order_note_key");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("sends add_index with columns and unique, hiding audit columns", async () => {
    const { mocks, onProposed } = await setup();
    fireEvent.click(await screen.findByRole("button", { name: "Add index" }));
    expect(screen.queryByLabelText("created_at")).toBeNull();
    expect(screen.getByRole("button", { name: "Review" })).toBeDisabled();
    fireEvent.click(screen.getByLabelText("customer_id"));
    fireEvent.click(screen.getByLabelText("note"));
    fireEvent.click(screen.getByLabelText("Unique"));
    review();
    const call = await propose(mocks);
    expect(call[0]).toEqual([{ op: "add_index", table: "order", columns: ["customer_id", "note"], unique: true }]);
    expect(call[3]).toBe("master");
    await waitFor(() => expect(onProposed).toHaveBeenCalledWith(aChange));
  });

  it("sends add_index without unique when unchecked", async () => {
    const { mocks } = await setup();
    fireEvent.click(await screen.findByRole("button", { name: "Add index" }));
    fireEvent.click(screen.getByLabelText("note"));
    review();
    expect((await propose(mocks))[0]).toEqual([{ op: "add_index", table: "order", columns: ["note"] }]);
  });

  it("sends add_unique", async () => {
    const { mocks } = await setup();
    fireEvent.click(await screen.findByRole("button", { name: "Add unique" }));
    fireEvent.click(screen.getByLabelText("note"));
    review();
    expect((await propose(mocks))[0]).toEqual([{ op: "add_unique", table: "order", columns: ["note"] }]);
  });

  it("sends add_foreign_key with only the required fields by default", async () => {
    const { mocks } = await setup();
    fireEvent.click(await screen.findByRole("button", { name: "Add foreign key" }));
    expect(screen.getByRole("button", { name: "Review" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Column"), { target: { value: "customer_id" } });
    fireEvent.change(screen.getByLabelText("Reference table"), { target: { value: "customer" } });
    review();
    expect((await propose(mocks))[0]).toEqual([{ op: "add_foreign_key", table: "order", column: "customer_id", ref_table: "customer" }]);
  });

  it("sends reference column and on delete; offers only the four actions and same-graph tables", async () => {
    const { mocks } = await setup();
    fireEvent.click(await screen.findByRole("button", { name: "Add foreign key" }));
    const del = screen.getByLabelText("On delete");
    expect(within(del).getAllByRole("option").map((o) => o.textContent)).toEqual(["Default", "restrict", "cascade", "set null", "no action"]);
    expect(within(screen.getByLabelText("Reference table")).getAllByRole("option").map((o) => o.textContent)).toEqual(["Choose a table", "customer", "order", "line"]);
    fireEvent.change(screen.getByLabelText("Column"), { target: { value: "customer_id" } });
    fireEvent.change(screen.getByLabelText("Reference table"), { target: { value: "customer" } });
    expect(within(screen.getByLabelText("Reference column")).getAllByRole("option").map((o) => o.textContent)).toEqual(["Primary key", "id", "code"]);
    fireEvent.change(screen.getByLabelText("Reference column"), { target: { value: "code" } });
    fireEvent.change(del, { target: { value: "set null" } });
    review();
    expect((await propose(mocks))[0]).toEqual([
      { op: "add_foreign_key", table: "order", column: "customer_id", ref_table: "customer", ref_column: "code", on_delete: "set null" },
    ]);
  });

  it("drops a constraint only with a reason; primary key has no Drop", async () => {
    const { mocks } = await setup();
    await screen.findByText("order_note_key");
    expect(screen.queryByRole("button", { name: "Drop constraint order_pkey" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Drop constraint order_note_key" }));
    expect(screen.getByRole("button", { name: "Propose" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: " no " } });
    expect(screen.getByRole("button", { name: "Propose" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "  not needed " } });
    expect((await propose(mocks))[0]).toEqual([{ op: "drop_constraint", table: "order", name: "order_note_key", reason: "not needed" }]);
  });

  it("drops an index with a reason; constraint-backed indexes have no Drop", async () => {
    const { mocks } = await setup();
    await screen.findByText("order_note_idx");
    expect(screen.queryByRole("button", { name: "Drop index order_pkey" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Drop index order_note_idx" }));
    expect(screen.getByRole("button", { name: "Propose" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "unused" } });
    expect((await propose(mocks))[0]).toEqual([{ op: "drop_index", name: "order_note_idx", reason: "unused" }]);
  });

  it("keeps the dialog and reason when the proposal is refused", async () => {
    const proposeChange = vi.fn();
    const { mocks } = await setup({ proposeChange });
    const { ApiError } = await import("@/lib/api");
    proposeChange.mockRejectedValue(new ApiError(422, "Nope."));
    fireEvent.click(await screen.findByRole("button", { name: "Drop index order_note_idx" }));
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "unused" } });
    await propose(mocks);
    expect(await screen.findByText("Nope.")).toBeInTheDocument();
    expect(screen.getByLabelText("Reason")).toHaveValue("unused");
  });

  it("latches the review: a refetch does not change the proposed operation", async () => {
    const getTable = vi.fn().mockResolvedValue(detail);
    const { mocks, rerender } = await setup({ getTable });
    fireEvent.click(await screen.findByRole("button", { name: "Add unique" }));
    fireEvent.click(screen.getByLabelText("note"));
    review();
    rerender({ refreshKey: 1 });
    await waitFor(() => expect(getTable).toHaveBeenCalledTimes(2));
    expect((await propose(mocks))[0]).toEqual([{ op: "add_unique", table: "order", columns: ["note"] }]);
  });

  it("ignores a stale response after switching table and closes open forms", async () => {
    let resolveOld: (d: TableDetail) => void = () => {};
    const getTable = vi.fn()
      .mockImplementationOnce(() => new Promise<TableDetail>((r) => { resolveOld = r; }))
      .mockResolvedValueOnce({ ...detail, name: "line", constraints: [{ name: "line_x", kind: "check", definition: "CHECK (true)" }], indexes: [] });
    const { rerender } = await setup({ getTable });
    rerender({ table: "line" });
    expect(await screen.findByText("line_x")).toBeInTheDocument();
    resolveOld(detail);
    await Promise.resolve();
    expect(screen.queryByText("order_note_key")).toBeNull();
    expect(getTable).toHaveBeenNthCalledWith(2, "line", "master");
  });

  it("a refused Add index keeps the form selection; Close returns to it", async () => {
    const refused = { ...aChange, status: "rejected", diff: [], check: { errors: ["index not allowed"] } };
    const { mocks, onProposed } = await setup({ proposeChange: vi.fn().mockResolvedValue(refused) });
    await screen.findByText("order_note_idx");
    fireEvent.click(screen.getByRole("button", { name: "Add index" }));
    fireEvent.click(screen.getByLabelText("note"));
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    expect(await screen.findByText("index not allowed")).toBeInTheDocument();
    expect(mocks.proposeChange).toHaveBeenCalledTimes(1);
    expect(onProposed).toHaveBeenCalledWith(refused);
    expect(screen.queryByRole("status")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByLabelText("note")).toBeChecked();
  });
});

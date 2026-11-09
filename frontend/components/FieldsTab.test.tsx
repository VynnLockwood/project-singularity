import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Me, TableDetail } from "@/lib/api";

const editor: Me = { id: 1, email: "e@x.io", name: "", role: "editor" };
const viewer: Me = { ...editor, role: "viewer" };
const aChange = { id: 9, status: "pending", kind: "schema", source: "web", requester: "e@x.io", prompt: "t", createdAt: "", decidedBy: null, error: "", decisionNote: "", tables: [], diff: ["+ x"], check: {} };
const c = (name: string, type = "text") => ({ name, type, nullable: true, default: null, comment: null, identity: false });
const detail: TableDetail = {
  name: "customer", comment: null, constraints: [], indexes: [],
  columns: [c("id", "integer"), c("email"), c("created_at", "timestamptz")],
  hiddenColumns: ["created_at"],
};

async function setup(opts: { me?: Me; getTable?: ReturnType<typeof vi.fn>; schema?: string; table?: string; refreshKey?: number } = {}) {
  vi.resetModules();
  const mocks = {
    getTable: opts.getTable ?? vi.fn().mockResolvedValue(detail),
    proposeChange: vi.fn().mockResolvedValue(aChange),
  };
  vi.doMock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), ...mocks }));
  const { FieldsTab } = await import("./FieldsTab");
  const onProposed = vi.fn();
  const el = (o: { schema?: string; table?: string; refreshKey?: number }) => (
    <FieldsTab me={opts.me ?? editor} schema={o.schema ?? "master"} table={o.table ?? "customer"} refreshKey={o.refreshKey ?? 0} onProposed={onProposed} />
  );
  const r = render(el(opts));
  return { mocks, onProposed, rerender: (o: { schema?: string; table?: string; refreshKey?: number }) => r.rerender(el(o)) };
}
const propose = async (mocks: { proposeChange: ReturnType<typeof vi.fn> }) => {
  fireEvent.click(screen.getByRole("button", { name: "Propose" }));
  await waitFor(() => expect(mocks.proposeChange).toHaveBeenCalled());
  return mocks.proposeChange.mock.calls[0];
};

describe("FieldsTab", () => {
  beforeEach(() => { vi.resetModules(); window.localStorage.clear(); });

  it("hides audit columns by default, shows them read-only when toggled and persists the choice", async () => {
    await setup();
    expect(await screen.findByText("email")).toBeInTheDocument();
    expect(screen.queryByText("created_at")).toBeNull();
    fireEvent.click(screen.getByLabelText("Show audit columns"));
    expect(screen.getByText("created_at")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Rename created_at" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Drop created_at" })).toBeNull();
    expect(screen.getByRole("button", { name: "Rename email" })).toBeInTheDocument();
    expect(window.localStorage.getItem("db.showAudit")).toBe("1");
  });

  it("restores the persisted toggle", async () => {
    window.localStorage.setItem("db.showAudit", "1");
    await setup();
    expect(await screen.findByText("created_at")).toBeInTheDocument();
  });

  it("survives a throwing localStorage", async () => {
    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("no"); });
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("no"); });
    await setup();
    await screen.findByText("email");
    fireEvent.click(screen.getByLabelText("Show audit columns"));
    expect(screen.getByText("created_at")).toBeInTheDocument();
    get.mockRestore(); set.mockRestore();
  });

  it("Add field sends a minimal add_column on the selected schema", async () => {
    const { mocks, onProposed } = await setup({ schema: "crm" });
    await screen.findByText("email");
    fireEvent.click(screen.getByRole("button", { name: "Add field" }));
    fireEvent.change(screen.getByLabelText("Field name"), { target: { value: "phone" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    const call = await propose(mocks);
    expect(call[0]).toEqual([{ op: "add_column", table: "customer", column: { name: "phone", type: "text" } }]);
    expect(call[3]).toBe("crm");
    await waitFor(() => expect(onProposed).toHaveBeenCalled());
  });

  it("Add field includes nullable, unique, default and comment when set", async () => {
    const { mocks } = await setup();
    await screen.findByText("email");
    fireEvent.click(screen.getByRole("button", { name: "Add field" }));
    fireEvent.change(screen.getByLabelText("Field name"), { target: { value: "age" } });
    fireEvent.change(screen.getByLabelText("Field type"), { target: { value: "integer" } });
    fireEvent.change(screen.getByLabelText("Nullable"), { target: { value: "no" } });
    fireEvent.click(screen.getByLabelText("Unique"));
    fireEvent.change(screen.getByLabelText("Default"), { target: { value: "0" } });
    fireEvent.change(screen.getByLabelText("Field comment"), { target: { value: "years" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    const call = await propose(mocks);
    expect(call[0]).toEqual([{ op: "add_column", table: "customer", column: { name: "age", type: "integer", nullable: false, unique: true, default: 0, comment: "years" } }]);
  });

  it.each([["Phone"], ["email"], ["a b"]])("blocks Add field with invalid or duplicate name %s", async (name) => {
    await setup();
    await screen.findByText("email");
    fireEvent.click(screen.getByRole("button", { name: "Add field" }));
    fireEvent.change(screen.getByLabelText("Field name"), { target: { value: name } });
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review" })).toBeDisabled();
  });

  it("Add field with a template type needs the size filled in, then sends it", async () => {
    const { mocks } = await setup();
    await screen.findByText("email");
    fireEvent.click(screen.getByRole("button", { name: "Add field" }));
    fireEvent.change(screen.getByLabelText("Field name"), { target: { value: "amount" } });
    fireEvent.change(screen.getByLabelText("Field type"), { target: { value: "varchar(n)" } });
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Exact type"), { target: { value: "varchar(40)" } });
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    const call = await propose(mocks);
    expect(call[0]).toEqual([{ op: "add_column", table: "customer", column: { name: "amount", type: "varchar(40)" } }]);
  });

  it("a refused Add field keeps the typed input and Close returns to the form", async () => {
    const refused = { id: 3, status: "rejected", kind: "schema", source: "web", requester: "e", prompt: "", createdAt: "", decidedBy: null, error: "", decisionNote: "", tables: [], diff: [], check: { violations: [{ severity: "block", message: "name too vague" }] } };
    const { mocks, onProposed } = await setup();
    mocks.proposeChange.mockResolvedValue(refused);
    await screen.findByText("email");
    fireEvent.click(screen.getByRole("button", { name: "Add field" }));
    fireEvent.change(screen.getByLabelText("Field name"), { target: { value: "amount" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    expect(await screen.findByText("name too vague")).toBeInTheDocument();
    expect(onProposed).toHaveBeenCalledWith(refused);
    expect(screen.queryByRole("status")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByLabelText("Field name")).toHaveValue("amount");
  });

  it("Drop requires a reason and sends drop_column", async () => {
    const { mocks } = await setup();
    await screen.findByText("email");
    fireEvent.click(screen.getByRole("button", { name: "Drop email" }));
    expect(screen.getByRole("button", { name: "Propose" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "unused" } });
    const call = await propose(mocks);
    expect(call[0]).toEqual([{ op: "drop_column", table: "customer", column: "email", reason: "unused" }]);
  });

  it("Rename sends rename_column and refuses an existing name", async () => {
    const { mocks } = await setup();
    await screen.findByText("email");
    fireEvent.click(screen.getByRole("button", { name: "Rename email" }));
    fireEvent.change(screen.getByLabelText("New name"), { target: { value: "id" } });
    expect(screen.getByRole("button", { name: "Review" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("New name"), { target: { value: "mail" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    const call = await propose(mocks);
    expect(call[0]).toEqual([{ op: "rename_column", table: "customer", column: "email", new_name: "mail" }]);
  });

  it("Type sends alter_column_type and refuses an unsupported type", async () => {
    const { mocks } = await setup();
    await screen.findByText("email");
    fireEvent.click(screen.getByRole("button", { name: "Type id" }));
    fireEvent.change(screen.getByLabelText("New type"), { target: { value: "bigint" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    const call = await propose(mocks);
    expect(call[0]).toEqual([{ op: "alter_column_type", table: "customer", column: "id", type: "bigint" }]);
  });

  it("Type form does not flag a server-formatted type and needs a different type", async () => {
    const d = { ...detail, columns: [c("id", "integer"), c("name", "character varying(40)")] };
    const { mocks } = await setup({ getTable: vi.fn().mockResolvedValue(d) });
    await screen.findByText("name");
    fireEvent.click(screen.getByRole("button", { name: "Type name" }));
    expect(screen.queryByText(/Unsupported type/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("button", { name: "Review" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("New type"), { target: { value: "text" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    const call = await propose(mocks);
    expect(call[0]).toEqual([{ op: "alter_column_type", table: "customer", column: "name", type: "text" }]);
  });

  it("Type form keeps Review disabled while the current type is unchanged", async () => {
    await setup();
    await screen.findByText("email");
    fireEvent.click(screen.getByRole("button", { name: "Type id" }));
    expect(screen.getByRole("button", { name: "Review" })).toBeDisabled();
  });

  it("an empty comment is allowed and clears the comment", async () => {
    const d = { ...detail, columns: [{ ...c("email"), comment: "login" }], hiddenColumns: [] };
    const { mocks } = await setup({ getTable: vi.fn().mockResolvedValue(d) });
    await screen.findByText("email");
    fireEvent.click(screen.getByRole("button", { name: "Comment email" }));
    fireEvent.change(screen.getByLabelText("Comment"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    const call = await propose(mocks);
    expect(call[0]).toEqual([{ op: "set_column_comment", table: "customer", column: "email", comment: "" }]);
  });

  it("Comment sends set_column_comment", async () => {
    const { mocks } = await setup();
    await screen.findByText("email");
    fireEvent.click(screen.getByRole("button", { name: "Comment email" }));
    fireEvent.change(screen.getByLabelText("Comment"), { target: { value: "login" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    const call = await propose(mocks);
    expect(call[0]).toEqual([{ op: "set_column_comment", table: "customer", column: "email", comment: "login" }]);
  });

  it("viewers see the table but no buttons", async () => {
    await setup({ me: viewer });
    expect(await screen.findByText("email")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("refetches on refreshKey and when the table or schema changes", async () => {
    const { mocks, rerender } = await setup();
    await screen.findByText("email");
    rerender({ refreshKey: 1 });
    await waitFor(() => expect(mocks.getTable).toHaveBeenCalledTimes(2));
    rerender({ refreshKey: 1, table: "orders" });
    await waitFor(() => expect(mocks.getTable).toHaveBeenLastCalledWith("orders", "master"));
    rerender({ refreshKey: 1, table: "orders", schema: "crm" });
    await waitFor(() => expect(mocks.getTable).toHaveBeenLastCalledWith("orders", "crm"));
  });

  it("ignores a stale response after switching table", async () => {
    let release!: (d: TableDetail) => void;
    const slow = new Promise<TableDetail>((r) => { release = r; });
    const other: TableDetail = { ...detail, name: "orders", columns: [c("total")], hiddenColumns: [] };
    const getTable = vi.fn((n: string) => (n === "customer" ? slow : Promise.resolve(other)));
    const { rerender } = await setup({ getTable });
    rerender({ table: "orders" });
    expect(await screen.findByText("total")).toBeInTheDocument();
    release(detail);
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByText("email")).toBeNull();
    expect(screen.getByText("total")).toBeInTheDocument();
  });

  it("shows a load error and an empty state", async () => {
    await setup({ getTable: vi.fn().mockRejectedValue(new Error("x")) });
    expect(await screen.findByText("Could not load the fields.")).toBeInTheDocument();
  });

  it("empty columns say so and viewers still get no buttons", async () => {
    await setup({ me: viewer, getTable: vi.fn().mockResolvedValue({ ...detail, columns: [], hiddenColumns: [] }) });
    expect(await screen.findByText("No fields.")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });
});

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Me, SchemaGraph } from "@/lib/api";

const col = (name: string) => ({ name, type: "text", nullable: true, primaryKey: false, comment: null, default: null });
const tbl = (name: string) => ({ name, comment: null, rowEstimate: 1, columns: [col("id")] });
const master: SchemaGraph = { tables: [tbl("customer"), tbl("orders"), tbl("item")], relations: [] };
const empty: SchemaGraph = { tables: [], relations: [] };
const editor: Me = { id: 1, email: "e@x.io", name: "", role: "editor" };

type Api = { getTable: ReturnType<typeof vi.fn>; getSchemas: ReturnType<typeof vi.fn>; getGraph: ReturnType<typeof vi.fn>; proposeChange: ReturnType<typeof vi.fn>; searchSchema?: ReturnType<typeof vi.fn> };
const viewer: Me = { ...editor, role: "viewer" };
const aChange = { id: 9, status: "pending", kind: "schema", source: "web", requester: "e@x.io", prompt: "t", createdAt: "", decidedBy: null, error: "", decisionNote: "", tables: [], diff: ["+ x"], check: {} };

async function setup(api: Partial<Api> = {}, refreshKey = 0, me: Me = editor, path = "/database") {
  history.replaceState(null, "", path);
  vi.resetModules();
  const mocks: Api = {
    getSchemas: api.getSchemas ?? vi.fn().mockResolvedValue([{ name: "master", tables: 3, comment: null }, { name: "crm", tables: 0, comment: null }]),
    proposeChange: api.proposeChange ?? vi.fn().mockResolvedValue(aChange),
    getTable: api.getTable ?? vi.fn().mockResolvedValue({ name: "t", comment: null, constraints: [], indexes: [], hiddenColumns: [], columns: [] }),
    ...(api.searchSchema ? { searchSchema: api.searchSchema } : {}),
    getGraph: api.getGraph ?? vi.fn(async (s: string) => (s === "crm" ? empty : master)),
  };
  vi.doMock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), ...mocks }));
  vi.doMock("./UniverseScene", () => ({ webglAvailable: () => false, UniverseScene: () => null }));
  const { DatabaseView } = await import("./DatabaseView");
  const { useRoute } = await import("@/lib/useRoute");
  const props = { me, live: [], onProposed: () => {}, onAskAssistant: () => {} };
  function Host({ k }: { k: number }) {
    const [route, navigate] = useRoute();
    return <DatabaseView {...props} route={route} navigate={navigate} refreshKey={k} />;
  }
  const r = render(<Host k={refreshKey} />);
  return { mocks, rerender: (k: number) => r.rerender(<Host k={k} />) };
}

describe("DatabaseView", () => {
  beforeEach(() => { vi.resetModules(); history.replaceState(null, "", "/database"); });

  it("lists schemas with table counts and marks the selected one", async () => {
    await setup();
    expect(await screen.findByRole("button", { name: "master (3)" })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("button", { name: "crm (0)" })).not.toHaveAttribute("aria-current");
    expect(await screen.findByRole("button", { name: "customer" })).toBeInTheDocument();
  });

  it("opening an empty schema loads its graph and says there are no tables", async () => {
    const { mocks } = await setup();
    fireEvent.click(await screen.findByRole("button", { name: "crm (0)" }));
    await waitFor(() => expect(mocks.getGraph).toHaveBeenCalledWith("crm"));
    expect((await screen.findAllByText("No tables yet."))[0]).toBeInTheDocument();
  });

  it("ignores a slow response from the previous schema", async () => {
    let release!: (g: SchemaGraph) => void;
    const slow = new Promise<SchemaGraph>((r) => { release = r; });
    const getGraph = vi.fn((s: string) => (s === "master" ? slow : Promise.resolve(empty)));
    await setup({ getGraph });
    fireEvent.click(await screen.findByRole("button", { name: "crm (0)" }));
    await screen.findAllByText("No tables yet.");
    await act(async () => { release(master); });
    expect(screen.queryByRole("button", { name: "customer" })).toBeNull();
    expect(screen.getAllByText("No tables yet.").length).toBeGreaterThan(0);
  });

  it("clears the selected table when switching schema", async () => {
    await setup();
    fireEvent.click(await screen.findByRole("button", { name: "customer" }));
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("master / customer");
    fireEvent.click(screen.getByRole("button", { name: "crm (0)" }));
    await waitFor(() => expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(/^crm$/));
  });

  it("filters the table list", async () => {
    await setup();
    await screen.findByRole("button", { name: "customer" });
    fireEvent.change(screen.getByLabelText("Filter tables"), { target: { value: "ord" } });
    expect(screen.queryByRole("button", { name: "customer" })).toBeNull();
    expect(screen.getByRole("button", { name: "orders" })).toBeInTheDocument();
  });

  it("shows problems text when the graph fails to load", async () => {
    await setup({ getGraph: vi.fn().mockRejectedValue(new Error("x")) });
    expect(await screen.findByText("Could not load the tables.")).toBeInTheDocument();
  });

  it("shows problems text when the schema list fails", async () => {
    await setup({ getSchemas: vi.fn().mockRejectedValue(new Error("x")) });
    expect((await screen.findAllByText("Could not load the databases."))[0]).toBeInTheDocument();
  });

  it("falls back to master if the selected schema vanished", async () => {
    const getSchemas = vi.fn()
      .mockResolvedValueOnce([{ name: "master", tables: 3, comment: null }, { name: "crm", tables: 0, comment: null }])
      .mockResolvedValue([{ name: "master", tables: 3, comment: null }]);
    const { rerender } = await setup({ getSchemas });
    fireEvent.click(await screen.findByRole("button", { name: "crm (0)" }));
    rerender(1);
    await waitFor(() => expect(screen.getByRole("button", { name: "master (3)" })).toHaveAttribute("aria-current", "true"));
  });

  it("refetches when refreshKey changes", async () => {
    const { mocks, rerender } = await setup();
    await screen.findByRole("button", { name: "customer" });
    const n = mocks.getGraph.mock.calls.length;
    rerender(1);
    await waitFor(() => expect(mocks.getGraph.mock.calls.length).toBeGreaterThan(n));
  });

  it("ERD tab renders the diagram for the selected schema; tabs expose aria-selected", async () => {
    const { mocks } = await setup();
    await screen.findByRole("button", { name: "customer" });
    fireEvent.click(screen.getByRole("tab", { name: "ERD" }));
    expect(screen.getByRole("tab", { name: "ERD" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByRole("group", { name: "Schema diagram" })).toBeInTheDocument();
    expect(mocks.getGraph).toHaveBeenCalledWith("master");
  });

  describe("New table", () => {
    it("viewers never see New table, even in an empty database", async () => {
      await setup({}, 0, viewer);
      await screen.findByRole("button", { name: "customer" });
      expect(screen.queryByRole("button", { name: "New table" })).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "crm (0)" }));
      await screen.findAllByText("No tables yet.");
      expect(screen.queryByRole("button", { name: "New table" })).toBeNull();
    });

    it("editors propose create_table in the selected database, also when it is empty", async () => {
      const { mocks } = await setup();
      fireEvent.click(await screen.findByRole("button", { name: "crm (0)" }));
      await screen.findAllByText("No tables yet.");
      fireEvent.click(screen.getByRole("button", { name: "New table" }));
      fireEvent.change(screen.getByLabelText("Table name"), { target: { value: "order" } });
      fireEvent.change(screen.getByLabelText("Field 1 name"), { target: { value: "id" } });
      fireEvent.click(screen.getByRole("button", { name: "Review" }));
      fireEvent.click(screen.getByRole("button", { name: "Propose" }));
      await waitFor(() => expect(mocks.proposeChange).toHaveBeenCalled());
      expect(mocks.proposeChange.mock.calls[0][0]).toEqual([{ op: "create_table", name: "order", columns: [{ name: "id", type: "text" }] }]);
      expect(mocks.proposeChange.mock.calls[0][3]).toBe("crm");
    });

    it("switching database closes the open New table form", async () => {
      await setup();
      fireEvent.click(await screen.findByRole("button", { name: "New table" }));
      expect(screen.getByLabelText("Table name")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "crm (0)" }));
      await waitFor(() => expect(screen.queryByLabelText("Table name")).toBeNull());
    });
  });

  it("viewers see no New database, Rename or Delete", async () => {
    await setup({}, 0, viewer);
    await screen.findByRole("button", { name: "customer" });
    fireEvent.click(screen.getByRole("button", { name: "crm (0)" }));
    await screen.findAllByText("No tables yet.");
    for (const n of [/new database/i, /rename/i, /delete/i]) expect(screen.queryByRole("button", { name: n })).toBeNull();
  });

  it("editors see New database but not Rename/Delete on master", async () => {
    await setup();
    await screen.findByRole("button", { name: "customer" });
    expect(screen.getByRole("button", { name: /new database/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /rename/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /delete/i })).toBeNull();
  });

  it("New database proposes create_schema on master", async () => {
    const { mocks } = await setup();
    await screen.findByRole("button", { name: "customer" });
    fireEvent.click(screen.getByRole("button", { name: /new database/i }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "sales" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    await waitFor(() => expect(mocks.proposeChange).toHaveBeenCalledWith([{ op: "create_schema", name: "sales" }], expect.any(String), "web", "master"));
  });

  it("selects the newly created database once it appears after a refresh", async () => {
    const getSchemas = vi.fn()
      .mockResolvedValueOnce([{ name: "master", tables: 3, comment: null }])
      .mockResolvedValue([{ name: "master", tables: 3, comment: null }, { name: "sales", tables: 0, comment: null }]);
    const { rerender } = await setup({ getSchemas });
    await screen.findByRole("button", { name: "customer" });
    fireEvent.click(screen.getByRole("button", { name: /new database/i }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "sales" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    await waitFor(() => expect(screen.getByRole("status")).toBeInTheDocument());
    rerender(1);
    await waitFor(() => expect(screen.getByRole("button", { name: "sales (0)" })).toHaveAttribute("aria-current", "true"));
  });

  it("closes the New database panel when the created database is auto-selected", async () => {
    const getSchemas = vi.fn()
      .mockResolvedValueOnce([{ name: "master", tables: 3, comment: null }])
      .mockResolvedValue([{ name: "master", tables: 3, comment: null }, { name: "sales", tables: 0, comment: null }]);
    const { rerender } = await setup({ getSchemas });
    await screen.findByRole("button", { name: "customer" });
    fireEvent.click(screen.getByRole("button", { name: /new database/i }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "sales" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    await screen.findByRole("status");
    rerender(1);
    await waitFor(() => expect(screen.getByRole("button", { name: "sales (0)" })).toHaveAttribute("aria-current", "true"));
    expect(screen.getByRole("button", { name: /new database/i })).toBeInTheDocument();
    expect(screen.queryByLabelText("Name")).toBeNull();
  });

  it("a remembered created name does not auto-select after the user navigated elsewhere", async () => {
    const getSchemas = vi.fn()
      .mockResolvedValueOnce([{ name: "master", tables: 3, comment: null }, { name: "crm", tables: 0, comment: null }])
      .mockResolvedValue([{ name: "master", tables: 3, comment: null }, { name: "crm", tables: 0, comment: null }, { name: "sales", tables: 0, comment: null }]);
    const { rerender } = await setup({ getSchemas });
    await screen.findByRole("button", { name: "customer" });
    fireEvent.click(screen.getByRole("button", { name: /new database/i }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "sales" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    await screen.findByRole("status");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: "crm (0)" }));
    rerender(1);
    await screen.findByRole("button", { name: "sales (0)" });
    expect(screen.getByRole("button", { name: "crm (0)" })).toHaveAttribute("aria-current", "true");
  });

  it("Delete is disabled with a title for a schema that has tables", async () => {
    await setup({ getSchemas: vi.fn().mockResolvedValue([{ name: "master", tables: 3, comment: null }, { name: "crm", tables: 2, comment: null }]) });
    fireEvent.click(await screen.findByRole("button", { name: "crm (2)" }));
    const del = screen.getByRole("button", { name: /delete/i });
    expect(del).toBeDisabled();
    expect(del).toHaveAttribute("title", "Only empty databases can be deleted");
  });

  it("Delete on an empty schema sends drop_schema with the typed reason", async () => {
    const { mocks } = await setup();
    fireEvent.click(await screen.findByRole("button", { name: "crm (0)" }));
    fireEvent.click(screen.getByRole("button", { name: /delete/i }));
    const propose = screen.getByRole("button", { name: "Propose" });
    expect(propose).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "not needed" } });
    fireEvent.click(propose);
    await waitFor(() => expect(mocks.proposeChange).toHaveBeenCalledWith([{ op: "drop_schema", schema: "crm", reason: "not needed" }], expect.any(String), "web", "master"));
  });

  it("Rename validates only the new name and proposes rename_schema", async () => {
    const { mocks } = await setup();
    fireEvent.click(await screen.findByRole("button", { name: "crm (0)" }));
    fireEvent.click(screen.getByRole("button", { name: /rename/i }));
    fireEvent.change(screen.getByLabelText("New name"), { target: { value: "master" } });
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("New name"), { target: { value: "clients" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    await waitFor(() => expect(mocks.proposeChange).toHaveBeenCalledWith([{ op: "rename_schema", schema: "crm", new_name: "clients" }], expect.any(String), "web", "master"));
  });

  it("fallback to master after the selected schema vanishes clears table, tab and filter", async () => {
    const getSchemas = vi.fn()
      .mockResolvedValueOnce([{ name: "master", tables: 3, comment: null }, { name: "crm", tables: 3, comment: null }])
      .mockResolvedValue([{ name: "master", tables: 3, comment: null }]);
    const { rerender } = await setup({ getSchemas, getGraph: vi.fn().mockResolvedValue(master) });
    fireEvent.click(await screen.findByRole("button", { name: "crm (3)" }));
    fireEvent.click(await screen.findByRole("button", { name: "orders" }));
    fireEvent.change(screen.getByLabelText("Filter tables"), { target: { value: "ord" } });
    fireEvent.click(screen.getByRole("tab", { name: "Fields" }));
    rerender(1);
    await waitFor(() => expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(/^master$/));
    expect(await screen.findByLabelText("Filter tables")).toHaveValue("");
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  });

  it("vanished table resets the tab to overview", async () => {
    const shrunk: SchemaGraph = { tables: [tbl("customer")], relations: [] };
    const getGraph = vi.fn().mockResolvedValueOnce(master).mockResolvedValue(shrunk);
    const { rerender } = await setup({ getGraph });
    fireEvent.click(await screen.findByRole("button", { name: "orders" }));
    expect(screen.getByRole("tab", { name: "Fields" })).toHaveAttribute("aria-selected", "true");
    rerender(1);
    await waitFor(() => expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(/^master$/));
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  });

  it("filter matching nothing says so", async () => {
    await setup();
    await screen.findByRole("button", { name: "customer" });
    fireEvent.change(screen.getByLabelText("Filter tables"), { target: { value: "zzz" } });
    expect(screen.getByText("No matching tables.")).toBeInTheDocument();
  });

  it("keeps the ERD tab when switching schema; other tabs reset to overview", async () => {
    await setup();
    await screen.findByRole("button", { name: "customer" });
    fireEvent.click(screen.getByRole("tab", { name: "ERD" }));
    fireEvent.click(screen.getByRole("button", { name: "crm (0)" }));
    expect(screen.getByRole("tab", { name: "ERD" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("tab", { name: "Relations" }));
    fireEvent.click(screen.getByRole("button", { name: "master (3)" }));
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  });

  it("Relations tab shows the graph's links for the selected table", async () => {
    const g: SchemaGraph = { ...master, relations: [
      { from: { table: "customer", column: "region_id" }, to: { table: "item", column: "id" } },
      { from: { table: "orders", column: "customer_id" }, to: { table: "customer", column: "id" } },
    ] };
    const { mocks } = await setup({ getGraph: vi.fn().mockResolvedValue(g) });
    fireEvent.click(await screen.findByRole("button", { name: "customer" }));
    fireEvent.click(screen.getByRole("tab", { name: "Relations" }));
    expect(await screen.findByText("customer.region_id → item.id")).toBeInTheDocument();
    expect(screen.getByText("orders.customer_id → customer.id")).toBeInTheDocument();
    expect(mocks.getTable).toHaveBeenCalledWith("customer", "master");
    fireEvent.click(screen.getByRole("button", { name: "Open item" }));
    await waitFor(() => expect(location.pathname).toBe("/database/master/item"));
  });

  it("Overview mounts the search scoped to the selected database", async () => {
    const searchSchema = vi.fn().mockResolvedValue({ results: [], confident: true, suggestion: null });
    await setup({ searchSchema } as never);
    fireEvent.click(await screen.findByRole("button", { name: "crm (0)" }));
    await screen.findAllByText("No tables yet.");
    expect(await screen.findByRole("group", { name: "0 tables" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search the schema"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(searchSchema).toHaveBeenCalledWith("x", "crm"));
  });

  describe("table actions", () => {
    const withDetail = async (me: Me) => {
      const getTable = vi.fn().mockResolvedValue({ name: "customer", comment: null, constraints: [], indexes: [], hiddenColumns: [], columns: [{ name: "id", type: "integer", nullable: false, default: null, comment: null, identity: false }] });
      const r = await setup({ getTable }, 0, me);
      return r;
    };
    it("editor can drop a table with a reason", async () => {
      const { mocks } = await withDetail(editor);
      fireEvent.click(await screen.findByRole("button", { name: "customer" }));
      fireEvent.click(screen.getByRole("button", { name: "Drop table" }));
      fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "obsolete" } });
      fireEvent.click(screen.getByRole("button", { name: "Propose" }));
      await waitFor(() => expect(mocks.proposeChange).toHaveBeenCalled());
      expect(mocks.proposeChange.mock.calls[0][0]).toEqual([{ op: "drop_table", table: "customer", reason: "obsolete" }]);
    });
    it("editor can rename a table", async () => {
      const { mocks } = await withDetail(editor);
      fireEvent.click(await screen.findByRole("button", { name: "customer" }));
      fireEvent.click(screen.getByRole("button", { name: "Rename table" }));
      fireEvent.change(screen.getByLabelText("New table name"), { target: { value: "client" } });
      fireEvent.click(screen.getByRole("button", { name: "Review" }));
      fireEvent.click(screen.getByRole("button", { name: "Propose" }));
      await waitFor(() => expect(mocks.proposeChange).toHaveBeenCalled());
      expect(mocks.proposeChange.mock.calls[0][0]).toEqual([{ op: "rename_table", table: "customer", new_name: "client" }]);
    });
    it("viewer sees no table actions", async () => {
      await withDetail(viewer);
      fireEvent.click(await screen.findByRole("button", { name: "customer" }));
      expect(screen.queryByRole("button", { name: "Drop table" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Rename table" })).toBeNull();
    });
  });

  const created2 = () => vi.fn()
    .mockResolvedValueOnce([{ name: "master", tables: 3, comment: null }, { name: "crm", tables: 0, comment: null }])
    .mockResolvedValue([{ name: "master", tables: 3, comment: null }, { name: "crm", tables: 0, comment: null }, { name: "sales", tables: 0, comment: null }]);
  const createSales = async () => {
    fireEvent.click(await screen.findByRole("button", { name: /new database/i }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "sales" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    await screen.findByRole("status");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
  };

  it("a later-applied create does not steal the view while a table is selected", async () => {
    const { rerender } = await setup({ getSchemas: created2() });
    await screen.findByRole("button", { name: "customer" });
    await createSales();
    fireEvent.click(screen.getByRole("button", { name: "customer" }));
    rerender(1);
    await screen.findByRole("button", { name: "sales (0)" });
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("master / customer");
  });

  it("opening another panel forgets the remembered create", async () => {
    const { rerender } = await setup({ getSchemas: created2() });
    await screen.findByRole("button", { name: "customer" });
    await createSales();
    fireEvent.click(screen.getByRole("button", { name: /new table/i }));
    rerender(1);
    await screen.findByRole("button", { name: "sales (0)" });
    expect(screen.getByRole("button", { name: "master (3)" })).toHaveAttribute("aria-current", "true");
    expect(screen.getByLabelText("Table name")).toBeInTheDocument();
  });

  it("an open Rename form is closed when the view switches schema", async () => {
    const getSchemas = vi.fn()
      .mockResolvedValueOnce([{ name: "master", tables: 3, comment: null }, { name: "crm", tables: 0, comment: null }])
      .mockResolvedValue([{ name: "master", tables: 3, comment: null }]);
    const { rerender } = await setup({ getSchemas });
    fireEvent.click(await screen.findByRole("button", { name: "crm (0)" }));
    fireEvent.click(screen.getByRole("button", { name: /^rename$/i }));
    expect(screen.getByLabelText("New name")).toBeInTheDocument();
    rerender(1);
    await waitFor(() => expect(screen.getByRole("button", { name: "master (3)" })).toHaveAttribute("aria-current", "true"));
    expect(screen.queryByLabelText("New name")).toBeNull();
  });

  it("a refused database rename returns to the rename form with the name kept", async () => {
    const refused = { ...aChange, status: "rejected", diff: [], check: { errors: ["no renames today"] } };
    await setup({ proposeChange: vi.fn().mockResolvedValue(refused) });
    fireEvent.click(await screen.findByRole("button", { name: "crm (0)" }));
    fireEvent.click(screen.getByRole("button", { name: /^rename$/i }));
    fireEvent.change(screen.getByLabelText("New name"), { target: { value: "clients" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    expect(await screen.findByText("no renames today")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.getByLabelText("New name")).toHaveValue("clients");
  });

  describe("address", () => {
    it("/database replaces itself with /database/master", async () => {
      await setup();
      await waitFor(() => expect(location.pathname).toBe("/database/master"));
    });

    it("starts at the database in the address", async () => {
      const { mocks } = await setup({}, 0, editor, "/database/crm");
      await waitFor(() => expect(mocks.getGraph).toHaveBeenCalledWith("crm"));
      expect(screen.getByRole("button", { name: "crm (0)" })).toHaveAttribute("aria-current", "true");
    });

    it("an unknown database says so and never loads its graph", async () => {
      const { mocks } = await setup({}, 0, editor, "/database/ghost");
      expect(await screen.findByText("There is no database ghost.")).toBeInTheDocument();
      expect(mocks.getGraph).not.toHaveBeenCalledWith("ghost");
    });

    it("an unknown table says so, keeps the list and never loads its detail", async () => {
      const { mocks } = await setup({}, 0, editor, "/database/master/nope");
      expect(await screen.findByText("There is no table nope in master.")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "customer" })).toBeInTheDocument();
      expect(mocks.getTable).not.toHaveBeenCalled();
    });

    it("?tab=relations opens Relations, junk opens Fields", async () => {
      await setup({}, 0, editor, "/database/master/customer?tab=relations");
      await waitFor(() => expect(screen.getByRole("tab", { name: "Relations" })).toHaveAttribute("aria-selected", "true"));
      cleanup();
      await setup({}, 0, editor, "/database/master/customer?tab=bogus");
      await waitFor(() => expect(screen.getByRole("tab", { name: "Fields" })).toHaveAttribute("aria-selected", "true"));
    });

    it("choosing a table navigates, choosing it again goes back to the database", async () => {
      await setup();
      fireEvent.click(await screen.findByRole("button", { name: "customer" }));
      await waitFor(() => expect(location.pathname).toBe("/database/master/customer"));
      expect(screen.getByRole("tab", { name: "Fields" })).toHaveAttribute("aria-selected", "true");
      fireEvent.click(screen.getByRole("button", { name: "customer" }));
      await waitFor(() => expect(location.pathname).toBe("/database/master"));
    });

    it("clicking a tab writes ?tab= without changing the path", async () => {
      await setup({}, 0, editor, "/database/master/customer");
      fireEvent.click(await screen.findByRole("tab", { name: "Relations" }));
      expect(location.search).toBe("?tab=relations");
      expect(location.pathname).toBe("/database/master/customer");
    });

    it("switching database keeps ?tab=erd only when the ERD was open", async () => {
      await setup({}, 0, editor, "/database/master?tab=erd");
      fireEvent.click(await screen.findByRole("button", { name: "crm (0)" }));
      await waitFor(() => expect(location.pathname).toBe("/database/crm"));
      expect(location.search).toBe("?tab=erd");
      fireEvent.click(screen.getByRole("tab", { name: "Overview" }));
      fireEvent.click(screen.getByRole("button", { name: "master (3)" }));
      await waitFor(() => expect(location.pathname).toBe("/database/master"));
      expect(location.search).toBe("");
    });

    it("a selected table that vanishes after a reload falls back to the database", async () => {
      const getGraph = vi.fn().mockResolvedValueOnce(master).mockResolvedValue({ tables: [tbl("customer")], relations: [] });
      const { rerender } = await setup({ getGraph }, 0, editor, "/database/master/orders");
      await screen.findByRole("tab", { name: "Fields" });
      rerender(1);
      await waitFor(() => expect(location.pathname).toBe("/database/master"));
    });

    it("the left pane is a disclosure", async () => {
      await setup();
      expect(screen.getByText("Databases and tables").closest("details")).not.toBeNull();
    });
  });

  describe("review fixes", () => {
    it("the Rename database result stays after the database disappears", async () => {
      const getSchemas = vi.fn()
        .mockResolvedValueOnce([{ name: "master", tables: 3, comment: null }, { name: "crm", tables: 0, comment: null }])
        .mockResolvedValue([{ name: "master", tables: 3, comment: null }, { name: "sales", tables: 0, comment: null }]);
      const { rerender } = await setup({ getSchemas }, 0, editor, "/database/crm");
      fireEvent.click(await screen.findByRole("button", { name: /^rename$/i }));
      fireEvent.change(screen.getByLabelText("New name"), { target: { value: "sales" } });
      fireEvent.click(screen.getByRole("button", { name: "Review" }));
      fireEvent.click(screen.getByRole("button", { name: "Propose" }));
      await screen.findByRole("status");
      rerender(1);
      await waitFor(() => expect(location.pathname).toBe("/database/master"));
      expect(screen.getByRole("status")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Propose" })).toBeNull();
    });

    it("a create does not steal the view after the user navigated by address", async () => {
      const { rerender } = await setup({ getSchemas: created2() });
      await screen.findByRole("button", { name: "customer" });
      await createSales();
      act(() => { history.pushState(null, "", "/database/crm"); dispatchEvent(new PopStateEvent("popstate")); });
      await waitFor(() => expect(screen.getByRole("button", { name: "crm (0)" })).toHaveAttribute("aria-current", "true"));
      rerender(1);
      await screen.findByRole("button", { name: "sales (0)" });
      expect(location.pathname).toBe("/database/crm");
    });

    it("Back restores the tab written in the address", async () => {
      await setup();
      fireEvent.click(await screen.findByRole("button", { name: "customer" }));
      fireEvent.click(screen.getByRole("tab", { name: "Relations" }));
      fireEvent.click(screen.getByRole("button", { name: "customer" }));
      fireEvent.click(screen.getByRole("button", { name: "customer" }));
      expect(screen.getByRole("tab", { name: "Fields" })).toHaveAttribute("aria-selected", "true");
      for (const p of ["/database/master", "/database/master/customer"]) {
        history.back();
        await waitFor(() => expect(location.pathname).toBe(p));
      }
      await waitFor(() => expect(screen.getByRole("tab", { name: "Relations" })).toHaveAttribute("aria-selected", "true"));
    });

    it("keeps ?tab= when /database redirects to the first database", async () => {
      await setup({}, 0, editor, "/database?tab=erd");
      await waitFor(() => expect(location.pathname).toBe("/database/master"));
      expect(location.search).toBe("?tab=erd");
    });

    it("a failed database list says so in the main pane too", async () => {
      await setup({ getSchemas: vi.fn().mockRejectedValue(new Error("x")) });
      await waitFor(() => expect(screen.getAllByText("Could not load the databases.").length).toBe(2));
    });
  });
});

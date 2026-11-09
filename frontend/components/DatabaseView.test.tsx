import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Me, SchemaGraph } from "@/lib/api";

const col = (name: string) => ({ name, type: "text", nullable: true, primaryKey: false, comment: null, default: null });
const tbl = (name: string) => ({ name, comment: null, rowEstimate: 1, columns: [col("id")] });
const master: SchemaGraph = { tables: [tbl("customer"), tbl("orders"), tbl("item")], relations: [] };
const empty: SchemaGraph = { tables: [], relations: [] };
const editor: Me = { id: 1, email: "e@x.io", name: "", role: "editor" };

type Api = { getSchemas: ReturnType<typeof vi.fn>; getGraph: ReturnType<typeof vi.fn>; proposeChange: ReturnType<typeof vi.fn> };
const viewer: Me = { ...editor, role: "viewer" };
const aChange = { id: 9, status: "pending", kind: "schema", source: "web", requester: "e@x.io", prompt: "t", createdAt: "", decidedBy: null, error: "", decisionNote: "", tables: [], diff: ["+ x"], check: {} };

async function setup(api: Partial<Api> = {}, refreshKey = 0, me: Me = editor) {
  vi.resetModules();
  const mocks: Api = {
    getSchemas: api.getSchemas ?? vi.fn().mockResolvedValue([{ name: "master", tables: 3, comment: null }, { name: "crm", tables: 0, comment: null }]),
    proposeChange: api.proposeChange ?? vi.fn().mockResolvedValue(aChange),
    getGraph: api.getGraph ?? vi.fn(async (s: string) => (s === "crm" ? empty : master)),
  };
  vi.doMock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), ...mocks }));
  vi.doMock("./UniverseScene", () => ({ webglAvailable: () => false, UniverseScene: () => null }));
  const { DatabaseView } = await import("./DatabaseView");
  const props = { me, live: [], onProposed: () => {}, onAskAssistant: () => {} };
  const r = render(<DatabaseView {...props} refreshKey={refreshKey} />);
  return { mocks, rerender: (k: number) => r.rerender(<DatabaseView {...props} refreshKey={k} />) };
}

describe("DatabaseView", () => {
  beforeEach(() => vi.resetModules());

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
    expect(await screen.findByText("Could not load the databases.")).toBeInTheDocument();
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
    expect(await screen.findByRole("img", { name: "Schema diagram" })).toBeInTheDocument();
    expect(mocks.getGraph).toHaveBeenCalledWith("master");
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
    expect(screen.getByLabelText("Filter tables")).toHaveValue("");
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
});

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Me, SchemaGraph } from "@/lib/api";

const col = (name: string) => ({ name, type: "text", nullable: true, primaryKey: false, comment: null, default: null });
const tbl = (name: string) => ({ name, comment: null, rowEstimate: 1, columns: [col("id")] });
const master: SchemaGraph = { tables: [tbl("customer"), tbl("orders"), tbl("item")], relations: [] };
const empty: SchemaGraph = { tables: [], relations: [] };
const editor: Me = { id: 1, email: "e@x.io", name: "", role: "editor" };

type Api = { getSchemas: ReturnType<typeof vi.fn>; getGraph: ReturnType<typeof vi.fn> };

async function setup(api: Partial<Api> = {}, refreshKey = 0) {
  vi.resetModules();
  const mocks: Api = {
    getSchemas: api.getSchemas ?? vi.fn().mockResolvedValue([{ name: "master", tables: 3, comment: null }, { name: "crm", tables: 0, comment: null }]),
    getGraph: api.getGraph ?? vi.fn(async (s: string) => (s === "crm" ? empty : master)),
  };
  vi.doMock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), ...mocks }));
  vi.doMock("./UniverseScene", () => ({ webglAvailable: () => false, UniverseScene: () => null }));
  const { DatabaseView } = await import("./DatabaseView");
  const props = { me: editor, live: [], onProposed: () => {}, onAskAssistant: () => {} };
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

  it("has no New database button yet", async () => {
    await setup();
    await screen.findByRole("button", { name: "customer" });
    expect(screen.queryByRole("button", { name: /new database/i })).toBeNull();
  });
});

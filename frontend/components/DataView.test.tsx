import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Me, RowsPage } from "@/lib/api";
import type { Route } from "@/lib/route";

const page: RowsPage = {
  table: "customer", total: 1, offset: 0, limit: 50,
  columns: [{ name: "id", type: "bigint", nullable: false, readOnly: true, references: null }, { name: "name", type: "text", nullable: true, readOnly: false, references: null }],
  rows: [{ id: 1, name: "Acme" }],
};
const me = (role: Me["role"] = "editor"): Me => ({ id: 1, email: "e@x.com", name: "", role });
const SCHEMAS = [{ name: "master", tables: 2, comment: null }, { name: "empty", tables: 0, comment: null }];
const graph = (names: string[]) => ({ tables: names.map((name) => ({ name, comment: null, rowEstimate: 3, columns: [{ name: "id", type: "bigint", nullable: false, primaryKey: true }, { name: "name", type: "text", nullable: true, primaryKey: false }] })), relations: [] });

async function setup(route: Route, opts: { role?: Me["role"]; tables?: string[]; schemas?: typeof SCHEMAS } = {}) {
  vi.resetModules();
  const getRows = vi.fn().mockResolvedValue(page);
  const getGraph = vi.fn().mockImplementation(async (s: string) => graph(s === "master" ? opts.tables ?? ["customer", "order_header"] : []));
  vi.doMock("@/lib/api", async (orig) => ({
    ...(await orig<typeof import("@/lib/api")>()),
    getRows, getGraph, getSchemas: vi.fn().mockResolvedValue(opts.schemas ?? SCHEMAS),
    getOptions: vi.fn().mockResolvedValue({ labelColumn: "name", truncated: false, options: [] }),
  }));
  const { DataView } = await import("./DataView");
  const navigate = vi.fn();
  const ui = (r: Route, refreshKey = 0) => (
    <DataView me={me(opts.role)} live={[]} route={r} navigate={navigate} refreshKey={refreshKey} />
  );
  const view = render(ui(route));
  return { getRows, getGraph, navigate, rerender: (r: Route, k = 0) => view.rerender(ui(r, k)) };
}

describe("DataView", () => {
  beforeEach(() => { history.replaceState(null, "", "/data"); });

  it("/data picks the first database with tables and its first table, replacing the address", async () => {
    const { navigate } = await setup({ view: "data" });
    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ view: "data", schema: "master", table: "customer" }, { replace: true }));
  });

  it("/data/<db> picks that database's first table", async () => {
    const { navigate } = await setup({ view: "data", schema: "master" });
    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ view: "data", schema: "master", table: "customer" }, { replace: true }));
  });

  it("an unknown table says so, keeps the list, and loads no rows", async () => {
    const { getRows, navigate } = await setup({ view: "data", schema: "master", table: "nope" });
    expect(await screen.findByText("There is no table nope in master.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /order_header/ })).toBeInTheDocument();
    expect(getRows).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("an unknown database says so and never calls the graph", async () => {
    const { getGraph } = await setup({ view: "data", schema: "ghost", table: "t" });
    expect(await screen.findByText("There is no database ghost.")).toBeInTheDocument();
    expect(getGraph).not.toHaveBeenCalled();
  });

  it("a hand-edited link with unknown columns and junk values sends none of them", async () => {
    history.replaceState(null, "", "/data/master/customer?sort=nope&dir=up&f.x=1&f.name=ab&inactive=2");
    const { getRows } = await setup({ view: "data", schema: "master", table: "customer" });
    await waitFor(() => expect(getRows).toHaveBeenCalled());
    for (const [, q] of getRows.mock.calls) expect(q).toMatchObject({ sort: undefined, dir: "asc", filters: { name: "ab" }, inactive: false });
  });

  it("switching tables from a filtered view asks for the new table once, with a clean view", async () => {
    history.replaceState(null, "", "/data/master/customer?sort=name&f.name=ab");
    const { getRows, rerender } = await setup({ view: "data", schema: "master", table: "customer" });
    await screen.findByText(/1–1 of 1/);
    getRows.mockClear();
    history.replaceState(null, "", "/data/master/order_header");
    rerender({ view: "data", schema: "master", table: "order_header" });
    await waitFor(() => expect(getRows).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 30));
    const calls = getRows.mock.calls.filter((c) => c[0] === "order_header");
    expect(calls).toHaveLength(1);
    expect(calls[0][1]).toMatchObject({ sort: undefined, filters: {} });
  });

  it("a link's query string reaches the grid", async () => {
    history.replaceState(null, "", "/data/master/customer?sort=name&f.name=ab");
    const { getRows } = await setup({ view: "data", schema: "master", table: "customer" });
    await waitFor(() => expect(getRows).toHaveBeenCalled());
    expect(getRows).toHaveBeenLastCalledWith("customer", expect.objectContaining({ sort: "name", dir: "asc", filters: { name: "ab" } }), "master");
  });

  it("changing a filter writes it into the address without changing the path", async () => {
    await setup({ view: "data", schema: "master", table: "customer" });
    await screen.findByText(/1–1 of 1/);
    fireEvent.change(screen.getByLabelText("Filter column"), { target: { value: "name" } });
    fireEvent.change(screen.getByLabelText("Filter text"), { target: { value: "ac" } });
    fireEvent.click(screen.getByRole("button", { name: "Add filter" }));
    await waitFor(() => expect(location.search).toBe("?f.name=ac"));
    expect(location.pathname).toBe("/data");  // the test never navigates: the path is whatever the test set
  });

  it("switching database navigates to it", async () => {
    const { navigate } = await setup({ view: "data", schema: "master", table: "customer" });
    await screen.findByText(/1–1 of 1/);
    fireEvent.change(screen.getByRole("combobox", { name: "Database" }), { target: { value: "empty" } });
    expect(navigate).toHaveBeenCalledWith({ view: "data", schema: "empty" });
  });

  it("a table that disappears after a reload falls back to the first table", async () => {
    const { rerender, navigate, getGraph } = await setup({ view: "data", schema: "master", table: "order_header" });
    await screen.findByText(/1–1 of 1/);
    getGraph.mockResolvedValue(graph(["customer"]));
    rerender({ view: "data", schema: "master", table: "order_header" }, 1);
    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ view: "data", schema: "master", table: "customer" }, { replace: true }));
  });

  it("a viewer has no Export CSV; an editor does", async () => {
    await setup({ view: "data", schema: "master", table: "customer" }, { role: "viewer" });
    await screen.findByText(/1–1 of 1/);
    expect(screen.queryByRole("button", { name: "Export CSV" })).toBeNull();
  });

  it("an editor sees Export CSV", async () => {
    await setup({ view: "data", schema: "master", table: "customer" });
    expect(await screen.findByRole("button", { name: "Export CSV" })).toBeInTheDocument();
  });

  it("no tables anywhere says how to get one", async () => {
    await setup({ view: "data" }, { schemas: [{ name: "master", tables: 0, comment: null }] });
    expect(await screen.findByText(/Ask the assistant to design one/)).toBeInTheDocument();
  });

  it("a database without tables says so and keeps the picker", async () => {
    await setup({ view: "data", schema: "master" }, { tables: [] });
    expect((await screen.findAllByText("No tables in this database.")).length).toBeGreaterThan(0);
    expect(screen.getByRole("combobox", { name: "Database" })).toBeInTheDocument();
  });
});

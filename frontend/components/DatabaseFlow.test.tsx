import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Me, SchemaGraph, SchemaInfo, TableDetail } from "@/lib/api";

const approver: Me = { id: 1, email: "a@x.io", name: "", role: "approver" };
const viewer: Me = { ...approver, id: 2, email: "v@x.io", role: "viewer" };
const aChange = { id: 9, status: "pending", kind: "schema", source: "web", requester: "a@x.io", prompt: "t", createdAt: "", decidedBy: null, error: "", decisionNote: "", tables: [], diff: ["+ x"], check: {} };

const col = (name: string) => ({ name, type: "text", nullable: true, primaryKey: false, comment: null, default: null });
const graphOf = (...names: string[]): SchemaGraph => ({ tables: names.map((n) => ({ name: n, comment: null, rowEstimate: 0, columns: [col("id")] })), relations: [] });
const dcol = (name: string) => ({ name, type: "text", nullable: true, default: null, comment: null, identity: false });
const detail: TableDetail = {
  name: "customer", comment: null, constraints: [], indexes: [],
  hiddenColumns: ["created_at", "created_by"],
  columns: [dcol("id"), dcol("email"), dcol("created_at"), dcol("created_by")],
};

/** Server state the mocked API reads from; tests mutate it to simulate an applied change. */
let schemas: SchemaInfo[];
let graphs: Record<string, SchemaGraph>;

async function setup(me: Me) {
  vi.resetModules();
  const mocks = {
    getSchemas: vi.fn(async () => schemas),
    getGraph: vi.fn(async (s: string) => graphs[s] ?? graphOf()),
    getTable: vi.fn(async () => detail),
    proposeChange: vi.fn().mockResolvedValue(aChange),
  };
  vi.doMock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), ...mocks }));
  vi.doMock("./UniverseScene", () => ({ webglAvailable: () => false, UniverseScene: () => null }));
  const { DatabaseView } = await import("./DatabaseView");
  const { useState } = await import("react");
  const { useRoute } = await import("@/lib/useRoute");
  history.replaceState(null, "", "/database");
  function Host() {
    const [key, setKey] = useState(0);
    const [route, navigate] = useRoute();
    return (
      <>
        <button onClick={() => setKey((k) => k + 1)}>applied</button>
        <DatabaseView me={me} live={[]} route={route} navigate={navigate} refreshKey={key} onProposed={() => {}} onAskAssistant={() => {}} />
      </>
    );
  }
  render(<Host />);
  return mocks;
}

beforeEach(() => {
  schemas = [{ name: "master", tables: 1, comment: null }];
  graphs = { master: graphOf("customer") };
  localStorage.clear();
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("Database tab flow", () => {
  it("approver: create database, create table in it, add a field, with audit columns hidden by default", async () => {
    const m = await setup(approver);
    await screen.findByRole("button", { name: "customer" });

    // New database proposal on master
    fireEvent.click(screen.getByRole("button", { name: /new database/i }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "sales" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    await screen.findByRole("status");
    expect(m.proposeChange).toHaveBeenCalledWith([{ op: "create_schema", name: "sales" }], expect.any(String), "web", "master");

    // The change is applied; the refresh lists and selects the new database
    schemas = [...schemas, { name: "sales", tables: 0, comment: null }];
    graphs.sales = graphOf();
    fireEvent.click(screen.getByRole("button", { name: "applied" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "sales (0)" })).toHaveAttribute("aria-current", "true"));
    await waitFor(() => expect(screen.getAllByText("No tables yet.").length).toBeGreaterThan(0));
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(/^sales$/);
    expect(screen.queryByLabelText("Name")).toBeNull();

    // New table with two columns, proposed against sales
    fireEvent.click(screen.getByRole("button", { name: "New table" }));
    fireEvent.change(screen.getByLabelText("Table name"), { target: { value: "customer" } });
    fireEvent.change(screen.getByLabelText("Field 1 name"), { target: { value: "id" } });
    fireEvent.click(screen.getByRole("button", { name: "Add field" }));
    fireEvent.change(screen.getByLabelText("Field 2 name"), { target: { value: "email" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    await waitFor(() => expect(m.proposeChange).toHaveBeenCalledTimes(2));
    expect(m.proposeChange.mock.calls[1][0]).toEqual([
      { op: "create_table", name: "customer", columns: [{ name: "id", type: "text" }, { name: "email", type: "text" }] },
    ]);
    expect(m.proposeChange.mock.calls[1][3]).toBe("sales");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));

    // Table applied; open it in Fields
    schemas = [schemas[0], { name: "sales", tables: 1, comment: null }];
    graphs.sales = graphOf("customer");
    fireEvent.click(screen.getByRole("button", { name: "applied" }));
    fireEvent.click(await screen.findByRole("button", { name: "customer" }));
    expect(await screen.findByText("email")).toBeInTheDocument();
    expect(m.getTable).toHaveBeenLastCalledWith("customer", "sales");

    // Audit columns hidden, then toggled on (read-only: no action buttons)
    expect(screen.queryByText("created_at")).toBeNull();
    fireEvent.click(screen.getByLabelText("Show audit columns"));
    expect(screen.getByText("created_at")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Rename created_at" })).toBeNull();
    expect(screen.getByRole("button", { name: "Rename email" })).toBeInTheDocument();

    // Add field
    fireEvent.click(screen.getByRole("button", { name: "Add field" }));
    fireEvent.change(screen.getByLabelText("Field name"), { target: { value: "phone" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    await waitFor(() => expect(m.proposeChange).toHaveBeenCalledTimes(3));
    expect(m.proposeChange.mock.calls[2][0]).toEqual([{ op: "add_column", table: "customer", column: { name: "phone", type: "text" } }]);
    expect(m.proposeChange.mock.calls[2][3]).toBe("sales");
  });

  it("switching database resets the table: no request mixes the old table with the new schema", async () => {
    schemas = [{ name: "master", tables: 1, comment: null }, { name: "crm", tables: 0, comment: null }];
    graphs.crm = graphOf();
    const m = await setup(approver);
    fireEvent.click(await screen.findByRole("button", { name: "customer" }));
    await screen.findByText("email");
    m.getTable.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "crm (0)" }));
    await waitFor(() => expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(/^crm$/));
    await act(async () => {});
    expect(m.getTable).not.toHaveBeenCalled();
  });

  it("export link carries the selected database and shows a server error", async () => {
    schemas = [{ name: "master", tables: 1, comment: null }, { name: "crm", tables: 0, comment: null }];
    graphs.crm = graphOf();
    const fetchMock = vi.fn(async () => ({ ok: false, status: 422, statusText: "Unprocessable", json: async () => ({ error: "Column type not supported" }) }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    await setup(approver);
    fireEvent.click(await screen.findByRole("button", { name: "crm (0)" }));
    await waitFor(() => expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(/^crm$/));
    fireEvent.click(screen.getByRole("button", { name: "Download SQL" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Column type not supported");
    expect(String((fetchMock.mock.calls[0] as unknown[])[0])).toMatch(/\/schema\/export\?schema=crm&data=0$/);
  });

  it("viewer sees the same screens with no write buttons, including empty states", async () => {
    schemas = [{ name: "master", tables: 1, comment: null }, { name: "crm", tables: 0, comment: null }];
    graphs.crm = graphOf();
    await setup(viewer);
    fireEvent.click(await screen.findByRole("button", { name: "customer" }));
    await screen.findByText("email");
    const writes = [/new database/i, /new table/i, /^rename/i, /^delete/i, /^drop/i, /^type /i, /^comment /i, /add field/i, /add relation|add index|add unique/i];
    const none = () => { for (const n of writes) expect(screen.queryByRole("button", { name: n })).toBeNull(); };
    none();
    fireEvent.click(screen.getByRole("tab", { name: "Relations" }));
    await screen.findByRole("tab", { name: "Relations" });
    none();
    fireEvent.click(screen.getByRole("button", { name: "crm (0)" }));
    await waitFor(() => expect(screen.getAllByText("No tables yet.").length).toBeGreaterThan(0));
    none();
    expect(screen.getByLabelText("Include data")).toBeDisabled();
  });
});

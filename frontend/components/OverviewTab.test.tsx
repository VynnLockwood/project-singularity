import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Me, SchemaGraph } from "@/lib/api";

const col = (name: string) => ({ name, type: "text", nullable: true, primaryKey: false, comment: null, default: null });
const graph: SchemaGraph = {
  tables: [
    { name: "customer", comment: null, rowEstimate: 1, columns: [col("id"), col("name")] },
    { name: "orders", comment: null, rowEstimate: 1, columns: [col("id")] },
  ],
  relations: [],
};
const me = (role: Me["role"]): Me => ({ id: 1, email: "e@x.io", name: "", role });

async function setup(role: Me["role"] = "editor", g: SchemaGraph | null = graph, comment: string | null = null) {
  const onOpenTable = vi.fn();
  vi.resetModules();
  const searchSchema = vi.fn().mockResolvedValue({
    results: [{ kind: "column", table: "customer", schema: "billing", column: "phone", confidence: 0.9, description: "d" }],
    confident: true, suggestion: null,
  });
  vi.doMock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), searchSchema }));
  const { OverviewTab } = await import("./OverviewTab");
  const onAsk = vi.fn();
  render(<OverviewTab me={me(role)} schema="crm" comment={comment} graph={g} onAskAssistant={onAsk} onOpenTable={onOpenTable} />);
  return { searchSchema, onAsk, onOpenTable };
}

describe("OverviewTab", () => {
  beforeEach(() => vi.resetModules());

  it("shows the name, comment and counts", async () => {
    await setup("editor", graph, "Customer data");
    expect(screen.getByText("Customer data")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "2 tables" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "3 columns" })).toBeInTheDocument();
  });

  it("an empty database shows zero counts", async () => {
    await setup("editor", { tables: [], relations: [] });
    expect(screen.getByRole("group", { name: "0 tables" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "0 columns" })).toBeInTheDocument();
  });

  it("does not claim counts while the graph is loading", async () => {
    await setup("editor", null);
    expect(screen.queryByRole("group", { name: /tables$/ })).toBeNull();
    expect(document.querySelector(".ui-skeleton")).not.toBeNull();
  });

  it("searches the selected database and shows the schema of hits", async () => {
    const { searchSchema } = await setup();
    fireEvent.change(screen.getByLabelText("Search the schema"), { target: { value: "q" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(searchSchema).toHaveBeenCalledWith("q", "crm"));
    expect(await screen.findByText(/· billing/)).toBeInTheDocument();
  });

  it("the assistant button names the database", async () => {
    const { onAsk } = await setup();
    fireEvent.click(screen.getByRole("button", { name: /Ask the assistant about crm/ }));
    expect(onAsk).toHaveBeenCalledWith(expect.stringContaining("crm"));
  });

  it("only approvers get the rebuild button", async () => {
    await setup("approver");
    expect(screen.getByRole("button", { name: "Rebuild search index" })).toBeInTheDocument();
  });
  it("editors do not get the rebuild button", async () => {
    await setup("editor");
    expect(screen.queryByRole("button", { name: "Rebuild search index" })).toBeNull();
  });
  it("viewers can search but have no assistant or rebuild actions", async () => {
    await setup("viewer");
    expect(screen.getByLabelText("Search the schema")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Rebuild search index" })).toBeNull();
  });

  const big: SchemaGraph = {
    tables: [
      { name: "customer", comment: null, rowEstimate: 1500, columns: [col("id"), col("name")] },
      { name: "<b>odd</b>", comment: null, rowEstimate: 2500, columns: [col("id")] },
    ],
    relations: [{ from: { table: "customer", column: "id" }, to: { table: "<b>odd</b>", column: "id" } }],
  };

  it("tiles show tables, columns, relations and approximate rows", async () => {
    await setup("editor", big);
    expect(screen.getByRole("group", { name: "2 tables" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "3 columns" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "1 relation" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "4,000 rows (approx.)" })).toBeInTheDocument();
  });

  it("lists each table with its columns and rows, and opens it", async () => {
    const { onOpenTable } = await setup("editor", big);
    const b = screen.getByRole("button", { name: /^customer/ });
    expect(b).toHaveTextContent("2 columns");
    expect(b).toHaveTextContent("1,500");
    fireEvent.click(b);
    expect(onOpenTable).toHaveBeenCalledWith("customer");
  });

  it("a table name with markup is text", async () => {
    await setup("editor", big);
    expect(screen.getByRole("button", { name: /^<b>odd<\/b>/ })).toBeInTheDocument();
    expect(document.querySelector("b")).toBeNull();
  });

  it("says so when there are no tables", async () => {
    await setup("editor", { tables: [], relations: [] });
    expect(screen.getByText("No tables yet.")).toBeInTheDocument();
  });
});

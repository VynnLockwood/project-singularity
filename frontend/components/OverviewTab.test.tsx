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
  vi.resetModules();
  const searchSchema = vi.fn().mockResolvedValue({
    results: [{ kind: "column", table: "customer", schema: "billing", column: "phone", confidence: 0.9, description: "d" }],
    confident: true, suggestion: null,
  });
  vi.doMock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), searchSchema }));
  const { OverviewTab } = await import("./OverviewTab");
  const onAsk = vi.fn();
  render(<OverviewTab me={me(role)} schema="crm" comment={comment} graph={g} onAskAssistant={onAsk} />);
  return { searchSchema, onAsk };
}

describe("OverviewTab", () => {
  beforeEach(() => vi.resetModules());

  it("shows the name, comment and counts", async () => {
    await setup("editor", graph, "Customer data");
    expect(screen.getByText("Customer data")).toBeInTheDocument();
    expect(screen.getByText("2 tables")).toBeInTheDocument();
    expect(screen.getByText("3 columns")).toBeInTheDocument();
  });

  it("an empty database shows zero counts", async () => {
    await setup("editor", { tables: [], relations: [] });
    expect(screen.getByText("0 tables")).toBeInTheDocument();
    expect(screen.getByText("0 columns")).toBeInTheDocument();
  });

  it("does not claim counts while the graph is loading", async () => {
    await setup("editor", null);
    expect(screen.queryByText(/ tables$/)).toBeNull();
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
});

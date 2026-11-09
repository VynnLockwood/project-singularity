import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SchemaGraph } from "@/lib/api";

const col = (name: string, pk = false) => ({ name, type: "text", nullable: true, primaryKey: pk, comment: null, default: null });
const graph: SchemaGraph = {
  tables: [
    { name: "customer", comment: "who buys", rowEstimate: 12, columns: [col("id", true), col("name")] },
    { name: "orderHeader", comment: null, rowEstimate: 3, columns: [col("id", true), col("customerId")] },
  ],
  relations: [{ from: { table: "orderHeader", column: "customerId" }, to: { table: "customer", column: "id" } }],
};

async function setup(webgl: boolean) {
  vi.resetModules();
  vi.doMock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), getGraph: vi.fn().mockResolvedValue(graph) }));
  // jsdom has no WebGL: stand in for the scene and expose its props
  vi.doMock("./UniverseScene", () => ({
    webglAvailable: () => webgl,
    UniverseScene: ({ universe, selected, onSelect, onUnavailable }: never) => (
      <div data-testid="scene" data-bodies={(universe as { bodies: unknown[] }).bodies.length} data-selected={selected ?? ""}>
        <button onClick={() => (onSelect as (n: string) => void)("customer")}>pick customer</button>
        <button onClick={() => (onUnavailable as () => void)()}>break</button>
      </div>
    ),
  }));
  const { SchemaView } = await import("./SchemaView");
  render(<SchemaView schema="master" refreshKey={0} />);
}

describe("SchemaView", () => {
  beforeEach(() => vi.resetModules());

  it("starts in 3D when WebGL exists, with one body per table", async () => {
    await setup(true);
    const scene = await screen.findByTestId("scene");
    expect(scene).toHaveAttribute("data-bodies", "2");
    expect(screen.getByText("3D")).toHaveAttribute("aria-pressed", "true");
  });

  it("starts in 2D without WebGL", async () => {
    await setup(false);
    expect(await screen.findByRole("img", { name: "Schema diagram" })).toBeInTheDocument();
    expect(screen.queryByTestId("scene")).toBeNull();
  });

  it("toggles between 3D and 2D", async () => {
    await setup(true);
    await screen.findByTestId("scene");
    fireEvent.click(screen.getByText("2D"));
    expect(screen.getByRole("img", { name: "Schema diagram" })).toBeInTheDocument();
    fireEvent.click(screen.getByText("3D"));
    expect(await screen.findByTestId("scene")).toBeInTheDocument();
  });

  it("selecting a body (or its list entry) shows the table's details and relations", async () => {
    await setup(true);
    fireEvent.click(await screen.findByText("pick customer"));
    const panel = await screen.findByLabelText("Table customer");
    expect(panel).toHaveTextContent("who buys");
    expect(panel).toHaveTextContent("Referenced by: orderHeader.customerId");
    expect(screen.getByTestId("scene")).toHaveAttribute("data-selected", "customer");

    fireEvent.click(screen.getByRole("button", { name: "orderHeader" }));
    expect(await screen.findByLabelText("Table orderHeader")).toHaveTextContent("Points to: customer (via customerId)");
  });

  it("falls back to 2D if the scene cannot start", async () => {
    await setup(true);
    fireEvent.click(await screen.findByText("break"));
    expect(await screen.findByRole("img", { name: "Schema diagram" })).toBeInTheDocument();
  });
});

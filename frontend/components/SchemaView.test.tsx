import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

async function setup(webgl: boolean, opts: { getGraph?: ReturnType<typeof vi.fn>; table?: string | null; onOpenTable?: (n: string) => void } = {}) {
  vi.resetModules();
  vi.doMock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), getGraph: opts.getGraph ?? vi.fn().mockResolvedValue(graph) }));
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
  const el = (k: number, table = opts.table) => <SchemaView schema="master" refreshKey={k} table={table} onOpenTable={opts.onOpenTable} />;
  const r = render(el(0));
  return { rerender: (k: number, table = opts.table) => r.rerender(el(k, table)) };
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
    expect(await screen.findByRole("group", { name: "Schema diagram" })).toBeInTheDocument();
    expect(screen.queryByTestId("scene")).toBeNull();
  });

  it("toggles between 3D and 2D", async () => {
    await setup(true);
    await screen.findByTestId("scene");
    fireEvent.click(screen.getByText("2D"));
    expect(screen.getByRole("group", { name: "Schema diagram" })).toBeInTheDocument();
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
    expect(await screen.findByRole("group", { name: "Schema diagram" })).toBeInTheDocument();
  });

  const edges = () => screen.getAllByTestId("relation");

  describe("2D selection", () => {
    it("cards are buttons; clicking one selects it, highlights its relations and shows the panel", async () => {
      await setup(false);
      const c = await screen.findByRole("button", { name: "customer" });
      expect(c).toHaveAttribute("aria-pressed", "false");
      fireEvent.click(c);
      expect(screen.getByRole("button", { name: "customer" })).toHaveAttribute("aria-pressed", "true");
      expect(edges()[0]).toHaveAttribute("data-hot", "true");
      const panel = screen.getByLabelText("Table customer");
      expect(panel).toHaveTextContent("Referenced by: orderHeader.customerId");
      expect(panel).toHaveTextContent("id");
    });

    it("relations that do not touch the selected table are dimmed", async () => {
      const g3: SchemaGraph = { tables: [...graph.tables, { name: "note", comment: null, rowEstimate: 0, columns: [col("id", true), col("ownerId")] }, { name: "owner", comment: null, rowEstimate: 0, columns: [col("id", true)] }],
        relations: [...graph.relations, { from: { table: "note", column: "ownerId" }, to: { table: "owner", column: "id" } }] };
      await setup(false, { getGraph: vi.fn().mockResolvedValue(g3) });
      fireEvent.click(await screen.findByRole("button", { name: "customer" }));
      const [a, b] = edges();
      expect(a).toHaveAttribute("data-hot", "true");
      expect(b).toHaveAttribute("data-dim", "true");
      expect(b).not.toHaveAttribute("data-hot");
    });

    it("Enter and Space select, clicking again or Escape clears", async () => {
      await setup(false);
      const c = await screen.findByRole("button", { name: "customer" });
      fireEvent.keyDown(c, { key: "Enter" });
      expect(screen.getByRole("button", { name: "customer" })).toHaveAttribute("aria-pressed", "true");
      fireEvent.click(screen.getByRole("button", { name: "customer" }));
      expect(screen.queryByLabelText("Table customer")).toBeNull();
      fireEvent.keyDown(screen.getByRole("button", { name: "orderHeader" }), { key: " " });
      expect(screen.getByLabelText("Table orderHeader")).toBeInTheDocument();
      fireEvent.keyDown(screen.getByRole("button", { name: "orderHeader" }), { key: "Escape" });
      expect(screen.queryByLabelText(/^Table /)).toBeNull();
      expect(edges()[0]).not.toHaveAttribute("data-hot");
      expect(edges()[0]).not.toHaveAttribute("data-dim");
    });

    it("Open fields hands the table name over untouched", async () => {
      const onOpenTable = vi.fn();
      const odd: SchemaGraph = { tables: ["a b", 'x"y', "<b>"].map((n) => ({ name: n, comment: null, rowEstimate: 0, columns: [col("id", true)] })), relations: [] };
      await setup(false, { getGraph: vi.fn().mockResolvedValue(odd), onOpenTable });
      for (const n of ["a b", 'x"y', "<b>"]) {
        fireEvent.click(await screen.findByRole("button", { name: n }));
        fireEvent.click(screen.getByRole("button", { name: "Open fields" }));
        expect(onOpenTable).toHaveBeenLastCalledWith(n);
        fireEvent.click(screen.getByRole("button", { name: n }));
      }
      expect(document.querySelector("svg b")).toBeNull();
    });

    it("a selected table that vanishes after a reload clears the panel", async () => {
      const smaller: SchemaGraph = { tables: [graph.tables[1]], relations: [] };
      const { rerender } = await setup(false, { getGraph: vi.fn().mockResolvedValueOnce(graph).mockResolvedValue(smaller) });
      fireEvent.click(await screen.findByRole("button", { name: "customer" }));
      rerender(1);
      await waitFor(() => expect(screen.queryByRole("button", { name: "customer" })).toBeNull());
      expect(screen.queryByLabelText(/^Table /)).toBeNull();
    });

    it("the table prop seeds the selection and follows changes", async () => {
      const { rerender } = await setup(false, { table: "customer" });
      expect(await screen.findByLabelText("Table customer")).toBeInTheDocument();
      rerender(0, "orderHeader");
      expect(await screen.findByLabelText("Table orderHeader")).toBeInTheDocument();
      expect(screen.queryByLabelText("Table customer")).toBeNull();
    });

    it("foreign-key columns are marked", async () => {
      await setup(false);
      await screen.findByRole("button", { name: "customer" });
      expect(screen.getByText("customerId")).toHaveAttribute("data-fk", "true");
      expect(screen.getByText("name")).not.toHaveAttribute("data-fk");
    });

    it("Fit to width toggles a class on the scroll container and the svg has a viewBox", async () => {
      await setup(false);
      const fit = await screen.findByRole("button", { name: "Fit to width" });
      expect(fit).toHaveAttribute("aria-pressed", "false");
      fireEvent.click(fit);
      expect(screen.getByRole("button", { name: "Fit to width" })).toHaveAttribute("aria-pressed", "true");
      expect(document.querySelector(".schema-scroll")).toHaveClass("schema-fit");
      expect(document.querySelector("svg")).toHaveAttribute("viewBox");
    });
  });

  it("the 2D/3D control is two pressed-state buttons and 3D is absent without WebGL", async () => {
    await setup(false);
    await screen.findByRole("group", { name: "Schema diagram" });
    expect(screen.getByRole("button", { name: "2D" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("button", { name: "3D" })).toBeNull();
    expect(screen.queryByTestId("scene")).toBeNull();
  });

  it("the 3D panel also offers Open fields", async () => {
    const onOpenTable = vi.fn();
    await setup(true, { onOpenTable });
    fireEvent.click(await screen.findByText("pick customer"));
    fireEvent.click(await screen.findByRole("button", { name: "Open fields" }));
    expect(onOpenTable).toHaveBeenCalledWith("customer");
  });
});

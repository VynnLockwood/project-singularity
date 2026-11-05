import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { GridColumn } from "@/lib/api";
import { ColumnStats } from "./ColumnStats";

const col = (name: string, type: string): GridColumn => ({ name, type, nullable: true, readOnly: false, references: null });

describe("ColumnStats", () => {
  it("summarises numeric and text columns", async () => {
    render(<ColumnStats columns={[col("qty", "integer"), col("note", "text")]}
      rows={[{ qty: 1, note: "a" }, { qty: 3, note: "a" }, { qty: null, note: "bb" }]} />);
    const qty = await screen.findByTestId("stats-qty");
    expect(qty).toHaveTextContent("2 distinct");
    expect(qty).toHaveTextContent("min 1");
    expect(qty).toHaveTextContent("max 3");
    expect(await screen.findByTestId("stats-note")).toHaveTextContent("a ×2");
  });
});

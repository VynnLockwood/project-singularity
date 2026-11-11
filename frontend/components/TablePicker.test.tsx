import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TablePicker } from "./TablePicker";

const schemas = [{ name: "master", tables: 3, comment: null }, { name: "empty", tables: 0, comment: null }];
const tables = [{ name: "customer", rowEstimate: 1200 }, { name: "order_header", rowEstimate: 0 }, { name: "Product" }];

function setup(over: Partial<Parameters<typeof TablePicker>[0]> = {}) {
  const onSchema = vi.fn(), onTable = vi.fn();
  render(<TablePicker schemas={schemas} schema="master" tables={tables} table="customer" onSchema={onSchema} onTable={onTable} {...over} />);
  return { onSchema, onTable };
}

describe("TablePicker", () => {
  it("lists databases and tables and marks the selected one", () => {
    setup();
    expect(screen.getByRole("combobox", { name: "Database" })).toHaveValue("master");
    expect(screen.getByRole("option", { name: "empty" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /customer/ })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("button", { name: /order_header/ })).not.toHaveAttribute("aria-current");
  });
  it("search narrows the list, case-insensitively", () => {
    setup();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search tables" }), { target: { value: "PROD" } });
    expect(screen.getByRole("button", { name: /Product/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /customer/ })).toBeNull();
  });
  it("says when nothing matches or the database is empty", () => {
    setup();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search tables" }), { target: { value: "zzz" } });
    expect(screen.getByText("No table matches.")).toBeInTheDocument();
  });
  it("empty database", () => {
    setup({ schema: "empty", tables: [], table: "" });
    expect(screen.getByText("No tables in this database.")).toBeInTheDocument();
  });
  it("choosing a table and a database call back", () => {
    const { onSchema, onTable } = setup();
    fireEvent.click(screen.getByRole("button", { name: /order_header/ }));
    expect(onTable).toHaveBeenCalledWith("order_header");
    fireEvent.change(screen.getByRole("combobox", { name: "Database" }), { target: { value: "empty" } });
    expect(onSchema).toHaveBeenCalledWith("empty");
  });
  it("sits in a Tables disclosure that closes after a choice", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: true }));
    setup();
    const d = screen.getByText("Tables").closest("details")!;
    d.open = true;
    fireEvent.click(within(d).getByRole("button", { name: /Product/ }));
    expect(d.open).toBe(false);
  });
});


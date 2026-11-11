import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TableList } from "./TableList";

const tables = [{ name: "customer", rowEstimate: 1200 }, { name: "Product" }, { name: "<b>x</b>" }];
function setup(over: Partial<Parameters<typeof TableList>[0]> = {}) {
  const onSelect = vi.fn();
  render(<TableList tables={tables} selected="customer" onSelect={onSelect} searchLabel="Filter tables"
    emptyText="No tables yet." noMatchText="No matching tables." listLabel="Tables" {...over} />);
  return onSelect;
}

describe("TableList", () => {
  it("search narrows case-insensitively", () => {
    setup();
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter tables" }), { target: { value: "PROD" } });
    expect(screen.getByRole("button", { name: "Product" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /customer/ })).toBeNull();
  });
  it("marks the selected one", () => {
    setup({ estimates: false });
    expect(screen.getByRole("button", { name: "customer" })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("button", { name: "Product" })).not.toHaveAttribute("aria-current");
  });
  it("says when empty or when nothing matches", () => {
    setup({ tables: [] });
    expect(screen.getByText("No tables yet.")).toBeInTheDocument();
  });
  it("no match text", () => {
    setup();
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter tables" }), { target: { value: "zzz" } });
    expect(screen.getByText("No matching tables.")).toBeInTheDocument();
  });
  it("without estimates the accessible name is exactly the table name", () => {
    setup({ estimates: false });
    expect(screen.getByRole("button", { name: "customer" })).toBeInTheDocument();
  });
  it("with estimates the count is shown", () => {
    setup({ estimates: true });
    expect(screen.getByText("1,200")).toBeInTheDocument();
  });
  it("clicking selects", () => {
    const onSelect = setup();
    fireEvent.click(screen.getByRole("button", { name: /Product/ }));
    expect(onSelect).toHaveBeenCalledWith("Product");
  });
  it("names render as text", () => {
    setup();
    expect(screen.getByText("<b>x</b>")).toBeInTheDocument();
    expect(document.querySelector("b")).toBeNull();
  });
  it("labels the list", () => {
    setup();
    expect(screen.getByRole("list", { name: "Tables" })).toBeInTheDocument();
  });
});

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { EMPTY_VIEW, type DataView } from "@/lib/dataRoute";
import { FilterBar } from "./FilterBar";

function setup(view: DataView = EMPTY_VIEW) {
  const onChange = vi.fn();
  render(<FilterBar columns={["name", "note"]} view={view} onChange={onChange} />);
  return onChange;
}
const text = () => screen.getByRole("textbox", { name: "Filter text" });

describe("FilterBar", () => {
  it("Enter adds a filter", () => {
    const onChange = setup();
    fireEvent.change(text(), { target: { value: "abc" } });
    fireEvent.keyDown(text(), { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith({ ...EMPTY_VIEW, filters: { name: "abc" } });
  });
  it("the Add button does the same, for the chosen column", () => {
    const onChange = setup();
    fireEvent.change(screen.getByRole("combobox", { name: "Filter column" }), { target: { value: "note" } });
    fireEvent.change(text(), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Add filter" }));
    expect(onChange).toHaveBeenCalledWith({ ...EMPTY_VIEW, filters: { note: "x" } });
  });
  it("empty text adds nothing", () => {
    const onChange = setup();
    fireEvent.keyDown(text(), { key: "Enter" });
    expect(onChange).not.toHaveBeenCalled();
  });
  it("shows chips and removes only that one", () => {
    const onChange = setup({ ...EMPTY_VIEW, filters: { name: "a", note: "b" } });
    expect(screen.getByText("name contains 'a'")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove filter name" }));
    expect(onChange).toHaveBeenCalledWith({ ...EMPTY_VIEW, filters: { note: "b" } });
  });
  it("a second filter on the same column replaces the first", () => {
    const onChange = setup({ ...EMPTY_VIEW, filters: { name: "a" } });
    fireEvent.change(text(), { target: { value: "z" } });
    fireEvent.keyDown(text(), { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith({ ...EMPTY_VIEW, filters: { name: "z" } });
  });
  it("sort chip shows direction and clears the sort", () => {
    const onChange = setup({ ...EMPTY_VIEW, sort: "name", dir: "desc" });
    expect(screen.getByText("Sorted by name ↓")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove sort" }));
    expect(onChange).toHaveBeenCalledWith({ ...EMPTY_VIEW, sort: null, dir: "asc" });
  });
  it("Clear all empties filters and sort but keeps inactive", () => {
    const onChange = setup({ sort: "name", dir: "desc", inactive: true, filters: { name: "a" } });
    fireEvent.click(screen.getByRole("button", { name: "Clear all" }));
    expect(onChange).toHaveBeenCalledWith({ sort: null, dir: "asc", inactive: true, filters: {} });
  });
  it("no chips, no Clear all", () => {
    setup();
    expect(screen.queryByRole("button", { name: "Clear all" })).toBeNull();
  });
  it("chip text is text, not markup", () => {
    setup({ ...EMPTY_VIEW, filters: { name: "<b>x</b>" } });
    expect(screen.getByText("name contains '<b>x</b>'")).toBeInTheDocument();
    expect(document.querySelector("b")).toBeNull();
  });
});

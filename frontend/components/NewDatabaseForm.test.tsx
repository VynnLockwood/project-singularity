import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Change, Me } from "@/lib/api";

vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), proposeChange: vi.fn() }));
import { proposeChange } from "@/lib/api";
import { NewDatabaseForm } from "./NewDatabaseForm";

const me: Me = { id: 1, email: "ed@example.com", name: "", role: "editor" };
const change: Change = {
  id: 7, status: "pending", kind: "schema", source: "web", requester: "ed@example.com", prompt: "t",
  createdAt: "", decidedBy: null, error: "", decisionNote: "", tables: [], diff: ["+ x"], check: {},
};
const setup = () => {
  const props = { me, existing: ["master", "crm"], onClose: vi.fn(), onProposed: vi.fn() };
  render(<NewDatabaseForm {...props} />);
  return props;
};
const type = (v: string) => fireEvent.change(screen.getByLabelText("Name"), { target: { value: v } });

beforeEach(() => vi.mocked(proposeChange).mockReset());

describe("NewDatabaseForm", () => {
  it("starts with submit disabled and no error", () => {
    setup();
    expect(screen.getByRole("button", { name: "Review" })).toBeDisabled();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it.each([["Sales"], ["my db"], ["public"], ["pg_x"], ["crm"], ["a".repeat(64)]])("refuses %s with an inline error", (name) => {
    setup();
    type(name);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review" })).toBeDisabled();
  });

  it("a valid name opens the dialog and Propose sends exactly create_schema on master", async () => {
    vi.mocked(proposeChange).mockResolvedValue(change);
    const p = setup();
    type("sales");
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    await waitFor(() => expect(p.onProposed).toHaveBeenCalledWith(change, "sales"));
    expect(proposeChange).toHaveBeenCalledWith([{ op: "create_schema", name: "sales" }], expect.any(String), "web", "master");
  });

  it("includes the trimmed comment when given", async () => {
    vi.mocked(proposeChange).mockResolvedValue(change);
    setup();
    type("sales");
    fireEvent.change(screen.getByLabelText("Comment (optional)"), { target: { value: " Sales data " } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    await waitFor(() => expect(proposeChange).toHaveBeenCalled());
    expect(vi.mocked(proposeChange).mock.calls[0][0]).toEqual([{ op: "create_schema", name: "sales", comment: "Sales data" }]);
  });

  it("keeps the status card when existing later includes the created name", async () => {
    vi.mocked(proposeChange).mockResolvedValue(change);
    const props = { me, existing: ["master"], onClose: vi.fn(), onProposed: vi.fn() };
    const r = render(<NewDatabaseForm {...props} />);
    type("sales");
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    await screen.findByRole("status");
    r.rerender(<NewDatabaseForm {...props} existing={["master", "sales"]} />);
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("Cancel closes", () => {
    const p = setup();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(p.onClose).toHaveBeenCalled();
  });
});

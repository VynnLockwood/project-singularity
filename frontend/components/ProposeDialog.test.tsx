import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type Change, type Me } from "@/lib/api";
import { ProposeDialog } from "./ProposeDialog";

vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), proposeChange: vi.fn() }));
import { proposeChange } from "@/lib/api";

const me: Me = { id: 1, email: "ed@example.com", name: "", role: "editor" };
const change = (over: Partial<Change> = {}): Change => ({
  id: 7, status: "pending", kind: "schema", source: "web", requester: "ed@example.com", prompt: "t",
  createdAt: "", decidedBy: null, error: "", decisionNote: "", tables: [], diff: ["+ x"], check: {}, ...over,
});
const setup = (extra: Partial<React.ComponentProps<typeof ProposeDialog>> = {}) => {
  const props = {
    me, schema: "crm", title: "Create database sales",
    build: vi.fn((r: string) => [{ op: "create_schema", name: "sales", reason: r }]),
    onClose: vi.fn(), onProposed: vi.fn(), ...extra,
  };
  render(<ProposeDialog {...props} />);
  return props;
};

beforeEach(() => vi.mocked(proposeChange).mockClear());

describe("ProposeDialog", () => {
  it("is an accessible dialog, focuses itself and lists operation lines", () => {
    setup();
    const d = screen.getByRole("dialog", { name: "Create database sales" });
    expect(d).toHaveAttribute("aria-modal", "true");
    expect(d.contains(document.activeElement)).toBe(true);
    expect(screen.getByText("Create database sales", { selector: "li" })).toBeInTheDocument();
  });

  it("destructive: needs a trimmed reason of 3+ chars and passes it to build", () => {
    const p = setup({ destructive: true });
    const btn = screen.getByRole("button", { name: "Propose" });
    const input = screen.getByLabelText("Reason");
    expect(btn).toBeDisabled();
    fireEvent.change(input, { target: { value: " ab " } });
    expect(btn).toBeDisabled();
    expect(screen.getByText(/at least 3/i)).toBeInTheDocument();
    fireEvent.change(input, { target: { value: " abc " } });
    expect(btn).toBeEnabled();
    expect(p.build).toHaveBeenLastCalledWith("abc");
  });

  it("non-destructive: no reason field, build gets empty string", () => {
    const p = setup();
    expect(screen.queryByLabelText("Reason")).toBeNull();
    expect(p.build).toHaveBeenCalledWith("");
  });

  it("proposes, shows the change card, fires onProposed once", async () => {
    vi.mocked(proposeChange).mockResolvedValue(change());
    const p = setup();
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    expect(proposeChange).toHaveBeenCalledWith(
      [{ op: "create_schema", name: "sales", reason: "" }], "Create database sales", "web", "crm");
    expect(await screen.findByLabelText("Change #7")).toBeInTheDocument();
    expect(p.onProposed).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(p.onClose).toHaveBeenCalled();
  });

  it("a refused proposal keeps the first step, the reason and the Propose button", async () => {
    vi.mocked(proposeChange).mockRejectedValueOnce(new ApiError(422, "Table still has rows"));
    const p = setup({ destructive: true });
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: (screen.getByLabelText("Reason") as HTMLInputElement).value + "cleanup" } });
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Table still has rows");
    expect(screen.getByLabelText("Reason")).toHaveValue("cleanup");
    await waitFor(() => expect(screen.getByRole("button", { name: "Propose" })).toBeEnabled());
    expect(p.onProposed).not.toHaveBeenCalled();
  });

  it("a network failure shows a generic message", async () => {
    vi.mocked(proposeChange).mockRejectedValueOnce(new TypeError("fetch failed"));
    setup();
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not reach the server/i);
  });

  it("a returned change with blockers is shown as-is", async () => {
    vi.mocked(proposeChange).mockResolvedValue(change({ status: "rejected", check: { errors: ["nope"] } }));
    setup();
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    expect(await screen.findByLabelText("Change #7")).toBeInTheDocument();
  });

  it("Close and Escape call onClose", async () => {
    const p = setup();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(p.onClose).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(p.onClose).toHaveBeenCalledTimes(2);
  });
});

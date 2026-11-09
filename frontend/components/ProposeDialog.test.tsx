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

beforeEach(() => vi.mocked(proposeChange).mockReset());

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
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "cleanup" } });
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

  it("a returned rejected change is shown as an error on the form, not as a card", async () => {
    vi.mocked(proposeChange).mockResolvedValue(change({ status: "rejected", check: { errors: ["nope"] } }));
    setup();
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("nope");
    expect(screen.queryByLabelText("Change #7")).toBeNull();
  });

  it("Close and Escape call onClose", async () => {
    const p = setup();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(p.onClose).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(p.onClose).toHaveBeenCalledTimes(2);
  });

  it("double click proposes once; Close and Escape are ignored while busy", async () => {
    let resolve!: (c: Change) => void;
    vi.mocked(proposeChange).mockReturnValue(new Promise<Change>((r) => (resolve = r)));
    const p = setup();
    const btn = screen.getByRole("button", { name: "Propose" });
    fireEvent.click(btn);
    fireEvent.click(btn);
    expect(proposeChange).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Close" })).toBeDisabled();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(p.onClose).not.toHaveBeenCalled();
    resolve(change());
    expect(await screen.findByLabelText("Change #7")).toBeInTheDocument();
    expect(p.onProposed).toHaveBeenCalledTimes(1);
  });

  it("Escape closes after success even when focus is on body", async () => {
    vi.mocked(proposeChange).mockResolvedValue(change());
    const p = setup();
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    await screen.findByLabelText("Change #7");
    (document.activeElement as HTMLElement | null)?.blur();
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(p.onClose).toHaveBeenCalledTimes(1);
  });

  it("unmounting mid-request does not throw", async () => {
    let resolve!: (c: Change) => void;
    vi.mocked(proposeChange).mockReturnValue(new Promise<Change>((r) => (resolve = r)));
    const props = {
      me, schema: "crm", title: "T", build: () => [{ op: "create_schema", name: "s" }], onClose: vi.fn(), onProposed: vi.fn(),
    };
    const { unmount } = render(<ProposeDialog {...props} />);
    fireEvent.click(screen.getAllByRole("button", { name: "Propose" }).at(-1)!);
    unmount();
    resolve(change());
    await Promise.resolve();
    await Promise.resolve();
  });

  it("a throwing onProposed does not show the network error", async () => {
    vi.mocked(proposeChange).mockResolvedValue(change());
    const onProposed = vi.fn(() => { throw new Error("parent"); });
    const err = vi.spyOn(console, "error").mockImplementation(() => {});
    const onUnhandled = vi.fn();
    process.on("unhandledRejection", onUnhandled);
    setup({ onProposed });
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    await screen.findByLabelText("Change #7");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(onProposed).toHaveBeenCalledTimes(1);
    await new Promise((r) => setTimeout(r, 0));
    process.off("unhandledRejection", onUnhandled);
    err.mockRestore();
  });

  it("empty operations disable Propose", () => {
    setup({ build: () => [] });
    expect(screen.getByRole("button", { name: "Propose" })).toBeDisabled();
  });

  it("a rejected create stays on the form with the reason and violations, calls onProposed", async () => {
    const refused = change({ status: "rejected", diff: [], check: { violations: [{ severity: "block", message: "nope", suggestion: "try later" }] } });
    vi.mocked(proposeChange).mockResolvedValue(refused);
    const p = setup({ destructive: true });
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "cleanup" } });
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("nope try later");
    expect(p.onProposed).toHaveBeenCalledWith(refused);
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByLabelText("Reason")).toHaveValue("cleanup");
  });

  it("Close uses onBack when given, onClose otherwise; after success Close is onClose", async () => {
    const onBack = vi.fn();
    const p = setup({ onBack });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onBack).toHaveBeenCalled();
    expect(p.onClose).not.toHaveBeenCalled();
    vi.mocked(proposeChange).mockResolvedValue(change());
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    await screen.findByRole("status");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(p.onClose).toHaveBeenCalled();
  });

  it("Escape after a successful create calls onClose, not onBack", async () => {
    vi.mocked(proposeChange).mockResolvedValue(change());
    const onBack = vi.fn();
    const p = setup({ onBack });
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    await screen.findByRole("status");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(p.onClose).toHaveBeenCalled();
    expect(onBack).not.toHaveBeenCalled();
  });
});

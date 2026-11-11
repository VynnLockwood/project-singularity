import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/api";
import type { Change, Me } from "@/lib/api";
import { change } from "@/lib/fixtures";
import { HistoryView } from "./HistoryView";

vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  listChanges: vi.fn(),
  revertChange: vi.fn(),
  getSchemas: vi.fn(async () => [{ name: "master", tables: 1, comment: null }, { name: "sales", tables: 1, comment: null }]),
}));
const listChanges = vi.mocked(api.listChanges);
const revertChange = vi.mocked(api.revertChange);

const me = (role: Me["role"] = "approver"): Me => ({ id: 1, email: "ap@x.com", name: "", role });
const applied = (id: number, over: Partial<Change> = {}) => change({ id, status: "applied", kind: "data", schema: "master", requester: "ed@x.com", prompt: `prompt ${id}`, reversible: true, ...over });
const page = (changes: Change[], more = false) => ({ changes, more });

beforeEach(() => {
  vi.clearAllMocks();
  history.replaceState(null, "", "/activity/history");
  listChanges.mockResolvedValue(page([applied(3), applied(2)]));
});

describe("HistoryView list", () => {
  it("loads once with default filters and shows a row per change", async () => {
    render(<HistoryView me={me()} live={[]} />);
    expect(await screen.findByText("prompt 3")).toBeInTheDocument();
    expect(listChanges).toHaveBeenCalledTimes(1);
    expect(listChanges).toHaveBeenCalledWith({}, 50);
    const row = screen.getByText("prompt 3").closest("button")!;
    expect(row).toHaveTextContent("#3");
    expect(row).toHaveTextContent("Applied");
    expect(row).toHaveTextContent("ed@x.com");
    expect(row).toHaveTextContent("master");
  });

  it("each filter changes the request, resets the limit and the address", async () => {
    render(<HistoryView me={me()} live={[]} />);
    await screen.findByText("prompt 3");
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "failed" } });
    await waitFor(() => expect(listChanges).toHaveBeenLastCalledWith({ status: "failed" }, 50));
    fireEvent.change(screen.getByLabelText("Kind"), { target: { value: "sql" } });
    await waitFor(() => expect(listChanges).toHaveBeenLastCalledWith({ status: "failed", kind: "sql" }, 50));
    fireEvent.change(await screen.findByLabelText("Database"), { target: { value: "sales" } });
    await waitFor(() => expect(listChanges).toHaveBeenLastCalledWith({ status: "failed", kind: "sql", schema: "sales" }, 50));
    fireEvent.click(screen.getByLabelText("Only my changes"));
    await waitFor(() => expect(listChanges).toHaveBeenLastCalledWith({ status: "failed", kind: "sql", schema: "sales", mine: true }, 50));
    expect(location.search).toBe("?status=failed&kind=sql&db=sales&mine=1");
  });

  it("starts from the address, and treats junk there as 'any'", async () => {
    history.replaceState(null, "", "/activity/history?status=applied&kind=nope&mine=maybe");
    render(<HistoryView me={me()} live={[]} />);
    await screen.findByText("prompt 3");
    expect(listChanges).toHaveBeenCalledWith({ status: "applied" }, 50);
    expect(screen.getByLabelText("Status")).toHaveValue("applied");
    expect(screen.getByLabelText("Kind")).toHaveValue("");
  });

  it("shows loading, empty (with Clear filters) and error states", async () => {
    let resolve!: (v: ReturnType<typeof page>) => void;
    listChanges.mockReturnValueOnce(new Promise((r) => { resolve = r; }));
    const { container } = render(<HistoryView me={me()} live={[]} />);
    expect(container.querySelector(".ui-skeleton")).not.toBeNull();
    await act(async () => resolve(page([])));
    expect(await screen.findByText("No changes match these filters.")).toBeInTheDocument();
    listChanges.mockResolvedValueOnce(page([applied(1)]));
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByText("prompt 1")).toBeInTheDocument();

    listChanges.mockRejectedValueOnce(new api.ApiError(500, "boom"));
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "stale" } });
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    listChanges.mockResolvedValueOnce(page([applied(1)]));
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("prompt 1")).toBeInTheDocument();
  });

  it("ignores a slow answer for an earlier filter", async () => {
    let first!: (v: ReturnType<typeof page>) => void;
    listChanges.mockReset();
    listChanges.mockReturnValueOnce(new Promise((r) => { first = r; }));
    listChanges.mockResolvedValueOnce(page([applied(8, { prompt: "the new one" })]));
    render(<HistoryView me={me()} live={[]} />);
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "applied" } });
    expect(await screen.findByText("the new one")).toBeInTheDocument();
    await act(async () => first(page([applied(1, { prompt: "the old one" })])));
    expect(screen.queryByText("the old one")).toBeNull();
    expect(screen.getByText("the new one")).toBeInTheDocument();
  });
});

describe("HistoryView Show more", () => {
  it("appears only when the server says more exist, raises the limit by 50, and keeps rows when it fails", async () => {
    listChanges.mockResolvedValueOnce(page([applied(3)], true));
    render(<HistoryView me={me()} live={[]} />);
    await screen.findByText("prompt 3");
    listChanges.mockRejectedValueOnce(new api.ApiError(500, "no luck"));
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    await waitFor(() => expect(listChanges).toHaveBeenLastCalledWith({}, 100));
    expect(await screen.findByRole("alert")).toHaveTextContent("no luck");
    expect(screen.getByText("prompt 3")).toBeInTheDocument();
  });

  it("is absent when nothing more exists", async () => {
    render(<HistoryView me={me()} live={[]} />);
    await screen.findByText("prompt 3");
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
  });

  it("stops at the server's cap of 200 and says to narrow the filters", async () => {
    listChanges.mockResolvedValue(page([applied(3)], true));
    render(<HistoryView me={me()} live={[]} />);
    for (const want of [100, 150, 200]) {
      fireEvent.click(await screen.findByRole("button", { name: "Show more" }));
      await waitFor(() => expect(listChanges).toHaveBeenLastCalledWith({}, want));
    }
    expect(await screen.findByText(/Showing the latest 200\. Narrow the filters/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
  });
});

describe("HistoryView rows and live changes", () => {
  it("expands a decided change into its card without Approve or Reject", async () => {
    render(<HistoryView me={me()} live={[]} />);
    fireEvent.click((await screen.findByText("prompt 3")).closest("button")!);
    const card = screen.getByRole("region", { name: "Change #3" });
    expect(within(card).queryByRole("button", { name: /Approve|Reject/ })).toBeNull();
  });

  it("adds a matching live change on top, skips a non-matching one and updates a listed row", async () => {
    listChanges.mockResolvedValue(page([applied(3, { status: "pending" })]));
    const { rerender } = render(<HistoryView me={me()} live={[]} />);
    await screen.findByText("prompt 3");
    rerender(<HistoryView me={me()} live={[applied(9, { prompt: "fresh" }), applied(10, { kind: "sql", prompt: "other" }), applied(3, { prompt: "prompt 3" })]} />);
    expect(await screen.findByText("fresh")).toBeInTheDocument();
    expect(screen.getByText("prompt 3").closest("button")).toHaveTextContent("Applied");
    const order = screen.getAllByRole("button", { name: /^#\d+/ }).map((b) => b.textContent);
    expect(order[0]).toContain("#10");
  });

  it("does not add a live change that fails the filters", async () => {
    history.replaceState(null, "", "/activity/history?kind=data");
    const { rerender } = render(<HistoryView me={me()} live={[]} />);
    await screen.findByText("prompt 3");
    rerender(<HistoryView me={me()} live={[applied(10, { kind: "sql", prompt: "other" })]} />);
    expect(screen.queryByText("other")).toBeNull();
  });
});

describe("HistoryView revert", () => {
  const open = async (role: Me["role"], rows: Change[]) => {
    listChanges.mockResolvedValue(page(rows));
    render(<HistoryView me={me(role)} live={[]} />);
    fireEvent.click((await screen.findByText(`prompt ${rows[0].id}`)).closest("button")!);
  };

  it("is shown to an approver for an applied, reversible change", async () => {
    await open("approver", [applied(3)]);
    expect(screen.getByRole("button", { name: /Revert/ })).toBeInTheDocument();
  });

  it.each(["editor", "viewer"] as const)("is never shown to a %s", async (role) => {
    await open(role, [applied(3)]);
    expect(screen.queryByRole("button", { name: /Revert/ })).toBeNull();
  });

  it("is hidden when the change is not reversible or not applied", async () => {
    await open("approver", [applied(3, { reversible: false })]);
    expect(screen.queryByRole("button", { name: /Revert/ })).toBeNull();
  });

  it("is hidden once a revert of it exists", async () => {
    await open("approver", [applied(5, { reverts: 3 }), applied(3)]);
    fireEvent.click(screen.getByText("prompt 3").closest("button")!);
    expect(screen.queryByRole("button", { name: /Revert/ })).toBeNull();
  });

  it("is shown again if the only revert failed", async () => {
    await open("approver", [applied(5, { reverts: 3, status: "failed" }), applied(3)]);
    fireEvent.click(screen.getByText("prompt 3").closest("button")!);
    expect(screen.getByRole("button", { name: /Revert/ })).toBeInTheDocument();
  });

  it("asks first, sends one request even when confirmed twice, then reloads", async () => {
    await open("approver", [applied(3)]);
    revertChange.mockResolvedValue(applied(4, { reverts: 3 }));
    fireEvent.click(screen.getByRole("button", { name: /Revert/ }));
    expect(screen.getByText("Revert change #3?")).toBeInTheDocument();
    expect(revertChange).not.toHaveBeenCalled();
    const yes = screen.getByRole("button", { name: "Yes, revert" });
    fireEvent.click(yes);
    fireEvent.click(yes);
    await waitFor(() => expect(listChanges).toHaveBeenCalledTimes(2));
    expect(revertChange).toHaveBeenCalledTimes(1);
    expect(revertChange).toHaveBeenCalledWith(3);
  });

  it("can be cancelled", async () => {
    await open("approver", [applied(3)]);
    fireEvent.click(screen.getByRole("button", { name: /Revert/ }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(revertChange).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /Revert/ })).toBeInTheDocument();
  });

  it("shows the server's refusal and keeps the row", async () => {
    await open("approver", [applied(3)]);
    revertChange.mockRejectedValue(new api.ApiError(409, "change #3 was already reverted"));
    fireEvent.click(screen.getByRole("button", { name: /Revert/ }));
    fireEvent.click(screen.getByRole("button", { name: "Yes, revert" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already reverted");
    expect(screen.getByText("prompt 3")).toBeInTheDocument();
  });
});

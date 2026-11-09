import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DuplicateDetail, DuplicateSummary, GridColumn, Me } from "@/lib/api";
import { change } from "@/lib/fixtures";

const me = (role: Me["role"]): Me => ({ id: 1, email: "ed@example.com", name: "", role });
const group = (o: Partial<DuplicateSummary> = {}): DuplicateSummary => ({
  id: 5, table: "customer", fields: ["name"], memberIds: [1, 2], score: 0.96, status: "open",
  detectedAt: "2026-11-05T00:00:00Z", mergeChange: null, ...o,
});
const detail: DuplicateDetail = {
  ...group(),
  members: [{ id: 1, name: "Acme Co., Ltd." }, { id: 2, name: "ACME Co Ltd" }],
  referencedBy: [{ table: "orderHeader", column: "customerId" }],
};

async function setup(role: Me["role"] = "editor", groups = [group()]) {
  vi.resetModules();
  const fns = {
    getGraph: vi.fn().mockResolvedValue({ tables: [{ name: "customer" }, { name: "product" }], relations: [] }),
    listDuplicates: vi.fn().mockResolvedValue({ groups, tables: { customer: groups.length } }),
    getDuplicate: vi.fn().mockResolvedValue(detail),
    scanDuplicates: vi.fn().mockResolvedValue({ table: "customer", scanned: 40, truncated: false, groups: 1, new: 1, removed: 0, fields: ["name"] }),
    dismissDuplicate: vi.fn().mockResolvedValue(group({ status: "dismissed" })),
    reopenDuplicate: vi.fn().mockResolvedValue(group()),
    mergeDuplicate: vi.fn().mockResolvedValue({ group: group({ status: "merge_pending" }), change: change({ id: 90, kind: "data", status: "pending" }) }),
  };
  vi.doMock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), ...fns }));
  const { DuplicatesView } = await import("./DuplicatesView");
  const onProposed = vi.fn();
  render(<DuplicatesView me={me(role)} refreshKey={0} onProposed={onProposed} />);
  await screen.findByLabelText("Duplicate groups");
  return { ...fns, onProposed };
}

describe("DuplicatesView", () => {
  beforeEach(() => vi.resetModules());

  it("lists groups for the first table", async () => {
    const { listDuplicates } = await setup();
    await waitFor(() => expect(listDuplicates).toHaveBeenCalledWith("customer"));
    expect(await screen.findByText(/2 records · 96%/)).toBeInTheDocument();
  });

  it("scans and reports the result", async () => {
    const { scanDuplicates } = await setup();
    fireEvent.click(screen.getByText("Scan for duplicates"));
    await waitFor(() => expect(scanDuplicates).toHaveBeenCalledWith("customer"));
    expect(await screen.findByText(/Scanned 40 records/)).toBeInTheDocument();
  });

  it("renders no audit columns, even from a stale server", async () => {
    await setup();
    const stale = { ...detail, primaryKey: "id", hiddenColumns: ["created_at"],
      members: [{ id: 1, name: "A", created_at: "2026-01-01" }, { id: 2, name: "B", created_at: "2026-01-02" }] };
    const api = await import("@/lib/api");
    (api.getDuplicate as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(stale);
    fireEvent.click(await screen.findByText(/2 records · 96%/));
    await screen.findByLabelText("keep record 2");
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["keep", "id", "name"]);
  });

  it("merges into the chosen survivor and hands the change up", async () => {
    const { mergeDuplicate, onProposed } = await setup();
    fireEvent.click(await screen.findByText(/2 records · 96%/));
    fireEvent.click(await screen.findByLabelText("keep record 2"));
    expect(screen.getByText(/orderHeader.customerId/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Merge into the selected record"));
    await waitFor(() => expect(mergeDuplicate).toHaveBeenCalledWith(5, 2));
    expect(onProposed).toHaveBeenCalledWith(expect.objectContaining({ id: 90 }));
  });

  it("dismisses an open group", async () => {
    const { dismissDuplicate } = await setup();
    fireEvent.click(await screen.findByText("Not duplicates"));
    await waitFor(() => expect(dismissDuplicate).toHaveBeenCalledWith(5));
  });

  it("hides dismissed groups until asked, and offers reopen", async () => {
    const { reopenDuplicate } = await setup("editor", [group({ status: "dismissed" })]);
    expect(screen.getByText(/No duplicate groups/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/show dismissed/));
    fireEvent.click(await screen.findByText("Reopen"));
    await waitFor(() => expect(reopenDuplicate).toHaveBeenCalledWith(5));
  });

  it("is read-only for viewers", async () => {
    await setup("viewer");
    expect(screen.queryByText("Scan for duplicates")).toBeNull();
    expect(screen.queryByText("Not duplicates")).toBeNull();
    fireEvent.click(await screen.findByText(/2 records · 96%/));
    await screen.findByLabelText("Duplicate group");
    expect(screen.queryByText("Merge into the selected record")).toBeNull();
  });
});

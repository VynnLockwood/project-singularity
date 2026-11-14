import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, getSchemas, getTable, uploadScriptChunked, type Me } from "@/lib/api";
import { answerImport, cancelImport, getImport, recheckImport, startImport, submitImport, type ImportRun } from "@/lib/imports";
import { ImportView } from "./ImportView";

vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getSchemas: vi.fn(), getTable: vi.fn(), uploadScriptChunked: vi.fn(),
}));
vi.mock("@/lib/imports", async (orig) => ({
  ...(await orig<typeof import("@/lib/imports")>()),
  startImport: vi.fn(), getImport: vi.fn(), answerImport: vi.fn(), recheckImport: vi.fn(), submitImport: vi.fn(), cancelImport: vi.fn(),
}));

const me: Me = { id: 1, email: "e@x.com", name: "E", role: "editor" };

const run = (over: Partial<ImportRun> = {}): ImportRun => ({
  id: 7, schema: "master", state: "ready", prompt: "vendors", fileName: "vendors.csv", round: 1,
  profile: { sheets: [{ name: "vendors", rows: 4, columns: [{ header: "Vendor Name", type: "text" }, { header: "Tax ID", type: "text" }, { header: "Status", type: "text" }, { header: "Credit Limit", type: "numeric" }] }] },
  matches: { sources: { vendors: { rows: { table: "partner", maybe: [{ row: 1, id: 1, score: 0.93, existing: "Acme Company Limited" }] } } } },
  plan: {
    mapping: { tables: { vendors: { target: "partner", columns: { name: "Vendor Name", tax_id: "Tax ID" }, drop: ["Status"], refs: [] } } },
    filters: { vendors: [{ column: "Status", op: "equals", value: "Active" }] },
    reasons: { vendors: "Looks like partners", "vendors.Tax ID": "13 digits" },
    questions: [], picked: {}, skip: {},
  },
  report: {
    ok: true, problems: [], scriptId: 1,
    tables: { vendors: { target: "partner", rows: 4, new: 2, exists: 1, filtered: 1, skipped: 0, maybe: [{ row: 1, id: 1, score: 0.93, existing: "Acme Company Limited" }] } },
    badRows: [{ line: 7, message: "bad" }], dropped: [{ sheet: "vendors", column: "Status", reason: "" }], warnings: [],
  },
  messages: [
    { role: "profiler", text: "vendors: 4 rows", details: { sheets: [] }, at: "2026-11-14T10:00:00Z" },
    { role: "matcher", text: "best table partner", details: {}, at: "2026-11-14T10:00:01Z" },
    { role: "designer", text: "Round 1:\nvendors -> partner", details: { round: 1 }, at: "2026-11-14T10:00:02Z" },
    { role: "critic", text: "Passed.", details: { ok: true, problems: [] }, at: "2026-11-14T10:00:03Z" },
  ],
  changeId: null, expiresAt: "2026-11-15T10:00:00Z", ...over,
});

const route = (r?: number) => ({ view: "import" as const, schema: "master", ...(r ? { run: r } : {}) });
async function setup(r?: number, navigate = vi.fn()) {
  const view = render(<ImportView me={me} route={route(r)} navigate={navigate} />);
  if (!r) await screen.findByRole("combobox", { name: "Database" });
  else await screen.findByText("Messages");
  return { navigate, ...view };
}

describe("ImportView", () => {
  beforeEach(() => {
    vi.mocked(getSchemas).mockResolvedValue([{ name: "master", tables: 3, comment: null }, { name: "crm", tables: 1, comment: null }]);
    vi.mocked(getTable).mockResolvedValue({
      name: "partner", comment: null, constraints: [], indexes: [], hiddenColumns: [],
      columns: ["name", "tax_id", "notes"].map((n) => ({ name: n, type: "text", nullable: true, default: null, comment: null, identity: false })),
    });
    vi.mocked(getImport).mockReset().mockResolvedValue(run());
    vi.mocked(startImport).mockReset().mockResolvedValue(run({ state: "profiling", messages: [] }));
    vi.mocked(uploadScriptChunked).mockReset().mockResolvedValue({ upload_id: 5 } as never);
    vi.mocked(answerImport).mockReset().mockResolvedValue(run({ state: "designing" }));
    vi.mocked(recheckImport).mockReset().mockResolvedValue(run({ state: "checking" }));
    vi.mocked(submitImport).mockReset().mockResolvedValue({ changeId: 42 });
    vi.mocked(cancelImport).mockReset().mockResolvedValue(undefined as never);
  });
  afterEach(() => { cleanup(); vi.useRealTimers(); });

  it("starts with the upload id, schema, prompt and key", async () => {
    const { navigate, container } = await setup();
    const file = new File(["a,b"], "vendors.csv", { type: "text/csv" });
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input.accept).toBe(".xlsx,.csv");
    fireEvent.change(input, { target: { files: [file] } });
    fireEvent.change(screen.getByLabelText("What should it do?"), { target: { value: "load vendors" } });
    const key = screen.getByLabelText("API key (only if not configured)") as HTMLInputElement;
    expect(key.type).toBe("password");
    fireEvent.change(key, { target: { value: "sk-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(startImport).toHaveBeenCalled());
    expect(vi.mocked(uploadScriptChunked).mock.calls[0][0]).toBe(file);
    expect(vi.mocked(uploadScriptChunked).mock.calls[0][3]).toBe("import");
    expect(startImport).toHaveBeenCalledWith({ uploadId: 5, schema: "master", prompt: "load vendors", apiKey: "sk-1" });
    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ view: "import", schema: "master", run: 7 }));
    expect(window.localStorage.length).toBe(0);
  });

  it("renders the thread in order with role labels and folded details", async () => {
    await setup(7);
    const items = screen.getAllByRole("listitem", { name: /message/i });
    expect(items.map((i) => within(i).getByTestId("role").textContent)).toEqual(["Profiler", "Matcher", "Designer", "Critic"]);
    expect(within(items[0]).getByText("vendors: 4 rows")).toBeInTheDocument();
    const d = items[0].querySelector("details") as HTMLDetailsElement;
    expect(d.open).toBe(false);
  });

  it("shows the mapping, reasons, counts and the maybe list", async () => {
    await setup(7);
    expect(await screen.findByLabelText("Target for Vendor Name")).toHaveValue("name");
    expect(screen.getByLabelText("Target for Tax ID")).toHaveValue("tax_id");
    expect(screen.getByLabelText("Target for Status")).toHaveValue("__drop__");
    expect(screen.getByText("13 digits")).toBeInTheDocument();
    expect(screen.getByText("Looks like partners")).toBeInTheDocument();
    const counts = screen.getByLabelText("Counts");
    expect(counts).toHaveTextContent(/New\s*2/);
    expect(counts).toHaveTextContent(/Already exist \(skipped\)\s*1/);
    expect(counts).toHaveTextContent(/Removed by filter\s*1/);
    expect(counts).toHaveTextContent(/Bad rows\s*1/);
    expect(counts).toHaveTextContent(/Dropped columns\s*1/);
    expect(screen.getByLabelText("Skip row 1")).not.toBeChecked();
    expect(screen.getByText(/Acme Company Limited/)).toBeInTheDocument();
  });

  it("polls every 1.5 s and stops at ready", async () => {
    vi.useFakeTimers();
    vi.mocked(getImport)
      .mockResolvedValueOnce(run({ state: "profiling", report: null, plan: null }))
      .mockResolvedValueOnce(run({ state: "designing", report: null, plan: null }))
      .mockResolvedValue(run());
    render(<ImportView me={me} route={route(7)} navigate={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(getImport).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1400); });
    expect(getImport).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    expect(getImport).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(getImport).toHaveBeenCalledTimes(3);
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(getImport).toHaveBeenCalledTimes(3);
  });

  it("stops polling on unmount and ignores a late answer", async () => {
    vi.useFakeTimers();
    let late!: (r: ImportRun) => void;
    vi.mocked(getImport)
      .mockResolvedValueOnce(run({ state: "matching" }))
      .mockImplementationOnce(() => new Promise<ImportRun>((r) => { late = r; }));
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    const { unmount } = render(<ImportView me={me} route={route(7)} navigate={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1600); });
    expect(getImport).toHaveBeenCalledTimes(2);
    unmount();
    await act(async () => { late(run()); await vi.advanceTimersByTimeAsync(10_000); });
    expect(getImport).toHaveBeenCalledTimes(2);
    expect(spy).not.toHaveBeenCalled();
  });

  it("rechecks with the edited mapping, the filters and the rows to skip", async () => {
    await setup(7);
    fireEvent.change(await screen.findByLabelText("Target for Tax ID"), { target: { value: "notes" } });
    fireEvent.click(screen.getByLabelText("Skip row 1"));
    fireEvent.click(screen.getByRole("button", { name: "Re-check" }));
    await waitFor(() => expect(recheckImport).toHaveBeenCalled());
    expect(recheckImport).toHaveBeenCalledWith(7, {
      mapping: { tables: { vendors: { target: "partner", columns: { name: "Vendor Name", notes: "Tax ID" }, drop: ["Status", "Credit Limit"], refs: [] } } },
      filters: { vendors: [{ column: "Status", op: "equals", value: "Active" }] },
      skip: { vendors: [1] },
    });
  });

  it("keeps Re-check off until something is edited, and Submit off after an edit", async () => {
    await setup(7);
    await screen.findByLabelText("Target for Tax ID");
    expect(screen.getByRole("button", { name: "Re-check" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Submit for approval" })).toBeEnabled();
    fireEvent.click(screen.getByLabelText("Skip row 1"));
    expect(screen.getByRole("button", { name: "Re-check" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Submit for approval" })).toBeDisabled();
  });

  it("answers a question", async () => {
    vi.mocked(getImport).mockResolvedValue(run({
      state: "waiting_for_answer",
      plan: { ...run().plan!, questions: ["Which table holds vendors?"] },
    }));
    await setup(7);
    expect(screen.getByText("Which table holds vendors?")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Your answer"), { target: { value: "partner" } });
    fireEvent.click(screen.getByRole("button", { name: "Send answer" }));
    await waitFor(() => expect(answerImport).toHaveBeenCalledWith(7, "partner", ""));
  });

  it("submits and links to the change", async () => {
    vi.mocked(getImport).mockResolvedValueOnce(run()).mockResolvedValue(run({ state: "submitted", changeId: 42 }));
    const { navigate } = await setup(7);
    await screen.findByLabelText("Target for Tax ID");
    fireEvent.click(screen.getByRole("button", { name: "Submit for approval" }));
    await waitFor(() => expect(submitImport).toHaveBeenCalledWith(7));
    const link = await screen.findByRole("link", { name: /change #42/i });
    expect(link).toHaveAttribute("href", "/activity/history");
    fireEvent.click(link);
    expect(navigate).toHaveBeenCalledWith({ view: "activity", tab: "history" });
  });

  it("shows the critic's last problems when failed", async () => {
    vi.mocked(getImport).mockResolvedValue(run({
      state: "failed", report: { ...run().report!, ok: false, problems: ["line 3: bad tax id"] },
      messages: [...run().messages, { role: "critic", text: "1 problem", details: { ok: false, problems: ["line 3: bad tax id"] }, at: "2026-11-14T10:01:00Z" }],
    }));
    await setup(7);
    const box = screen.getByRole("alert");
    expect(box).toHaveTextContent("line 3: bad tax id");
    expect(screen.getByRole("button", { name: "Submit for approval" })).toBeDisabled();
  });

  it("disables Cancel while running and cancels otherwise", async () => {
    vi.mocked(getImport).mockResolvedValue(run({ state: "designing" }));
    const first = await setup(7);
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    first.unmount();
    vi.mocked(getImport).mockResolvedValue(run());
    const { navigate } = await setup(7);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(cancelImport).toHaveBeenCalledWith(7));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ view: "import", schema: "master" }));
  });

  it("shows a refusal such as busy", async () => {
    vi.mocked(startImport).mockRejectedValue(new ApiError(409, "You already have an import open.", "busy"));
    const { container } = await setup();
    fireEvent.change(container.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [new File(["x"], "a.csv")] } });
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("You already have an import open.");
  });

  it("adds a new column with a name and type as add_columns", async () => {
    await setup(7);
    fireEvent.change(await screen.findByLabelText("Target for Credit Limit"), { target: { value: "__new__" } });
    expect(screen.getByLabelText("New column name for Credit Limit")).toHaveValue("credit_limit");
    expect(screen.getByLabelText("Type of Credit Limit")).toHaveValue("numeric");
    fireEvent.change(screen.getByLabelText("Type of Credit Limit"), { target: { value: "integer" } });
    fireEvent.click(screen.getByRole("button", { name: "Re-check" }));
    await waitFor(() => expect(recheckImport).toHaveBeenCalled());
    expect(vi.mocked(recheckImport).mock.calls[0][1].mapping).toEqual({
      tables: { vendors: {
        target: "partner", columns: { name: "Vendor Name", tax_id: "Tax ID", credit_limit: "Credit Limit" }, drop: ["Status"], refs: [],
        add_columns: [{ name: "credit_limit", type: "integer", nullable: true }],
      } },
    });
  });

  it("refuses a bad or clashing new column name", async () => {
    await setup(7);
    fireEvent.change(await screen.findByLabelText("Target for Credit Limit"), { target: { value: "__new__" } });
    const name = screen.getByLabelText("New column name for Credit Limit");
    fireEvent.change(name, { target: { value: "Name" } });
    expect(screen.getByText(/lowercase snake_case/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Re-check" })).toBeDisabled();
    fireEvent.change(name, { target: { value: "notes" } });
    expect(screen.getByText(/already exists/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Re-check" })).toBeDisabled();
  });

  it("offers no new column when the target table does not exist", async () => {
    vi.mocked(getTable).mockRejectedValue(new ApiError(404, "no table"));
    await setup(7);
    await screen.findByLabelText("Target for Tax ID");
    expect(within(screen.getByLabelText("Target for Tax ID")).queryByRole("option", { name: "new column" })).toBeNull();
  });

  it("keeps a ticked maybe row visible after re-check, and unticking sends it out of skip", async () => {
    // the backend leaves a ticked row out of the report's maybe list
    const after = run({
      plan: { ...run().plan!, picked: { vendors: [1] }, skip: { vendors: [1] } },
      report: { ...run().report!, tables: { vendors: { ...run().report!.tables!.vendors, maybe: [], skipped: 1 } } },
    });
    vi.mocked(recheckImport).mockResolvedValue(after);
    await setup(7);
    fireEvent.click(await screen.findByLabelText("Skip row 1"));
    fireEvent.click(screen.getByRole("button", { name: "Re-check" }));
    await waitFor(() => expect(recheckImport).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByLabelText("Skip row 1")).toBeChecked());
    fireEvent.click(screen.getByLabelText("Skip row 1"));
    expect(screen.getByLabelText("Skip row 1")).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Re-check" }));
    await waitFor(() => expect(recheckImport).toHaveBeenCalledTimes(2));
    expect(vi.mocked(recheckImport).mock.calls[1][1].skip).toEqual({ vendors: [] });
  });

  it("refetches the run after a rejected submit and shows the failure", async () => {
    vi.mocked(getImport).mockResolvedValueOnce(run()).mockResolvedValue(run({
      state: "failed", report: { ...run().report!, ok: false, problems: ["the plan no longer fits"] },
    }));
    vi.mocked(submitImport).mockRejectedValue(new ApiError(400, "the plan no longer passes the check", "invalid"));
    await setup(7);
    await screen.findByLabelText("Target for Tax ID");
    fireEvent.click(screen.getByRole("button", { name: "Submit for approval" }));
    await waitFor(() => expect(getImport).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getAllByRole("alert").map((a) => a.textContent).join(" ")).toMatch(/the plan no longer fits/));
    expect(screen.getByRole("button", { name: "Submit for approval" })).toBeDisabled();
  });
});

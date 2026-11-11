import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ScriptsTab } from "./ScriptsTab";

/** /api/schema/graph answers with bare table names in schema `master`. */
const graph = {
  tables: [
    { name: "district", comment: null, rowEstimate: 928, columns: [
      { name: "code", type: "character varying", nullable: false, primaryKey: true },
      { name: "name", type: "text", nullable: false, primaryKey: false }] },
    { name: "subdistrict", comment: null, rowEstimate: 0, columns: [
      { name: "code", type: "character varying", nullable: false, primaryKey: true },
      { name: "name", type: "text", nullable: false, primaryKey: false },
      { name: "postcode", type: "character varying", nullable: true, primaryKey: false },
      { name: "district_id", type: "integer", nullable: false, primaryKey: false }] },
  ],
  relations: [],
};

describe("ScriptsTab destination picker", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      const path = String(url);
      const body = path.includes("/schema/graph") ? graph : path.endsWith("/scripts") ? { scripts: [] } : {};
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    }));
  });
  afterEach(() => vi.unstubAllGlobals());

  function fill(label: string, value: string) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  }

  it("writes an external ref when resolving against an existing table", async () => {
    render(<ScriptsTab canEdit />);

    fill("Table 1 source table", "subdistrict_src");
    fireEvent.click(screen.getByLabelText("Table 1: load into an existing table"));
    await waitFor(() => expect(screen.getByLabelText("Table 1 existing table")).toBeInTheDocument());
    fill("Table 1 existing table", "subdistrict");

    fill("Table 1 field 1 source column", "code");
    fill("Table 1 field 1 destination field", "code");
    fireEvent.click(screen.getByRole("button", { name: "Add field to table 1" }));
    fill("Table 1 field 2 source column", "name");
    fill("Table 1 field 2 destination field", "name");
    fireEvent.click(screen.getByRole("button", { name: "Add field to table 1" }));
    fill("Table 1 field 3 source column", "postcode");
    fill("Table 1 field 3 destination field", "postcode");

    fireEvent.click(screen.getByRole("button", { name: "Add ref to table 1" }));
    fill("Table 1 ref 1 foreign key column", "district_id");
    fill("Table 1 ref 1 source column", "district_code");
    fill("Table 1 ref 1 parent table", "district");
    fill("Table 1 ref 1 parent key", "code");

    const json = await screen.findByTestId("mapping-json");
    expect(JSON.parse(json.textContent!)).toEqual({
      tables: {
        subdistrict_src: {
          target: "subdistrict",
          columns: { code: "code", name: "name", postcode: "postcode" },
          refs: [{ column: "district_id", parent: "master.district", parent_key: "code",
                   source_column: "district_code", external: true }],
        },
      },
    });
  });

  it("emits a new-table destination without refs (columns are target -> source)", async () => {
    render(<ScriptsTab canEdit />);

    fill("Table 1 source table", "province_src");
    fill("Table 1 new table name", "province");
    fill("Table 1 field 1 source column", "province_id");
    fill("Table 1 field 1 destination field", "code");
    fireEvent.click(screen.getByRole("button", { name: "Add field to table 1" }));
    fill("Table 1 field 2 source column", "province_name");
    fill("Table 1 field 2 destination field", "name");

    const json = await screen.findByTestId("mapping-json");
    const parsed = JSON.parse(json.textContent!);
    expect(parsed).toEqual({
      tables: { province_src: { target: "province", columns: { code: "province_id", name: "province_name" } } },
    });
    expect(parsed.tables.province_src.refs).toBeUndefined();
  });

  it("says what is wrong instead of sending a bad mapping", async () => {
    render(<ScriptsTab canEdit />);

    fill("Table 1 source table", "district_src");
    fill("Table 1 new table name", "Master.District");

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/snake_case/i);
    expect(screen.queryByTestId("mapping-json")).not.toBeInTheDocument();
  });

  it("keeps the raw mapping JSON behind the advanced toggle", async () => {
    render(<ScriptsTab canEdit />);

    fill("Table 1 source table", "province_src");
    fill("Table 1 new table name", "province");
    fill("Table 1 field 1 source column", "province_id");
    fill("Table 1 field 1 destination field", "code");

    fireEvent.click(screen.getByText(/Advanced/));
    const raw = await screen.findByLabelText("Mapping JSON (advanced)");
    expect((raw as HTMLTextAreaElement).value).toContain('"target": "province"');
  });
});

// ------------------------------------------------------------------ one-click flow (api mocked)

type Api = Record<string, ReturnType<typeof vi.fn>>;

class MockApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

const doneReport = (extra: Record<string, unknown> = {}) => ({
  state: "done", ok: true, script_id: 5, statements: 10, mapping_used: "suggested", errors: [], ignored: [],
  skipped: [], tables: [{ name: "a", source: "a", rows: 10, orphans: 0, sample_orphans: [] },
                        { name: "b", source: "b", rows: 5, orphans: 0, sample_orphans: [] }],
  ops_summary: { total: 4, by_op: {}, tables: ["a", "b"] }, dry_run: { ok: true, skipped: false, error: null, rows_loaded: {} },
  ...extra,
});

async function setup(over: Partial<Api> = {}, listed: unknown[] = []) {
  vi.resetModules();
  const api: Api = {
    getGraph: vi.fn(async () => ({ tables: [], relations: [] })),
    listScripts: vi.fn(async () => listed),
    getScriptLimits: vi.fn(async () => ({ max_bytes: 1000, chunk_bytes: 100 })),
    uploadScriptChunked: vi.fn(async (_f: File, progress: (a: number, b: number) => void) => {
      progress(50, 100);
      return { script_id: 5, sha256: "x", size: 100 };
    }),
    validateScriptAsync: vi.fn(async () => ({ state: "running" })),
    getScript: vi.fn(async () => ({ id: 5, name: "d.sql", size: 100, status: "validated", change_id: null, report: doneReport() })),
    submitScript: vi.fn(async () => ({ change_id: 9 })),
    pasteScript: vi.fn(), uploadScript: vi.fn(), validateScript: vi.fn(),
    ...over,
  };
  vi.doMock("@/lib/api", () => ({ ...api, ApiError: MockApiError }));
  const { ScriptsTab: Tab } = await import("./ScriptsTab");
  const utils = render(<Tab canEdit />);
  await waitFor(() => expect(api.getScriptLimits).toHaveBeenCalled());
  return { api, ...utils };
}

const pick = (size = 100) => {
  const input = screen.getByLabelText(/Choose a \.sql file/) as HTMLInputElement;
  const file = new File(["x".repeat(size)], "d.sql", { type: "text/plain" });
  fireEvent.change(input, { target: { files: [file] } });
};

const tick = (ms = 2100) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

describe("ScriptsTab one-click flow", () => {
  beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }); });
  afterEach(() => { vi.useRealTimers(); vi.doUnmock("@/lib/api"); });

  it("shows the size limit before a file is chosen", async () => {
    await setup();
    expect(await screen.findByText(/1,000 bytes|1000 bytes|1\.0 KB|1 KB/i)).toBeInTheDocument();
  });

  it("choosing a file uploads then validates then shows Ready without another click", async () => {
    const { api } = await setup();
    pick();
    await tick();
    expect(await screen.findByText("Ready: 2 tables, 15 rows")).toBeInTheDocument();
    expect(api.uploadScriptChunked).toHaveBeenCalledTimes(1);
    expect(api.validateScriptAsync).toHaveBeenCalledWith(5, undefined);
    expect(screen.getByRole("button", { name: /submit for approval/i })).toBeEnabled();
  });

  it("validate runs once per upload", async () => {
    const { api } = await setup();
    pick();
    await tick(); await tick(); await tick();
    expect(api.validateScriptAsync).toHaveBeenCalledTimes(1);
  });

  it("shows the phase while the check runs", async () => {
    const getScript = vi.fn()
      .mockResolvedValueOnce({ id: 5, name: "d.sql", size: 100, status: "uploaded", change_id: null, report: { state: "running", phase: "parsing" } })
      .mockResolvedValue({ id: 5, name: "d.sql", size: 100, status: "validated", change_id: null, report: doneReport() });
    await setup({ getScript });
    pick();
    await tick();
    expect(screen.getByText("Parsing")).toBeInTheDocument();
    await tick();
    expect(await screen.findByText("Ready: 2 tables, 15 rows")).toBeInTheDocument();
  });

  it("a failed upload keeps the previous script and Validate stays enabled", async () => {
    const { api } = await setup(
      { uploadScriptChunked: vi.fn(async () => { throw new MockApiError(413, "The server refused the file: too big"); }) },
      [{ id: 1, name: "old.sql", size: 10, status: "uploaded", change_id: null }]);
    await screen.findByText(/old\.sql/);
    pick();
    expect(await screen.findByText(/too big/)).toBeInTheDocument();
    expect(screen.getByText(/old\.sql/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Validate" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(api.validateScriptAsync).not.toHaveBeenCalled();
  });

  it("file over the limit is refused before any request", async () => {
    const { api } = await setup();
    pick(2000);
    expect(await screen.findByRole("alert")).toHaveTextContent(/larger than|over the limit|too large/i);
    expect(api.uploadScriptChunked).not.toHaveBeenCalled();
    expect(api.validateScriptAsync).not.toHaveBeenCalled();
  });

  it("skipped statements render under Not imported and a logic group is open", async () => {
    const skipped = [
      { line: 3, statement: "CREATE FUNCTION f()", reason: "logic reason", kind: "logic" },
      { line: 1, statement: "CREATE EXTENSION pg_trgm", reason: "ext reason", kind: "extension" },
    ];
    await setup({ getScript: vi.fn(async () => ({ id: 5, name: "d.sql", size: 100, status: "validated", change_id: null, report: doneReport({ skipped }) })) });
    pick();
    await tick();
    const outer = (await screen.findByText(/Not imported, and why/)).closest("details")!;
    expect(outer).toBeInTheDocument();
    const logic = screen.getByText(/Functions, triggers/).closest("details")!;
    const ext = screen.getByText(/^Extensions/).closest("details")!;
    expect(logic).toHaveAttribute("open");
    expect(ext).not.toHaveAttribute("open");
    expect(logic).toHaveTextContent(/warning/i);
    expect(logic).toHaveTextContent("CREATE FUNCTION f()");
  });

  it("changing a customize field re-runs validation once", async () => {
    const { api } = await setup();
    pick();
    await tick();
    await screen.findByText("Ready: 2 tables, 15 rows");
    fireEvent.change(screen.getByLabelText("Mapping JSON (advanced)"),
      { target: { value: '{"tables":{"a":{"target":"a","columns":{"x":"x"}}}}' } });
    await tick(1200); await tick(); await tick();
    expect(api.validateScriptAsync).toHaveBeenCalledTimes(2);
    expect(api.validateScriptAsync).toHaveBeenLastCalledWith(5, { tables: { a: { target: "a", columns: { x: "x" } } } });
  });

  it("a failed job shows the message and a Try again", async () => {
    const failed = { id: 5, name: "d.sql", size: 100, status: "uploaded", change_id: null,
                     report: { state: "failed", message: "The check stopped unexpectedly. Try again." } };
    const { api } = await setup({ getScript: vi.fn(async () => failed) });
    pick();
    await tick();
    expect(await screen.findByText(/stopped unexpectedly/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await tick();
    expect(api.validateScriptAsync).toHaveBeenCalledTimes(2);
  });

  it("re-attaches to a check that is already running after a reload", async () => {          // review finding 4
    const running = { id: 5, name: "d.sql", size: 100, status: "uploaded", change_id: null, report: { state: "running", phase: "parsing" } };
    const { api } = await setup({}, [running]);
    await tick();
    expect(await screen.findByText("Ready: 2 tables, 15 rows")).toBeInTheDocument();
    expect(api.getScript).toHaveBeenCalled();
    expect(api.validateScriptAsync).not.toHaveBeenCalled();
  });

  it("shows the finished result of the latest script on load without a new check", async () => {
    const done = { id: 5, name: "d.sql", size: 100, status: "validated", change_id: null, report: doneReport() };
    const { api } = await setup({}, [done]);
    expect(await screen.findByText("Ready: 2 tables, 15 rows")).toBeInTheDocument();
    expect(api.validateScriptAsync).not.toHaveBeenCalled();
  });

  it("survives one failed poll", async () => {
    const getScript = vi.fn()
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValue({ id: 5, name: "d.sql", size: 100, status: "validated", change_id: null, report: doneReport() });
    await setup({ getScript });
    pick();
    await tick(); await tick();
    expect(await screen.findByText("Ready: 2 tables, 15 rows")).toBeInTheDocument();
  });
});

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { answerImport, cancelImport, getImport, recheckImport, startImport, submitImport } from "./imports";
import { uploadScriptChunked } from "./api";

describe("imports api", () => {
  const calls: { url: string; init?: RequestInit }[] = [];
  const respond = (body: unknown, status = 200) => {
    calls.length = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      return { ok: status < 400, status, json: async () => body };
    }));
  };
  const sent = (i = 0) => JSON.parse(String(calls[i].init?.body));
  beforeEach(() => { document.cookie = "csrftoken=t"; });
  afterEach(() => vi.unstubAllGlobals());

  it("startImport posts the upload id, schema, prompt and key", async () => {
    respond({ id: 3 });
    await startImport({ uploadId: 9, schema: "master", prompt: "go", apiKey: "k" });
    expect(calls[0].url).toBe("/api/imports");
    expect(calls[0].init?.method).toBe("POST");
    expect(sent()).toEqual({ uploadId: 9, schema: "master", prompt: "go", apiKey: "k" });
  });
  it("startImport leaves an empty key out", async () => {
    respond({ id: 3 });
    await startImport({ uploadId: 9, schema: "master", prompt: "", apiKey: "" });
    expect(sent()).toEqual({ uploadId: 9, schema: "master", prompt: "" });
  });
  it("getImport, answer, recheck, submit, cancel use the run's URLs", async () => {
    respond({ id: 4 });
    await getImport(4);
    expect(calls[0].url).toBe("/api/imports/4");
    await answerImport(4, "yes", "k");
    expect(calls[1].url).toBe("/api/imports/4/answer");
    expect(sent(1)).toEqual({ text: "yes", apiKey: "k" });
    await recheckImport(4, { mapping: { tables: {} }, filters: {}, skip: { a: [2] } });
    expect(calls[2].url).toBe("/api/imports/4/recheck");
    expect(sent(2)).toEqual({ mapping: { tables: {} }, filters: {}, skip: { a: [2] } });
    respond({ changeId: 8 });
    expect(await submitImport(4, "n")).toEqual({ changeId: 8 });
    expect(calls[0].url).toBe("/api/imports/4/submit");
    respond({});
    await cancelImport(4);
    expect(calls[0].init?.method).toBe("DELETE");
  });
});

describe("uploadScriptChunked for an import", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("sends the kind, skips complete and returns the upload id", async () => {
    document.cookie = "csrftoken=t";
    const urls: string[] = [];
    let beginBody: unknown;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      urls.push(`${init?.method} ${url}`);
      if (url === "/api/scripts/uploads") {
        beginBody = JSON.parse(String(init?.body));
        return { ok: true, status: 201, json: async () => ({ upload_id: 5, chunk_bytes: 10, chunks: 1 }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    }));
    const r = await uploadScriptChunked(new File(["a,b\n1,2\n"], "v.csv"), () => {}, undefined, "import");
    expect(beginBody).toEqual({ name: "v.csv", size: 8, kind: "import" });
    expect(r).toEqual({ upload_id: 5 });
    expect(urls).toEqual(["POST /api/scripts/uploads", "PUT /api/scripts/uploads/5/0"]);
  });
});

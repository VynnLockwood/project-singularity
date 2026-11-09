import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const json = (status: number, body: unknown) =>
  ({ ok: status >= 200 && status < 300, status, statusText: "Err", json: async () => body }) as Response;

async function setup(canExportData: boolean, fetchImpl: () => Promise<Response>) {
  vi.resetModules();
  const fetchMock = vi.fn((_url: string, _init?: RequestInit) => fetchImpl());
  vi.stubGlobal("fetch", fetchMock);
  const clicked: string[] = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    clicked.push(this.getAttribute("href") ?? "");
  });
  const { ExportMenu } = await import("./ExportMenu");
  render(<ExportMenu schema="crm" canExportData={canExportData} />);
  return { fetchMock, clicked };
}

const download = () => fireEvent.click(screen.getByRole("button", { name: "Download SQL" }));

describe("ExportMenu", () => {
  beforeEach(() => vi.resetModules());
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("downloads without data by default", async () => {
    const { clicked, fetchMock } = await setup(true, async () => json(200, {}));
    download();
    await waitFor(() => expect(clicked).toHaveLength(1));
    expect(clicked[0]).toMatch(/\/schema\/export\?schema=crm&data=0$/);
    expect(String(fetchMock.mock.calls[0][0])).toBe(clicked[0]);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("includes data when an editor ticks the box", async () => {
    const { clicked } = await setup(true, async () => json(200, {}));
    fireEvent.click(screen.getByLabelText("Include data"));
    download();
    await waitFor(() => expect(clicked).toHaveLength(1));
    expect(clicked[0]).toMatch(/schema=crm&data=1$/);
  });

  it("disables the checkbox for viewers and never asks for data", async () => {
    const { clicked, fetchMock } = await setup(false, async () => json(200, {}));
    const box = screen.getByLabelText("Include data") as HTMLInputElement;
    expect(box.disabled).toBe(true);
    expect(box.title).toBe("Editors can export data");
    download();
    await waitFor(() => expect(clicked).toHaveLength(1));
    expect(clicked[0]).toMatch(/data=0$/);
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/data=0$/);
  });

  it("shows the server's message on 422 and does not download", async () => {
    const { clicked } = await setup(true, async () => json(422, { error: "column foo has type vector…" }));
    download();
    expect((await screen.findByRole("alert")).textContent).toBe("column foo has type vector…");
    expect(clicked).toHaveLength(0);
  });

  it("shows the message on 403", async () => {
    const { clicked } = await setup(true, async () => json(403, { error: "Only editors can export data." }));
    download();
    expect((await screen.findByRole("alert")).textContent).toBe("Only editors can export data.");
    expect(clicked).toHaveLength(0);
  });

  it("falls back to the status text when the body is not JSON", async () => {
    await setup(true, async () => ({ ok: false, status: 500, statusText: "Server Error", json: async () => { throw new Error("x"); } }) as unknown as Response);
    download();
    expect((await screen.findByRole("alert")).textContent).toBe("Server Error");
  });

  it("reports an unreachable server", async () => {
    const { clicked } = await setup(true, async () => { throw new TypeError("network"); });
    download();
    expect((await screen.findByRole("alert")).textContent).toBe("Could not reach the server.");
    expect(clicked).toHaveLength(0);
  });
});

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const hit = (table: string, column: string | null, confidence: number) =>
  ({ kind: column ? "column" : "table", table, column, confidence, description: `${table} ${column ?? ""} doc` }) as const;

async function setup(result: unknown, canPropose = true) {
  vi.resetModules();
  const onAsk = vi.fn();
  vi.doMock("@/lib/api", async (orig) => {
    const real = await orig<typeof import("@/lib/api")>();
    return {
      ...real,
      searchSchema: result instanceof Error ? vi.fn().mockRejectedValue(new real.ApiError(503, result.message)) : vi.fn().mockResolvedValue(result),
    };
  });
  const { SearchBox } = await import("./SearchBox");
  render(<SearchBox canPropose={canPropose} onAskAssistant={onAsk} />);
  fireEvent.change(screen.getByLabelText("Search the schema"), { target: { value: "เบอร์โทรลูกค้า" } });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  return onAsk;
}

describe("SearchBox", () => {
  beforeEach(() => vi.resetModules());

  it("lists hits with confidence and no suggestion when the best one is confident", async () => {
    await setup({ results: [hit("customer", "phoneNumber", 0.91), hit("customer", null, 0.4)], confident: true, suggestion: null });
    await waitFor(() => expect(screen.getByText("customer.phoneNumber")).toBeInTheDocument());
    expect(screen.getByText(/91%/)).toBeInTheDocument();
    expect(screen.queryByText(/propose a new column/)).toBeNull();
  });

  it("offers to ask the assistant, pre-filled with the search, when nothing fits", async () => {
    const onAsk = await setup({ results: [hit("product", null, 0.3)], confident: false, suggestion: "Nothing matches well." });
    await waitFor(() => screen.getByRole("button", { name: /propose a new column/ }));
    fireEvent.click(screen.getByRole("button", { name: /propose a new column/ }));
    expect(onAsk).toHaveBeenCalledWith(expect.stringContaining("เบอร์โทรลูกค้า"));
  });

  it("does not offer proposals to viewers", async () => {
    await setup({ results: [], confident: false, suggestion: "Nothing matches well." }, false);
    await waitFor(() => screen.getByText("Nothing matches well."));
    expect(screen.queryByRole("button", { name: /propose a new column/ })).toBeNull();
  });

  it("shows the server's error", async () => {
    await setup(new Error("search is not configured"));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("search is not configured"));
  });

  it("lets approvers rebuild the index and reports what happened", async () => {
    vi.resetModules();
    const reindex = vi.fn().mockResolvedValue({ embedded: 12, unchanged: 0, removed: 0 });
    vi.doMock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), reindexSearch: reindex }));
    const { SearchBox } = await import("./SearchBox");
    render(<SearchBox canPropose canReindex onAskAssistant={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Rebuild search index" }));
    await waitFor(() => expect(screen.getByText("Index rebuilt: 12 documents embedded.")).toBeInTheDocument());
    expect(reindex).toHaveBeenCalledWith(true);
  });

  it("hides the rebuild button from everyone else", async () => {
    await setup({ results: [], confident: true, suggestion: null });
    expect(screen.queryByRole("button", { name: "Rebuild search index" })).toBeNull();
  });
});

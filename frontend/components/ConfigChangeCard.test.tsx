import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { change } from "@/lib/fixtures";
import { ChangeCard } from "./ChangeCard";

const me = (role: "viewer" | "editor" | "approver") => ({ id: 1, email: "ed@x.com", name: "", role });
const cfg = change({ id: 12, kind: "config", status: "pending", requester: "ed@x.com", source: "mcp", diff: ["~ setting search.topK: 5 → 9"] });

describe("ChangeCard for a settings change", () => {
  it("is labelled, shows the diff and lets an approver decide", () => {
    const onDecide = vi.fn();
    render(<ChangeCard change={cfg} me={me("approver")} onDecide={onDecide} />);
    expect(screen.getByText(/Settings change/)).toBeInTheDocument();
    expect(screen.getByText("~ setting search.topK: 5 → 9")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Approve"));
    expect(onDecide).toHaveBeenCalledWith(cfg, "approve");
  });

  it("does not let the editor who proposed it approve it", () => {
    render(<ChangeCard change={cfg} me={me("editor")} onDecide={vi.fn()} />);
    expect(screen.queryByText("Approve")).toBeNull();
  });
});

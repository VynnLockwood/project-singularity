import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Me } from "@/lib/api";
import { change } from "@/lib/fixtures";
import { ChangeCard } from "./ChangeCard";

const me = (role: Me["role"]): Me => ({ id: 1, email: "ed@x.com", name: "", role, active: true });
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

  it("tells an approver that a role change needs an admin, and lets an admin approve it", () => {
    const roles = change({ id: 13, kind: "config", status: "pending", requester: "ed@x.com", operations: [{ op: "set_user_role" }], diff: ["~ role of a@x.com: viewer → editor"] });
    const { unmount } = render(<ChangeCard change={roles} me={me("approver")} onDecide={vi.fn()} />);
    expect(screen.queryByText("Approve")).toBeNull();
    expect(screen.getByText("Needs an admin to approve.")).toBeInTheDocument();
    unmount();
    render(<ChangeCard change={roles} me={me("admin")} onDecide={vi.fn()} />);
    expect(screen.getByText("Approve")).toBeInTheDocument();
  });

  it("does not let the editor who proposed it approve it", () => {
    render(<ChangeCard change={cfg} me={me("editor")} onDecide={vi.fn()} />);
    expect(screen.queryByText("Approve")).toBeNull();
  });
});

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Change, Me } from "@/lib/api";
import { ChangeCard } from "./ChangeCard";

const me = (role: Me["role"], email = "ap@x.com"): Me => ({ id: 1, email, name: "", role });
const sqlChange = (over: Partial<Change> = {}): Change => ({
  id: 9, status: "pending", kind: "sql", source: "mcp", requester: "ed@x.com", prompt: "fix the typo in t",
  createdAt: "2026-01-01T00:00:00Z", decidedBy: null, error: "", decisionNote: "", tables: [], schema: "master",
  diff: ["UPDATE 3 (statement 1)"], sql: ["UPDATE master.t SET a = 1"],
  operations: [{ op: "raw_sql", sql: "UPDATE master.t SET a = 1", reason: "fix the typo in t" }],
  check: { violations: [{ severity: "warn", message: "Rules were not checked: this is raw SQL." }] },
  ...over,
});

describe("ChangeCard for raw SQL", () => {
  it("shows Raw SQL, the reason, the SQL already open and the standing note, and lets an approver decide", () => {
    render(<ChangeCard change={sqlChange()} me={me("approver")} onDecide={vi.fn()} />);
    expect(screen.getByText(/Raw SQL/)).toBeInTheDocument();
    expect(screen.getByText(/fix the typo in t/)).toBeInTheDocument();
    expect(screen.getByText("UPDATE master.t SET a = 1")).toBeVisible();
    expect(screen.getByText(/cannot be undone automatically/)).toBeInTheDocument();
    expect(screen.getAllByText(/Rules were not checked/)).toHaveLength(2);
    expect(screen.getByRole("button", { name: /Approve/ })).toBeEnabled();
  });

  it("gives the requesting editor no Approve button", () => {
    render(<ChangeCard change={sqlChange()} me={me("editor", "ed@x.com")} onDecide={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /Approve/ })).toBeNull();
    expect(screen.getByText(/Waiting for an approver/)).toBeInTheDocument();
  });
});

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Me } from "@/lib/api";
import { change } from "@/lib/fixtures";
import { ApprovalPopup } from "./ApprovalPopup";
import { ChangeCard } from "./ChangeCard";
import { SchemaDiagram } from "./SchemaView";

const me = (role: Me["role"], email = "ed@example.com"): Me => ({ id: 1, email, name: "", role });

describe("ChangeCard", () => {
  it("colours diff lines and lets an approver approve or reject", () => {
    const onDecide = vi.fn();
    render(<ChangeCard change={change({ diff: ["+ create table customer", "- drop column x"] })} me={me("approver", "a@x.com")} onDecide={onDecide} />);
    expect(screen.getByText("+ create table customer")).toHaveAttribute("data-kind", "add");
    expect(screen.getByText("- drop column x")).toHaveAttribute("data-kind", "remove");
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onDecide.mock.calls.map((c) => c[1])).toEqual(["approve", "reject"]);
  });

  it("gives an editor no buttons on a schema change, only a waiting note", () => {
    render(<ChangeCard change={change()} me={me("editor")} onDecide={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.getByText("Waiting for an approver.")).toBeInTheDocument();
  });

  it("lets an editor approve their own data change", () => {
    render(<ChangeCard change={change({ kind: "data" })} me={me("editor")} onDecide={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("shows warnings and toggles the SQL", () => {
    const c = change({ check: { violations: [{ severity: "warn", message: "careful with drops" }] } });
    render(<ChangeCard change={c} me={me("viewer")} />);
    expect(screen.getByLabelText("Warnings")).toHaveTextContent("careful with drops");
    expect(screen.queryByText("CREATE TABLE ...")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show SQL" }));
    expect(screen.getByText("CREATE TABLE ...")).toBeInTheDocument();
  });

  it("shows why the gate blocked a change", () => {
    const c = change({ status: "rejected", check: { violations: [{ severity: "block", message: "use camelCase", suggestion: "supplierContact" }] } });
    render(<ChangeCard change={c} me={me("editor")} onDecide={vi.fn()} />);
    expect(screen.getByLabelText("Blocked by rules")).toHaveTextContent("use camelCase (try supplierContact)");
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });
});

describe("ApprovalPopup", () => {
  it("shows nothing when there is nothing this person can decide", () => {
    const { container } = render(<ApprovalPopup me={me("editor")} changes={[change()]} onDecide={vi.fn()} onSnooze={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("asks about the oldest decidable change, counts the rest, and can snooze", () => {
    const onSnooze = vi.fn();
    const changes = [change({ id: 7 }), change({ id: 8 }), change({ id: 9, status: "applied" })];
    render(<ApprovalPopup me={me("approver", "a@x.com")} changes={changes} onDecide={vi.fn()} onSnooze={onSnooze} />);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByLabelText("Change #7")).toBeInTheDocument();
    expect(screen.getByText("1 more waiting")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Later" }));
    expect(onSnooze).toHaveBeenCalledWith(7);
  });
});

describe("SchemaDiagram", () => {
  it("draws a card per table and a line per relation", () => {
    const col = (name: string, primaryKey = false) => ({ name, type: "bigint", nullable: false, primaryKey });
    render(<SchemaDiagram graph={{
      tables: [
        { name: "customer", comment: null, rowEstimate: 0, columns: [col("id", true)] },
        { name: "orderHeader", comment: null, rowEstimate: 0, columns: [col("id", true), col("customerId")] },
      ],
      relations: [{ from: { table: "orderHeader", column: "customerId" }, to: { table: "customer", column: "id" } }],
    }} />);
    expect(screen.getAllByTestId("table")).toHaveLength(2);
    expect(screen.getAllByTestId("relation")).toHaveLength(1);
    expect(screen.getByText("customer")).toBeInTheDocument();
  });

  it("explains an empty schema", () => {
    render(<SchemaDiagram graph={{ tables: [], relations: [] }} />);
    expect(screen.getByText(/No tables yet/)).toBeInTheDocument();
  });
});

describe("ChatView", () => {
  beforeEach(() => vi.resetModules());

  it("sends a message, shows the reply, hides tool output and shows the proposed change", async () => {
    vi.doMock("@/lib/api", async (orig) => ({
      ...(await orig<typeof import("@/lib/api")>()),
      listConversations: vi.fn().mockResolvedValue([]),
      createConversation: vi.fn().mockResolvedValue({ id: 5, title: "", model: "m", updatedAt: "" }),
      sendMessage: vi.fn().mockResolvedValue({
        messages: [
          { id: 1, role: "user", content: "add customers" },
          { id: 2, role: "assistant", content: "", toolCalls: ["propose_change"] },
          { id: 3, role: "tool", content: '{"secret":"raw tool json"}', toolName: "propose_change" },
          { id: 4, role: "assistant", content: "Proposed, waiting for approval." },
        ],
        changes: [change({ id: 11 })],
      }),
    }));
    const { ChatView } = await import("./ChatView");
    render(<ChatView me={me("editor")} live={[]} onDecide={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "add customers" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(screen.getByText("Proposed, waiting for approval.")).toBeInTheDocument());
    expect(screen.getByText("add customers")).toBeInTheDocument();
    expect(screen.queryByText(/raw tool json/)).toBeNull();
    expect(screen.getByLabelText("Change #11")).toBeInTheDocument();
  });

  it("shows the server's error and keeps the text so it can be retried", async () => {
    vi.doMock("@/lib/api", async (orig) => {
      const real = await orig<typeof import("@/lib/api")>();
      return {
        ...real,
        listConversations: vi.fn().mockResolvedValue([]),
        createConversation: vi.fn().mockResolvedValue({ id: 5, title: "", model: "m", updatedAt: "" }),
        sendMessage: vi.fn().mockRejectedValue(new real.ApiError(503, "the chat is not configured", "not_configured")),
      };
    });
    const { ChatView } = await import("./ChatView");
    render(<ChatView me={me("viewer")} live={[]} onDecide={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("the chat is not configured"));
    expect(screen.getByLabelText("Message")).toHaveValue("hello");
  });
});

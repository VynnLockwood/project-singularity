import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Change, GraphTable, Me } from "@/lib/api";

vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), proposeChange: vi.fn() }));
import { ApiError, proposeChange } from "@/lib/api";
import { NewTableForm } from "./NewTableForm";

const me: Me = { id: 1, email: "ed@example.com", name: "", role: "editor" };
const change: Change = {
  id: 7, status: "pending", kind: "schema", source: "web", requester: "ed@example.com", prompt: "t",
  createdAt: "", decidedBy: null, error: "", decisionNote: "", tables: [], diff: ["+ x"], check: {},
};
const tbl = (name: string): GraphTable => ({ name, comment: null, rowEstimate: 0, columns: [] });
const tables = [tbl("customer"), tbl("product")];
const setup = (t = tables) => {
  const props = { me, schema: "crm", tables: t, onClose: vi.fn(), onProposed: vi.fn() };
  const r = render(<NewTableForm {...props} />);
  return { props, r };
};
const set = (label: string, v: string) => fireEvent.change(screen.getByLabelText(label), { target: { value: v } });
const review = () => screen.getByRole("button", { name: "Review" });

beforeEach(() => vi.mocked(proposeChange).mockReset());

describe("NewTableForm", () => {
  it("starts disabled with no error and notes automatic audit columns", () => {
    setup();
    expect(review()).toBeDisabled();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText(/audit columns are added automatically/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/created_at|updated_at/i)).toBeNull();
  });

  it.each([["Sales"], ["my table"], ["a".repeat(64)], ["customer"]])("refuses table name %s", (n) => {
    setup();
    set("Table name", n);
    set("Field 1 name", "id");
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(review()).toBeDisabled();
  });

  it("refuses invalid column names and types", () => {
    setup();
    set("Table name", "order");
    set("Field 1 name", "Bad Name");
    expect(review()).toBeDisabled();
    set("Field 1 name", "ok");
    expect(review()).not.toBeDisabled();
    fireEvent.change(screen.getByLabelText("Field 1 type"), { target: { value: "varchar(n)" } });
    expect(review()).toBeDisabled();
    set("Field 1 exact type", "varchar(40)");
    expect(review()).not.toBeDisabled();
    set("Field 1 exact type", "blob");
    expect(review()).toBeDisabled();
  });

  it("refuses duplicate column names and an empty column list", () => {
    setup();
    set("Table name", "order");
    set("Field 1 name", "a");
    fireEvent.click(screen.getByRole("button", { name: "Add field" }));
    set("Field 2 name", "a");
    expect(screen.getAllByRole("alert")[0]).toHaveTextContent(/duplicate/i);
    expect(review()).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Remove field 2" }));
    expect(review()).not.toBeDisabled();
    expect(screen.queryByRole("button", { name: /Remove field/ })).toBeNull();
  });

  it("a blank added row blocks submit", () => {
    setup();
    set("Table name", "order");
    set("Field 1 name", "a");
    fireEvent.click(screen.getByRole("button", { name: "Add field" }));
    expect(review()).toBeDisabled();
  });

  it("sends the exact create_table op, references only when chosen", async () => {
    vi.mocked(proposeChange).mockResolvedValue(change);
    const { props } = setup();
    set("Table name", "order");
    set("Table comment (optional)", " Orders ");
    set("Field 1 name", "id");
    set("Field 1 type", "uuid");
    set("Field 1 default", "gen_random_uuid()");
    fireEvent.click(screen.getByRole("button", { name: "Add field" }));
    set("Field 2 name", "customer_id");
    set("Field 2 references", "customer");
    fireEvent.change(screen.getByLabelText("Field 2 nullable"), { target: { value: "no" } });
    fireEvent.click(screen.getByLabelText("Field 2 unique"));
    set("Field 2 comment", " who ");
    fireEvent.click(review());
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    await waitFor(() => expect(props.onProposed).toHaveBeenCalledWith(change, "order"));
    expect(proposeChange).toHaveBeenCalledWith(
      [{
        op: "create_table", name: "order", comment: "Orders",
        columns: [
          { name: "id", type: "uuid", default: "gen_random_uuid()" },
          { name: "customer_id", type: "text", nullable: false, unique: true, comment: "who", references: { table: "customer" } },
        ],
      }],
      expect.any(String), "web", "crm",
    );
  });

  it("reference picker lists only the given tables", () => {
    setup();
    const opts = Array.from((screen.getByLabelText("Field 1 references") as HTMLSelectElement).options).map((o) => o.value);
    expect(opts).toEqual(["", "customer", "product"]);
  });

  it("a refused proposal keeps the dialog, the message and the input", async () => {
    vi.mocked(proposeChange).mockRejectedValueOnce(new ApiError(422, "gate says no"));
    setup();
    set("Table name", "order");
    set("Field 1 name", "id");
    fireEvent.click(review());
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    expect(await screen.findByText("gate says no")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Propose" })).not.toBeDisabled();
  });

  it("a rejected create keeps the typed columns and shows the violation; Close goes back to the form", async () => {
    const refused: Change = { ...change, status: "rejected", diff: [], check: { violations: [{ severity: "block", message: "table needs a comment" }] } };
    vi.mocked(proposeChange).mockResolvedValue(refused);
    const { props } = setup();
    set("Table name", "order");
    set("Field 1 name", "id");
    fireEvent.click(review());
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    expect(await screen.findByText("table needs a comment")).toBeInTheDocument();
    expect(props.onProposed).toHaveBeenCalledWith(refused, "order");
    expect(screen.queryByRole("status")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(props.onClose).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Table name")).toHaveValue("order");
    expect(screen.getByLabelText("Field 1 name")).toHaveValue("id");
  });

  it("keeps the status card when tables later include the created name", async () => {
    vi.mocked(proposeChange).mockResolvedValue(change);
    const { props, r } = setup();
    set("Table name", "order");
    set("Field 1 name", "id");
    fireEvent.click(review());
    fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    await screen.findByRole("status");
    r.rerender(<NewTableForm {...props} tables={[...tables, tbl("order")]} />);
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("Cancel closes", () => {
    const { props } = setup();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(props.onClose).toHaveBeenCalled();
  });
});

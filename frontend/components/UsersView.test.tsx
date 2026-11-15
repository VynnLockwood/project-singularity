import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Me, User } from "@/lib/api";

const me: Me = { id: 1, email: "admin@x.com", name: "Ada Admin", role: "admin", active: true };
const people: User[] = [
  { id: 1, email: "admin@x.com", name: "Ada Admin", role: "admin", active: true, lastLoginAt: "2026-11-14T09:30:00Z" },
  { id: 2, email: "ed@x.com", name: "Ed Editor", role: "editor", active: true, lastLoginAt: null },
  { id: 3, email: "off@x.com", name: "", role: "viewer", active: false, lastLoginAt: null },
];

async function setup(list: User[] = people, overrides: Record<string, unknown> = {}) {
  vi.resetModules();
  const fns = {
    listUsers: vi.fn().mockResolvedValue(list),
    createUser: vi.fn().mockResolvedValue({ id: 9, email: "new@x.com", name: "", role: "editor", active: true, lastLoginAt: null }),
    updateUser: vi.fn().mockResolvedValue(list[1]),
    ...overrides,
  };
  vi.doMock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), ...fns }));
  const { UsersView } = await import("./UsersView");
  render(<UsersView me={me} />);
  await screen.findByRole("table", { name: "People" });
  return fns;
}

/** Thrown from the same module instance the view gets after vi.resetModules, so `instanceof ApiError` holds. */
const refuse = (status: number, message: string) => vi.fn(async () => {
  const { ApiError } = await import("@/lib/api");
  throw new ApiError(status, message);
});

afterEach(() => { vi.doUnmock("@/lib/api"); vi.resetModules(); });

describe("UsersView", () => {
  it("test_users_view_lists_adds_and_changes", async () => {
    const { createUser, updateUser, listUsers } = await setup();
    const table = screen.getByRole("table", { name: "People" });
    expect(within(table).getByText("ed@x.com")).toBeInTheDocument();
    expect(within(table).getByText("Ed Editor")).toBeInTheDocument();
    expect(within(table).getAllByText("Never").length).toBeGreaterThan(0);

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "new@x.com" } });
    fireEvent.change(screen.getByLabelText("Role", { selector: "#add-user-role" }), { target: { value: "editor" } });
    fireEvent.click(screen.getByRole("button", { name: "Add user" }));
    await waitFor(() => expect(createUser).toHaveBeenCalledWith({ email: "new@x.com", role: "editor" }));
    expect(await screen.findByText(/new@x.com can now sign in/)).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toHaveValue("");

    fireEvent.change(screen.getByLabelText("Role of ed@x.com"), { target: { value: "approver" } });
    await waitFor(() => expect(updateUser).toHaveBeenCalledWith(2, { role: "approver" }));

    fireEvent.click(screen.getByRole("switch", { name: "off@x.com can sign in" }));
    await waitFor(() => expect(updateUser).toHaveBeenCalledWith(3, { active: true }));
    expect(listUsers.mock.calls.length).toBeGreaterThan(1); // reloads after each change
  });

  it("offers every role, admin included", async () => {
    await setup();
    const opts = within(screen.getByLabelText("Role of ed@x.com")).getAllByRole("option").map((o) => o.textContent);
    expect(opts).toEqual(["viewer", "editor", "approver", "admin"]);
  });

  it("checks the email before sending it, but leaves the rest to the server", async () => {
    const { createUser } = await setup();
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "not an email" } });
    fireEvent.click(screen.getByRole("button", { name: "Add user" }));
    expect(await screen.findByText(/Enter an email address like name@example.com/)).toBeInTheDocument();
    expect(createUser).not.toHaveBeenCalled();
  });

  it("sends a name only when one is typed", async () => {
    const { createUser } = await setup();
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "n@x.com" } });
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: " Nia Ng " } });
    fireEvent.click(screen.getByRole("button", { name: "Add user" }));
    await waitFor(() => expect(createUser).toHaveBeenCalledWith({ email: "n@x.com", role: "viewer", name: "Nia Ng" }));
  });

  it("test_users_view_disables_self_and_last_admin", async () => {
    await setup([
      ...people,
      { id: 4, email: "boss@x.com", name: "", role: "admin", active: false, lastLoginAt: null },
    ]);
    // own row: both controls off, with the reason next to them
    expect(screen.getByLabelText("Role of admin@x.com")).toBeDisabled();
    expect(screen.getByRole("switch", { name: "admin@x.com can sign in" })).toBeDisabled();
    expect(screen.getByLabelText("Role of admin@x.com")).toHaveAccessibleDescription(/This is you/);
    // others stay editable
    expect(screen.getByLabelText("Role of ed@x.com")).toBeEnabled();
    expect(screen.getByRole("switch", { name: "boss@x.com can sign in" })).toBeEnabled();
  });

  it("locks the last active admin even when it is someone else", async () => {
    await setup([
      { id: 1, email: "admin@x.com", name: "", role: "approver", active: true, lastLoginAt: null },
      { id: 5, email: "only@x.com", name: "", role: "admin", active: true, lastLoginAt: null },
    ]);
    expect(screen.getByLabelText("Role of only@x.com")).toBeDisabled();
    expect(screen.getByRole("switch", { name: "only@x.com can sign in" })).toBeDisabled();
    expect(screen.getByLabelText("Role of only@x.com")).toHaveAccessibleDescription(/last active admin/i);
  });

  it("test_users_view_shows_server_errors", async () => {
    const { updateUser } = await setup(people, {
      updateUser: refuse(400, "cannot remove the last active admin"),
      createUser: refuse(400, "a user with this email already exists"),
    });
    fireEvent.change(screen.getByLabelText("Role of ed@x.com"), { target: { value: "viewer" } });
    await waitFor(() => expect(updateUser).toHaveBeenCalled());
    const row = screen.getByText("ed@x.com").closest("tr")!;
    expect(await within(row).findByRole("alert")).toHaveTextContent(/cannot remove the last active admin/i);

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "ed@x.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Add user" }));
    const form = screen.getByRole("form", { name: "Add user" });
    expect(await within(form).findByRole("alert")).toHaveTextContent(/already exists/i);
  });

  it("says what to do when the list cannot load", async () => {
    vi.resetModules();
    vi.doMock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()),
      listUsers: refuse(500, "request failed (500)") }));
    const { UsersView } = await import("./UsersView");
    render(<UsersView me={me} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load people. Request failed (500) Reload the page to try again.");
  });
});

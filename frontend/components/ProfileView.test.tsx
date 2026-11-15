import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Capabilities, Me, Role } from "@/lib/api";
import { NAV, atLeast, navFor } from "@/lib/nav";
import { ProfileView } from "./ProfileView";

const TOOLS: [string, Role][] = [["read_rows", "viewer"], ["propose_change", "editor"], ["approve_change", "approver"], ["list_users", "approver"]];
const ROLES: Role[] = ["viewer", "editor", "approver", "admin"];

const caps = (role: Role): Capabilities => ({
  mcpTools: TOOLS.map(([name, minRole]) => ({ name, minRole, allowed: atLeast(role, minRole), description: `${name} does things.` })),
  roleSummary: ["Read data and the schema", "Edit data, import files, propose schema changes", "Approve changes, settings, backups", "Manage users and roles"],
});
const me = (role: Role, extra: Partial<Me> = {}): Me => ({
  id: 1, email: "pat@x.com", name: "Pat Lee", role, active: true, lastLoginAt: "2026-11-14T09:30:00Z", capabilities: caps(role), ...extra,
});
const texts = (list: HTMLElement) => within(list).getAllByRole("listitem").map((li) => li.querySelector("[data-name]")?.textContent);

describe("ProfileView", () => {
  it("shows who you are", () => {
    render(<ProfileView me={me("editor")} navigate={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Pat Lee" })).toBeInTheDocument();
    expect(screen.getByText("pat@x.com")).toBeInTheDocument();
    expect(screen.getByLabelText("Your role")).toHaveTextContent("editor");
  });

  it.each(ROLES)("test_profile_lists_nav_items_for_role: %s", (role) => {
    render(<ProfileView me={me(role)} navigate={vi.fn()} />);
    const pages = screen.getByRole("list", { name: "Pages you can open" });
    expect(texts(pages)).toEqual(navFor(role).flatMap((g) => g.items.map((i) => i.label)));
    const tools = screen.getByRole("list", { name: "AI tools you can use" });
    expect(texts(tools)).toEqual(caps(role).mcpTools.filter((t) => t.allowed).map((t) => t.name));

    const higher = screen.queryByRole("list", { name: "Needs a higher role" });
    const lockedPages = NAV.flatMap((g) => g.items).filter((i) => !atLeast(role, i.minRole));
    const lockedTools = caps(role).mcpTools.filter((t) => !t.allowed);
    if (role === "admin") {
      expect(higher).toBeNull();
      expect(screen.getByText(/every page and tool/)).toBeInTheDocument();
    } else {
      expect(texts(higher!).sort()).toEqual([...lockedPages.map((i) => i.label), ...lockedTools.map((t) => t.name)].sort());
      for (const i of lockedPages) {
        const li = within(higher!).getByText(i.label).closest("li")!;
        expect(li).toHaveTextContent(`needs ${i.minRole}`);
      }
      for (const t of lockedTools) expect(within(higher!).getByText(t.name).closest("li")).toHaveTextContent(`needs ${t.minRole}`);
    }
  });

  it("explains the roles and marks yours", () => {
    render(<ProfileView me={me("approver")} navigate={vi.fn()} />);
    const ladder = screen.getByRole("list", { name: "Roles" });
    const items = within(ladder).getAllByRole("listitem");
    expect(items).toHaveLength(4);
    expect(items[2]).toHaveAttribute("aria-current", "true");
    expect(items[2]).toHaveTextContent("Approve changes, settings, backups");
  });

  it("opens a page from the list and links to Connect", () => {
    const navigate = vi.fn();
    render(<ProfileView me={me("editor")} navigate={navigate} />);
    fireEvent.click(within(screen.getByRole("list", { name: "Pages you can open" })).getByRole("button", { name: "Import" }));
    expect(navigate).toHaveBeenCalledWith({ view: "import" });
    fireEvent.click(screen.getByRole("button", { name: "Connect an AI client" }));
    expect(navigate).toHaveBeenCalledWith({ view: "connect" });
  });

  it("gives a next step when the server sent no tool list", () => {
    render(<ProfileView me={me("editor", { capabilities: undefined })} navigate={vi.fn()} />);
    expect(screen.getByText(/did not say which AI tools/)).toHaveTextContent(/Reload/);
    expect(screen.getByRole("list", { name: "Pages you can open" })).toBeInTheDocument();
  });
});

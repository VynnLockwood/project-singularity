import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Home } from "./Home";
import { t } from "@/lib/i18n";
import type { Me } from "@/lib/api";

const me = (role: Me["role"]): Me => ({ id: 1, email: "a@example.com", name: "A", role });

describe("Home", () => {
  it("shows a Google sign-in link when signed out", () => {
    render(<Home me={null} locale="en" loginUrl="/api/auth/google/login" />);
    const link = screen.getByRole("link", { name: "Sign in with Google" });
    expect(link).toHaveAttribute("href", "/api/auth/google/login");
  });

  it("viewer can only read", () => {
    render(<Home me={me("viewer")} locale="en" loginUrl="/x" />);
    expect(screen.getByTestId("role")).toHaveTextContent("Viewer");
    const flags = screen.getAllByRole("listitem").map((li) => li.getAttribute("data-allowed"));
    expect(flags).toEqual(["true", "false", "false"]);
  });

  it("editor can read and edit", () => {
    render(<Home me={me("editor")} locale="en" loginUrl="/x" />);
    const flags = screen.getAllByRole("listitem").map((li) => li.getAttribute("data-allowed"));
    expect(flags).toEqual(["true", "true", "false"]);
  });

  it("approver can do everything", () => {
    render(<Home me={me("approver")} locale="en" loginUrl="/x" />);
    const flags = screen.getAllByRole("listitem").map((li) => li.getAttribute("data-allowed"));
    expect(flags).toEqual(["true", "true", "true"]);
  });

  it("renders Thai text", () => {
    render(<Home me={me("editor")} locale="th" loginUrl="/x" />);
    expect(screen.getByTestId("role")).toHaveTextContent("ผู้แก้ไข");
  });

  it("shows sign-out only when a handler is given", () => {
    const { rerender } = render(<Home me={me("viewer")} locale="en" loginUrl="/x" />);
    expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
    rerender(<Home me={me("viewer")} locale="en" loginUrl="/x" onLogout={() => {}} />);
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });
});

describe("i18n", () => {
  it("has the same keys in English and Thai", () => {
    for (const key of ["signIn", "role_viewer", "role_editor", "role_approver", "forbiddenBody"] as const) {
      expect(t("en", key)).toBeTruthy();
      expect(t("th", key)).toBeTruthy();
    }
  });
});

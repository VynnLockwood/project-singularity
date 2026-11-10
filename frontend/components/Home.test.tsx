import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Home } from "./Home";
import { t } from "@/lib/i18n";

describe("Home (sign-in screen)", () => {
  it("shows a Google sign-in link when signed out", () => {
    render(<Home locale="en" loginUrl="/api/auth/google/login" />);
    const link = screen.getByRole("link", { name: "Sign in with Google" });
    expect(link).toHaveAttribute("href", "/api/auth/google/login");
    expect(screen.getByRole("heading", { level: 1, name: "Singularity" })).toBeInTheDocument();
    expect(screen.getByText("Design master data with a prompt.")).toBeInTheDocument();
  });

  it("renders Thai text", () => {
    render(<Home locale="th" loginUrl="/x" />);
    expect(screen.getByRole("link", { name: "เข้าสู่ระบบด้วย Google" })).toBeInTheDocument();
    expect(screen.getByText("ออกแบบข้อมูลหลักด้วยการสั่งงาน")).toBeInTheDocument();
  });

  it("while loading it is the same screen without the sign-in link", () => {
    render(<Home locale="en" loginUrl="/x" loading />);
    expect(screen.getByRole("heading", { level: 1, name: "Singularity" })).toBeInTheDocument();
    expect(screen.getByText("Design master data with a prompt.")).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Loading…");
  });
});

describe("i18n", () => {
  it("has the same keys in English and Thai", () => {
    for (const key of ["signIn", "role_viewer", "role_editor", "role_approver", "forbiddenBody", "gateLine"] as const) {
      expect(t("en", key)).toBeTruthy();
      expect(t("th", key)).toBeTruthy();
    }
  });
});

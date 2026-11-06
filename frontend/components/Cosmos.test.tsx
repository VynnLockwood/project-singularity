import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Cosmos } from "./Cosmos";

describe("Cosmos background", () => {
  it("renders a decorative canvas and does not crash without a 2D context (jsdom)", () => {
    const { container, unmount } = render(<Cosmos />);
    const c = container.querySelector("canvas.cosmos");
    expect(c).not.toBeNull();
    expect(c).toHaveAttribute("aria-hidden", "true");
    unmount();
  });
});

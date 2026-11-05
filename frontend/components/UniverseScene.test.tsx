import { render, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { UniverseScene, webglAvailable } from "./UniverseScene";

describe("UniverseScene without WebGL (jsdom)", () => {
  it("reports no WebGL", () => {
    expect(webglAvailable()).toBe(false);
  });

  it("asks the parent to fall back instead of crashing", async () => {
    const onUnavailable = vi.fn();
    render(<UniverseScene universe={{ bodies: [], links: [], engine: "js" }} selected={null} onSelect={() => {}} onUnavailable={onUnavailable} />);
    await waitFor(() => expect(onUnavailable).toHaveBeenCalled(), { timeout: 4000 });
  });
});

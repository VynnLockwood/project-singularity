import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { Change, Me } from "@/lib/api";
import { change } from "@/lib/fixtures";
import { ReviewTray } from "./ReviewTray";

const me: Me = { id: 1, email: "a@x.com", name: "", role: "approver" };

function Host({ changes, deciding = null, onOutside }: { changes: Change[]; deciding?: number | null; onOutside?: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>Open review</button>
      <button onClick={onOutside}>Outside</button>
      <ReviewTray me={me} changes={changes} open={open} onClose={() => setOpen(false)} onDecide={vi.fn()} deciding={deciding} />
    </>
  );
}

describe("ReviewTray", () => {
  it("renders nothing while closed", () => {
    render(<Host changes={[change({ id: 7 })]} />);
    expect(screen.queryByRole("complementary")).toBeNull();
  });

  it("lists what is waiting, oldest first, with a count", () => {
    render(<Host changes={[change({ id: 8 }), change({ id: 7 })]} />);
    fireEvent.click(screen.getByText("Open review"));
    const tray = screen.getByRole("complementary", { name: "Needs review" });
    const cards = [...tray.querySelectorAll("section.change")].map((n) => n.getAttribute("aria-label"));
    expect(cards).toEqual(["Change #7", "Change #8"]);
    expect(screen.getByText("2 waiting")).toBeInTheDocument();
  });

  it("says so when nothing is waiting", () => {
    render(<Host changes={[]} />);
    fireEvent.click(screen.getByText("Open review"));
    expect(screen.getByText("Nothing is waiting for review.")).toBeInTheDocument();
  });

  it("takes focus on open and gives it back on Escape", () => {
    render(<Host changes={[change({ id: 7 })]} />);
    const opener = screen.getByText("Open review");
    opener.focus();
    fireEvent.click(opener);
    const tray = screen.getByRole("complementary");
    expect(tray.contains(document.activeElement)).toBe(true);
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    expect(screen.queryByRole("complementary")).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it("is not a modal: the page behind stays usable", () => {
    const onOutside = vi.fn();
    render(<Host changes={[change({ id: 7 })]} onOutside={onOutside} />);
    fireEvent.click(screen.getByText("Open review"));
    expect(document.querySelector('[aria-modal="true"]')).toBeNull();
    fireEvent.click(screen.getByText("Outside"));
    expect(onOutside).toHaveBeenCalled();
  });

  it("survives the open change being decided somewhere else", () => {
    const { rerender } = render(<Host changes={[change({ id: 7 }), change({ id: 8 })]} />);
    fireEvent.click(screen.getByText("Open review"));
    const tray = screen.getByRole("complementary");
    rerender(<Host changes={[change({ id: 8 })]} />);
    expect(screen.queryByLabelText("Change #7")).toBeNull();
    expect(screen.getByLabelText("Change #8")).toBeInTheDocument();
    expect(tray.contains(document.activeElement)).toBe(true);
  });

  it("disables only the card being decided", () => {
    render(<Host changes={[change({ id: 7 }), change({ id: 8 })]} deciding={7} />);
    fireEvent.click(screen.getByText("Open review"));
    const buttons = (id: number) => [...screen.getByLabelText(`Change #${id}`).querySelectorAll<HTMLButtonElement>(".actions button")];
    expect(buttons(7).every((b) => b.disabled)).toBe(true);
    expect(buttons(8).every((b) => !b.disabled)).toBe(true);
    expect(screen.getByLabelText("Change #7")).toHaveTextContent("Applying…");
  });
});

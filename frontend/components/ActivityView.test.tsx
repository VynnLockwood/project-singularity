import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Me } from "@/lib/api";
import { change } from "@/lib/fixtures";
import { ActivityView } from "./ActivityView";

vi.mock("./HistoryView", () => ({ HistoryView: () => <p>history pane</p> }));

const me = (role: Me["role"] = "approver"): Me => ({ id: 1, email: "ap@x.com", name: "", role });
const props = (over = {}) => ({ me: me(), tab: "queue" as const, navigate: vi.fn(), pending: [change({ id: 1 }), change({ id: 2 })], live: [], onDecide: vi.fn(), ...over });

describe("ActivityView", () => {
  it("selects Queue and shows the waiting cards, with the count on the tab", () => {
    render(<ActivityView {...props()} />);
    expect(screen.getByRole("tab", { name: /Queue/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: /Queue/ })).toHaveTextContent("2");
    expect(screen.getAllByRole("region", { name: /Change #/ })).toHaveLength(2);
  });

  it("shows no count when nothing waits", () => {
    render(<ActivityView {...props({ pending: [] })} />);
    expect(screen.getByRole("tab", { name: "Queue" })).toBeInTheDocument();
  });

  it("shows History when asked and switches by navigating", () => {
    const navigate = vi.fn();
    const { rerender } = render(<ActivityView {...props({ navigate })} />);
    fireEvent.click(screen.getByRole("tab", { name: /History/ }));
    expect(navigate).toHaveBeenCalledWith({ view: "activity", tab: "history" });
    rerender(<ActivityView {...props({ navigate, tab: "history" })} />);
    expect(screen.getByText("history pane")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /Queue/ }));
    expect(navigate).toHaveBeenLastCalledWith({ view: "activity" });
  });

  it("gives a viewer both tabs", () => {
    render(<ActivityView {...props({ me: me("viewer"), pending: [] })} />);
    expect(screen.getAllByRole("tab")).toHaveLength(2);
  });
});

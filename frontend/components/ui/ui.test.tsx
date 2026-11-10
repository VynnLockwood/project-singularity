import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Badge, Button, EmptyState, Icon, Kbd, Panel, ProgressBar, Skeleton, statusMeta } from "@/components/ui";

describe("Icon", () => {
  it("is decorative unless it is given a label", () => {
    const { container } = render(<Icon name="check" />);
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });
  it("is an image with a name when labelled", () => {
    render(<Icon name="check" label="Applied" />);
    expect(screen.getByRole("img", { name: "Applied" })).toBeInTheDocument();
  });
});

describe("Button", () => {
  it("is a link when it has an href", () => {
    render(<Button href="/x">Go</Button>);
    expect(screen.getByRole("link", { name: "Go" })).toHaveAttribute("href", "/x");
  });
  it("is a plain button that does not submit forms by default", () => {
    render(<Button variant="danger">Drop</Button>);
    const b = screen.getByRole("button", { name: "Drop" });
    expect(b).toHaveAttribute("type", "button");
    expect(b).toHaveAttribute("data-variant", "danger");
  });
  it("can be a submit button", () => {
    render(<Button type="submit">Save</Button>);
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute("type", "submit");
  });
});

describe("statusMeta and Badge", () => {
  it("maps each change status to a tone, an icon and a word", () => {
    expect(statusMeta("applied")).toEqual({ tone: "ok", icon: "check", label: "Applied" });
    expect(statusMeta("pending")).toMatchObject({ tone: "wait", label: "Waiting" });
    expect(statusMeta("rejected")).toMatchObject({ tone: "bad", label: "Rejected" });
    expect(statusMeta("failed")).toMatchObject({ tone: "bad", label: "Failed" });
  });
  it("shows an unknown status as itself, in the idle tone", () => {
    expect(statusMeta("archived")).toMatchObject({ tone: "idle", label: "archived" });
  });
  it("never shows status by colour alone: a word and an icon", () => {
    const { container } = render(<Badge status="rejected" />);
    expect(screen.getByText("Rejected")).toBeInTheDocument();
    expect(container.querySelector("svg")).not.toBeNull();
    expect(container.firstElementChild).toHaveAttribute("data-tone", "bad");
  });
});

describe("feedback parts", () => {
  it("ProgressBar is only a progressbar while active", () => {
    const { rerender } = render(<ProgressBar active={false} />);
    expect(screen.queryByRole("progressbar")).toBeNull();
    rerender(<ProgressBar active />);
    expect(screen.getByRole("progressbar", { name: "Loading" })).toBeInTheDocument();
  });
  it("Skeleton is hidden from assistive tech and draws the rows asked for", () => {
    const { container } = render(<Skeleton rows={4} />);
    expect(container.firstElementChild).toHaveAttribute("aria-hidden", "true");
    expect(container.querySelectorAll(".ui-skeleton-row")).toHaveLength(4);
  });
  it("EmptyState names the place and offers one action", () => {
    render(<EmptyState icon="inbox" title="No tables yet" action={<Button>New table</Button>}>Create the first one.</EmptyState>);
    expect(screen.getByRole("heading", { name: "No tables yet" })).toBeInTheDocument();
    expect(screen.getByText("Create the first one.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New table" })).toBeInTheDocument();
  });
  it("Panel shows its title as a heading and its actions", () => {
    render(<Panel title="Rules" actions={<Button>Add</Button>}>body</Panel>);
    expect(screen.getByRole("heading", { name: "Rules" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add" })).toBeInTheDocument();
  });
  it("Kbd renders a keyboard key", () => {
    const { container } = render(<Kbd>K</Kbd>);
    expect(container.querySelector("kbd")).toHaveTextContent("K");
  });
});

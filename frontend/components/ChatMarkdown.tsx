"use client";

import { isValidElement, type ComponentProps, type ReactNode } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Button, useToast } from "@/components/ui";

const SAFE_URL = /^(https?:|mailto:)/i;

function textOf(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children);
  return "";
}

function CodeBlock({ children }: { children?: ReactNode }) {
  const toast = useToast();
  const code = isValidElement<{ className?: string; children?: ReactNode }>(children) ? children : null;
  const lang = /language-([\w-]+)/.exec(code?.props.className ?? "")?.[1];
  const text = textOf(code?.props.children ?? children).replace(/\n$/, "");
  const copy = () => navigator.clipboard?.writeText(text).then(() => toast.success("Copied"), () => toast.error("Could not copy"));
  return (
    <div className="md-code">
      <div className="md-code-bar">
        {lang && <span className="md-lang">{lang}</span>}
        <Button size="sm" variant="ghost" icon="copy" onClick={copy}>Copy</Button>
      </div>
      <pre>{children}</pre>
    </div>
  );
}

const heading = (tag: "h3" | "h4" | "h5" | "h6") => {
  const Tag = tag;
  return function Heading({ children }: ComponentProps<"h1">) { return <Tag>{children}</Tag>; };
};

const components: Components = {
  h1: heading("h3"), h2: heading("h4"), h3: heading("h5"), h4: heading("h6"), h5: heading("h6"), h6: heading("h6"),
  a: ({ href, children }) =>
    href && SAFE_URL.test(href) ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
  table: ({ children }) => <div className="md-scroll"><table>{children}</table></div>,
  pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
};

/** A model reply as formatted text. Raw HTML is never rendered; only http, https and mailto links are live. */
export function ChatMarkdown({ text }: { text: string }) {
  return (
    <div className="md">
      <Markdown remarkPlugins={[remarkGfm]} components={components} urlTransform={(url) => (SAFE_URL.test(url) ? url : "")}>
        {text}
      </Markdown>
    </div>
  );
}

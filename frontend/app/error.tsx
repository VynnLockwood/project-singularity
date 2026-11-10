"use client";

import { Button, Panel } from "@/components/ui";

/** Shown instead of Next's blank "Application error" page, with the real message so it can be reported. */
export default function ErrorPage({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <main className="center">
      <Panel title="Something went wrong" className="center-panel">
        <p className="problems" role="alert">{error.message || "Unexpected error"}</p>
        {error.digest && <p className="muted small">Reference: <code>{error.digest}</code></p>}
        <div className="actions">
          <Button variant="primary" icon="refresh" onClick={reset}>Reload</Button>
          <Button variant="ghost" href="/">Go to Chat</Button>
        </div>
      </Panel>
    </main>
  );
}

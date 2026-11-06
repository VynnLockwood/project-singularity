"use client";

/** Shown instead of Next's blank "Application error" page, with the real message so it can be reported. */
export default function ErrorPage({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <main className="center">
      <h1 className="title">Something went wrong</h1>
      <p className="problems" role="alert">{error.message || "Unexpected error"}</p>
      <button className="button" onClick={reset}>Try again</button>
    </main>
  );
}

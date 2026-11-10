import { Button, EmptyState } from "@/components/ui";

export function NotFound({ path, onHome }: { path: string; onHome: () => void }) {
  return (
    <EmptyState icon="search" title="Page not found" action={<Button variant="primary" onClick={onHome}>Go to Chat</Button>}>
      Nothing lives at <code>{path}</code>. The link may be old or mistyped.
    </EmptyState>
  );
}

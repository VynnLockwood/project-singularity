import type { Change } from "./api";

export type ServerEvent = { type: "change"; change: Change };

/** Subscribes to /ws/events, reconnecting with backoff. Returns a function that stops it. */
export function connectEvents(onEvent: (e: ServerEvent) => void, onState?: (open: boolean) => void): () => void {
  let ws: WebSocket | null = null;
  let stopped = false;
  let delay = 500;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const open = () => {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws/events`);
    ws.onopen = () => {
      delay = 500;
      onState?.(true);
    };
    ws.onmessage = (m) => {
      try {
        const data = JSON.parse(m.data);
        if (data?.type === "change") onEvent(data as ServerEvent);
      } catch {
        /* ignore malformed frames */
      }
    };
    ws.onclose = (e) => {
      onState?.(false);
      if (stopped || e.code === 4401) return;
      timer = setTimeout(open, delay);
      delay = Math.min(delay * 2, 15000);
    };
  };
  open();
  return () => {
    stopped = true;
    clearTimeout(timer);
    ws?.close();
  };
}

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError, createConversation, getConversation, listConversations, sendMessage,
  type Change, type ChatMessage, type ConversationSummary, type Me,
} from "@/lib/api";
import { ChangeCard } from "./ChangeCard";

interface Props {
  me: Me;
  /** Latest version of every change we know of (kept fresh by WebSocket events). */
  live: Change[];
  onDecide: (change: Change, action: "approve" | "reject") => Promise<void> | void;
  busy?: boolean;
  /** Text to place in the message box (from search); cleared via onDraftUsed. */
  draft?: string;
  onDraftUsed?: () => void;
}

export function ChatView({ me, live, onDecide, busy, draft, onDraftUsed }: Props) {
  const [convs, setConvs] = useState<ConversationSummary[]>([]);
  const [current, setCurrent] = useState<number | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [changes, setChanges] = useState<Change[]>([]);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const bottom = useRef<HTMLDivElement>(null);

  const refreshList = useCallback(() => listConversations().then(setConvs).catch(() => {}), []);
  useEffect(() => { refreshList(); }, [refreshList]);
  useEffect(() => {
    if (draft) {
      setText(draft);
      onDraftUsed?.();
    }
  }, [draft, onDraftUsed]);
  useEffect(() => { bottom.current?.scrollIntoView?.({ block: "end" }); }, [messages, changes]);

  const open = async (id: number) => {
    setCurrent(id);
    setError("");
    const d = await getConversation(id);
    setMessages(d.messages);
    setChanges(d.changes);
  };

  const newChat = () => {
    setCurrent(null);
    setMessages([]);
    setChanges([]);
    setError("");
  };

  const submit = async () => {
    const content = text.trim();
    if (!content || sending) return;
    setSending(true);
    setError("");
    setText("");
    try {
      let id = current;
      if (id === null) {
        id = (await createConversation()).id;
        setCurrent(id);
      }
      setMessages((m) => [...m, { id: -Date.now(), role: "user", content }]);
      const r = await sendMessage(id, content);
      setMessages((m) => [...m.filter((x) => x.id >= 0), ...r.messages]);
      setChanges(r.changes);
      refreshList();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong. Try again.");
      setMessages((m) => m.filter((x) => x.id >= 0));
      setText(content);
    } finally {
      setSending(false);
    }
  };

  // Show the freshest copy of each change: live events override what the last fetch returned.
  const merged = changes.map((c) => live.find((l) => l.id === c.id) ?? c);
  const visible = messages.filter((m) => m.role !== "tool");

  return (
    <div className="chat">
      <aside className="chat-list" aria-label="Conversations">
        <button className="button" onClick={newChat}>New chat</button>
        <ul>
          {convs.map((c) => (
            <li key={c.id}>
              <button className="link" data-active={c.id === current} onClick={() => open(c.id)}>
                {c.title || "Untitled"}
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <div className="chat-main">
        <div className="chat-scroll" aria-live="polite">
          {visible.length === 0 && (
            <p className="muted">
              {me.role === "viewer"
                ? "Ask about the schema or the data."
                : "Describe what you need, e.g. “add a customer table with a unique customer code”."}
            </p>
          )}
          {visible.map((m) => (
            <div key={m.id} className="bubble" data-role={m.role}>
              {m.content && <p>{m.content}</p>}
              {m.toolCalls && m.toolCalls.length > 0 && <p className="muted small">used: {m.toolCalls.join(", ")}</p>}
            </div>
          ))}
          {merged.map((c) => (
            <ChangeCard key={c.id} change={c} me={me} onDecide={onDecide} busy={busy} />
          ))}
          {sending && <p className="muted">Thinking…</p>}
          <div ref={bottom} />
        </div>
        {error && <p className="problems" role="alert">{error}</p>}
        <form className="composer" onSubmit={(e) => { e.preventDefault(); submit(); }}>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
            placeholder="Message Singularity…"
            aria-label="Message"
            rows={2}
          />
          <button className="button" type="submit" disabled={sending || !text.trim()}>Send</button>
        </form>
      </div>
    </div>
  );
}


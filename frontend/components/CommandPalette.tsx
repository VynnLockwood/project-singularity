"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { getGraph, getSchemas, type Me } from "@/lib/api";
import { navFor } from "@/lib/nav";
import type { Command } from "@/lib/palette";
import type { Navigate } from "@/lib/useRoute";
import { Palette } from "@/components/ui";

interface Props {
  me: Me;
  open: boolean;
  onClose: () => void;
  navigate: Navigate;
  /** Bumps when the schema changes, so the table list is read again. */
  refreshKey: number;
  onLogout: () => void;
}

interface TableRef { schema: string; table: string }

/** The command palette's content: every view, every table, and the actions this role may take. */
export function CommandPalette({ me, open, onClose, navigate, refreshKey, onLogout }: Props) {
  const [tables, setTables] = useState<TableRef[]>([]);
  const opened = useRef(false);
  if (open) opened.current = true;

  // Tables load the first time the palette opens and again after a schema change. If the load
  // fails the palette still works for views and actions.
  useEffect(() => {
    if (!opened.current) return;
    let current = true;
    getSchemas()
      .then((schemas) => Promise.all(schemas.map((s) =>
        getGraph(s.name).then((g) => g.tables.map((t) => ({ schema: s.name, table: t.name }))).catch(() => [] as TableRef[]))))
      .then((lists) => { if (current) setTables(lists.flat()); })
      .catch(() => {});
    return () => { current = false; };
  }, [open && opened.current, refreshKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const commands = useMemo<Command[]>(() => {
    const go: Command[] = navFor(me.role).flatMap((g) => g.items).map((item) => ({
      id: `go:${item.view}`, label: `Go to ${item.label}`, group: "Go to", icon: item.icon, run: () => navigate({ view: item.view }),
    }));
    const tbl: Command[] = tables.map(({ schema, table }) => ({
      id: `table:${schema}.${table}`, label: `${schema}.${table}`, hint: "Table", group: "Tables", icon: "table",
      run: ({ shift }) => navigate({ view: shift ? "data" : "database", schema, table }),
    }));
    const act: Command[] = [
      { id: "act:chat", label: "New chat", group: "Actions", icon: "plus", run: () => navigate({ view: "chat" }) },
      ...(me.role === "viewer" ? [] : [
        { id: "act:table", label: "New table", hint: "Opens Database", group: "Actions", icon: "plus", run: () => navigate({ view: "database" }) },
        { id: "act:link", label: "Create connection link", hint: "Opens Connect", group: "Actions", icon: "plug", run: () => navigate({ view: "connect" }) },
      ] satisfies Command[]),
      { id: "act:logout", label: "Sign out", group: "Actions", icon: "logout", run: onLogout },
    ];
    return [...go, ...tbl, ...act];
  }, [me.role, tables, navigate, onLogout]);

  return <Palette open={open} onClose={onClose} commands={commands} />;
}

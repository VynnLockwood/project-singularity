"use client";

import { useEffect, useRef, useState } from "react";
import { getTable, type Change, type Me, type TableDetail } from "@/lib/api";
import { usePersistentFlag } from "@/lib/storage";
import {
  addColumnOp, alterColumnTypeOp, COLUMN_TYPES, dropColumnOp, normalizeType, renameColumnOp, setColumnCommentOp,
  validateName, validateType, type ColumnSpec,
} from "@/lib/databaseOps";
import { ProposeDialog } from "./ProposeDialog";
import { Button, Icon, Menu, Panel } from "./ui";

type Action =
  | { kind: "add" }
  | { kind: "rename" | "type" | "comment" | "drop"; column: string };

export function FieldsTab({ me, schema, table, refreshKey, onProposed }: {
  me: Me; schema: string; table: string; refreshKey: number; onProposed: (c: Change) => void;
}) {
  const [detail, setDetail] = useState<TableDetail | null>(null);
  const [error, setError] = useState("");
  const [showAudit, setShowAudit] = usePersistentFlag("db.showAudit", false);
  const [action, setAction] = useState<Action | null>(null);
  const [review, setReview] = useState(false);
  const [text, setText] = useState("");
  // add-field form
  const [fType, setFType] = useState("text");
  const [customType, setCustomType] = useState(false);
  const [fNullable, setFNullable] = useState("");
  const [fUnique, setFUnique] = useState(false);
  const [fDefault, setFDefault] = useState("");
  const [fComment, setFComment] = useState("");
  const seq = useRef(0);
  const canWrite = me.role !== "viewer";

  // Table or database switched: drop the old detail and any open form straight away.
  useEffect(() => {
    setDetail(null);
    setAction(null);
    setReview(false);
  }, [schema, table]);

  useEffect(() => {
    const id = ++seq.current;
    setError("");
    getTable(table, schema)
      .then((d) => { if (id === seq.current) setDetail(d); })
      .catch(() => { if (id === seq.current) setError("Could not load the fields."); });
    return () => { seq.current++; };
  }, [schema, table, refreshKey]);

  const hidden = new Set(detail?.hiddenColumns ?? []);
  const columns = (detail?.columns ?? []).filter((c) => showAudit || !hidden.has(c.name));
  const taken = (detail?.columns ?? []).map((c) => c.name);

  const open = (a: Action, initial = "") => {
    setAction(a);
    setReview(false);
    setText(initial);
    setFType("text"); setCustomType(false); setFNullable(""); setFUnique(false); setFDefault(""); setFComment("");
  };
  const close = () => { setAction(null); setReview(false); };

  const current = action && action.kind !== "add" ? detail?.columns.find((c) => c.name === action.column) : undefined;
  const nameTrim = text.trim();

  let fieldError: string | null = null;
  if (action?.kind === "add") fieldError = text === "" ? null : validateName(text, taken) ?? validateType(fType);
  if (action?.kind === "rename") fieldError = text === "" ? null : validateName(text, taken);
  if (action?.kind === "type") fieldError = text === "" ? null : validateType(text);
  const typeBlocked = action?.kind === "add" && validateType(fType) !== null;
  const canReview =
    action?.kind === "comment" ? true
    : action?.kind === "add" ? text !== "" && !fieldError && !typeBlocked
    : action?.kind === "type" ? text !== "" && !fieldError && normalizeType(text) !== normalizeType(current?.type ?? "")
    : text !== "" && !fieldError;

  const spec = (): ColumnSpec => {
    const c: ColumnSpec = { name: text, type: fType };
    if (fNullable) c.nullable = fNullable === "yes";
    if (fUnique) c.unique = true;
    if (fDefault.trim()) c.default = fDefault;
    if (fComment.trim()) c.comment = fComment.trim();
    return c;
  };

  const dialog = () => {
    if (!action || !review) return null;
    const common = { me, schema, onClose: close, onBack: () => setReview(false), onProposed };
    switch (action.kind) {
      case "add":
        return <ProposeDialog {...common} title={`Add field ${table}.${text}`} build={() => [addColumnOp(table, spec())]} />;
      case "rename":
        return <ProposeDialog {...common} title={`Rename field ${table}.${action.column} to ${text}`} build={() => [renameColumnOp(table, action.column, text)]} />;
      case "type":
        return <ProposeDialog {...common} title={`Change type of ${table}.${action.column}`} build={() => [alterColumnTypeOp(table, action.column, text)]} />;
      case "comment":
        return <ProposeDialog {...common} title={`Comment on ${table}.${action.column}`} build={() => [setColumnCommentOp(table, action.column, text.trim())]} />;
      case "drop":
        return <ProposeDialog {...common} destructive title={`Drop field ${table}.${action.column}`} build={(reason) => [dropColumnOp(table, action.column, reason)]} />;
    }
  };

  if (error) return <p className="problems">{error}</p>;
  if (!detail) return <p className="muted">Loading…</p>;

  const formOpen = canWrite && action && action.kind !== "drop" && !review;
  // "PRIMARY KEY (id)" / "UNIQUE (email)": only a constraint on this one column counts.
  const single = (kind: string, column: string) =>
    detail.constraints.some((k) => {
      if (k.kind !== kind) return false;
      const cols = /\(([^)]*)\)/.exec(k.definition)?.[1].split(",").map((x) => x.trim().replace(/^"|"$/g, ""));
      return cols?.length === 1 && cols[0] === column;
    });
  return (
    <div className="fields-tab">
      <div className="grid-bar">
        <label>
          <input type="checkbox" checked={showAudit} onChange={(e) => setShowAudit(e.target.checked)} />{" "}
          Show audit columns
        </label>
        {canWrite && <Button size="sm" icon="plus" onClick={() => open({ kind: "add" })}>Add field</Button>}
      </div>
      {columns.length === 0 ? <p className="muted">No fields.</p> : (
        <Panel className="fields-panel">
          <div className="schema-scroll">
            <table className="fields-table">
              <thead>
                <tr><th>Name</th><th>Type</th><th>Null?</th><th>Default</th><th>Comment</th>{canWrite && <th><span className="visually-hidden">Actions</span></th>}</tr>
              </thead>
              <tbody>
                {columns.map((c) => (
                  <tr key={c.name}>
                    <td>
                      {single("primary_key", c.name) && <Icon name="key" size={14} label="primary key" />}{" "}
                      {c.name}
                      {single("unique", c.name) && <>{" "}<span className="fields-badge">unique</span></>}
                    </td>
                    <td><code>{c.type}</code></td>
                    <td>{c.nullable ? <span className="muted">null</span> : "not null"}</td>
                    <td>{c.default ? <code>{c.default}</code> : ""}</td>
                    <td>{c.comment ?? ""}</td>
                    {canWrite && (
                      <td>
                        {!hidden.has(c.name) && (
                          <Menu
                            label={`More actions for ${c.name}`}
                            items={[
                              { label: "Rename", ariaLabel: `Rename ${c.name}`, onSelect: () => open({ kind: "rename", column: c.name }) },
                              { label: "Type", ariaLabel: `Type ${c.name}`, onSelect: () => open({ kind: "type", column: c.name }, validateType(c.type) === null ? normalizeType(c.type) : "") },
                              { label: "Comment", ariaLabel: `Comment ${c.name}`, onSelect: () => open({ kind: "comment", column: c.name }, c.comment ?? "") },
                              { label: "Drop", ariaLabel: `Drop ${c.name}`, danger: true, onSelect: () => { setAction({ kind: "drop", column: c.name }); setReview(true); } },
                            ]}
                          />
                        )}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
      {formOpen && action && (
        <Panel className="fields-form" title={action.kind === "add" ? "Add field" : `${action.kind === "rename" ? "Rename" : action.kind === "type" ? "Change type of" : "Comment on"} ${action.column}`}>
        <form
          className="db-inline-form"
          onSubmit={(e) => { e.preventDefault(); if (canReview) setReview(true); }}
        >
          {action.kind === "add" ? (
            <>
              <label>Field name<input value={text} onChange={(e) => setText(e.target.value)} aria-invalid={fieldError ? "true" : undefined} autoFocus /></label>
              <label>Field type
                <select value={COLUMN_TYPES.includes(fType) ? fType : ""} onChange={(e) => { setCustomType(false); setFType(e.target.value); }}>
                  <option value="" disabled>Custom</option>
                  {COLUMN_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </label>
              {(customType || !COLUMN_TYPES.includes(fType) || validateType(fType) !== null) && (
                <label>Exact type<input value={fType} onChange={(e) => { setCustomType(true); setFType(e.target.value); }} aria-invalid={typeBlocked ? "true" : undefined} /></label>
              )}
              <label>Nullable
                <select value={fNullable} onChange={(e) => setFNullable(e.target.value)}>
                  <option value="">Default</option><option value="yes">Yes</option><option value="no">No</option>
                </select>
              </label>
              <label><span><input type="checkbox" checked={fUnique} onChange={(e) => setFUnique(e.target.checked)} /> Unique</span></label>
              <label>Default<input value={fDefault} onChange={(e) => setFDefault(e.target.value)} /></label>
              <label>Field comment<input value={fComment} onChange={(e) => setFComment(e.target.value)} /></label>
            </>
          ) : action.kind === "type" ? (
            <>
              <label>New type
                <select value={COLUMN_TYPES.includes(text) ? text : ""} onChange={(e) => { setCustomType(false); setText(e.target.value); }} autoFocus>
                  <option value="" disabled>Choose a type</option>
                  {COLUMN_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </label>
              {text !== "" && (customType || !COLUMN_TYPES.includes(text) || validateType(text) !== null) && (
                <label>Exact type<input value={text} onChange={(e) => { setCustomType(true); setText(e.target.value); }} aria-invalid={fieldError ? "true" : undefined} /></label>
              )}
            </>
          ) : (
            <label>
              {action.kind === "rename" ? "New name" : "Comment"}
              <input value={text} onChange={(e) => setText(e.target.value)} aria-invalid={fieldError ? "true" : undefined} autoFocus />
            </label>
          )}
          {action.kind === "add" && typeBlocked && <p className="problems small" role="alert">{validateType(fType)}</p>}
          {fieldError && !(action.kind === "add" && typeBlocked) && <p className="problems small" role="alert">{fieldError}</p>}
          <div>
            <Button type="submit" variant="primary" size="sm" disabled={!canReview}>Review</Button>{" "}
            <Button type="button" variant="ghost" size="sm" onClick={close}>Cancel</Button>
          </div>
        </form>
        </Panel>
      )}
      {canWrite && dialog()}
    </div>
  );
}

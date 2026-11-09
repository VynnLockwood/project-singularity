"use client";

import { useRef, useState } from "react";
import type { Change, GraphTable, Me } from "@/lib/api";
import { COLUMN_TYPES, createTableOp, validateName, validateType, type ColumnSpec } from "@/lib/databaseOps";
import { ProposeDialog } from "./ProposeDialog";

interface Row {
  id: number;
  name: string;
  type: string;
  nullable: string; // "" = server default, "yes", "no"
  unique: boolean;
  def: string;
  comment: string;
  ref: string; // referenced table, "" = none
}

const blank = (id: number): Row => ({ id, name: "", type: "text", nullable: "", unique: false, def: "", comment: "", ref: "" });

/** Name, comment and columns for a new table in `schema`; validates live, then opens the review dialog. */
export function NewTableForm({ me, schema, tables, onClose, onProposed }: {
  me: Me; schema: string; tables: GraphTable[]; onClose: () => void; onProposed: (c: Change, name: string) => void;
}) {
  const nextId = useRef(2);
  const [name, setName] = useState("");
  const [comment, setComment] = useState("");
  const [rows, setRows] = useState<Row[]>([blank(1)]);
  const [submitted, setSubmitted] = useState<{ name: string; comment: string; columns: ColumnSpec[] } | null>(null);

  const patch = (id: number, p: Partial<Row>) => setRows((rs) => rs.map((r) => (r.id === id ? { ...r, ...p } : r)));

  const tableError = name === "" ? null : validateName(name, tables.map((t) => t.name));
  const rowErrors = rows.map((r) => {
    const others = rows.filter((o) => o.id !== r.id).map((o) => o.name).filter(Boolean);
    const nameErr = r.name === "" ? null : validateName(r.name, others);
    return { name: nameErr && nameErr.endsWith("already exists.") ? `Duplicate field name "${r.name}".` : nameErr, type: validateType(r.type) };
  });
  const valid =
    name !== "" && !tableError && rows.length > 0 &&
    rows.every((r, i) => r.name !== "" && !rowErrors[i].name && !rowErrors[i].type);

  const specs = (): ColumnSpec[] =>
    rows.map((r) => {
      const c: ColumnSpec = { name: r.name, type: r.type };
      if (r.nullable) c.nullable = r.nullable === "yes";
      if (r.unique) c.unique = true;
      if (r.def.trim()) c.default = r.def;
      if (r.comment.trim()) c.comment = r.comment.trim();
      if (r.ref) c.references = { table: r.ref };
      return c;
    });

  // Latched at submit: a refresh that adds this table to `tables` must not unmount the dialog.
  if (submitted) {
    return (
      <ProposeDialog
        me={me}
        schema={schema}
        title={`Create table ${submitted.name}`}
        build={() => [createTableOp(submitted.name, submitted.columns, submitted.comment || undefined)]}
        onClose={onClose}
        onProposed={(c) => onProposed(c, submitted.name)}
      />
    );
  }

  return (
    <form
      className="db-inline-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (valid) setSubmitted({ name, comment: comment.trim(), columns: specs() });
      }}
    >
      <label>
        Table name
        <input value={name} onChange={(e) => setName(e.target.value)} aria-invalid={tableError ? "true" : undefined} autoFocus />
      </label>
      {tableError && <p className="problems small" role="alert">{tableError}</p>}
      <label>
        Table comment (optional)
        <input value={comment} onChange={(e) => setComment(e.target.value)} />
      </label>
      <p className="muted small">Audit columns are added automatically.</p>
      {rows.map((r, i) => {
        const n = i + 1;
        const err = rowErrors[i];
        return (
          <fieldset key={r.id}>
            <legend>Field {n}</legend>
            <label>
              Field {n} name
              <input value={r.name} onChange={(e) => patch(r.id, { name: e.target.value })} aria-invalid={err.name ? "true" : undefined} />
            </label>
            <label>
              Field {n} type
              <select value={COLUMN_TYPES.includes(r.type) ? r.type : ""} onChange={(e) => patch(r.id, { type: e.target.value })}>
                <option value="" disabled>Custom</option>
                {COLUMN_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </label>
            {(!COLUMN_TYPES.includes(r.type) || err.type !== null) && (
              <label>
                Field {n} exact type
                <input value={r.type} onChange={(e) => patch(r.id, { type: e.target.value })} aria-invalid={err.type ? "true" : undefined} />
              </label>
            )}
            <label>
              Field {n} nullable
              <select value={r.nullable} onChange={(e) => patch(r.id, { nullable: e.target.value })}>
                <option value="">Default</option><option value="yes">Yes</option><option value="no">No</option>
              </select>
            </label>
            <label>
              <span><input type="checkbox" checked={r.unique} onChange={(e) => patch(r.id, { unique: e.target.checked })} /> Field {n} unique</span>
            </label>
            <label>
              Field {n} default
              <input value={r.def} onChange={(e) => patch(r.id, { def: e.target.value })} />
            </label>
            <label>
              Field {n} comment
              <input value={r.comment} onChange={(e) => patch(r.id, { comment: e.target.value })} />
            </label>
            <label>
              Field {n} references
              <select value={r.ref} onChange={(e) => patch(r.id, { ref: e.target.value })}>
                <option value="">No reference</option>
                {tables.map((t) => <option key={t.name} value={t.name}>{t.name}</option>)}
              </select>
            </label>
            {r.ref && <span className="muted small">Points to the primary key of {r.ref}.</span>}
            {(err.name || err.type) && <p className="problems small" role="alert">{err.name ?? err.type}</p>}
            {rows.length > 1 && (
              <button type="button" className="link" aria-label={`Remove field ${n}`} onClick={() => setRows((rs) => rs.filter((x) => x.id !== r.id))}>
                Remove
              </button>
            )}
          </fieldset>
        );
      })}
      {rows.length === 0 && <p className="problems small" role="alert">Add at least one field.</p>}
      <div>
        <button type="button" className="link" onClick={() => setRows((rs) => [...rs, blank(nextId.current++)])}>Add field</button>
      </div>
      <div>
        <button type="submit" className="button" disabled={!valid}>Review</button>{" "}
        <button type="button" className="link" onClick={onClose}>Cancel</button>
      </div>
    </form>
  );
}

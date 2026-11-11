"use client";

import { useEffect, useRef, useState } from "react";
import { getTable, type Change, type Me, type Operation, type SchemaGraph, type TableDetail } from "@/lib/api";
import { addForeignKeyOp, addIndexOp, addUniqueOp, dropConstraintOp, dropIndexOp } from "@/lib/databaseOps";
import { ProposeDialog } from "./ProposeDialog";
import { Button, Panel } from "./ui";

const ON_DELETE = ["restrict", "cascade", "set null", "no action"];
const KIND_LABEL: Record<string, string> = {
  primary_key: "primary key", unique: "unique", check: "check", foreign_key: "foreign key",
};

type Form = "index" | "unique" | "fk";
/** What the review step shows. Captured when the form is submitted, so later edits or reloads cannot change it. */
interface Review { title: string; destructive?: boolean; build: (reason: string) => Operation[] }

export function RelationsTab({ me, schema, table, graph, refreshKey, onProposed, onOpenTable }: {
  me: Me; schema: string; table: string; graph: SchemaGraph | null; refreshKey: number; onProposed: (c: Change) => void;
  onOpenTable: (name: string) => void;
}) {
  const [detail, setDetail] = useState<TableDetail | null>(null);
  const [error, setError] = useState("");
  const [form, setForm] = useState<Form | null>(null);
  const [review, setReview] = useState<Review | null>(null);
  const [cols, setCols] = useState<string[]>([]);
  const [uniqueIdx, setUniqueIdx] = useState(false);
  const [fkCol, setFkCol] = useState("");
  const [refTable, setRefTable] = useState("");
  const [refCol, setRefCol] = useState("");
  const [onDelete, setOnDelete] = useState("");
  const seq = useRef(0);
  const canWrite = me.role !== "viewer";

  useEffect(() => {
    setDetail(null);
    setForm(null);
    setReview(null);
  }, [schema, table]);

  useEffect(() => {
    const id = ++seq.current;
    setError("");
    getTable(table, schema)
      .then((d) => { if (id === seq.current) setDetail(d); })
      .catch(() => { if (id === seq.current) setError("Could not load the relations."); });
    return () => { seq.current++; };
  }, [schema, table, refreshKey]);

  const hidden = new Set(detail?.hiddenColumns ?? []);
  const fields = (detail?.columns ?? []).filter((c) => !hidden.has(c.name));
  const refTables = graph?.tables ?? [];
  const refColumns = refTables.find((t) => t.name === refTable)?.columns ?? [];

  const openForm = (f: Form) => {
    setForm(f); setReview(null);
    setCols([]); setUniqueIdx(false); setFkCol(""); setRefTable(""); setRefCol(""); setOnDelete("");
  };
  const close = () => { setForm(null); setReview(null); };

  const canReview = form === "fk" ? fkCol !== "" && refTable !== "" : cols.length > 0;

  const submit = () => {
    if (!canReview) return;
    if (form === "fk") {
      const [c, rt, rc, od] = [fkCol, refTable, refCol, onDelete];
      setReview({ title: `Add foreign key ${table}.${c}`, build: () => [addForeignKeyOp(table, c, rt, rc || undefined, od || undefined)] });
    } else if (form === "unique") {
      const snap = [...cols];
      setReview({ title: `Add unique constraint on ${table}`, build: () => [addUniqueOp(table, snap)] });
    } else {
      const snap = [...cols];
      const u = uniqueIdx;
      setReview({ title: `Add index on ${table}`, build: () => [addIndexOp(table, snap, u)] });
    }
  };

  const dropConstraint = (name: string) =>
    setReview({ title: `Drop constraint ${table}.${name}`, destructive: true, build: (r) => [dropConstraintOp(table, name, r)] });
  const dropIndex = (name: string) =>
    setReview({ title: `Drop index ${name}`, destructive: true, build: (r) => [dropIndexOp(name, r)] });

  if (error) return <p className="problems">{error}</p>;
  if (!detail) return <p className="muted">Loading…</p>;

  const pointsTo = (graph?.relations ?? []).filter((r) => r.from.table === table);
  const referencedBy = (graph?.relations ?? []).filter((r) => r.to.table === table);
  const constraintNames = new Set(detail.constraints.map((k) => k.name));
  const none = detail.constraints.length === 0 && detail.indexes.length === 0 && pointsTo.length === 0 && referencedBy.length === 0;

  const kindBadge = (kind: string) => <span className="fields-badge" data-kind={kind}>{KIND_LABEL[kind] ?? kind}</span>;
  return (
    <div className="relations-tab">
      {canWrite && (
        <div className="grid-bar">
          <Button size="sm" icon="plus" onClick={() => openForm("index")}>Add index</Button>
          <Button size="sm" icon="plus" onClick={() => openForm("unique")}>Add unique</Button>
          <Button size="sm" icon="plus" onClick={() => openForm("fk")}>Add foreign key</Button>
        </div>
      )}
      {none && <p className="muted">No relations, constraints or indexes.</p>}
      <Panel title="Points to">
        {!graph ? <p className="muted">Loading…</p> : pointsTo.length === 0 ? <p className="muted">Nothing.</p> : (
          <ul className="relation-list">
            {pointsTo.map((r, i) => (
              <li key={i}>
                <span>{r.from.table}.{r.from.column} → {r.to.table}.{r.to.column}</span>
                <Button size="sm" variant="ghost" icon="external" onClick={() => onOpenTable(r.to.table)}>Open {r.to.table}</Button>
              </li>
            ))}
          </ul>
        )}
      </Panel>
      <Panel title="Referenced by">
        {!graph ? <p className="muted">Loading…</p> : referencedBy.length === 0 ? <p className="muted">Nothing.</p> : (
          <ul className="relation-list">
            {referencedBy.map((r, i) => (
              <li key={i}>
                <span>{r.from.table}.{r.from.column} → {r.to.table}.{r.to.column}</span>
                <Button size="sm" variant="ghost" icon="external" onClick={() => onOpenTable(r.from.table)}>Open {r.from.table}</Button>
              </li>
            ))}
          </ul>
        )}
      </Panel>
      <Panel title="Indexes and constraints">
        {detail.constraints.length === 0 ? <p className="muted">No constraints.</p> : (
          <div className="schema-scroll">
            <table>
              <thead><tr><th>Name</th><th>Kind</th><th>Definition</th>{canWrite && <th><span className="visually-hidden">Actions</span></th>}</tr></thead>
              <tbody>
                {detail.constraints.map((k) => (
                  <tr key={k.name}>
                    <td>{k.name}</td><td>{kindBadge(k.kind)}</td><td><code>{k.definition}</code></td>
                    {canWrite && (
                      <td>
                        {k.kind !== "primary_key" && (
                          <Button size="sm" variant="danger" aria-label={`Drop constraint ${k.name}`} onClick={() => { setForm(null); dropConstraint(k.name); }}>Drop</Button>
                        )}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {detail.indexes.length === 0 ? <p className="muted">No indexes.</p> : (
          <div className="schema-scroll">
            <table>
              <thead><tr><th>Name</th><th>Definition</th>{canWrite && <th><span className="visually-hidden">Actions</span></th>}</tr></thead>
              <tbody>
                {detail.indexes.map((x) => (
                  <tr key={x.name}>
                    <td>{x.name}</td><td><code>{x.definition}</code></td>
                    {canWrite && (
                      <td>
                        {/* An index that backs a constraint goes away with that constraint, not on its own. */}
                        {!constraintNames.has(x.name) && (
                          <Button size="sm" variant="danger" aria-label={`Drop index ${x.name}`} onClick={() => { setForm(null); dropIndex(x.name); }}>Drop</Button>
                        )}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
      {canWrite && form && !review && (
        <Panel title={form === "index" ? "Add index" : form === "unique" ? "Add unique constraint" : "Add foreign key"}>
        <form className="db-inline-form" onSubmit={(e) => { e.preventDefault(); submit(); }}>
          {form === "fk" ? (
            <>
              <label>Column
                <select value={fkCol} onChange={(e) => setFkCol(e.target.value)}>
                  <option value="">Choose a column</option>
                  {fields.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
                </select>
              </label>
              <label>Reference table
                <select value={refTable} onChange={(e) => { setRefTable(e.target.value); setRefCol(""); }}>
                  <option value="">Choose a table</option>
                  {refTables.map((t) => <option key={t.name} value={t.name}>{t.name}</option>)}
                </select>
              </label>
              <label>Reference column
                <select value={refCol} onChange={(e) => setRefCol(e.target.value)}>
                  <option value="">Primary key</option>
                  {refColumns.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
                </select>
              </label>
              <label>On delete
                <select value={onDelete} onChange={(e) => setOnDelete(e.target.value)}>
                  <option value="">Default</option>
                  {ON_DELETE.map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              </label>
            </>
          ) : (
            <>
              <fieldset>
                <legend>Columns</legend>
                {fields.map((c) => (
                  <label key={c.name}>
                    <input
                      type="checkbox"
                      checked={cols.includes(c.name)}
                      onChange={(e) => setCols(e.target.checked ? [...cols, c.name] : cols.filter((x) => x !== c.name))}
                    />{" "}{c.name}
                  </label>
                ))}
              </fieldset>
              {form === "index" && (
                <label><input type="checkbox" checked={uniqueIdx} onChange={(e) => setUniqueIdx(e.target.checked)} /> Unique</label>
              )}
            </>
          )}
          <div>
            <Button type="submit" variant="primary" size="sm" disabled={!canReview}>Review</Button>{" "}
            <Button type="button" variant="ghost" size="sm" onClick={close}>Cancel</Button>
          </div>
        </form>
        </Panel>
      )}
      {canWrite && review && (
        <ProposeDialog
          me={me} schema={schema} title={review.title} destructive={review.destructive}
          build={review.build} onClose={close} onBack={() => setReview(null)} onProposed={onProposed}
        />
      )}
    </div>
  );
}

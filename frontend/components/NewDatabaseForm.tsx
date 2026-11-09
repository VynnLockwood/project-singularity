"use client";

import { useState } from "react";
import type { Change, Me } from "@/lib/api";
import { createSchemaOp, validateSchemaName } from "@/lib/databaseOps";
import { ProposeDialog } from "./ProposeDialog";

/** Name (+ optional comment) for a new database; validates live, then opens the review dialog. */
export function NewDatabaseForm({ me, existing, onClose, onProposed }: {
  me: Me; existing: string[]; onClose: () => void; onProposed: (c: Change, name: string) => void;
}) {
  const [name, setName] = useState("");
  const [comment, setComment] = useState("");
  const [review, setReview] = useState(false);

  const error = name === "" ? null : validateSchemaName(name, existing);
  const valid = name !== "" && !error;

  if (review && valid) {
    return (
      <ProposeDialog
        me={me}
        schema="master"
        title={`Create database ${name}`}
        build={() => [createSchemaOp(name, comment.trim() || undefined)]}
        onClose={onClose}
        onProposed={(c) => onProposed(c, name)}
      />
    );
  }

  return (
    <form className="db-inline-form" onSubmit={(e) => { e.preventDefault(); if (valid) setReview(true); }}>
      <label>
        Name
        <input value={name} onChange={(e) => setName(e.target.value)} aria-invalid={error ? "true" : undefined} autoFocus />
      </label>
      {error && <p className="problems small" role="alert">{error}</p>}
      <label>
        Comment (optional)
        <input value={comment} onChange={(e) => setComment(e.target.value)} />
      </label>
      <div>
        <button type="submit" className="button" disabled={!valid}>Review</button>{" "}
        <button type="button" className="link" onClick={onClose}>Cancel</button>
      </div>
    </form>
  );
}

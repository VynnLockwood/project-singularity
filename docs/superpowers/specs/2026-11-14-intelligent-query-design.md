# Intelligent query: design

## Intent

A new tab, **Intelligent query**, for people who already write SQL. They type a comment saying what they
want, in Thai or English, press Enter, and the editor shows a SQL query as gray suggestion text
(like GitHub Copilot). Tab accepts it, Esc dismisses it. The point is speed: skip looking up table names,
column names, joins and ids.

```sql
-- อยากได้ข้อมูลสัญญาของบริษัท xxx
SELECT c.* FROM "sppProject" c JOIN "sppBusinessPartner" b ON ...   ← gray until Tab

-- และสถานะ cod ยังเปิดอยู่
SELECT c.* FROM ... WHERE ... AND ...                               ← builds on the comment above
```

Later comments build on earlier ones. The newest comment counts most, but all comments above the cursor
are kept as context, because people often continue a thought ("and the COD status is still open").

The embedding model (`multilingual-e5-small`, already running in the `embed` container) is used to find the
tables, columns and past queries that fit the comment. The chat model writes the SQL.

## Goals

- A suggestion appears after Enter on a comment line, in an editor in the new tab.
- It uses the right tables, columns, joins and real values (a company named in the comment) for the selected database.
- Follow-up comments refine the previous query: the suggestion is the full new query, not a fragment.
- It improves with use: queries people accept and run successfully become examples for later suggestions.
- The person can run the editor's query (or the statement under the cursor) and see rows, using the existing read-only query path.

## Non-goals

- Explaining queries in plain language or helping people who don't know SQL.
- Writing anything: suggestions are SELECT only. No INSERT/UPDATE/DDL, ever.
- A chat box. The editor is the interface.
- Inline completion while typing SQL (completing `SELECT ... FROM sp|`). Only comment → query.
- Saving editor tabs/files on the server. (Keeping the editor text in the browser between visits is fine.)

## Anti-goals (fails even if it "works")

- A suggestion that can change data, or that bypasses the read-only, timeout and row limits of `/api/query`.
- The editor freezing or lagging while a suggestion is on the way.
- Stale suggestions popping in after the person has moved on or kept typing.
- Learning from wrong queries: a query that errors must never become an example.
- Uncontrolled model cost: a call on every keystroke.

## Constraints (from the project)

- Backend Django 5, frontend Next.js 15 / React 19, Postgres with pgvector and pg_trgm (both already used by search).
- Embeddings: `search/embedder.embed(texts, kind)` → 384-d vectors, `query`/`passage` prefixes handled by the service.
  Table/column documents already exist in `schema_embeddings` (`search/indexer.py`), with a few sample values per column,
  and are reindexed when a schema change is applied.
- Model calls: `chat/providers.complete(provider, messages, tools, model, key)` with the configured provider (OpenRouter)
  and model. Key, same as the chat (builder's call, recorded): the person's own key from the browser if they set one,
  otherwise the server key. MCP `suggest_query` uses the server key. Default model today: `anthropic/claude-sonnet-4.5`.
- Running SQL: `schema/query.run_select` (SELECT only, single statement, read-only transaction, statement timeout,
  row cap, `search_path` set to the chosen schema). `/api/query` exposes it.
- Roles: viewer / editor / approver. Reading data is allowed for viewers today.
- Each "database" is a Postgres schema; the tab works on one schema at a time (picker, default `master`).

## Approaches considered

1. **Retrieve, then the model writes SQL.** Embed the comments, find tables/columns and real values, give them to the model.
   Works from day one and handles new questions; one model call (about 2–5 s) per suggestion.
2. **Reuse past queries only.** Embed the comment, return the nearest previously accepted query. Instant and free, but
   empty until there is history and cannot combine comments.
3. **Approach 1 plus a memory of accepted queries.** As 1, and every accepted suggestion that runs successfully is stored
   with its comments and embedding. The closest past pairs are sent to the model as examples, and a near-identical
   past comment can show its saved query instantly while the model is working.

**Picked: 3** (by the user). It meets the "use the embedding model to improve it" goal twice: finding schema context
and recalling the team's own past queries, so suggestions learn the team's naming and joins.

## How it works

### When a suggestion is asked for

Only when the person presses Enter on a line that is a `--` comment (cursor leaves the comment). Not on other keys.
Any keystroke, cursor move away, or new Enter cancels the pending request; a late answer for an old request is dropped.

### What is sent to the server

- `schema`
- `comments`: every `--` comment line above the cursor, in order, at most the last 10. The last one is the newest.
- `sql_context`: the SQL statements already in the editor above the cursor, at most about 4,000 characters,
  nearest first. This is what "and the COD status is still open" builds on.

### Two calls per suggestion (builder's call, recorded)

The client sends two requests at once with the same body and `request_id`:

- `POST /api/intelligent-query/recall`: fast, no model. It returns a memory hit or nothing (step 1).
- `POST /api/intelligent-query/suggest`: the full path (steps 2–7).

Both are plain synchronous Django views. The client shows the recall answer if one comes back. It replaces it with the
suggest answer only if the person hasn't accepted or dismissed the suggestion yet. Cancelling means the client aborts
the fetch and ignores late answers; the server does not stop a model call that has already started.

### Server steps

1. **Instant memory hit (recall).** Embed the newest comment together with the one before it. A stored example for this
   schema counts as a hit only when its cosine is ≥ 0.95 **and** the values found by the value lookup (step 3, run here
   too) are the same as the values stored with the example. Otherwise "contracts of company A" would show company B's
   query. On a hit, return its SQL as `source: "memory"`.
2. **Schema context.** Search `schema_embeddings` (the same ranking as `search.service.search`) with a query text built
   from the comments, newest weighted most: newest comment, then newest + previous, then all comments. Take the union of
   the top tables. Then add each table's columns, primary and foreign keys from `introspect`, and include tables
   directly joined by a foreign key to the picked ones. Cap: about 8 tables in the prompt.
3. **Value lookup.** For text-like columns of the picked tables, find stored values that appear in the comments,
   e.g. the company name. Use `word_similarity(value, comments)` from pg_trgm on distinct values. It must be bounded:
   statement timeout of about 800 ms in total, a read-only connection, and only the top few columns by search score.
   If it times out, continue without values. Found values go into the prompt as "values seen: column = 'value'".
4. **Examples.** The 3 nearest stored examples for this schema (cosine on the same comment embedding), as
   comment → SQL pairs.
5. **Model call.** Use the configured provider and model, or `intelligentQuery.model` if set. Send a system prompt that
   says: PostgreSQL; SELECT only; one statement; quote identifiers exactly as given (mixed case); use only the given
   tables and columns; the newest comment is the main request and earlier comments and SQL are context to build on;
   answer with SQL only. With no tools, the reply is one completion.
6. **Check before returning.** Strip code fences. Reject a reply that is not a single SELECT/WITH using the same rule as
   `run_select`. Run `EXPLAIN` on it in the chosen schema, read-only, with a short timeout. If EXPLAIN fails, make one
   retry with the error message added to the prompt. If it still fails, return no suggestion, with the reason.
7. **Return** `{sql, source: "model"|"memory", tables: [...], request_id}`.

### Memory of accepted queries

- When the person accepts a suggestion (Tab) and later runs it successfully, the client calls
  `POST /api/intelligent-query/learn` with the comments, the SQL that ran and the schema. The SQL must be the accepted
  text, possibly edited by the person before running.
- What counts as accepted and run (builder's call, recorded): the client remembers the accepted suggestion's text range,
  the comments sent with it, and the values the server found. If Run, in the same page session, executes a statement
  that starts inside that range (edits allowed) and succeeds, the client calls learn with those comments, values and
  the statement as it ran.
- Stored as one row per example: schema, comments (text), sql, values found (json), embedding of newest + previous
  comment, user, created, last_used, use_count, status (`candidate` | `active`), and the users/days that confirmed it.
- **Second use before it counts** (the user's choice): a newly learned query is a `candidate` and is never used for
  suggestions. A learn call matches an existing example when the comment embedding is ≥ 0.97 and the SQL is the same
  after normalizing whitespace, case of keywords and a trailing `;`. A match from a different person, or from the same
  person on a different day, makes it `active`. Other matches only bump the count. Recall and examples use `active` only.
- Shared by everyone using that schema, so the team teaches it together, and one wrong query does not spread.
- When retrieved, an example whose SQL no longer passes EXPLAIN (the schema changed) is deleted and skipped.
- Approvers can list examples (active and candidate) and delete them (simple list in the tab or in Settings).

### The tab (frontend)

- New nav item **Intelligent query** in the Work group, viewer and up, at its own route.
- Layout: schema picker and Run button on top, editor, results grid below (reuse the existing rows/grid display),
  a small status line ("Suggesting…", "From memory", "No suggestion: <reason>").
- Editor: CodeMirror 6 with SQL highlighting, gray inline suggestion, Tab to accept, Esc to dismiss. Accepting inserts
  the SQL on the lines below the comment.
- Run executes the statement under the cursor, or the selection, via `/api/query` with the chosen schema.
- Editor text is kept in browser storage per schema, best effort.

### Limits and cost

- One suggestion request in flight per person; a new one cancels the old one on the client, and the server ignores
  results nobody waits for.
- Rate limit: about 20 suggestions per minute per person. Over that, return 429 and show "Slow down a moment".
- Cache identical requests (same schema, comments and sql_context) for about 10 minutes.
- Whole request deadline about 20 s.

### Errors

- Embedding service down: still ask the model, using tables found by name similarity alone (trigram), and say
  "search is offline" in the status line.
- Model not configured or unreachable: no suggestion, with the reason. The editor and Run still work.
- Nothing relevant found (low search confidence): still try, but say so.

### MCP

Add one tool, `suggest_query(schema, comments, sql_context?)`, returning the same result, so agents can use it too.
Approver-only management tools are not needed.

## Settings (singularity.yaml / tunables)

`intelligentQuery`: `model` (empty = chat default), `maxTables` (8), `examples` (3), `memoryHit` (0.95),
`ratePerMinute` (20), `valueLookupMs` (800).

## Left to the builder

- The exact prompt wording and how comments and SQL are laid out in it.
- How CodeMirror is wrapped as a React component, and its styling to match the app.
- Exact cache and rate-limit mechanism (Django cache is fine).
- Test strategy details. The model call is mocked in tests. Retrieval, value lookup, checks and memory are tested against
  the real test database and a fake embedder.
- Whether the examples list lives in the tab or in Settings.

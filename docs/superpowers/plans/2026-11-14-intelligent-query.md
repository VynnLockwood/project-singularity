# Intelligent Query Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new **Intelligent query** tab where a person types a `--` comment, presses Enter, and gets a SQL suggestion as gray text (Tab accepts), built from embedding retrieval, real values, team examples and the chat model.

**Architecture:** A new Django app `iquery` holds four parts: context (tables and values for a comment), memory (examples learned from accepted queries), suggest (prompt, model call, safety check) and views. The frontend adds a CodeMirror 6 editor with a gray-suggestion extension, pure helpers that read comments and statements around the cursor, and a tab that fires `recall` and `suggest` together, runs queries through `/api/query`, and reports accepted-and-run queries to `learn`.

**Tech Stack:** Django 5, Postgres (pgvector, pg_trgm), the existing `embed` container (multilingual-e5-small), OpenRouter via `chat.providers`; Next.js 15, React 19, CodeMirror 6, vitest.

**Spec:** `docs/superpowers/specs/2026-11-14-intelligent-query-design.md`

## Global Constraints

- Suggestions are SELECT only: one statement, `SELECT`/`WITH` start (same rule as `schema/query._START`), and no data-modifying CTE.
- Running SQL only through `schema/query.run_select` (`/api/query`): read-only, statement timeout, row cap.
- A suggestion is asked for only when Enter is pressed on a `--` comment line. Never on other keys.
- Comments sent: every `--` line above the cursor, at most the last 10, newest last. `sql_context`: SQL above the cursor, at most 4,000 characters, nearest first.
- Settings `intelligentQuery`: `model: ""` (empty = `chat.defaultModel`), `maxTables: 8`, `examples: 3`, `memoryHit: 0.95`, `ratePerMinute: 20`, `valueLookupMs: 800`.
- Memory match for learn: comment cosine ≥ 0.97 and the same normalized SQL. Recall hit: cosine ≥ `memoryHit` and the same values. Only `active` examples are used.
- Cache identical suggest requests for 10 minutes; whole request deadline 20 s; one retry when EXPLAIN fails.
- API key: the browser's `apiKey` (same storage as chat, `getChatApiKey()`), else the server key. Never stored or logged. MCP uses the server key.
- Roles: suggest, recall, learn and the tab are for viewer and up; the examples list and delete are for approvers.
- Status copy: "Suggesting…", "From memory", "No suggestion: <reason>", "Slow down a moment", "search is offline", "Not sure which tables you mean".

## Review Focus

1. A model reply wrapped in ```` ```sql ```` fences, with prose around it, or with two statements → fences and prose stripped; two statements rejected (Task 4 test `test_reply_cleanup`).
2. `WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d` → rejected, even though it starts with WITH (Task 4 `test_rejects_data_modifying_cte`).
3. Mixed-case identifiers (`sppProject`) written unquoted → EXPLAIN fails, the retry includes the error, the quoted version is returned (Task 4 `test_retry_with_explain_error`).
4. A value lookup on a big table that hits the timeout → the suggestion still comes back, without values (Task 2 `test_value_lookup_timeout_is_not_fatal`).
5. A suggestion that arrives after the person kept typing → dropped, not shown (Task 7 `drops a late answer`).

---

## File Structure

Backend (`backend/iquery/`, a new app added to `INSTALLED_APPS` after `search`):
- `models.py`: `QueryExample`, `SuggestCall`.
- `context.py`: comment texts, table picking, value lookup, table descriptions for the prompt.
- `memory.py`: SQL normalizing, learn/promote, recall, examples, stale cleanup.
- `suggest.py`: prompt, model call, reply cleanup, safety check, cache, rate limit.
- `views.py`: HTTP endpoints.

Also modified: `config/siconfig.py` (defaults), `config/settings.py`, `config/urls.py`, `mcp_server/tools.py`, `mcp_server/app.py`, `chat/toolspecs.py`, `tests/test_mcp_parity.py` (if it lists routes).

Frontend:
- `lib/iquery.ts`: API calls and pure text helpers.
- `lib/ghost.ts`: CodeMirror extension (gray suggestion, Tab/Esc, Enter-on-comment).
- `components/SqlEditor.tsx`: React wrapper around CodeMirror.
- `components/IntelligentQueryView.tsx`: the tab.
- Modified: `lib/route.ts`, `lib/nav.ts`, `components/AppShell.tsx`, `components/ui` icon set (if an icon is missing), `package.json`.

---

### Task 1: Settings, models and migration

**Files:**
- Create: `backend/iquery/__init__.py`, `apps.py`, `models.py`, `migrations/0001_initial.py`
- Modify: `backend/config/siconfig.py` (DEFAULTS), `backend/config/settings.py` (INSTALLED_APPS), `singularity.yaml` (commented `intelligentQuery` block)
- Test: `backend/tests/test_iquery_models.py`

**Interfaces:**
- Produces:
  - `QueryExample(schema: str[63], comments: text, sql: text, sql_norm: text, values: json list, embedding: VectorField(384), status: "candidate"|"active", confirmations: json list of {"user": id, "day": "YYYY-MM-DD"}, use_count: int, created_by: FK user null, created_at, last_used)`, with an HNSW cosine index and `db_table = "iquery_examples"`.
  - `SuggestCall(user FK, created_at)`, `db_table = "iquery_calls"`, indexed on `(user, created_at)`.
  - `get_config()["intelligentQuery"]` with the Global Constraints defaults.

- [ ] **Step 1: Write failing tests**: `test_defaults` asserts `get_config()["intelligentQuery"] == {"model": "", "maxTables": 8, "examples": 3, "memoryHit": 0.95, "ratePerMinute": 20, "valueLookupMs": 800}`. `test_example_roundtrip` creates a `QueryExample` with a 384-length vector and reads `status == "candidate"` by default.
- [ ] **Step 2: Run** `pytest tests/test_iquery_models.py -q`. Expected: FAIL (no module `iquery`).
- [ ] **Step 3: Implement** the app, the models, the defaults, and `python manage.py makemigrations iquery`.
- [ ] **Step 4: Run** the test again. Expected: PASS.
- [ ] **Step 5: Commit** `feat(iquery): settings and models`.

### Task 2: Context: tables and values for a comment

**Files:**
- Create: `backend/iquery/context.py`
- Test: `backend/tests/test_iquery_context.py` (uses the `embed` fake from `tests/test_search.py`: move `fake_vector` and the `embed` fixture into `tests/conftest.py` so both files share them)

**Interfaces:**
- Consumes: `search.service.search(query, top_k, schema)`, `search.embedder.SearchError`, `schema.introspect.graph(schema)` / `describe_table(name, schema)`, `schema.conn.ro_connection()`.
- Produces:
  - `query_texts(comments: list[str]) -> list[str]`: `[newest, previous + "\n" + newest, "\n".join(all)]`, without duplicates, each cut to 500 chars.
  - `memory_text(comments: list[str]) -> str`: `previous + "\n" + newest` (newest alone when there is one comment).
  - `pick_tables(schema: str, comments: list[str], limit: int) -> dict` returning `{"tables": [str], "searchOffline": bool, "confident": bool}`: table names in score order, then foreign-key neighbours, at most `limit`; `confident` is the search result's own flag for the best text.
  - `find_values(schema: str, tables: list[str], comments: list[str], timeout_ms: int) -> list[dict]`: `[{"table","column","value"}]`.
  - `describe_for_prompt(schema: str, tables: list[str]) -> str`: one block per table: quoted name, columns with type and PK mark, then `FK "col" -> "other"."col"` lines.

- [ ] **Step 1: Write failing tests**, against the test database:
  - `test_query_texts_put_newest_first`: `query_texts(["a", "b"]) == ["b", "a\nb"]` (the third text equals the second, so it is dropped).
  - `test_pick_tables_adds_fk_neighbours`: tables `partner(id, name)` and `contract(id, partner_id → partner)`, embed index built; a comment about contracts returns `contract` first and includes `partner`.
  - `test_pick_tables_respects_limit`: `limit=1` gives one table.
  - `test_pick_tables_search_offline`: patch `search.service.search` to raise `SearchError`; result is a non-empty list ranked by `word_similarity(table_name, comments)` over `information_schema.tables`, and `searchOffline` is `True`.
  - `test_find_values_finds_name_in_thai_comment`: `partner.name` has `'บริษัท สยามพลังงาน จำกัด'`; the comment `อยากได้ข้อมูลสัญญาของ สยามพลังงาน` returns that value for `partner.name`.
  - `test_value_lookup_timeout_is_not_fatal`: `timeout_ms=1` on a 200k-row table returns `[]` and does not raise.
  - `test_describe_quotes_mixed_case`: a table `"sppProject"` appears as `"sppProject"` with its columns.
- [ ] **Step 2: Run** `pytest tests/test_iquery_context.py -q`. Expected: FAIL (no module).
- [ ] **Step 3: Implement.** Value lookup: only `text`/`character varying` columns of the picked tables, at most 3 columns (the ones search scored highest, else the first ones), one read-only connection with `SET LOCAL statement_timeout = <timeout_ms>` and `SET LOCAL pg_trgm.word_similarity_threshold = 0.6`, per column `SELECT DISTINCT col::text AS v FROM t WHERE col::text <% %(c)s ORDER BY word_similarity(col::text, %(c)s) DESC LIMIT 3`, where `c` is all comments joined. Any `psycopg.Error` ends the lookup with what was found so far.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(iquery): table and value context`.

### Task 3: Memory: learn, promote, recall, examples

**Files:**
- Create: `backend/iquery/memory.py`
- Test: `backend/tests/test_iquery_memory.py`

**Interfaces:**
- Consumes: `context.memory_text`, `search.embedder.embed`, `QueryExample`, `schema.query` connection rules (for EXPLAIN).
- Produces:
  - `normalize_sql(sql: str) -> str`: whitespace collapsed, a trailing `;` dropped, keywords lower-cased outside quotes (identifiers and string literals untouched).
  - `learn(user, schema: str, comments: list[str], sql: str, values: list[dict]) -> QueryExample`.
  - `recall(schema: str, comments: list[str], values: list[dict], threshold: float) -> QueryExample | None`.
  - `examples(schema: str, comments: list[str], k: int) -> list[QueryExample]`.
  - `explains(schema: str, sql: str) -> str | None`: `None` if `EXPLAIN (FORMAT JSON)` succeeds read-only within 2,000 ms and has no `ModifyTable` node; otherwise the error text. Task 4 reuses it.

- [ ] **Step 1: Write failing tests:**
  - `test_normalize_keeps_identifiers`: `normalize_sql('SELECT  "sppProject".x FROM "sppProject";') == 'select "sppProject".x from "sppProject"'`.
  - `test_first_learn_is_candidate`; `test_same_user_same_day_stays_candidate` (`use_count == 2`).
  - `test_other_user_promotes`; `test_same_user_next_day_promotes` (freeze the date).
  - `test_different_sql_is_a_new_example`.
  - `test_recall_needs_same_values`: an active example with values `[{"table":"partner","column":"name","value":"A"}]` is returned for the same comment with value A, and `None` with value B.
  - `test_recall_ignores_candidates`.
  - `test_examples_drop_stale`: an active example selecting from a table that has since been dropped is deleted when `examples()` reaches it, and is not returned.
  - `test_explains_rejects_modifying_cte`: `explains("master", "WITH d AS (DELETE FROM partner RETURNING *) SELECT * FROM d")` is not `None`, and `partner` still has its rows.
- [ ] **Step 2: Run** `pytest tests/test_iquery_memory.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Matching for learn: same schema, `sql_norm` equal, cosine ≥ 0.97 on the `memory_text` embedding (`passage` kind for stored, `query` kind for lookups). Promotion: when the new confirmation's user differs from every stored one, or its day differs from that user's earlier days. Recall and examples: `status="active"`, ordered by cosine distance; recall takes the nearest and checks cosine and that the value sets are equal (compared as sets of `(table, column, value)`). `examples` and `recall` run `explains` on each candidate they would return, delete failures, and update `last_used`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(iquery): team memory of accepted queries`.

### Task 4: Suggest pipeline

**Files:**
- Create: `backend/iquery/suggest.py`
- Test: `backend/tests/test_iquery_suggest.py` (patch `chat.providers.complete`)

**Interfaces:**
- Consumes: Task 2 and Task 3 functions; `chat.providers.complete(provider, messages, tools, model, key)` returning `{"content": str, ...}`; `chat.errors.ChatError`.
- Produces:
  - `class SuggestError(Exception)` with `code` in `{"rate_limited", "not_configured", "provider_error", "bad_request"}`.
  - `clean_reply(text: str) -> str`: strips ```` ``` ```` fences and anything before the first `SELECT`/`WITH` line or after the statement's end.
  - `suggest(user, schema: str, comments: list[str], sql_context: str, api_key: str = "") -> dict` returning `{"sql": str | None, "source": "model", "tables": [str], "values": [dict], "searchOffline": bool, "lowConfidence": bool, "reason": str | None}`. `lowConfidence` is true when the best search confidence is below `search.minConfidence`; it still suggests.
  - `recall_for(schema: str, comments: list[str]) -> dict` returning `{"sql": str | None, "source": "memory", "values": [dict]}` (runs `find_values` then `memory.recall`).

- [ ] **Step 1: Write failing tests:**
  - `test_reply_cleanup`: `clean_reply("Here:\n```sql\nSELECT 1;\n```\nDone")` gives `"SELECT 1;"`; a reply with `SELECT 1; SELECT 2` gives `sql=None` and a reason.
  - `test_rejects_data_modifying_cte`: the model returns the DELETE CTE twice → `sql is None`, the table is unchanged, and `complete` was called twice.
  - `test_retry_with_explain_error`: the first reply is `SELECT * FROM sppProject` (unquoted), the second `SELECT * FROM "sppProject"`; the result is the second, and the second call's messages contain the EXPLAIN error text.
  - `test_prompt_has_context`: the messages sent contain the newest comment marked as the main request, the earlier comments, `sql_context`, the table blocks, a "values seen" line, and the example pairs.
  - `test_uses_browser_key_else_server`: `api_key="k"` is passed as `key="k"`; empty passes `key=""`.
  - `test_model_setting`: with `intelligentQuery.model` empty the chat default is used, otherwise the setting.
  - `test_rate_limit`: the 21st call within a minute raises `SuggestError("rate_limited")`, and the provider is not called.
  - `test_cache`: two identical calls make one provider call; the second still counts for the rate limit.
  - `test_not_configured`: no model configured → `SuggestError("not_configured")`.
  - `test_recall_for`: with an active example and the same values, `recall_for` returns its SQL.
- [ ] **Step 2: Run** `pytest tests/test_iquery_suggest.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Order: check the rate limit (count `SuggestCall` rows in the last minute for the user, then insert one); check the cache (`django.core.cache`, key = sha256 of the JSON of schema, comments, sql_context and model; 600 s); `pick_tables`, `find_values`, `examples`; build the messages (a system prompt with the rules from the spec's step 5, and a user message with the tables, values, examples, earlier comments, SQL so far and the newest comment); call the provider with `tools=[]`; `clean_reply`; check the single-statement rule and `memory.explains`; on failure, one retry with the error appended; the overall deadline is 20 s, after which no retry happens and the reason is "took too long". Map `ChatError` codes to `SuggestError`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(iquery): suggest pipeline with safety check`.

### Task 5: HTTP endpoints and MCP tool

**Files:**
- Create: `backend/iquery/views.py`
- Modify: `backend/config/urls.py`, `backend/mcp_server/tools.py` (`suggest_query` plus `DESCRIPTIONS`, `TOOL_ROLES` "viewer", `IMPL`), `backend/mcp_server/app.py` (`@mcp.tool` wrapper), `backend/chat/toolspecs.py` (`SPECS` entry)
- Test: `backend/tests/test_iquery_api.py`, plus the existing `tests/test_mcp_parity.py` passing

**Interfaces:**
- Consumes: `accounts.api.endpoint`, `error`, `schema_param`; Task 3 and 4 functions.
- Produces routes, each body `{schema, comments: [str], sqlContext?: str, apiKey?: str, requestId?: str}`:
  - `POST /api/intelligent-query/recall` → `recall_for` result plus `requestId`.
  - `POST /api/intelligent-query/suggest` → `suggest` result plus `requestId`. Status codes: 429 rate_limited, 503 not_configured, 502 provider_error, 400 bad input.
  - `POST /api/intelligent-query/learn` with `{schema, comments, sql, values}` → `{"id", "status"}`.
  - `GET /api/intelligent-query/examples?schema=` (approver) → `{"examples": [{"id","comments","sql","status","useCount","createdBy","lastUsed"}]}`; `DELETE /api/intelligent-query/examples/<id>` (approver) → 204.
  - MCP `suggest_query(user, schema: str, comments: list[str], sql_context: str = "") -> dict`.

- [ ] **Step 1: Write failing tests:** input checks (`comments` must be a non-empty list of strings: more than 10 keeps the last 10, each one cut to 500 chars; `sqlContext` cut to 4,000 chars keeping the end nearest the cursor; `apiKey` over 500 chars → 400; unknown schema → 400); a viewer can call recall/suggest/learn; a viewer gets 403 on examples; an approver lists and deletes; learn rejects SQL that is not a single SELECT (400); `requestId` is echoed; the MCP tool returns the same shape as the endpoint.
- [ ] **Step 2: Run** `pytest tests/test_iquery_api.py tests/test_mcp_parity.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the views and wiring. Never log or echo `apiKey`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(iquery): endpoints and MCP suggest_query`.

### Task 6: Frontend helpers and API client

**Files:**
- Create: `frontend/lib/iquery.ts`
- Test: `frontend/lib/iquery.test.ts`

**Interfaces:**
- Produces:
  - `commentsAbove(text: string, pos: number): string[]`: `--` lines (text after `--`, trimmed, empty ones dropped) above `pos`, at most the last 10, oldest first.
  - `sqlContextAbove(text: string, pos: number): string`: the non-comment text above `pos`, last 4,000 chars.
  - `statementAt(text: string, pos: number): { from: number; to: number; sql: string } | null`: splits on `;` outside quotes, `$$` bodies and `--` comments; drops comment lines from `sql`; returns null for blank.
  - `isCommentLine(line: string): boolean`.
  - Types `Suggestion = { sql: string | null; source: "model" | "memory"; tables: string[]; values: FoundValue[]; searchOffline?: boolean; reason?: string | null; requestId: string }`, `FoundValue = { table: string; column: string; value: string }`.
  - `recallQuery(body, signal)`, `suggestQuery(body, signal)`, `learnQuery(body)`, `runQuery(schema, sql)`, `listExamples(schema)`, `deleteExample(id)`, using the existing `call` helper. Suggest/recall send `apiKey: getChatApiKey()`.

- [ ] **Step 1: Write failing tests:** `commentsAbove("-- a\nselect 1;\n-- b\n", 18)` gives `["a", "b"]`; it caps at 10; `statementAt` ignores `;` inside `'a;b'` and in `-- x;` comments; `statementAt` returns the second statement for a cursor in it; `suggestQuery` posts the body with `apiKey` and passes the abort signal.
- [ ] **Step 2: Run** `npx vitest run lib/iquery.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(iquery): frontend helpers`.

### Task 7: Editor with gray suggestion

**Files:**
- Modify: `frontend/package.json` (`@codemirror/state`, `@codemirror/view`, `@codemirror/commands`, `@codemirror/lang-sql`, `@codemirror/language`; latest 6.x)
- Create: `frontend/lib/ghost.ts`, `frontend/components/SqlEditor.tsx`
- Test: `frontend/lib/ghost.test.ts` (with `EditorState` only, no DOM), `frontend/components/SqlEditor.test.tsx` (mount smoke test)

**Interfaces:**
- Produces:
  - `ghostExtension(handlers: { onCommentEnter(pos: number): void; onAccept(s: Accepted): void; onDismiss(): void }): Extension`.
  - Effects `setGhost.of({ pos: number; text: string; requestId: string } | null)`, and `ghostField` holding it.
  - `Accepted = { from: number; to: number; requestId: string }`. Accepting inserts `text + "\n"` at `pos` and clears the ghost.
  - Any document change or cursor move off `pos` clears the ghost (so a late answer for an old position is not shown).
  - `onCommentEnter` fires when a transaction with user event `input` inserts a newline at the end of a line whose text satisfies `isCommentLine`.
  - `SqlEditor({ value, onChange, extensions, onReady(view) })`.

- [ ] **Step 1: Write failing tests** (in `ghost.test.ts`): `shows a ghost at its position`; `Tab accepts` (run the Tab command: the text is inserted, `onAccept` gets the inserted range); `Esc dismisses`; `typing clears it`; `drops a late answer`: a `setGhost` whose `pos` is not the current cursor is ignored; `Enter on a comment line fires onCommentEnter`, and Enter on a SQL line does not.
- [ ] **Step 2: Run** `npx vitest run lib/ghost.test.ts components/SqlEditor.test.tsx`. Expected: FAIL.
- [ ] **Step 3: Implement.** The ghost is a `Decoration.widget` (class `ghost-text`, muted color, `pre-wrap`), at `Prec.highest` keymap for Tab/Escape that returns `false` when there is no ghost, so Tab still indents normally.
- [ ] **Step 4: Run** the tests, then `npm run typecheck`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(iquery): editor with gray suggestions`.

### Task 8: The Intelligent query tab

**Files:**
- Create: `frontend/components/IntelligentQueryView.tsx`, `frontend/components/IntelligentQueryView.test.tsx`
- Modify: `frontend/lib/route.ts` (`View` gets `"query"`; route `{ view: "query"; schema?: string }` at `/query/<schema>`), `frontend/lib/nav.ts` (Work group, after Data: `{ view: "query", label: "Intelligent query", icon: <existing code-ish icon or a new one>, minRole: "viewer" }`), `frontend/components/AppShell.tsx`, plus route and nav tests

**Interfaces:**
- Consumes: Task 6 and Task 7.

- [ ] **Step 1: Write failing tests** (mock the API module):
  - `asks recall and suggest together on comment Enter`: both are called with the same `requestId`, comments and schema.
  - `memory answer then model answer`: recall resolves first and is shown with "From memory"; suggest resolves and replaces it.
  - `model answer does not replace after accept`.
  - `a new Enter aborts the old requests` (the signals are aborted).
  - `shows reasons`: 429 → "Slow down a moment"; `sql: null` with reason → "No suggestion: <reason>"; `searchOffline` → "search is offline"; `lowConfidence` → "Not sure which tables you mean".
  - `Run uses the statement under the cursor or the selection` and shows rows and the error text.
  - `learns after accept and successful run`: accept, edit inside the range, Run succeeds → `learnQuery` with the comments, values and SQL as run; a run that fails, or a statement outside the range, does not learn.
  - `keeps text per schema in browser storage` (try/catch: works when storage throws).
  - route `/query/master` parses and builds back; nav shows the item for a viewer.
- [ ] **Step 2: Run** `npx vitest run components/IntelligentQueryView.test.tsx lib/route.test.ts lib/nav.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement.** Layout: schema picker (same source as the Database tab) and Run button on top, the editor, a status line, and a results table (columns, rows, a "truncated" note). The accepted range is mapped through later edits with `ChangeSet.mapPos`. Values for learn come from the suggestion that was accepted.
- [ ] **Step 4: Run** the full frontend suite, `npm run typecheck` and `npm run build`. Expected: all pass.
- [ ] **Step 5: Commit** `feat(iquery): Intelligent query tab`.

### Task 9: Docs and full check

**Files:**
- Modify: `README.md` (a short "Intelligent query" section: how to use it, that it needs the embed service and a chat model, settings), `singularity.yaml` (commented block from Task 1 checked)

- [ ] **Step 1:** Run the backend suite: `pytest tests -q`. Expected: all pass.
- [ ] **Step 2:** Run the frontend suite and `npm run build`. Expected: all pass.
- [ ] **Step 3:** Manual check in a running stack, if available: comment `-- contracts of partner X`, Enter, Tab, Run; then `-- and status still open`, Enter: the suggestion builds on the first query.
- [ ] **Step 4: Commit** `docs(iquery): README`.

# AI import: design

## Intent

A person has a whole Excel or CSV file (a vendor list from purchasing, a project register from another team)
and wants it in master. Today that means opening the file, guessing which columns match which tables,
hand-writing a mapping, cleaning names, and checking for records that already exist.

The new **Import** tab does that work. The person drops in the file and writes, in Thai or English, what they
want ("this is purchasing's vendor list, put it into partner, only active vendors"). A relay of roles then
works it out and talks through it on screen, like a chat thread:

```
Profiler   Sheet "Vendors": 1,240 rows, 9 columns. "Tax ID" is 13 digits and unique. "Status" is Active/Inactive.
Matcher    "Vendor Name" fits partner.name (0.93). 212 rows look like partners we already have.
Designer   Map Vendors -> partner. "Tax ID" -> tax_id. Skip Inactive rows. "Region" fits nothing: new table region?
Critic     Gate check: 1 problem: partner.tax_id is varchar(10), 13 digits do not fit.
Designer   Changed: add column tax_id_13 ... (round 2)
Critic     Gate check passed. 1,028 new rows, 212 already exist (skipped), 0 bad rows.
```

The person reviews the result, can edit the mapping, and submits it. It becomes one change that waits for
approval like any other. Nothing is written by the roles themselves.

It also learns sources. A mapping that was approved is kept as a recipe for that file's shape, so next
month's export of the same vendor list is matched straight away.

The local embedding model (`multilingual-e5-small` in the `embed` container) does the matching. The cloud chat
model sees only the profile (headers, types, stats, a few sample values per column), never the whole file.

## Goals

- From one file plus one prompt, produce a mapping into the selected database. Each sheet goes to an existing
  table, a new table, or is skipped. Each column goes to an existing column, a new column, or is dropped.
- Point out rows that already exist in master, and rows that would fail, before anything is submitted.
- Show each role's turn as a readable thread, so the person can see why each decision was made.
- Reuse the scripts pipeline (mapping, validation, bulk loads, approval) rather than build a second loader.
- Learn recurring sources: the same file shape next time starts from the approved mapping.

## Non-goals

- Updating existing rows from the file. In v1 a row that already exists is skipped and listed. Merging
  is the Duplicates tab's job, and updates go through the Data tab.
- Free-form multi-agent chat (approach B), and anything that runs SQL outside the gate.
- Other formats in v1 (Google Sheets links, JSON, Parquet, .xls). The design leaves room for them.
- An MCP tool for uploading a file. AI clients already have `begin_load` / `append_rows` / `finish_load`.

## Anti-goals

- Writing data or schema without a person's submit and an approver's approval.
- Sending the whole file to a cloud provider.
- A mapping that silently drops columns or rows. Everything not loaded is listed with a reason.

## Constraints

- Editors and approvers can run imports, since an import proposes changes. Viewers do not see the tab.
- Limits come from the existing script limits (`maxScriptBytes`, `maxScriptRows`, `maxOpsPerScript`), plus a
  file cap `import.maxFileBytes` (default 50 MB).
- The gate's rules apply unchanged: snake_case, a `<table>_id` key, a business key, comments, and so on.
- The read-only role cannot use pg_trgm (see the Intelligent query rulings). Matching uses the embedding model
  and plain SQL.
- The `embed` service may be down. Matching then falls back to name similarity and says so.
- openpyxl must be added to `requirements.txt`. pandas is not needed.

## Approaches

- **A. Fixed relay of roles (picked).** Profiler, Matcher, Designer and Critic each hand structured results
  to the next. Only the Designer uses the chat model, and the Critic's gate errors go back to it for at
  most 3 rounds. It is cheap, testable and repeatable, and it reuses the scripts mapping and validation.
  The thread UI still shows the roles "talking".
- **B. Free conversation between several AI agents, with a judge.** It is the most lively to watch, but
  costs several times more, gives a different answer each run, is hard to test, and sends more data out.
- **C. The existing chat with two new tools.** It is cheapest, but it is one model doing everything, with
  no structure to test against.

The person picked A.

## How it works

### 1. Intake

- The file is uploaded with the same chunked upload the Scripts tab uses, under a new kind, `import`.
  Accepted files are `.xlsx` and `.csv`. A CSV is read as UTF-8 (with or without a BOM), with a TIS-620
  fallback for old Thai exports.
- Each sheet (or the CSV) becomes one **source**. The header is the first non-empty row. Blank or repeated
  headers get names like `column_3`, and the Profiler says so. Fully empty rows and columns are ignored.
  Formulas are read as their cached values.
- Every source is turned into a pg_dump-style script and stored through `scripts.storage`. From then on the
  existing mapping, validation, bulk_insert and submit code handle it. This is the key reuse.
  - A sheet that goes to a **new table** gets a `CREATE TABLE "<sheet>"` with the profiled types, plus a
    `COPY` block.
  - A sheet that goes to an **existing table** gets only the `COPY` block. That is how the mapping already
    loads into an existing table, and the types come from the target.
- The script is regenerated at submit from the final decisions, so the row filter and the skipped existing
  rows are left out of the stored rows. The executor does not change.
- **Small mapping extension (my call):** a new optional `add_columns` key for an existing target lists new
  columns (name, type, nullable, comment). They become `add_column` ops before the bulk insert. The key is
  additive, so current mappings, the Scripts tab and `submit_script` / `validate_script` keep working.
  It gets its own tests.

### 2. Profiler (local, no AI)

For each column it reports:
- the type it looks like (integer, numeric, date, boolean, text) and how sure it is;
- the empty %, the distinct count and whether the values are unique;
- the longest value, and patterns (fixed length, digits only, a date format);
- the 5 most common values.

**Types (my call).** A column gets a type (integer, numeric, date, boolean) only when every non-empty value
parses as that type. Otherwise it stays text, and the Profiler says so ("97% integers, kept as text: 31 rows
are not numbers"). The Designer can then choose a typed target, and the odd rows show as bad rows. Thai
digits and Buddhist-era years (2567) are converted only when the whole column parses that way.

### 3. Matcher (local embedding model plus the duplicates engine)

- **Recipe first.** The file's header fingerprint is the sorted, trimmed, lower-cased header names. It
  does not use the file or sheet name, so `vendors_2026_11.csv` matches last month's export. If it
  matches an approved recipe for this database, the recipe is the starting mapping. The Designer then
  only handles drift: new or missing columns, or a target column that was renamed or dropped since. The
  Critic finds the last case when it re-checks the recipe, and the thread reports any drift.
- **Columns.** For each source column, a text of its header, the profiled type and a few sample values
  is embedded and compared with the existing column documents in the search index. The best 3 candidates
  are kept with their scores. The best target table per sheet is the one with the most column hits.
- **References.** A column whose values match a parent table's business key or name column (for example
  province names) is marked as a likely reference to that table.
- **Rows (my call).** For a sheet whose target is an existing table, rows are compared with that table.
  The duplicates engine only compares rows within one list, so a small adapter runs it against the
  table.
  - **Exact business-key match:** "already exists". Skipped automatically and listed.
  - **Name match from the duplicates engine:** "maybe the same", shown with the existing record. These
    are **loaded by default**, and the person ticks the ones to skip. A false duplicate can be merged
    later in the Duplicates tab, while a wrongly skipped row is lost silently (the anti-goal).
  - If a row starts to collide between submit and approval, the change fails the usual way and the
    person re-runs the import.

### 4. Designer (chat model, no tools)

The input:
- the person's prompt;
- the profiles (headers, types, stats, at most 5 sample values per column, cut to 80 chars);
- the Matcher's candidates and the recipe, if there is one;
- the team's rules;
- a compact description of the candidate target tables.

The output is strict JSON:
- the scripts **mapping JSON** (the existing format);
- for each sheet, an optional **row filter**: column equals, in, or not empty, combined with AND (for
  example "only active");
- a one-line **reason** for each table and column decision;
- at most 3 **questions** for the person, when a choice genuinely needs them ("Is 'Code' the vendor code
  or the tax ID?").

If there are questions, the run pauses. The person answers in the thread, and the Designer continues with
the answers. JSON that does not parse is retried once with the parse error.

### 5. Critic (local)

The Critic runs the existing script validation (a gate dry run, orphan references, bad rows) on the mapping
plus the filter. Rows that already exist are excluded from the load stream before validation. Problems go
back to the Designer, at most 3 rounds in total. When it passes, the report gives:
- new rows per table;
- rows already existing (skipped);
- rows removed by the filter;
- bad rows, with their line and the reason;
- columns dropped, with the reason.

### 6. Review and submit

The tab shows:
- the thread;
- a mapping table (source column -> target, confidence, reason) that the person can change. Changing it
  re-runs the Critic only, not the Designer;
- the counts.

**Submit** uses the existing scripts submit. It creates one change (schema ops plus bulk loads) that waits
for an approver. Once that change is applied, the mapping is saved as the recipe for this file shape and
database. A newer approval replaces the older recipe.

### Runs

- An import run is a background job (the existing `scripts.jobs`). The page polls its state, the same
  way script validation does.
- Run states: `profiling`, `matching`, `designing`, `waiting_for_answer`, `checking`, `ready`, `failed`,
  `submitted`. Each role's turn is stored as a thread message with a role, a short text and a details JSON.
- A run, its files and its thread expire after 24 hours unless submitted, like upload sessions.
- One open run per user per database. The chat-model calls use the browser's API key if set, else the
  server's, like chat.

## Settings (`singularity.yaml`, all optional)

```yaml
# import:
#   model: ""            # empty = the chat default model
#   maxFileBytes: 52428800
#   sampleValues: 5      # sample values per column sent to the chat model; 0 = none
#   maxRounds: 3         # Designer <-> Critic rounds
#   rowMatch: true       # look for rows that already exist
```

## Decided by me (the person handed over the how)

- The job is onboarding a source into master; designing new tables comes out of columns that fit nothing.
- Existing rows are skipped and listed, not updated.
- `.xlsx` and `.csv` only in v1.
- The tab is called Import, in the Work group after Data, for editors and up.
- The row filter is deliberately small (equals / in / not empty). It is not an expression language.
- A recipe is keyed by sheet name plus sorted headers, per database.

## Left to the builder

- How the upload kind and the background job plug into `scripts.uploads` and `scripts.jobs`.
- The exact prompt wording and JSON schema for the Designer.
- How the reference columns to existing parent tables are expressed. The bulk loads already support refs
  to existing tables (`begin_load`), so the mapping may need the same.
- Thread UI details, and whether the thread streams over the existing websocket or polls.
- The MCP parity entries: the new routes are web-only, with a reason.

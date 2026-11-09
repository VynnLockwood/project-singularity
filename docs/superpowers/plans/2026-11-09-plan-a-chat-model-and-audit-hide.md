# Plan A: chat provider and model from the website, audit columns out of Duplicates

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An approver picks the chat provider (openrouter or opencode) and adds, selects and removes chat models on the Settings page with no deploy, and the chat really uses them; the Duplicates compare view no longer shows audit columns.

**Architecture:** Provider is already a tunable but the chat always calls OpenRouter, so add one dispatch function and use it. The model list moves from `singularity.yaml` (`chat.allowedModels`, deploy-time) to a database-backed tunable `chat.models` seeded from that yaml list. Duplicates `detail()` drops audit columns server side, and the frontend stops filtering by stale camelCase names.

**Tech Stack:** Django, pytest (scratch Postgres via `tests/pgsetup.py`), Next.js with Vitest.

**Spec:** `docs/superpowers/specs/2026-11-09-database-tab-design.md` (section 1, Plan A).

## Global Constraints

- Provider options are exactly `openrouter` and `opencode` (`settings_app/tunables.py: PROVIDER_KEYS`). API keys stay in the server environment; the site never stores, accepts or shows a key (only the existing "set / missing" boolean).
- Model id must match `^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,119}$`. At most 50 saved models. The default must be one of the saved models. Duplicates in the list are rejected.
- Only approvers change settings (existing `tunables.apply` rule). Every change is audited through the existing `record(...)` call.
- Seed: while `chat.models` has never been overridden, the effective list is `chat.allowedModels` from `singularity.yaml`. `allowedModels` stays valid in the file.
- Audit columns are found through `schema.roles.hidden_columns()` and `roles.roles_of()` (the `auditColumns` rule); no column names are hard-coded.
- Do not touch the embedding model (`embedding.model`); it stays deploy-time.
- Tests: the full suite has pre-existing failures; run it once per task and compare with the failure list captured before Task 1 (`pytest -q 2>&1 | grep -E "^(FAILED|ERROR)" | sort > /tmp/baseline-a.txt`). No new failures allowed.
- Commits: no attribution lines, author is the repo's configured user.

## Review Focus

- A model id with spaces, quotes, newline or 200 characters: rejected with a clear message, nothing saved.
- Removing the model that is currently the default: rejected, or default must change in the same request (never leave a default that is not in the list).
- Setting `chat.models` and `chat.defaultModel` in one request where the default is a newly added model: accepted.
- Resetting `chat.models` (value null) while the default is a site-added model: rejected or default reset too; never a dangling default.
- Provider selected has no key: chat answers with the existing `not_configured` error naming the right environment variable, not a 500.
- An `opencode` failure surfaces as a handled chat error (today `opencode.ChatError` is a different class from the one the view catches).
- Duplicates detail for a table with `created_at`, `updated_at`, `created_by`, `is_active`: none appear; the primary key still does (the merge UI needs it).

---

## File Structure

- `backend/chat/errors.py` (create): the single `ChatError`.
- `backend/chat/providers.py` (create): `complete(provider, messages, tools, model)`.
- `backend/chat/openrouter.py`, `backend/chat/opencode.py`, `backend/chat/orchestrator.py`, `backend/chat/views.py` (modify).
- `backend/config/siconfig.py`, `backend/settings_app/tunables.py`, `singularity.yaml` (modify).
- `backend/duplicates/service.py` (modify).
- `frontend/lib/api.ts`, `frontend/components/SettingsView.tsx`, `frontend/components/DuplicatesView.tsx` (modify); `frontend/components/ChatModelSettings.tsx` (create).
- Tests: `backend/tests/test_chat_provider_dispatch.py`, `backend/tests/test_chat_models_setting.py`, additions to `backend/tests/test_duplicates.py`; `frontend/components/ChatModelSettings.test.tsx`, additions to `DuplicatesView.test.tsx`.

---

### Task 1: Provider dispatch and one `ChatError`

**Files:**
- Create: `backend/chat/errors.py`, `backend/chat/providers.py`
- Modify: `backend/chat/openrouter.py`, `backend/chat/opencode.py`, `backend/chat/orchestrator.py`, `backend/chat/views.py`
- Test: `backend/tests/test_chat_provider_dispatch.py`

**Interfaces:**
- Produces: `chat.errors.ChatError(code: str, message: str)` with `.code` and `.message`; both client modules re-export it (`openrouter.ChatError is opencode.ChatError`). `chat.providers.complete(provider: str, messages: list[dict], tools: list[dict], model: str) -> dict` returning the same shape as the clients (`{"content": str, "tool_calls": [...]}`); unknown provider raises `ChatError("not_configured", "unknown chat provider ...")`.
- Consumes: `get_config()["chat"]["provider"]`.

- [ ] **Step 1: Write the failing tests** in `test_chat_provider_dispatch.py`:
  - `test_one_chat_error_class`: `openrouter.ChatError is opencode.ChatError is errors.ChatError`.
  - `test_dispatch_routes_by_provider`: monkeypatch `providers._CLIENTS` entries with fakes recording the model; `complete("opencode", [], [], "m1")` calls the opencode fake only, `complete("openrouter", ...)` the other.
  - `test_unknown_provider_is_not_configured`: `complete("nope", [], [], "m")` raises `ChatError` with `code == "not_configured"`.
  - `test_run_turn_uses_configured_provider` (django_db): set `chat.provider` to `opencode` through `tunables.apply(approver, {"chat.provider": "opencode"})`, monkeypatch `providers._CLIENTS["opencode"]` to return `{"content": "ok", "tool_calls": []}`, run `orchestrator.run_turn(conv, user, "hi")` and assert the fake was called and the assistant message is "ok".
  - `test_opencode_error_is_handled_by_the_view` (django_db): with `OPENCODE_API_KEY` unset and provider `opencode`, POST a message through the Django test client; expect HTTP 503 and `code == "not_configured"`, not a 500.
- [ ] **Step 2: Run** `pytest tests/test_chat_provider_dispatch.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `errors.py` (move the class there), make `openrouter.py` and `opencode.py` import it (`from .errors import ChatError`), add `providers.py` with `_CLIENTS = {"openrouter": openrouter.complete, "opencode": opencode.complete}` looked up at call time (so tests can patch the dict) and `complete(...)`; in `orchestrator.run_turn` replace `openrouter.complete(history(conv), specs, conv.model)` with `providers.complete(get_config()["chat"]["provider"], history(conv), specs, conv.model)` and drop the unused import; in `views.py` import `ChatError` from `.errors`.
- [ ] **Step 4: Run** the new tests and `pytest tests/test_chat.py tests/test_chat_providers.py tests/test_chat_settings.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `fix(chat): dispatch to the configured provider; one ChatError class`.

### Task 2: `chat.models` tunable and model validation

**Files:**
- Modify: `backend/config/siconfig.py`, `backend/settings_app/tunables.py`, `backend/chat/views.py`, `singularity.yaml` (comment only)
- Test: `backend/tests/test_chat_models_setting.py`

**Interfaces:**
- Produces:
  - `get_config()["chat"]["models"]: list[str]` (effective list; seeded from `chat.allowedModels` when never overridden; a copy, never the yaml object itself).
  - Tunable `chat.models`, kind `"models"` (a JSON list of strings). `describe()` returns it with `value` as the list, `default` as the seed list, `overridden` flag.
  - `tunables.MODEL_ID_RE`, `tunables.MAX_MODELS = 50`, `tunables.validate_models(models: object) -> list[str]` (returns the cleaned list or raises `ValueError` with a user-readable message).
  - `GET /api/chat/models` returns `{"default": str, "allowed": get_config()["chat"]["models"]}`; creating a conversation accepts a model only if it is in that list.
- Consumes: `apply(actor, changes)`.

- [ ] **Step 1: Write failing tests** in `test_chat_models_setting.py` (django_db; approver and editor users as in `test_settings.py`):
  - `test_seed_comes_from_yaml_until_overridden`: with `_yaml_config()["chat"]["allowedModels"]` monkeypatched to `["a/b"]`, `get_config()["chat"]["models"] == ["a/b"]`.
  - `test_add_models_and_default_in_one_request`: `apply(approver, {"chat.models": ["x/y", "z/w"], "chat.defaultModel": "z/w"})` succeeds; `get_config()` shows both; audit row `settings.updated` exists.
  - `test_reject_bad_ids`: ids `""`, `"has space"`, `"a'b"`, `"a\nb"`, `"x" * 121`, a non-string, a duplicate, 51 entries; each raises `TunableError` and saves nothing.
  - `test_default_must_be_in_list`: `{"chat.defaultModel": "unknown/m"}` rejected; `{"chat.models": ["q/r"]}` alone while the current default is `old/m` rejected with the message "default model must stay in the list" (the default is never left dangling).
  - `test_reset_models_with_dangling_default_rejected`: models override `["a/1","a/2"]`, default `a/2`; `{"chat.models": None}` with seed lacking `a/2` is rejected.
  - `test_editor_cannot_change`: editor gets `TunableError` ("only approvers").
  - `test_chat_models_endpoint_and_create_conversation`: after adding `x/y`, `GET /api/chat/models` lists it; POST `/api/chat/conversations` with `{"model": "x/y"}` returns 201; with `"nope/m"` returns 400.
- [ ] **Step 2: Run** `pytest tests/test_chat_models_setting.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - `siconfig._yaml_config()`: after merging, set `cfg["chat"]["models"] = list(cfg["chat"]["allowedModels"])` (so `DEFAULTS["chat"]` also gets `"models": []`).
  - `tunables.py`: add `"chat.models": ("Chat models", "Models the chat may use. Add or remove ids here; no deploy needed.", "models", None, None)` to TUNABLES; extend `_clean` for kind `"models"` via `validate_models`; change `OPTIONS["model"]` to read `get_config()["chat"]["models"]`; in `apply`, validate `chat.defaultModel` against the models list **as it will be after this request** (new `chat.models` if present in `changes`, else the stored one, and for `None` the seed), and when `chat.models` changes without a `chat.defaultModel`, require the current default to remain in the new list. `describe()` must serialize list values untouched (frontend gets arrays).
  - `chat/views.py`: `models()` and `conversations()` use `get_config()["chat"]["models"]` instead of `allowedModels`.
- [ ] **Step 4: Run** the new file, `tests/test_settings.py tests/test_chat_settings.py tests/test_chat.py tests/test_siconfig.py -q`. Expected: PASS (adjust only tests that asserted the old `allowedModels`-only behavior, and say so in the commit body).
- [ ] **Step 5: Commit** `feat(chat): site-managed model list (chat.models) seeded from yaml`.

### Task 3: Settings page: provider dropdown and model manager

**Files:**
- Create: `frontend/components/ChatModelSettings.tsx`
- Modify: `frontend/lib/api.ts`, `frontend/components/SettingsView.tsx`
- Test: `frontend/components/ChatModelSettings.test.tsx`, `frontend/components/SettingsView.test.tsx`

**Interfaces:**
- Produces: `Tunable.kind` gains `"models"`; `Tunable.value`/`default` type widened to `number | string | string[]`; `saveSettings(changes: Record<string, number | string | string[] | null>)`. Component `ChatModelSettings({ provider: Tunable, models: Tunable, defaultModel: Tunable, keySet: Record<string, boolean>, canEdit: boolean, onSaved: (p: SettingsPayload) => void })`.
- Consumes: `getSettings`, `saveSettings` (unchanged URLs: `GET/PATCH /settings`).

- [ ] **Step 1: Write failing tests** (Vitest + Testing Library, mocking `@/lib/api` the way `SettingsView.test.tsx` does):
  - provider `<select aria-label="Chat provider">` has exactly the options `openrouter`, `opencode`, shows `openrouter: set / opencode: missing` badges from `keySet`, and choosing `opencode` then **Save** calls `saveSettings({"chat.provider": "opencode"})`.
  - model input (`aria-label="Add chat model"`) plus **Add** appends to the list shown, Save sends `{"chat.models": [..., "new/model"]}`; adding a duplicate or an id with a space shows an inline error and does not enable Save.
  - each saved model has a **Use as default** radio and a **Remove** button; removing the default model is disabled with the title "Pick another default first"; selecting a default sends `{"chat.defaultModel": "..."}`; when both models and default change, one `saveSettings` call carries both keys.
  - viewer/editor (`canEdit=false`): input and buttons disabled, text "Only approvers can change these."
  - server error from `saveSettings` is shown in a `role="alert"`.
- [ ] **Step 2: Run** `npx vitest run components/ChatModelSettings.test.tsx` from `frontend`. Expected: FAIL.
- [ ] **Step 3: Implement** the component with local draft state initialised from props; client-side check uses the same regex as the server (copy the pattern into a shared constant `CHAT_MODEL_RE` in `lib/api.ts`); in `SettingsView.tsx` render `ChatModelSettings` in the Chat list in place of the three separate rows for `chat.provider`, `chat.models`, `chat.defaultModel` (keep the remaining `chat.*` rows and the "provider keys" explanation).
- [ ] **Step 4: Run** `npx vitest run components/ChatModelSettings.test.tsx components/SettingsView.test.tsx` and `npx tsc --noEmit`. Expected: PASS, no type errors.
- [ ] **Step 5: Commit** `feat(ui): provider dropdown and model manager in Settings`.

### Task 4: Duplicates compare view without audit columns

**Files:**
- Modify: `backend/duplicates/service.py` (`detail`), `frontend/components/DuplicatesView.tsx`
- Test: `backend/tests/test_duplicates.py`, `frontend/components/DuplicatesView.test.tsx`

**Interfaces:**
- Produces: `duplicates.service.detail(g)` members contain every column **except** audit columns: names in `roles.hidden_columns()` and the `created_at`/`updated_at`/`created_by`/`is_active` role columns from `roles.roles_of(...)`. The primary key column stays. The response also carries `"hiddenColumns": [..]` (names removed) for transparency.

- [ ] **Step 1: Write failing tests.**
  - Backend: create a table with `<t>_id`, `name`, and the four audit columns through the normal change flow (as existing `test_duplicates.py` does), make a group, call the detail endpoint; assert member keys equal `{<t>_id, name}` plus any other data columns, none of the audit names, and `hiddenColumns` lists the four.
  - Backend: a table with no audit columns returns all columns unchanged and `hiddenColumns == []`.
  - Frontend: member objects without audit keys render one header cell per remaining column; a `created_at` key passed in (stale server) is still not rendered (the view filters `hiddenColumns` and the legacy list).
- [ ] **Step 2: Run** `pytest tests/test_duplicates.py -q` and `npx vitest run components/DuplicatesView.test.tsx`. Expected: FAIL.
- [ ] **Step 3: Implement** in `detail()`: compute `drop = roles.hidden_columns() | {c for r, c in roles.roles_of(described).items() if r != "primary_key"}` from `_describe(g.table_name)`, filter each member dict, add `hiddenColumns: sorted(drop & columns_present)`. In `DuplicatesView.tsx` line ~88 replace the camelCase filter with `open.hiddenColumns ?? []` (and keep `primaryKey` logic). Add `hiddenColumns?: string[]` to the `DuplicateDetail` type in `lib/api.ts`.
- [ ] **Step 4: Run** the backend duplicates tests, merge tests, and the frontend test + `npx tsc --noEmit`. Expected: PASS (merge behavior unchanged).
- [ ] **Step 5: Commit** `fix(duplicates): hide audit columns in the compare view`.

### Task 5: End-to-end check

**Files:** Test: `backend/tests/test_chat_models_setting.py` (append).

- [ ] **Step 1:** Append `test_site_change_reaches_the_next_chat`: approver sets provider `opencode`, adds model `x/y` and makes it default through the PATCH `/api/settings` endpoint; a fresh conversation created without a model gets `x/y`; `run_turn` with a fake opencode client receives `model == "x/y"` and the fake is the one called.
- [ ] **Step 2:** Run it. Expected: PASS.
- [ ] **Step 3:** Run the whole backend suite and the frontend suite (`npx vitest run`, `npx tsc --noEmit`); compare with `/tmp/baseline-a.txt`. Expected: no new failures.
- [ ] **Step 4: Commit** `test: site-managed chat model reaches the chat`.

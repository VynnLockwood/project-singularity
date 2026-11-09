# Chat provider + model settings — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an admin pick the chat LLM provider (`openrouter` / `opencode`) and model from Settings, using config that already exists but has no UI.

**Architecture:** `backend/config/siconfig.py:28` already defines `chat: {provider: "openrouter", defaultModel: "", allowedModels: [], maxToolRounds: 8, messagesPerMinute: 20}` and `backend/chat/openrouter.py` already implements the OpenAI-compatible client. Two things are missing: an `opencode` client (same wire shape, thin module) and any way to change it from the UI. The API key never leaves the server — the UI shows only a "key set" boolean.

**Tech Stack:** Django 5 + DRF, Python 3.11, `requests`; Next.js 15 frontend. Tests: Django `pytest`, Vitest.

**Spec:** `docs/superpowers/specs/2026-11-08-five-ui-gaps-design.md` §1

## Global Constraints

- **Security (owner directive, non-negotiable):** API keys are server-side only. No key value ever appears in a GET response body, log line, or the frontend bundle. The UI gets a boolean indicator only.
- Provider is the Chat tab's LLM. The separate `search: {topK, minConfidence, sampleValues}` block is OUT OF SCOPE — do not touch it.
- No new Python dependencies unless unavoidable; `requests` is already used.
- In-container checks need `DJANGO_SETTINGS_MODULE=config.settings` or they die `ImproperlyConfigured`.

---

### Task 1: opencode provider client

**Files:**
- Create: `backend/chat/opencode.py`
- Test: `backend/tests/test_chat_providers.py`

**Interfaces:**
- Consumes: nothing; mirrors `backend/chat/openrouter.py`
- Produces: `complete(messages, tools, model, *, timeout) -> dict` and `ChatError`

Read `backend/chat/openrouter.py` in full first (it is ~45 lines). The contract to mirror exactly:

```python
class ChatError(Exception):
    def __init__(self, code: str, message: str): ...

def complete(messages: list[dict], tools: list[dict], model: str, *, timeout: float = 90) -> dict:
    """Returns {"content": str, "tool_calls": [{"id","name","arguments"}]}."""
```

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_chat_providers.py`:

```python
import pytest
from chat import opencode

def test_raises_not_configured_when_key_missing(monkeypatch):
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    with pytest.raises(opencode.ChatError) as e:
        opencode.complete([{"role": "user", "content": "hi"}], [], "m")
    assert e.value.code == "not_configured"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && pytest tests/test_chat_providers.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `backend/chat/opencode.py`**

Mirror `openrouter.py` structure with these differences only:
- `BASE_URL = os.environ.get("OPENCODE_BASE_URL", "https://opencode.ai/api/v1")`
- key from `OPENCODE_API_KEY`
- error strings say `OpenCode` instead of `OpenRouter`
Keep the same three error codes: `not_configured`, `unavailable`, `provider_error`.

- [ ] **Step 4: Run the test**

Run: `cd backend && pytest tests/test_chat_providers.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/chat/opencode.py backend/tests/test_chat_providers.py
git commit -m "feat(chat): opencode provider client"
```

---

### Task 2: Settings API for chat provider/model

**Files:**
- Modify: `backend/config/` (settings view — locate the existing settings read/write view in this package)
- Test: `backend/tests/test_chat_settings.py`

**Interfaces:**
- Consumes: `config/siconfig.py` chat block
- Produces: read/write endpoint for provider + model; key-set boolean

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_chat_settings.py`:

```python
import pytest

@pytest.mark.django_db
def test_reading_chat_settings_never_exposes_the_key(client, admin):
    r = client.get("/api/settings/chat")
    assert r.status_code == 200
    body = r.json()
    assert "apiKey" not in body and "key" not in body
    assert isinstance(body["keySet"], bool)

@pytest.mark.django_db
def test_viewer_cannot_change_chat_settings(client, viewer):
    r = client.post("/api/settings/chat", {"provider": "opencode"}, content_type="application/json")
    assert r.status_code in (401, 403)
```

Use whatever auth fixtures the existing settings tests already use — read `backend/tests/` for the established pattern before writing.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && pytest tests/test_chat_settings.py -v`
Expected: FAIL — endpoint does not exist.

- [ ] **Step 3: Implement the endpoint**

Follow the existing settings view pattern in `backend/config/`. GET returns:

```python
{"provider": str, "model": str, "allowedModels": list[str], "keySet": bool}
```

POST accepts `provider` and `model`, restricted to approvers (mirror the role gate used elsewhere: `me.role == "approver"`). Validate `provider` against a literal `("openrouter", "opencode")`. Never read or return the key itself — only `bool(os.environ.get(...))`.

- [ ] **Step 4: Run the tests**

Run: `cd backend && pytest tests/test_chat_settings.py -v`
Expected: PASS both.

- [ ] **Step 5: Verify the no-leak test is load-bearing**

Temporarily add the env var's value to the GET response, confirm the first test FAILS, then revert. Mandatory for any key-handling code.

- [ ] **Step 6: Commit**

```bash
git add backend/config/ backend/tests/test_chat_settings.py
git commit -m "feat(chat): settings endpoint for provider and model"
```

---

### Task 3: Settings UI

**Files:**
- Modify: `frontend/components/SettingsView.tsx`
- Modify: `frontend/lib/api.ts`
- Test: `frontend/components/SettingsView.test.tsx`

**Interfaces:**
- Consumes: the GET/POST endpoint from Task 2
- Produces: provider select + model select, approver-gated

- [ ] **Step 1: Write the failing test**

```tsx
  it("lets an approver pick a provider", async () => {
    await setup("approver");
    fireEvent.change(screen.getByLabelText("Chat provider"), { target: { value: "opencode" } });
    await waitFor(() => expect(screen.getByText(/saved/i)).toBeInTheDocument());
  });
```

Read the existing SettingsView test setup first and reuse its mock helper.

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run components/SettingsView.test.tsx -t "pick a provider"`
Expected: FAIL — no such control.

- [ ] **Step 3: Add the API functions and UI**

In `frontend/lib/api.ts` add `getChatSettings()` / `saveChatSettings(body)` following the existing `call()` helper pattern. In `SettingsView.tsx` add a "Chat" section (mirroring the existing Background animation `<li>`) with:
- `<select aria-label="Chat provider">` — `openrouter`, `opencode`
- model select constrained by `allowedModels` when non-empty, else a free text input
- key status line: "API key: set" / "API key: missing" — never the value
- disabled, following the existing `{!canEdit && ...}` gate

- [ ] **Step 4: Run tests + typecheck**

Run: `cd frontend && npm test && npm run typecheck`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/lib/api.ts frontend/components/SettingsView.tsx frontend/components/SettingsView.test.tsx
git commit -m "feat(ui): chat provider and model in Settings"
```

---

### Task 4: Verify routing end-to-end

- [ ] **Step 1:** Add a test asserting a chosen provider actually changes the outbound base URL (monkeypatch `requests.post` and assert the URL host) — this is the only check that proves routing, not just config storage.
- [ ] **Step 2:** Deploy; in Settings change provider, send one Chat message, confirm no 500 and a clear error if no key is set.
- [ ] **Step 3:** Grep the built frontend bundle for any key string — must be zero hits.

Skipped: per-user model overrides, streaming. Add when requested.

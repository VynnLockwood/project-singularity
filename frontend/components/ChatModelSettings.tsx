"use client";

import { useEffect, useState } from "react";
import { ApiError, CHAT_MODEL_RE, CHAT_MODELS_MAX, saveSettings, type SettingsPayload, type Tunable } from "@/lib/api";

const asList = (v: Tunable["value"]) => (Array.isArray(v) ? v : []);
const same = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i]);

/** Provider dropdown + model list manager. Keys stay in the server environment; only set/missing is shown. */
export function ChatModelSettings({ provider, models, defaultModel, keySet, canEdit, onSaved }: {
  provider: Tunable; models: Tunable; defaultModel: Tunable; keySet: Record<string, boolean>;
  canEdit: boolean; onSaved: (p: SettingsPayload) => void;
}) {
  const [prov, setProv] = useState(String(provider.value));
  const [list, setList] = useState<string[]>(asList(models.value));
  const [def, setDef] = useState(String(defaultModel.value));
  const [input, setInput] = useState("");
  const [inputError, setInputError] = useState("");
  const [error, setError] = useState("");
  const [msg, setMsg] = useState("");

  // Re-sync the draft when the saved values change (after a save or a reset elsewhere).
  const modelsKey = JSON.stringify(models.value);
  useEffect(() => {
    setProv(String(provider.value)); setList(asList(models.value)); setDef(String(defaultModel.value));
  }, [provider.value, modelsKey, defaultModel.value]); // eslint-disable-line react-hooks/exhaustive-deps

  const changes: Record<string, string | string[]> = {};
  if (prov !== String(provider.value)) changes["chat.provider"] = prov;
  if (!same(list, asList(models.value))) changes["chat.models"] = list;
  if (def !== String(defaultModel.value)) changes["chat.defaultModel"] = def;
  const dirty = Object.keys(changes).length > 0;
  const canSave = canEdit && dirty && !inputError && list.includes(def);

  const add = () => {
    const id = input.trim();
    if (!id) return;
    if (!CHAT_MODEL_RE.test(id)) return setInputError("Use letters, digits and . _ : / @ + - only (no spaces), up to 120 characters.");
    if (list.includes(id)) return setInputError(`${id} is already in the list.`);
    if (list.length >= CHAT_MODELS_MAX) return setInputError(`At most ${CHAT_MODELS_MAX} models.`);
    setList([...list, id]); setInput(""); setInputError("");
  };
  const remove = (id: string) => setList(list.filter((m) => m !== id));
  const save = async () => {
    setError(""); setMsg("");
    try {
      const p = await saveSettings(changes);
      onSaved(p); setMsg("Saved. Other servers pick it up within a few seconds.");
    } catch (e) { setError(e instanceof ApiError ? e.message : "Something went wrong."); }
  };

  return (
    <li>
      <div>
        <strong>Chat provider and models</strong>
        <p className="muted small">Pick which provider the chat uses and which models it may offer. The default must be one of the models.</p>
        <div className="setting-input">
          <select aria-label="Chat provider" disabled={!canEdit} value={prov} onChange={(e) => setProv(e.target.value)}>
            {["openrouter", "opencode"].map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
          {Object.entries(keySet).map(([p, isSet]) => (
            <span key={p} className="muted small" aria-label={`${p} API key`}>{p}: {isSet ? "set" : "missing"}</span>
          ))}
        </div>
        <ul className="settings-list" aria-label="Chat models">
          {list.map((m) => {
            const isDef = m === def;
            return (
              <li key={m}>
                <label>
                  <input type="radio" name="chat-default-model" aria-label={`Use ${m} as default`} disabled={!canEdit}
                    checked={isDef} onChange={() => setDef(m)} /> {m}
                </label>
                {isDef && <span className="badge">default</span>}
                <button className="link" aria-label={`Remove ${m}`} disabled={!canEdit || isDef}
                  title={isDef ? "Pick another default first" : undefined} onClick={() => remove(m)}>Remove</button>
              </li>
            );
          })}
        </ul>
        <div className="setting-input">
          <input aria-label="Add chat model" placeholder="provider/model-id" disabled={!canEdit} value={input}
            onChange={(e) => { setInput(e.target.value); setInputError(""); }}
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add(); } }} />
          <button className="button" disabled={!canEdit} onClick={add}>Add</button>
        </div>
        {inputError && <p className="problems small">{inputError}</p>}
        {error && <p className="problems" role="alert">{error}</p>}
        {msg && <p className="muted small">{msg}</p>}
        {canEdit ? <button className="button" disabled={!canSave} onClick={save}>Save</button>
          : <p className="muted small">Only approvers can change these.</p>}
      </div>
    </li>
  );
}

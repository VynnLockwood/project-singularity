"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ApiError, getSettings, listRules, listUsers, rollbackRule, ruleVersions, saveSettings, setUserRole, updateRule,
  type Me, type Rule, type RuleVersion, type Tunable,
} from "@/lib/api";

type Section = "quality" | "rules" | "team";
const fail = (e: unknown) => (e instanceof ApiError ? e.message : "Something went wrong.");

export function QualitySettings({ canEdit }: { canEdit: boolean }) {
  const [items, setItems] = useState<Tunable[]>([]);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(() => getSettings().then((s) => { setItems(s); setDraft({}); }).catch((e) => setError(fail(e))), []);
  useEffect(() => { load(); }, [load]);

  const dirty = Object.keys(draft);
  const save = async () => {
    setError(""); setMsg("");
    const changes: Record<string, number | string> = {};
    for (const k of dirty) {
      const t = items.find((i) => i.key === k)!;
      changes[k] = t.kind === "model" ? draft[k] : Number(draft[k]);
    }
    try { setItems(await saveSettings(changes)); setDraft({}); setMsg("Saved. Other servers pick it up within a few seconds."); } catch (e) { setError(fail(e)); }
  };
  const reset = async (k: string) => {
    setError(""); setMsg("");
    try { setItems(await saveSettings({ [k]: null })); setDraft((d) => { const n = { ...d }; delete n[k]; return n; }); } catch (e) { setError(fail(e)); }
  };

  return (
    <div>
      <p className="muted small">Quality knobs for search, duplicate finding, limits and chat. They override singularity.yaml until reset.</p>
      {error && <p className="problems" role="alert">{error}</p>}
      {msg && <p className="muted small">{msg}</p>}
      <ul className="settings-list" aria-label="Quality settings">
        {items.map((t) => {
          const shown = draft[t.key] ?? String(t.value);
          return (
            <li key={t.key}>
              <div>
                <strong>{t.label}</strong>{t.overridden && <span className="badge" data-status="open">changed</span>}
                <p className="muted small">{t.help}</p>
              </div>
              <div className="setting-input">
                {t.kind === "model" ? (
                  <select aria-label={t.label} disabled={!canEdit} value={shown} onChange={(e) => setDraft({ ...draft, [t.key]: e.target.value })}>
                    {(t.options ?? []).map((o) => <option key={o}>{o}</option>)}
                  </select>
                ) : (
                  <input aria-label={t.label} type="number" disabled={!canEdit} value={shown} min={t.min ?? undefined} max={t.max ?? undefined}
                    step={t.kind === "float" ? 0.01 : 1} onChange={(e) => setDraft({ ...draft, [t.key]: e.target.value })} />
                )}
                <span className="muted small">default {String(t.default)}</span>
                {canEdit && t.overridden && <button className="link" onClick={() => reset(t.key)} aria-label={`Reset ${t.label}`}>Reset</button>}
              </div>
            </li>
          );
        })}
      </ul>
      {canEdit && <button className="button" disabled={dirty.length === 0} onClick={save}>Save changes</button>}
      {!canEdit && <p className="muted small">Only approvers can change these.</p>}
    </div>
  );
}

function RuleRow({ rule, canEdit, onChanged }: { rule: Rule; canEdit: boolean; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [params, setParams] = useState(JSON.stringify(rule.params, null, 2));
  const [versions, setVersions] = useState<RuleVersion[] | null>(null);
  const [error, setError] = useState("");
  const run = async (fn: () => Promise<unknown>) => { setError(""); try { await fn(); onChanged(); } catch (e) { setError(fail(e)); } };

  useEffect(() => { setParams(JSON.stringify(rule.params, null, 2)); }, [rule.params]);

  const saveParams = () => {
    let parsed: Record<string, unknown>;
    try { parsed = JSON.parse(params); } catch { setError("Parameters are not valid JSON."); return; }
    run(() => updateRule(rule.id, { params: parsed }));
  };

  return (
    <li data-enabled={rule.enabled}>
      <div className="rule-head">
        <strong>{rule.name}</strong>
        <span className="muted small">{rule.type} · v{rule.version}</span>
        <label className="small">
          <input type="checkbox" aria-label={`${rule.name} enabled`} checked={rule.enabled} disabled={!canEdit}
            onChange={(e) => run(() => updateRule(rule.id, { enabled: e.target.checked }))} /> on
        </label>
        <select aria-label={`${rule.name} severity`} value={rule.severity} disabled={!canEdit}
          onChange={(e) => run(() => updateRule(rule.id, { severity: e.target.value as Rule["severity"] }))}>
          <option value="block">block</option>
          <option value="warn">warn</option>
        </select>
        <button className="link" onClick={() => setOpen(!open)}>{open ? "Hide details" : "Details"}</button>
      </div>
      <p className="muted small">{rule.description}</p>
      {error && <p className="problems" role="alert">{error}</p>}
      {open && (
        <div>
          <textarea aria-label={`${rule.name} parameters`} className="code" rows={8} value={params} disabled={!canEdit} onChange={(e) => setParams(e.target.value)} />
          <div className="actions">
            {canEdit && <button className="button" onClick={saveParams}>Save parameters</button>}
            <button className="link" onClick={() => ruleVersions(rule.id).then(setVersions).catch((e) => setError(fail(e)))}>History</button>
          </div>
          {versions && (
            <ul className="small" aria-label={`${rule.name} history`}>
              {versions.map((v) => (
                <li key={v.version}>
                  v{v.version} {v.action} {v.changedBy ? `by ${v.changedBy}` : ""} · {new Date(v.createdAt).toLocaleString()}
                  {canEdit && v.version !== rule.version && (
                    <button className="link" onClick={() => run(() => rollbackRule(rule.id, v.version))}>Restore this version</button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </li>
  );
}

export function RulesPanel({ canEdit }: { canEdit: boolean }) {
  const [rules, setRules] = useState<Rule[]>([]);
  const [error, setError] = useState("");
  const load = useCallback(() => listRules().then(setRules).catch((e) => setError(fail(e))), []);
  useEffect(() => { load(); }, [load]);
  return (
    <div>
      <p className="muted small">Every proposed change is checked against these. <strong>block</strong> stops it; <strong>warn</strong> shows a warning and lets an approver decide. Each edit is versioned and can be restored.</p>
      {error && <p className="problems" role="alert">{error}</p>}
      <ul className="settings-list" aria-label="Rules">
        {rules.map((r) => <RuleRow key={r.id} rule={r} canEdit={canEdit} onChanged={load} />)}
      </ul>
    </div>
  );
}

export function TeamPanel({ me }: { me: Me }) {
  const [users, setUsers] = useState<Me[]>([]);
  const [error, setError] = useState("");
  const load = useCallback(() => listUsers().then(setUsers).catch((e) => setError(fail(e))), []);
  useEffect(() => { load(); }, [load]);
  return (
    <div>
      <p className="muted small">New people sign in as <em>viewer</em> (see singularity.yaml). Editors can propose changes; approvers can also approve schema changes and change settings.</p>
      {error && <p className="problems" role="alert">{error}</p>}
      <ul className="settings-list" aria-label="Team">
        {users.map((u) => (
          <li key={u.id}>
            <div><strong>{u.email}</strong> <span className="muted small">{u.name}</span></div>
            <select aria-label={`Role of ${u.email}`} value={u.role} disabled={u.id === me.id}
              onChange={async (e) => { setError(""); try { await setUserRole(u.id, e.target.value as Me["role"]); await load(); } catch (x) { setError(fail(x)); } }}>
              <option value="viewer">viewer</option>
              <option value="editor">editor</option>
              <option value="approver">approver</option>
            </select>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function SettingsView({ me, waiting = 0, onOpenQueue, refreshKey = 0 }: { me: Me; waiting?: number; onOpenQueue?: () => void; refreshKey?: number }) {
  const approver = me.role === "approver";
  const [section, setSection] = useState<Section>("quality");
  const sections: [Section, string][] = [["quality", "Quality"], ["rules", "Rules"], ...(approver ? ([["team", "Team"]] as [Section, string][]) : [])];
  return (
    <div className="settings">
      <div className="grid-bar" role="group" aria-label="Settings sections">
        {sections.map(([k, label]) => (
          <button key={k} className="link" aria-pressed={section === k} onClick={() => setSection(k)}>{label}</button>
        ))}
      </div>
      {waiting > 0 && (
        <p className="warnings" role="status">
          {waiting} settings, rules or roles {waiting === 1 ? "change is" : "changes are"} waiting for an approver (proposed by an AI client or chat).{" "}
          {onOpenQueue && approver && <button className="link" onClick={onOpenQueue}>Review in the Queue</button>}
        </p>
      )}
      {section === "quality" && <QualitySettings key={`q${refreshKey}`} canEdit={approver} />}
      {section === "rules" && <RulesPanel key={`r${refreshKey}`} canEdit={approver} />}
      {section === "team" && approver && <TeamPanel key={`t${refreshKey}`} me={me} />}
    </div>
  );
}

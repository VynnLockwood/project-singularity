"""Final review fixes: what the Critic tells the model, nothing-new files, real header names and the row check on
the final plan."""
import copy
import json

import pytest

from config import siconfig
from intake import run as runs
from intake.models import ImportRun

from .test_intake_match import build, shop  # noqa: F401  (shop is a fixture)
from .test_intake_run import FILTERS, MAPPING, begin, editor, inline, model, texts, upload  # noqa: F401

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]


def rounds(monkeypatch, n):
    real = siconfig.get_config

    def cfg():
        c = real()
        c["import"]["maxRounds"] = n
        return c
    monkeypatch.setattr(runs, "get_config", cfg)


def plan(tables, filters=None):
    return json.dumps({"mapping": {"tables": tables}, "filters": filters or {}, "reasons": {}, "questions": []})


def all_prompts(model):
    return "\n".join(model.prompt(n) for n in range(len(model.calls)))


# ---------------------------------------------------------------- 1. no cell values in the Critic's feedback

PRIVATE_HEADERS = ["Vendor Name", "Tax ID", "Note", "Dup Code", "Region", "Status"]
PRIVATE_CSV = ",".join(PRIVATE_HEADERS) + "\n" + "".join(
    f"Shop {i:02d} Unique,01055000009{i:02d},note {i:02d},D{i:02d},{region},Active\n"
    for i, region in zip(range(1, 8), ["Bangkok", "Chiang Mai", "Phuket", "P4", "P5", "P6", "Bangkok"])
) + "Shop 08 Unique,0105500000908,,DUPX-777,Atlantisville,Active\n" \
    "Shop 09 Unique,0105500000909,note 09,DUPX-777,Bangkok,Active\n"
# row 8: no Note (NOT NULL name), DUPX-777 twice (unique tax_id), Atlantisville is no province (orphan)
SECRETS = ["0105500000908", "DUPX-777", "Atlantisville", "Shop 08 Unique", "Shop 09 Unique"]


def private_rounds():
    rest = lambda *used: [h for h in PRIVATE_HEADERS if h not in used]  # noqa: E731
    not_null = {"vendors": {"target": "partner", "columns": {"name": "Note", "tax_id": "Tax ID"},
                            "drop": rest("Note", "Tax ID")}}
    unique = {"vendors": {"target": "partner", "columns": {"name": "Vendor Name", "tax_id": "Dup Code"},
                          "drop": rest("Vendor Name", "Dup Code")}}
    orphan = {"vendors": {"target": "partner", "columns": {"name": "Vendor Name", "tax_id": "Tax ID"},
                          "drop": rest("Vendor Name", "Tax ID", "Region"),
                          "refs": [{"column": "province_id", "parent": "province", "parent_key": "name",
                                    "source_column": "Region", "external": True}]}}
    fine = {"vendors": {"target": "partner", "columns": {"name": "Vendor Name", "tax_id": "Tax ID"},
                        "drop": rest("Vendor Name", "Tax ID")}}
    return [plan(not_null), plan(unique), plan(orphan), plan(fine)]


def test_critic_feedback_never_carries_cell_values(shop, editor, model, inline, monkeypatch):
    build("ALTER TABLE master.partner ALTER COLUMN name SET NOT NULL",
          "INSERT INTO master.province (name) VALUES ('P4'), ('P5'), ('P6')")
    rounds(monkeypatch, 4)
    model.replies = private_rounds()
    r = begin(editor, text=PRIVATE_CSV)
    assert len(model.calls) == 4, texts(r)
    critic = texts(r, "critic")
    # the person still sees the whole story
    assert "Failing row" in critic[0] or "0105500000908" in critic[0], critic[0]
    assert "DUPX-777" in critic[1], critic[1]
    assert "Atlantisville" in critic[2], critic[2]
    # the model gets what failed, by column / constraint / table and a count, but no cell
    assert "not-null" in model.prompt(1) and "name" in model.prompt(1).split("Problems found")[-1]
    assert "partner_tax_id_key" in model.prompt(2)
    tail = model.prompt(3).split("Problems found")[-1]
    assert "Region" in tail and "province" in tail and "1 row" in tail
    sent = all_prompts(model)
    leaked = [s for s in SECRETS if s in sent]
    assert not leaked, leaked
    assert "Failing row" not in sent and "Key (" not in sent
    assert r.state == ImportRun.State.READY, texts(r)


def test_feedback_after_an_answer_is_the_safe_one(shop, editor, model, inline):
    # a stored failed report feeds the next Designer run (after an answer): it must be the model-safe text too
    build("ALTER TABLE master.partner ALTER COLUMN name SET NOT NULL",
          "INSERT INTO master.province (name) VALUES ('P4'), ('P5'), ('P6')")
    model.replies = [private_rounds()[0], json.dumps({"mapping": {}, "filters": {}, "reasons": {},
                                                      "questions": ["Which column is the name?"]}),
                     private_rounds()[3]]
    r = begin(editor, text=PRIVATE_CSV)
    assert r.state == ImportRun.State.WAITING_FOR_ANSWER, texts(r)
    runs.answer(editor, r.pk, "Vendor Name")
    sent = all_prompts(model)
    assert not [s for s in SECRETS if s in sent]


# ---------------------------------------------------------------- 2. nothing new to load

def test_nothing_new_to_load_fails_at_once(shop, editor, model, inline):
    text = ("Vendor Name,Tax ID,Province,Status\n"
            "Acme,0105500000001,Bangkok,Active\n"
            "Siam Steel,0105500000002,Bangkok,Active\n"
            "Closed,0105500000003,Bangkok,Inactive\n")
    r = begin(editor, text=text)
    assert len(model.calls) == 1, texts(r)
    assert r.state == ImportRun.State.FAILED
    assert r.report["problems"] == ["Nothing new to load: 2 rows already exist, 1 removed by the filter."]
    assert r.report.get("nothing") is True
    assert not any("gate:" in t for t in texts(r))
    assert any("Nothing new to load" in t for t in texts(r, "critic"))


# ---------------------------------------------------------------- 3. real names, never stream names

THAI_CSV = "ชื่อผู้ขาย,เลขภาษี,Credit Limit\nร้านหนึ่ง,0105500000771,100\nร้านสอง,0105500000772,200\n"


def test_problems_name_the_real_headers(shop, editor, model, inline, monkeypatch):
    rounds(monkeypatch, 3)
    sheet = "ผู้ขาย"
    missing = plan({sheet: {"target": "partner", "columns": {"name": "ชื่อผู้ขาย"}, "drop": []}})
    echo = plan({sheet: {"target": "partner", "columns": {"name": "column_1", "tax_id": "เลขภาษี"},
                         "drop": ["Credit Limit"]}})
    fine = plan({sheet: {"target": "partner", "columns": {"name": "ชื่อผู้ขาย", "tax_id": "เลขภาษี"},
                         "drop": ["Credit Limit"]}})
    model.replies = [missing, echo, fine]
    r = ImportRun.objects.get(pk=runs.start(editor, upload(editor, THAI_CSV, name=f"{sheet}.csv"), "master",
                                            "x").pk)
    critic = texts(r, "critic")
    assert len(critic) == 3, critic
    first = critic[0]
    assert "Credit Limit" in first and "เลขภาษี" in first, first
    for stream in ("column_2", "column_3", "Credit_Limit", "sheet_1"):
        assert stream not in first and stream not in model.prompt(1), stream
    assert "unknown source column" in critic[1] and "column_1" in critic[1], critic[1]
    assert r.state == ImportRun.State.READY, texts(r)
    assert r.plan["mapping"]["tables"][sheet]["columns"] == {"name": "ชื่อผู้ขาย", "tax_id": "เลขภาษี"}


def test_spaced_headers_and_a_stream_name_from_the_person(shop, editor, model, inline):
    r = begin(editor)
    assert r.state == ImportRun.State.READY
    bad = copy.deepcopy(MAPPING)
    bad["tables"]["vendors"]["columns"]["name"] = "Vendor_Name"      # the stream name of "Vendor Name"
    runs.recheck(editor, r.pk, bad, FILTERS, {})
    r.refresh_from_db()
    assert r.state == ImportRun.State.FAILED
    assert any("unknown source column" in p and "Vendor_Name" in p for p in r.report["problems"]), r.report

    unmapped = copy.deepcopy(MAPPING)
    del unmapped["tables"]["vendors"]["columns"]["tax_id"]
    runs.recheck(editor, r.pk, unmapped, FILTERS, {})
    r.refresh_from_db()
    joined = " ".join(r.report["problems"])
    assert "Tax ID" in joined and "Tax_ID" not in joined, joined


# ---------------------------------------------------------------- 4. the row check follows the final plan

ROWS_CSV = ("Vendor Name,Tax ID,Ref No,Province,Status\n"
            "Alpha,0105500000002,0105500000555,Bangkok,Active\n"   # Tax ID exists, Ref No is new
            "Beta,0105500000556,0105500000001,Bangkok,Active\n")   # Ref No exists, Tax ID is new


def test_rows_checked_against_the_designers_key(shop, editor, model, inline):
    model.replies = [plan({"vendors": {"target": "partner", "columns": {"name": "Vendor Name", "tax_id": "Ref No"},
                                       "drop": ["Tax ID", "Status", "Province"]}})]
    r = begin(editor, text=ROWS_CSV)
    rows = r.matches["sources"]["vendors"]["rows"]
    assert rows["table"] == "partner" and rows["key"] == "tax_id" and rows["header"] == "Tax ID"
    assert [e["row"] for e in rows["exists"]] == [1]       # the Matcher's guess
    assert r.state == ImportRun.State.READY, texts(r)
    t = r.report["tables"]["vendors"]
    assert (t["exists"], t["new"]) == (1, 1), t
    assert r.plan["skip"] == {"vendors": [2]}              # Beta's Ref No is loaded already; Alpha is new


def test_rows_checked_on_the_designers_target(shop, editor, model, inline):
    build("CREATE TABLE master.supplier (supplier_id bigserial PRIMARY KEY, label text, code text UNIQUE)",
          "INSERT INTO master.supplier (label, code) VALUES ('Old', 'S-1')")
    text = "Vendor Name,Tax ID,Province,Status\nOld,S-1,Bangkok,Active\nNew,S-2,Bangkok,Active\n"
    model.replies = [plan({"vendors": {"target": "supplier", "columns": {"label": "Vendor Name", "code": "Tax ID"},
                                       "drop": ["Status", "Province"]}})]
    r = begin(editor, text=text)
    assert r.matches["sources"]["vendors"]["rows"]["table"] != "supplier"
    assert r.state == ImportRun.State.READY, texts(r)
    t = r.report["tables"]["vendors"]
    assert (t["target"], t["exists"], t["new"]) == ("supplier", 1, 1), t
    assert r.plan["skip"] == {"vendors": [1]}


def test_matcher_rows_kept_when_the_plan_uses_the_same_key(shop, editor, model, inline, monkeypatch):
    from intake import match as matcher
    calls = []
    real = matcher.row_check
    monkeypatch.setattr(matcher, "row_check", lambda *a, **k: calls.append(a) or real(*a, **k))
    r = begin(editor)
    assert r.state == ImportRun.State.READY
    assert r.plan["skip"] == {"vendors": [2]}
    assert len(calls) == 1      # the Matcher's own; the Critic reused it

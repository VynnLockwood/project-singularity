import datetime
import re
import zipfile

import pytest
from openpyxl import Workbook

from intake.reader import ReaderError, read


def _save(wb, tmp_path, name="f.xlsx"):
    p = tmp_path / name
    wb.save(p)
    return str(p)


def test_header_detection_edge_cases(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"] = "Customer export"
    ws.merge_cells("A1:D1")
    ws["A2"] = "generated 2026-11-14"
    ws.append(["Name", "Name", None, "Code"])  # row 3
    ws.append(["a", "b", "c", "d"])
    ws.append([None, None, None, None])  # empty row
    ws.append(["e", "f", "g", "h"])
    ws["F4"] = None  # keep column F empty
    [src] = read(_save(wb, tmp_path), 100)
    assert src["name"] == "Data"
    assert src["headers"] == ["Name", "Name_2", "column_3", "Code"]
    assert src["rows"] == [["a", "b", "c", "d"], ["e", "f", "g", "h"]]
    notes = " ".join(src["notes"])
    assert "title" in notes and "column_3" in notes and "_2" in notes and "empty row" in notes
    assert src["truncated"] is False


def test_duplicate_headers_lowercase(tmp_path):
    wb = Workbook()
    wb.active.append(["name", "name"])
    wb.active.append(["x", "y"])
    [src] = read(_save(wb, tmp_path), 10)
    assert src["headers"] == ["name", "name_2"]


def test_empty_columns_dropped(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["a", None, "c"])
    ws.append(["1", None, "3"])
    [src] = read(_save(wb, tmp_path), 10)
    assert src["headers"] == ["a", "c"]
    assert src["rows"] == [["1", "3"]]
    assert any("empty column" in n for n in src["notes"])


def test_cell_values(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["h"] * 10)
    ws.append([12, 12.0, 3.5, datetime.datetime(2026, 11, 14, 8, 30), datetime.date(2026, 11, 14),
               True, False, "=1+1", 1234567890123, datetime.datetime(2026, 11, 14)])
    path = _save(wb, tmp_path)
    # give the first formula a cached value by patching the sheet xml
    out = tmp_path / "cached.xlsx"
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(out, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                s = data.decode()
                s2 = re.sub(r"(<f>1\+1</f>)<v\s*/>|(<f>1\+1</f>)<v></v>", r"<f>1+1</f><v>2</v>", s)
                assert s2 != s
                data = s2.encode()
            zout.writestr(item, data)
    [src] = read(str(out), 10)
    assert src["rows"][0] == ["12", "12", "3.5", "2026-11-14T08:30:00", "2026-11-14",
                              "true", "false", "2", "1234567890123", "2026-11-14"]
    # formula without cached value -> None
    [src2] = read(path, 10)
    assert src2["rows"][0][7] is None


def test_numeric_cell_with_zero_padded_format(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["code", "id"])
    ws.append([123, 5])
    ws["A2"].number_format = "00000"
    ws["B2"].number_format = "0000000000000"
    ws.append([4.0, 1234567890123])
    ws["A3"].number_format = "00000"
    [src] = read(_save(wb, tmp_path), 10)
    assert src["rows"][0] == ["00123", "0000000000005"]
    assert src["rows"][1] == ["00004", "1234567890123"]


def test_csv_encodings(tmp_path):
    p = tmp_path / "a.csv"
    p.write_bytes("﻿name,city\nA,Bangkok\n".encode("utf-8"))
    [src] = read(str(p), 10)
    assert src["headers"] == ["name", "city"] and src["rows"] == [["A", "Bangkok"]]
    q = tmp_path / "t.csv"
    q.write_bytes("ชื่อ,เมือง\nสมชาย,กรุงเทพ\n".encode("cp874"))
    [src] = read(str(q), 10)
    assert src["headers"] == ["ชื่อ", "เมือง"] and src["rows"] == [["สมชาย", "กรุงเทพ"]]


def test_multiple_sheets(tmp_path):
    wb = Workbook()
    wb.active.title = "One"
    wb.active.append(["a"])
    wb.active.append(["1"])
    wb.create_sheet("Blank")
    wb.create_sheet("Two").append(["b"])
    wb["Two"].append(["2"])
    srcs = read(_save(wb, tmp_path), 10)
    assert [s["name"] for s in srcs] == ["One", "Two"]


def test_max_rows_truncates(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["n"])
    for i in range(20):
        ws.append([i])
    [src] = read(_save(wb, tmp_path), 5)
    assert len(src["rows"]) == 5
    assert src["truncated"] is True
    assert any("5" in n and "row" in n for n in src["notes"])


def test_not_a_spreadsheet(tmp_path):
    p = tmp_path / "x.xlsx"
    p.write_bytes(b"this is not a zip")
    with pytest.raises(ReaderError):
        read(str(p), 10)
    t = tmp_path / "x.txt"
    t.write_text("a,b")
    with pytest.raises(ReaderError):
        read(str(t), 10)

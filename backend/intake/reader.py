"""Read an .xlsx or .csv file into Source dicts for the profiler and matcher."""
import csv
import datetime
import io
import os
import re
from decimal import Decimal
from typing import TypedDict

from openpyxl import load_workbook


class Source(TypedDict):
    name: str
    headers: list
    rows: list
    notes: list
    truncated: bool


class ReaderError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


_ZERO_FORMAT = re.compile(r"^0+$")
_HEADER_SCAN = 25  # raw non-empty rows examined when looking for the header


def _num_text(value, number_format=None):
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return str(value)
        if value == int(value):
            value = int(value)
        else:
            return format(Decimal(repr(value)), "f")
    text = str(value)
    if isinstance(value, int) and number_format and _ZERO_FORMAT.match(number_format):
        text = text.zfill(len(number_format)) if value >= 0 else text
    return text


def _cell_text(value, number_format=None):
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _num_text(value, number_format)
    if isinstance(value, datetime.datetime):
        if value.time() == datetime.time(0):
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    text = str(value)
    return text if text.strip() != "" else None


def _blank(row):
    return all(v is None for v in row)


def _find_header(buffer):
    """Index of the header row in buffer (non-empty rows): the first row filled in
    at least half of the widest row's columns."""
    width = max(len(r) for r in buffer)
    need = max(1, -(-width // 2)) if width > 1 else 1
    for i, r in enumerate(buffer):
        if sum(v is not None for v in r) >= need:
            return i
    return 0


def _build(name, raw_rows, max_rows, skipped_empty_rows=0):
    """raw_rows: iterable of lists of str|None (empty rows included)."""
    notes = []
    empty_rows = skipped_empty_rows
    it = iter(raw_rows)
    buffer = []
    for r in it:
        if _blank(r):
            empty_rows += 1
            continue
        buffer.append(r)
        if len(buffer) >= _HEADER_SCAN:
            break
    if not buffer:
        return None
    h = _find_header(buffer)
    if h:
        notes.append(f"Skipped {h} title row{'s' if h > 1 else ''} above the header.")
    header_raw = buffer[h]
    data, truncated = [], False

    def feed(rows):
        nonlocal empty_rows, truncated
        for r in rows:
            if _blank(r):
                empty_rows += 1
                continue
            if len(data) >= max_rows:
                truncated = True
                return True
            data.append(r)
        return False

    stop = feed(buffer[h + 1:])
    if not stop:
        feed(it)
    if truncated:
        notes.append(f"Only the first {max_rows} rows were read; the rest were left out.")
    if empty_rows:
        notes.append(f"Dropped {empty_rows} empty row{'s' if empty_rows != 1 else ''}.")

    width = max([len(header_raw)] + [len(r) for r in data])
    header_raw = list(header_raw) + [None] * (width - len(header_raw))
    data = [list(r) + [None] * (width - len(r)) for r in data]
    keep = [i for i in range(width)
            if header_raw[i] is not None or any(r[i] is not None for r in data)]
    dropped = width - len(keep)
    if dropped:
        notes.append(f"Dropped {dropped} empty column{'s' if dropped != 1 else ''}.")
    # column_N uses the position among the original columns (1-based)
    headers, seen = [], set()
    for i in keep:
        label = header_raw[i]
        if label is None:
            label = f"column_{i + 1}"
            notes.append(f"Column {i + 1} had no header; named it {label}.")
        label = label.strip()
        final, n = label, 1
        while final in seen:
            n += 1
            final = f"{label}_{n}"
        if final != label:
            notes.append(f"Duplicate header {label!r} renamed to {final!r}.")
        seen.add(final)
        headers.append(final)
    rows = [[r[i] for i in keep] for r in data]
    return Source(name=name, headers=headers, rows=rows, notes=notes, truncated=truncated)


def _read_xlsx(path, max_rows):
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise ReaderError(f"Cannot open {os.path.basename(path)} as an Excel file: {exc}") from exc
    sources = []
    try:
        for ws in wb.worksheets:
            try:
                ws.reset_dimensions()
            except Exception:
                pass

            def rows(ws=ws):
                for row in ws.iter_rows():
                    yield [_cell_text(getattr(c, "value", None), getattr(c, "number_format", None))
                           for c in row]

            try:
                src = _build(ws.title, rows(), max_rows)
            except ReaderError:
                raise
            except Exception as exc:
                raise ReaderError(f"Cannot read sheet {ws.title!r}: {exc}") from exc
            if src:
                sources.append(src)
    finally:
        wb.close()
    return sources


def _decode(raw):
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return raw.decode("cp874")
        except UnicodeDecodeError as exc:
            raise ReaderError("The CSV file is neither UTF-8 nor TIS-620 text.") from exc


def _read_csv(path, max_rows):
    try:
        with open(path, "rb") as fh:
            text = _decode(fh.read())
    except OSError as exc:
        raise ReaderError(f"Cannot read file: {exc}") from exc
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    try:
        rows = ([_cell_text(v) for v in r] for r in csv.reader(io.StringIO(text, newline=""), dialect))
        src = _build(os.path.splitext(os.path.basename(path))[0], rows, max_rows)
    except csv.Error as exc:
        raise ReaderError(f"Cannot parse CSV: {exc}") from exc
    return [src] if src else []


def read(path: str, max_rows: int) -> list:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xlsx":
        return _read_xlsx(path, max_rows)
    if ext == ".csv":
        return _read_csv(path, max_rows)
    raise ReaderError("Only .xlsx and .csv files can be imported.")

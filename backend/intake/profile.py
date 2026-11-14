"""Profile each source column: detected type, shape and samples for the AI import."""
import datetime
import re
from collections import Counter

_THAI = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
_THAI_RE = re.compile("[๐-๙]")
_DIGITS = re.compile(r"^\d+$", re.ASCII)
_INT = re.compile(r"^[+-]?\d+$", re.ASCII)
_NUM = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)$", re.ASCII)
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$", re.ASCII)
_BOOL = {"true", "false", "yes", "no", "t", "f", "y", "n"}
_CODE_LEN = 13  # digits-only values this long are identifiers (tax IDs), not numbers
_NOUNS = {"integer": "integers", "numeric": "numbers", "date": "dates", "boolean": "booleans"}
_BE_OFFSET = 543
_BE_MIN_YEAR = 2400
_TYPES = ("integer", "numeric", "date", "boolean")


def _code_like(v):
    """Digits-only with a leading zero, or long enough to be an identifier."""
    return bool(_DIGITS.match(v.lstrip("+-"))) and (
        len(v.lstrip("+-")) >= _CODE_LEN or (len(v.lstrip("+-")) > 1 and v.lstrip("+-")[0] == "0")
    )


def _is_date(v):
    m = _DATE.match(v)
    if not m:
        return False
    try:
        datetime.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return False
    return True


def _parses(v, kind):
    if kind == "integer":
        return bool(_INT.match(v)) and not _code_like(v)
    if kind == "numeric":
        return bool(_NUM.match(v)) and not _code_like(v)
    if kind == "date":
        return _is_date(v)
    return v.lower() in _BOOL


def _from_be(v):
    m = _DATE.match(v)
    if not m or int(m[1]) < _BE_MIN_YEAR:
        return None
    out = f"{int(m[1]) - _BE_OFFSET:04d}-{m[2]}-{m[3]}"
    return out if _is_date(out) else None


def _blank(v):
    return v is None or str(v).strip() == ""


def _best_type(values):
    counts = {k: sum(_parses(v, k) for v in values) for k in _TYPES}
    # integers also parse as numeric; prefer the narrower type on ties
    best = max(_TYPES, key=lambda k: (counts[k], -_TYPES.index(k)))
    return best, counts[best]


def _convert_kind(values, kind):
    """Return (convert, type, parsed) when a whole-column conversion makes it typed."""
    if values and all(_THAI_RE.search(v) for v in values):
        t = [v.translate(_THAI) for v in values]
        for k in ("integer", "numeric", "date"):
            if all(_parses(v, k) for v in t):
                return "thai_digits", k
    if values and all(_from_be(v) for v in values):
        return "buddhist_year", "date"
    return None, None


def _pattern(values):
    if values and all(_DIGITS.match(v) for v in values):
        lengths = {len(v) for v in values}
        if len(lengths) == 1:
            return f"digits({lengths.pop()})"
    return None


def _clip(v):
    return v[:80]


def _profile_column(header, cells, samples):
    total = len(cells)
    values = [str(c).strip() for c in cells if not _blank(c)]
    kind, parsed = _best_type(values)
    convert, ctype = _convert_kind(values, kind)
    note = None
    if convert:
        col_type, share = ctype, 1.0
    elif values and parsed == len(values):
        col_type, share = kind, 1.0
    else:
        col_type = "text"
        share = int(parsed * 100 / len(values)) / 100 if values else 0.0
        bad = len(values) - parsed
        if parsed:
            note = (f"{int(share * 100)}% {_NOUNS[kind]}, kept as text: "
                    f"{bad} rows are not {_NOUNS[kind]}")
    counts = Counter(values)
    return {
        "header": header,
        "type": col_type,
        "share": share,
        "empty": (total - len(values)) / total if total else 0.0,
        "distinct": len(counts),
        "unique": bool(values) and len(counts) == len(values),
        "maxLength": max((len(v) for v in values), default=0),
        "pattern": _pattern(values) if col_type == "text" else None,
        "top": [[_clip(v), n] for v, n in counts.most_common(5)],
        "samples": [_clip(v) for v in list(counts)[:samples]],
        "convert": convert,
        "note": note,
    }


def profile(source, samples):
    columns = []
    for i, header in enumerate(source["headers"]):
        cells = [row[i] if i < len(row) else None for row in source["rows"]]
        columns.append(_profile_column(header, cells, samples))
    return {"name": source["name"], "rows": len(source["rows"]), "columns": columns}


def convert_value(value, column):
    """Apply the column's whole-column conversion to one value (None for empty)."""
    if _blank(value):
        return None
    v = str(value).strip()
    if column.get("convert") in ("thai_digits", "buddhist_year"):
        v = v.translate(_THAI)
    if column.get("convert") == "buddhist_year":
        return _from_be(v) or v
    return v

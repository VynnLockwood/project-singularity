from intake.profile import convert_value, profile


def src(*columns, name="t"):
    headers = [f"c{i}" for i in range(len(columns))]
    rows = [list(r) for r in zip(*columns)]
    return {"name": name, "headers": headers, "rows": rows, "notes": [], "truncated": False}


def col(values, samples=5):
    return profile(src(values), samples)["columns"][0]


def test_all_integer_column():
    c = col([str(i) for i in range(1, 21)])
    assert c["type"] == "integer" and c["share"] == 1.0 and c["note"] is None
    assert c["unique"] is True and c["empty"] == 0.0 and c["convert"] is None


def test_numeric_and_boolean_and_date():
    assert col(["1.5", "2", "-3.25"])["type"] == "numeric"
    assert col(["true", "No", "yes"])["type"] == "boolean"
    assert col(["2024-01-31", "2023-12-01"])["type"] == "date"


def test_mostly_integer_stays_text_with_share_and_note():
    c = col([str(i) for i in range(97)] + ["n/a", "x", "y"])
    assert c["type"] == "text"
    assert c["share"] == 0.97
    assert c["note"] == "97% integers, kept as text: 3 rows are not integers"


def test_leading_zero_codes_stay_text():
    c = col(["00123", "00456", "00789"])
    assert c["type"] == "text" and c["pattern"] == "digits(5)"
    assert c["top"][0] == ["00123", 1]
    assert col(["0812345678", "0898765432"])["type"] == "text"
    mixed = col(["00123", "0812345678", "1234567890123"])
    assert mixed["type"] == "text"
    assert col(["1234567890123"] * 2)["pattern"] == "digits(13)"
    assert col(["1234567890123456", "1234567890123457"])["type"] == "text"


def test_thai_digits_converted_when_whole_column():
    c = col(["๑๒๓", "๔๕๖", "๗"])
    assert c["type"] == "integer" and c["convert"] == "thai_digits"
    assert convert_value("๑๒๓", c) == "123"
    mixed = col(["๑๒๓", "456", "๗"])
    assert mixed["convert"] is None and mixed["type"] == "text"


def test_buddhist_year_converted_when_whole_column():
    c = col(["2567-01-31", "2566-12-01"])
    assert c["type"] == "date" and c["convert"] == "buddhist_year"
    assert convert_value("2567-01-31", c) == "2024-01-31"


def test_one_non_be_date_blocks_conversion():
    c = col(["2567-01-31", "2024-02-01"])
    assert c["convert"] is None and c["type"] == "date"
    assert convert_value("2567-01-31", c) == "2567-01-31"


def test_unique_and_empty():
    c = col(["a", "b", None, ""])
    assert c["empty"] == 0.5 and c["unique"] is True and c["distinct"] == 2
    assert col(["a", "a", "b"])["unique"] is False


def test_samples_and_top_capped():
    long = "x" * 200
    c = col([long] + [f"v{i}" for i in range(10)] + ["v1", "v1", "v2"], samples=5)
    assert len(c["samples"]) == 5 and all(len(s) <= 80 for s in c["samples"])
    assert len(c["top"]) == 5 and c["top"][0] == ["v1", 3]
    assert c["maxLength"] == 200


def test_profile_shape():
    p = profile(src(["1", "2"], ["a", "b"], name="sheet"), 3)
    assert p["name"] == "sheet" and p["rows"] == 2 and len(p["columns"]) == 2
    assert p["columns"][0]["header"] == "c0"

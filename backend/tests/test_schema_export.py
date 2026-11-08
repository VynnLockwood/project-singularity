"""DB-free tests for the pure DDL renderer. Shape mirrors introspect.graph()."""

from schema.export import to_sql

GRAPH = {
    "tables": [
        {
            "name": "province",
            "comment": "Provinces",
            "rowEstimate": 77,
            "columns": [
                {"name": "province_id", "type": "integer", "nullable": False, "primaryKey": True},
                {"name": "name", "type": "text", "nullable": True, "primaryKey": False},
            ],
        },
        {
            "name": "district",
            "comment": None,
            "rowEstimate": 928,
            "columns": [
                {"name": "district_id", "type": "integer", "nullable": False, "primaryKey": True},
                {"name": "province_id", "type": "integer", "nullable": False, "primaryKey": False},
                {"name": "name", "type": "text", "nullable": False, "primaryKey": False},
            ],
        },
        {
            "name": "subdistrict",
            "comment": None,
            "rowEstimate": 7435,
            "columns": [
                {"name": "subdistrict_id", "type": "integer", "nullable": False, "primaryKey": True},
                {"name": "district_id", "type": "integer", "nullable": False, "primaryKey": False},
            ],
        },
    ],
    "relations": [
        {"from": {"table": "district", "column": "province_id"},
         "to": {"table": "province", "column": "province_id"}},
        {"from": {"table": "subdistrict", "column": "district_id"},
         "to": {"table": "district", "column": "district_id"}},
    ],
}


def test_create_table_columns_and_pk():
    sql = to_sql(GRAPH)
    assert 'CREATE TABLE "province"' in sql
    assert '"province_id" integer NOT NULL' in sql
    assert '"district_id" integer NOT NULL' in sql
    assert 'PRIMARY KEY ("subdistrict_id")' in sql
    assert sql.count("CREATE TABLE") == 3


def test_nullable_omits_not_null():
    sql = to_sql(GRAPH)
    province_block = sql.split('CREATE TABLE "district"')[0]
    assert '"name" text NOT NULL' not in province_block
    assert '"name" text,' in province_block or '"name" text\n' in province_block


def test_foreign_keys_emitted_from_relations():
    sql = to_sql(GRAPH)
    assert (
        'ALTER TABLE "district" ADD FOREIGN KEY ("province_id") '
        'REFERENCES "province" ("province_id");'
    ) in sql
    assert (
        'ALTER TABLE "subdistrict" ADD FOREIGN KEY ("district_id") '
        'REFERENCES "district" ("district_id");'
    ) in sql


def test_composite_primary_key_ordering():
    graph = {"tables": [{
        "name": "link", "comment": None, "rowEstimate": 0,
        "columns": [
            {"name": "a_id", "type": "integer", "nullable": False, "primaryKey": True},
            {"name": "b_id", "type": "integer", "nullable": False, "primaryKey": True},
        ],
    }], "relations": []}
    assert 'PRIMARY KEY ("a_id", "b_id")' in to_sql(graph)


def test_identifiers_are_quoted_including_embedded_quotes():
    graph = {"tables": [{
        'name': 'we"ird', "comment": None, "rowEstimate": 0,
        "columns": [
            {"name": 'co"l', "type": "text", "nullable": False, "primaryKey": True},
        ],
    }], "relations": [
        {"from": {"table": 'we"ird', "column": 'co"l'},
         "to": {"table": 'ta"rget', "column": 'c"2'}},
    ]}
    sql = to_sql(graph)
    assert 'CREATE TABLE "we""ird"' in sql
    assert '"co""l" text NOT NULL' in sql
    assert 'ALTER TABLE "we""ird" ADD FOREIGN KEY ("co""l") REFERENCES "ta""rget" ("c""2");' in sql


def test_empty_graph_returns_empty_string():
    assert to_sql({"tables": [], "relations": []}) == ""
    assert to_sql({}) == ""

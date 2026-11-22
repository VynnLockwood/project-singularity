"""SQL isolation: `query`, intelligent query, raw SQL and scripts cannot reach databases the user lacks.

Every statement runs with `search_path` set to one database, but a qualified name (`crm.t`) still resolves, so
names are checked in the app (no per-database Postgres roles, by decision). Two layers:

- the text (pglast): `named_schemas` lists every database a statement names (relations, types, functions,
  operators, collations, the targets of COMMENT/ALTER/GRANT/DROP/CREATE, names inside string literals given
  to name-taking functions such as `nextval('crm.s')` or casts such as `'crm.t'::regclass`, and the SQL
  inside sql/plpgsql bodies). A shape whose names cannot be determined (dynamic SQL, functions that run SQL
  text or turn object ids into names, a statement kind not listed here, SQL that does not parse) raises
  `Unclear`: callers refuse it for non-admins (fail closed). `catalog_names_leak` says whether a statement
  reads the system catalogs (`pg_*`, `information_schema`), which list every database's names.
- the plan: `check_plan` runs `EXPLAIN (VERBOSE, FORMAT JSON)` and refuses any relation outside the allowed
  databases anywhere in the plan tree (subplans, CTEs, InitPlans, function and foreign scans). Views are
  expanded by the planner, so a view in a readable database over another database is refused too.

Hidden equals missing: a named database the user cannot reach is refused with the very text of one that does
not exist (`introspect.unknown_schema_message`); the check never asks whether it exists. The other refusal
texts name nothing outside the user's own SQL.

Known, accepted gap: a function in a readable database that itself reads another database is not detected
(the plan shows only the call). See README "Database access".
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import pglast
from pglast import ast, enums
from pglast.stream import RawStream

from . import introspect


class QueryError(Exception):
    """A refused or failed read-only query (the text is safe to show)."""


class Unclear(Exception):
    """The databases a statement reaches cannot be determined; refused for non-admins."""


SYSTEM = frozenset({"pg_catalog", "information_schema"})   # their functions and types stay allowed
CATALOG_REFUSAL = ("reading the system catalogs is not allowed; use list_schemas, list_tables and describe_table "
                   "to see what exists")
PLAN_REFUSAL = "this query reads data outside the databases you can reach"

REG_TYPES = frozenset({"regclass", "regnamespace", "regproc", "regprocedure", "regoper", "regoperator", "regtype",
                       "regrole", "regcollation", "regconfig", "regdictionary"})
# functions that run SQL text, or change settings (search_path) for the rest of the statement
_RUNS_SQL = frozenset({"ts_stat", "ts_rewrite", "set_config", "query_to_xml", "query_to_xmlschema",
                       "query_to_xml_and_xmlschema", "cursor_to_xml", "cursor_to_xmlschema"})
_RUNS_SQL_SUFFIX = ("_to_xml", "_to_xmlschema", "_to_xml_and_xmlschema")   # table_/schema_/database_to_xml...
# functions that turn object ids into names or describe objects by id (pg_proc: reg*/oid arguments)
_BY_OID = frozenset({"acldefault", "makeaclitem", "obj_description", "col_description", "shobj_description",
                     "format_type", "row_security_active", "satisfies_hash_partition", "gin_clean_pending_list",
                     "amvalidate", "plpgsql_validator", "postgresql_fdw_validator",
                     "currtid", "currtid2"})    # currtid2('<relation>', tid) reads a relation named in text
_BY_OID_PREFIX = ("binary_upgrade_", "brin_", "lo_", "dblink", "_pg_", "fmgr_")
_BY_OID_SUFFIX = ("sel", "joinsel", "_validator")
# pg_catalog functions (outside pg_*) with an oid / oidvector / reg* (regconfig and regdictionary included)
# argument, OUT argument or result: they turn object ids into names (`regclassout`, `oidvectortypes`,
# `format_type`), resolve names (`regclassin`) or act on objects by id. Generated from pg_proc (Postgres 16)
# minus the harmless oid comparisons/aggregates in _OID_SAFE; text search functions are allowed through
# _TS_CONFIG when their configuration/dictionary argument is a checked constant (or left out);
# tests/test_access_isolation.py::test_risky_function_list_covers_the_server keeps it in step with the server.
_BY_TYPE = frozenset("""
    acldefault aclexplode amvalidate anycompatiblemultirange_in anycompatiblerange_in anymultirange_in
    anyrange_in areajoinsel areasel array_in array_recv arraycontjoinsel arraycontsel
    binary_upgrade_set_missing_value binary_upgrade_set_next_array_pg_type_oid
    binary_upgrade_set_next_heap_pg_class_oid binary_upgrade_set_next_heap_relfilenode
    binary_upgrade_set_next_index_pg_class_oid binary_upgrade_set_next_index_relfilenode
    binary_upgrade_set_next_multirange_array_pg_type_oid binary_upgrade_set_next_multirange_pg_type_oid
    binary_upgrade_set_next_pg_authid_oid binary_upgrade_set_next_pg_enum_oid
    binary_upgrade_set_next_pg_tablespace_oid binary_upgrade_set_next_pg_type_oid
    binary_upgrade_set_next_toast_pg_class_oid binary_upgrade_set_next_toast_relfilenode bit_in bit_recv
    bpcharin bpcharrecv brin_desummarize_range brin_summarize_new_values brin_summarize_range btequalimage
    btoidvectorcmp btvarstrequalimage col_description contjoinsel contsel currval domain_in domain_recv enum_in
    enum_recv eqjoinsel eqsel fmgr_c_validator fmgr_internal_validator fmgr_sql_validator format_type
    gin_clean_pending_list gist_box_consistent gist_box_distance gist_circle_consistent gist_circle_distance
    gist_point_consistent gist_point_distance gist_poly_consistent gist_poly_distance gtsquery_consistent
    gtsvector_consistent has_any_column_privilege has_column_privilege has_database_privilege
    has_foreign_data_wrapper_privilege has_function_privilege has_language_privilege has_parameter_privilege
    has_schema_privilege has_sequence_privilege has_server_privilege has_table_privilege
    has_tablespace_privilege has_type_privilege hashoidvector hashoidvectorextended iclikejoinsel iclikesel
    icnlikejoinsel icnlikesel icregexeqjoinsel icregexeqsel icregexnejoinsel icregexnesel inet_gist_consistent
    interval_in interval_recv json_to_tsvector jsonb_to_tsvector likejoinsel likesel lo_creat lo_create
    lo_export lo_from_bytea lo_get lo_import lo_open lo_put lo_unlink makeaclitem matchingjoinsel matchingsel
    multirange_gist_consistent multirange_in multirange_recv multirangesel nameconcatoid neqjoinsel neqsel
    networkjoinsel networksel nextval nlikejoinsel nlikesel numeric_in numeric_recv obj_description oidrecv
    oidsend oidvectoreq oidvectorge oidvectorgt oidvectorin oidvectorle oidvectorlt oidvectorne oidvectorout
    oidvectorrecv oidvectorsend oidvectortypes phraseto_tsquery plainto_tsquery plpgsql_validator
    positionjoinsel positionsel postgresql_fdw_validator prefixjoinsel prefixsel range_gist_consistent range_in
    range_recv rangesel record_in record_recv regclass regclassin regclassout regclassrecv regclasssend
    regcollationin regcollationout regcollationrecv regcollationsend regconfigin regconfigout regconfigrecv
    regconfigsend regdictionaryin regdictionaryout regdictionaryrecv regdictionarysend regexeqjoinsel regexeqsel
    regexnejoinsel regexnesel regnamespacein regnamespaceout regnamespacerecv regnamespacesend regoperatorin
    regoperatorout regoperatorrecv regoperatorsend regoperin regoperout regoperrecv regopersend regprocedurein
    regprocedureout regprocedurerecv regproceduresend regprocin regprocout regprocrecv regprocsend regrolein
    regroleout regrolerecv regrolesend regtypein regtypeout regtyperecv regtypesend row_security_active
    satisfies_hash_partition scalargejoinsel scalargesel scalargtjoinsel scalargtsel scalarlejoinsel scalarlesel
    scalarltjoinsel scalarltsel setval shobj_description table_to_xml table_to_xml_and_xmlschema
    table_to_xmlschema time_in time_recv timestamp_in timestamp_recv timestamptz_in timestamptz_recv timetz_in
    timetz_recv to_regclass to_regcollation to_regnamespace to_regoper to_regoperator to_regproc to_regprocedure
    to_regrole to_regtype to_tsquery to_tsvector ts_debug ts_headline ts_lexize ts_parse ts_token_type
    tsmatchjoinsel tsmatchsel varbit_in varbit_recv varcharin varcharrecv websearch_to_tsquery
""".split())
_OID_SAFE = frozenset({"max", "min", "int8", "oid", "oideq", "oidne", "oidlt", "oidle", "oidgt", "oidge",
                       "oidlarger", "oidsmaller", "hashoid", "hashoidextended", "btoidcmp", "oidin", "oidout",
                       "get_current_ts_config"})
# text search functions: the number of arguments from which the first one is a configuration (or dictionary) name
_TS_CONFIG = {"to_tsvector": 2, "to_tsquery": 2, "plainto_tsquery": 2, "phraseto_tsquery": 2,
              "websearch_to_tsquery": 2, "json_to_tsvector": 3, "jsonb_to_tsvector": 3, "ts_headline": 3,
              "ts_debug": 2, "ts_lexize": 1}
_BY_TYPE_PATTERN = re.compile(r"^(reg\w*(in|out|recv|send)|oidvector\w*)$")
# pg_catalog functions that take text / name / cstring / regtype arguments and are allowed: reviewed (PG 16), none
# reads such an argument as the name of a relation, schema, type or other database object (pg_input_is_valid,
# currtid2 and the name-taking ones above are not here). A new one after an upgrade fails
# tests/test_access_isolation.py::test_text_argument_functions_are_all_reviewed until it is reviewed.
TEXT_ARGS_REVIEWED = frozenset("""
    aclitemin any_in anyarray_in anycompatible_in anycompatiblearray_in anycompatiblenonarray_in anyelement_in
    anyenum_in anynonarray_in anytextcat array_to_string ascii big5_to_euc_tw big5_to_mic big5_to_utf8
    bit_length boolin box_in bpchar bpchariclike bpcharicnlike bpcharicregexeq bpcharicregexne bpcharlike
    bpcharnlike bpcharregexeq bpcharregexne btnamecmp btnametextcmp btrim bttext_pattern_cmp bttextcmp
    bttextnamecmp byteain cash_in char char_length character_length charin cidin cidr_in circle_in concat_ws
    convert convert_from convert_to cstring_in cstring_out cstring_send current_setting date_add date_in
    date_part date_subtract date_trunc daterange decode encode euc_cn_to_mic euc_cn_to_utf8
    euc_jis_2004_to_shift_jis_2004 euc_jis_2004_to_utf8 euc_jp_to_mic euc_jp_to_sjis euc_jp_to_utf8
    euc_kr_to_mic euc_kr_to_utf8 euc_tw_to_big5 euc_tw_to_mic euc_tw_to_utf8 event_trigger_in extract
    fdw_handler_in float4in float8in format gb18030_to_utf8 gbk_to_utf8 generate_series gin_cmp_prefix
    gin_cmp_tslexeme gin_compare_jsonb gtsvectorin hashname hashnameextended hashtext hashtextextended
    index_am_handler_in inet_in initcap int2in int2vectorin int4in int4range int8in int8range internal_in
    is_normalized iso8859_1_to_utf8 iso8859_to_utf8 iso_to_koi8r iso_to_mic iso_to_win1251 iso_to_win866
    johab_to_utf8 json_in json_object_field json_object_field_text jsonb_delete jsonb_exists jsonb_in
    jsonb_object_field jsonb_object_field_text jsonb_set_lax jsonpath_in koi8r_to_iso koi8r_to_mic koi8r_to_utf8
    koi8r_to_win1251 koi8r_to_win866 koi8u_to_utf8 language_handler_in latin1_to_mic latin2_to_mic
    latin2_to_win1250 latin3_to_mic latin4_to_mic left length like like_escape line_in lower lpad lseg_in ltrim
    macaddr8_in macaddr_in make_timestamptz max md5 mic_to_big5 mic_to_euc_cn mic_to_euc_jp mic_to_euc_kr
    mic_to_euc_tw mic_to_iso mic_to_koi8r mic_to_latin1 mic_to_latin2 mic_to_latin3 mic_to_latin4 mic_to_sjis
    mic_to_win1250 mic_to_win1251 mic_to_win866 min name nameeq nameeqtext namege namegetext namegt namegttext
    nameiclike nameicnlike nameicregexeq nameicregexne namein namele nameletext namelike namelt namelttext
    namene namenetext namenlike nameout nameregexeq nameregexne namesend normalize notlike numrange octet_length
    oidin overlay parse_ident path_in pg_size_bytes point_in poly_in position quote_ident quote_literal
    quote_nullable regexp_count regexp_instr regexp_like regexp_match regexp_matches regexp_replace
    regexp_split_to_array regexp_split_to_table regexp_substr repeat replace reverse right rpad rtrim shell_in
    shift_jis_2004_to_euc_jis_2004 shift_jis_2004_to_utf8 similar_escape similar_to_escape sjis_to_euc_jp
    sjis_to_mic sjis_to_utf8 split_part starts_with string_agg string_agg_transfn string_to_array
    string_to_table strpos substr substring table_am_handler_in text text_ge text_gt text_larger text_le text_lt
    text_pattern_ge text_pattern_gt text_pattern_le text_pattern_lt text_smaller textanycat textcat texteq
    texteqname textgename textgtname texticlike texticnlike texticregexeq texticregexne textin textlen
    textlename textlike textltname textne textnename textnlike textout textregexeq textregexne textsend tidin
    timezone to_ascii to_char to_date to_number to_timestamp translate trigger_in ts_delete ts_match_tq
    ts_match_tt tsm_handler_in tsqueryin tsrange tstzrange tsvectorin txid_snapshot_in uhc_to_utf8 unistr
    unknownin upper utf8_to_big5 utf8_to_euc_cn utf8_to_euc_jis_2004 utf8_to_euc_jp utf8_to_euc_kr
    utf8_to_euc_tw utf8_to_gb18030 utf8_to_gbk utf8_to_iso8859 utf8_to_iso8859_1 utf8_to_johab utf8_to_koi8r
    utf8_to_koi8u utf8_to_shift_jis_2004 utf8_to_sjis utf8_to_uhc utf8_to_win uuid_in varchar void_in
    win1250_to_latin2 win1250_to_mic win1251_to_iso win1251_to_koi8r win1251_to_mic win1251_to_win866
    win866_to_iso win866_to_koi8r win866_to_mic win866_to_win1251 win_to_utf8 xid8in xidin xml xml_in
    xml_is_well_formed xml_is_well_formed_content xml_is_well_formed_document xmlcomment xmlexists xmlvalidate
    xpath xpath_exists
""".split())
# trigger functions in pg_catalog a non-admin may attach (they take no object names from the trigger's arguments)
_SAFE_CATALOG_TRIGGERS = frozenset({"suppress_redundant_updates_trigger"})
_CATALOG_TRIGGERS = frozenset({
    "ri_fkey_cascade_del", "ri_fkey_cascade_upd", "ri_fkey_check_ins", "ri_fkey_check_upd", "ri_fkey_noaction_del",
    "ri_fkey_noaction_upd", "ri_fkey_restrict_del", "ri_fkey_restrict_upd", "ri_fkey_setdefault_del",
    "ri_fkey_setdefault_upd", "ri_fkey_setnull_del", "ri_fkey_setnull_upd", "suppress_redundant_updates_trigger",
    "tsvector_update_trigger", "tsvector_update_trigger_column", "unique_key_recheck"})
# settings that change identity, the run's limits or where unqualified names resolve: never set from SQL that is
# checked here (a body could otherwise point search_path at another database)
FENCED_SETTINGS = frozenset({"role", "session_authorization", "statement_timeout", "lock_timeout",
                             "idle_in_transaction_session_timeout", "transaction_timeout", "search_path",
                             "default_transaction_read_only", "transaction_read_only"})
# pg_* functions that show nothing of any database (every other pg_* function is refused for non-admins)
PG_OK = frozenset({"pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_typeof", "pg_column_size", "pg_size_pretty",
                   "pg_size_bytes", "pg_backend_pid", "pg_client_encoding", "pg_get_serial_sequence", "pg_sequence_last_value",
                   "pg_advisory_xact_lock", "pg_advisory_xact_lock_shared", "pg_try_advisory_xact_lock",
                   "pg_try_advisory_xact_lock_shared"})
# functions whose first argument is a relation/type/function name (text resolved at run time)
_NAME_FIRST = frozenset({"nextval", "currval", "setval", "pg_get_serial_sequence", "pg_sequence_last_value",
                         "to_regclass", "to_regtype", "to_regtypemod", "to_regproc", "to_regprocedure",
                         "to_regoper", "to_regoperator", "to_regcollation", "to_regrole"}
                        | (REG_TYPES - {"regnamespace"}))
_SCHEMA_FIRST = frozenset({"to_regnamespace", "regnamespace"})   # the argument is a database name itself

_THREE_PART = {enums.ObjectType.OBJECT_COLUMN, enums.ObjectType.OBJECT_TRIGGER, enums.ObjectType.OBJECT_POLICY,
               enums.ObjectType.OBJECT_RULE, enums.ObjectType.OBJECT_TABCONSTRAINT}
_NAME_ATTRS = ("funcname", "defnames", "domainname", "conversion_name", "opclassname", "opfamilyname", "collname",
               "objname", "typeName", "name", "opclass", "collation", "dictname", "cfgname")

# statement kinds whose names are understood here; any other kind is Unclear (refused for non-admins). Every
# function these can name is either a FuncCall (expressions: checked like any call) or a field checked by a handler
# (CreateTrigStmt.funcname, CreateFunctionStmt/AlterFunctionStmt SUPPORT). Kinds that name functions in other
# ways (CREATE AGGREGATE/OPERATOR/TYPE/TEXT SEARCH ... = DefineStmt, CREATE CAST, TRANSFORM, CONVERSION,
# OPERATOR CLASS/FAMILY, EVENT TRIGGER, LANGUAGE, FOREIGN DATA WRAPPER/SERVER, ACCESS METHOD, STATISTICS, ...)
# are deliberately not listed. tests/test_access_isolation.py::test_statement_kinds_table pins this set.
KNOWN_STATEMENTS = frozenset({
    "SelectStmt", "InsertStmt", "UpdateStmt", "DeleteStmt", "MergeStmt", "CreateStmt", "AlterTableStmt",
    "IndexStmt", "ViewStmt", "CreateTableAsStmt", "CreateSeqStmt", "AlterSeqStmt", "DropStmt", "TruncateStmt",
    "CommentStmt", "RenameStmt", "AlterObjectSchemaStmt", "AlterOwnerStmt", "GrantStmt",
    "AlterDefaultPrivilegesStmt", "CreateFunctionStmt", "AlterFunctionStmt", "DoStmt", "CallStmt",
    "CreateTrigStmt", "CreateEnumStmt", "AlterEnumStmt", "CompositeTypeStmt", "CreateDomainStmt",
    "AlterDomainStmt", "CreatePolicyStmt", "AlterPolicyStmt",
    "RuleStmt", "VariableSetStmt", "ReindexStmt", "ClusterStmt", "VacuumStmt", "RefreshMatViewStmt", "LockStmt",
    "ExplainStmt", "ConstraintsSetStmt", "TransactionStmt",
})

_DYNAMIC = ("dynamic SQL (EXECUTE) cannot be checked for the databases it reaches")


@dataclass
class Scan:
    schemas: set[str] = field(default_factory=set)
    catalog: bool = False            # reads a system catalog relation
    unclear: list[str] = field(default_factory=list)
    unparsable: bool = False
    settings: list[str] = field(default_factory=list)                 # every SET / RESET name, bodies included
    names: set[str] = field(default_factory=set)                      # every identifier (not string constants)
    operators: set[tuple[str | None, str]] = field(default_factory=set)  # (schema or None, symbol) of A_Expr
    relations: set[tuple[str | None, str]] = field(default_factory=set)  # (schema or None, name) of every RangeVar


_IDENT = re.compile(r'\s*("(?:[^"]|"")+"|[A-Za-z_\u0080-\uffff][\w$\u0080-\uffff]*)\s*')


def _literal_name(text: str) -> list[str]:
    """`'crm.t'` / `'"Crm"."T"'` -> the dotted parts as Postgres reads them (1 to 3 identifiers).

    Anything else is Unclear: arrays (`'{crm.t,t}'`), argument lists (`'f(crm.t)'`), operators, lists. Postgres
    would resolve every name inside those, so a partial reading would let one through unchecked."""
    parts, pos = [], 0
    while True:
        m = _IDENT.match(text, pos)
        if not m:
            raise Unclear("a name in a string must be a plain (optionally qualified) name to be checked")
        p = m.group(1)
        parts.append(p[1:-1].replace('""', '"') if p.startswith('"') else p.lower())
        pos = m.end()
        if pos >= len(text):
            if len(parts) > 3:
                raise Unclear("a name in a string must be a plain (optionally qualified) name to be checked")
            return parts
        if text[pos] != ".":
            raise Unclear("a name in a string must be a plain (optionally qualified) name to be checked")
        pos += 1


def _strings(names) -> list[str] | None:
    if isinstance(names, tuple) and names and all(isinstance(x, ast.String) for x in names):
        return [x.sval for x in names]
    return None


def _const_text(node) -> str | None:
    while isinstance(node, ast.TypeCast):      # 'simple'::regconfig, as pg_get_viewdef writes it
        node = node.arg
    if isinstance(node, ast.A_Const) and isinstance(node.val, ast.String):
        return node.val.sval
    return None


def _is_const(node) -> bool:
    return isinstance(node, ast.A_Const)


def risky_function(name: str) -> bool:
    """A function a non-admin may not call by name: it runs SQL text, changes settings, or turns names or object ids
    into catalog information (see the lists above). Name-taking functions checked on their arguments are not."""
    name = name.lower()
    return (name == "set_config" or name in _RUNS_SQL or name.endswith(_RUNS_SQL_SUFFIX) or name in _BY_OID
            or name.startswith(_BY_OID_PREFIX) or name.endswith(_BY_OID_SUFFIX)
            or (name.startswith("pg_") and name not in PG_OK)
            or (name in _BY_TYPE and not name.startswith("has_")) or bool(_BY_TYPE_PATTERN.match(name)))


def closure_risky_function(name: str) -> bool:
    """A function that may not be called where its arguments are not visible here (a view's rule, a trigger, an
    operator): `risky_function`, plus the name-taking ones, whose arguments are checked only in a call we parse.
    Text search functions are fine there: the configuration they use is followed as its own dependency."""
    name = name.lower()
    if name in _TS_CONFIG:
        return False
    return risky_function(name) or name in _NAME_FIRST or name in _SCHEMA_FIRST or (
        name.startswith("has_") and name.endswith("_privilege"))


def _function_refusal(name: str) -> str:
    return f"{name}() is not allowed: it can show or read objects outside the databases you can reach"


class _Collector:
    def __init__(self, scan: Scan):
        self.scan = scan
        self.cast_ok: set[int] = set()
        self.seen: set[int] = set()       # name lists a typed handler read (the fallback refuses the others)

    def mark(self, names):
        if isinstance(names, tuple):
            self.seen.add(id(names))
        return names

    # ---- helpers
    def add(self, name) -> None:
        if isinstance(name, str) and name:
            self.scan.schemas.add(name)

    def unclear(self, why: str) -> None:
        self.scan.unclear.append(why)

    def qualified(self, names, offset: int = 2) -> None:
        self.mark(names)
        parts = _strings(names)
        if parts and len(parts) >= offset:
            self.add(parts[-offset])

    def literal(self, node, *, is_schema: bool = False) -> None:
        text = _const_text(node)
        if text is None:
            self.unclear("a name given as an expression (not a string constant) cannot be checked")
            return
        try:
            parts = _literal_name(text)
        except Unclear as exc:
            self.unclear(str(exc))
            return
        if is_schema:
            self.add(parts[-1])
        elif len(parts) >= 2:
            self.add(parts[-2])

    def obj(self, objtype, obj) -> None:
        """The target of COMMENT / DROP / ALTER ... OWNER / SET SCHEMA / RENAME / GRANT (names by object type)."""
        self.mark(obj)
        if isinstance(obj, ast.String):
            if objtype == enums.ObjectType.OBJECT_SCHEMA:
                self.add(obj.sval)
            return
        parts = _strings(obj)
        if not parts:
            return
        if objtype == enums.ObjectType.OBJECT_SCHEMA:
            for p in parts:
                self.add(p)
        elif objtype in _THREE_PART:
            if len(parts) >= 3:
                self.add(parts[-3])
        elif len(parts) >= 2:
            self.add(parts[-2])

    # ---- walk
    def walk(self, node, owner: tuple[str, str] | None = None) -> None:
        if isinstance(node, (tuple, list)):
            self.fallback(node, owner)
            for x in node:
                self.walk(x, owner)
            return
        if not isinstance(node, ast.Node):
            return
        if isinstance(node, ast.A_Const):           # a string constant is data, not a name
            return
        if isinstance(node, ast.String):
            self.scan.names.add(node.sval.lower())
        handler = getattr(self, "on_" + type(node).__name__, None)
        if handler is not None:
            handler(node)
        self.generic(node)
        for member in node:
            self.walk(getattr(node, member), (type(node).__name__, member))

    def fallback(self, value, owner) -> None:
        """Fail closed: a qualified name list (2+ identifiers) that no typed handler read, in a position not known
        to hold column names or data, cannot be checked."""
        if (isinstance(value, tuple) and len(value) >= 2 and all(isinstance(x, ast.String) for x in value)
                and id(value) not in self.seen and owner not in _NOT_NAMES):
            where = ".".join(owner) if owner else "a list"
            self.unclear(f"a qualified name in {where} cannot be checked for the databases it names")

    def generic(self, node) -> None:
        for attr in _NAME_ATTRS:
            value = getattr(node, attr, None)
            if isinstance(value, tuple):
                self.qualified(value)

    # ---- nodes
    def on_RangeVar(self, n):  # noqa: N802
        self.add(n.schemaname)
        self.scan.relations.add((n.schemaname, n.relname))
        if n.schemaname in SYSTEM or (n.schemaname or "").startswith("pg_") \
                or (n.schemaname is None and n.relname.startswith("pg_")):
            self.scan.catalog = True

    def on_TypeName(self, n):  # noqa: N802
        parts = _strings(self.mark(n.names)) or []
        if n.pct_type:              # schema.table.column%TYPE
            if len(parts) >= 3:
                self.add(parts[-3])
        elif len(parts) >= 2:
            self.add(parts[-2])
        if parts and parts[-1] in REG_TYPES and id(n) not in self.cast_ok:
            self.unclear(f"values of type {parts[-1]} cannot be checked for the databases they name")

    def on_TypeCast(self, n):  # noqa: N802
        parts = _strings(n.typeName.names) if n.typeName is not None else None
        if parts and parts[-1] in REG_TYPES:
            self.cast_ok.add(id(n.typeName))
            if n.typeName.arrayBounds:      # '{crm.t,t}'::regclass[]: every element is resolved
                self.unclear(f"arrays of {parts[-1]} cannot be checked for the databases they name")
            else:
                self.literal(n.arg, is_schema=parts[-1] == "regnamespace")

    def on_FuncCall(self, n):  # noqa: N802
        parts = _strings(self.mark(n.funcname)) or []
        if len(parts) >= 2:
            self.add(parts[-2])
        base = parts[-1].lower() if parts else ""
        args = n.args or ()
        if base == "set_config":
            self.scan.settings.append(_const_text(args[0]).lower() if args and _const_text(args[0]) else "set_config")
        if base in _NAME_FIRST or base in _SCHEMA_FIRST:   # before the type rule: their name argument is checked
            if args:
                self.literal(args[0], is_schema=base in _SCHEMA_FIRST)
        elif base in _TS_CONFIG:
            if len(args) >= _TS_CONFIG[base]:          # the form whose first argument is a configuration/dictionary
                self.literal(args[0])
        elif base.startswith("has_") and base.endswith("_privilege"):
            if not all(_is_const(a) for a in args):
                self.unclear(f"{base}() with a name given as an expression cannot be checked")
            elif base == "has_schema_privilege":
                if args:
                    self.literal(args[0 if len(args) == 2 else 1], is_schema=True)
            else:
                for a in args:
                    text = _const_text(a)
                    if text is not None and "." in text:
                        self.literal(a)
        elif risky_function(base):
            self.unclear(_function_refusal(base))

    def on_CreateTrigStmt(self, n):  # noqa: N802
        """The trigger function gets the function rule; a catalog trigger function (tsvector_update_trigger takes a
        configuration name as an argument) is refused unless known safe; dotted arguments are checked as names."""
        self.mark(n.args)
        parts = _strings(n.funcname) or []
        base = parts[-1].lower() if parts else ""
        if base and closure_risky_function(base):
            self.unclear(_function_refusal(base))
        elif (base in _CATALOG_TRIGGERS and (len(parts) == 1 or parts[-2] == "pg_catalog")
              and base not in _SAFE_CATALOG_TRIGGERS):
            self.unclear(f"the catalog trigger function {base}() cannot be checked")
        for a in n.args or ():
            if isinstance(a, ast.String) and "." in a.sval:
                try:
                    names = _literal_name(a.sval)
                except Unclear:
                    continue                          # not a name: plain data for the function
                if len(names) >= 2:
                    self.add(names[-2])

    def support(self, options) -> None:
        if _option(options, "support") is not None:
            self.unclear("a SUPPORT function cannot be checked")

    def on_AlterFunctionStmt(self, n):  # noqa: N802
        self.support(n.actions)

    def on_VariableSetStmt(self, n):  # noqa: N802
        """SET / RESET anywhere (top level, DO and function bodies, `CREATE/ALTER FUNCTION ... SET`). The name is
        the parser's (comments and U& escapes already decoded). A fenced setting cannot be checked."""
        name = (n.name or "").lower()
        if n.kind == enums.VariableSetKind.VAR_RESET_ALL:
            name = "all"
        self.scan.settings.append(name)
        if name in FENCED_SETTINGS or name == "all":
            label = "every setting (RESET ALL)" if name == "all" else name
            self.unclear(f"changing {label} is not allowed: the role, the limits and where names resolve are fixed")
        for a in n.args or ():               # default_text_search_config = 'crm.cfg': a value naming an object
            text = _const_text(a)
            if text is not None and "." in text:
                self.literal(a)

    def on_ColumnRef(self, n):  # noqa: N802
        fields = self.mark(n.fields) or ()
        star = bool(fields) and isinstance(fields[-1], ast.A_Star)
        parts = _strings(fields[:-1] if star else fields) or []
        need = 2 if star else 3          # schema.table.* / schema.table.column
        if len(parts) >= need:
            self.add(parts[-need])

    def on_SubLink(self, n):  # noqa: N802
        parts = _strings(self.mark(n.operName))          # x OP ANY (subquery)
        if parts:
            self.add(parts[-2] if len(parts) >= 2 else None)
            self.scan.operators.add((parts[-2] if len(parts) >= 2 else None, parts[-1]))

    def on_SortBy(self, n):  # noqa: N802
        parts = _strings(self.mark(n.useOp))             # ORDER BY x USING op
        if parts:
            self.add(parts[-2] if len(parts) >= 2 else None)
            self.scan.operators.add((parts[-2] if len(parts) >= 2 else None, parts[-1]))

    def on_A_Expr(self, n):  # noqa: N802
        self.qualified(n.name)           # OPERATOR(crm.+)
        parts = _strings(n.name)
        if parts:
            self.scan.operators.add((parts[-2] if len(parts) >= 2 else None, parts[-1]))

    def on_DropStmt(self, n):  # noqa: N802
        for o in n.objects or ():
            self.obj(n.removeType, o)

    def on_CommentStmt(self, n):  # noqa: N802
        self.obj(n.objtype, n.object)

    def on_RenameStmt(self, n):  # noqa: N802
        self.obj(n.renameType, n.object)

    def on_AlterOwnerStmt(self, n):  # noqa: N802
        self.obj(n.objectType, n.object)

    def on_AlterObjectSchemaStmt(self, n):  # noqa: N802
        self.obj(n.objectType, n.object)
        self.add(n.newschema)

    def on_GrantStmt(self, n):  # noqa: N802
        """Every object kind a GRANT/REVOKE names: schemas (and ALL ... IN SCHEMA), tables and sequences
        (RangeVar), functions/procedures/routines (ObjectWithArgs), types and domains (name lists). Database-wide
        kinds (DATABASE, LANGUAGE, TABLESPACE, FOREIGN ..., LARGE OBJECT, PARAMETER) cannot be checked."""
        O = enums.ObjectType
        if n.objtype == O.OBJECT_SCHEMA or n.targtype == enums.GrantTargetType.ACL_TARGET_ALL_IN_SCHEMA:
            for o in n.objects or ():
                if isinstance(o, ast.String):
                    self.add(o.sval)
        elif n.objtype in (O.OBJECT_TYPE, O.OBJECT_DOMAIN):
            for o in n.objects or ():
                self.obj(n.objtype, o)
        elif n.objtype not in (O.OBJECT_TABLE, O.OBJECT_SEQUENCE, O.OBJECT_FUNCTION, O.OBJECT_PROCEDURE,
                               O.OBJECT_ROUTINE):
            self.unclear("GRANT/REVOKE on this kind of object cannot be checked")

    def on_DefElem(self, n):  # noqa: N802
        """Options that name objects: OWNED BY schema.table.column, SEQUENCE NAME schema.sequence, IN SCHEMA."""
        if n.defname == "owned_by":
            self.qualified(n.arg, 3)
        elif n.defname == "sequence_name":
            self.qualified(n.arg)

    def on_Constraint(self, n):  # noqa: N802
        """EXCLUDE (c WITH crm.=): each element's operator is a name and an operator."""
        for pair in n.exclusions or ():
            if isinstance(pair, tuple) and len(pair) == 2:
                op = self.mark(pair[1])
                parts = _strings(op)
                if parts:
                    self.add(parts[-2] if len(parts) >= 2 else None)
                    self.scan.operators.add((parts[-2] if len(parts) >= 2 else None, parts[-1]))

    def on_AlterDefaultPrivilegesStmt(self, n):  # noqa: N802
        for d in n.options or ():
            if isinstance(d, ast.DefElem) and d.defname == "schemas":
                for s in _strings(self.mark(d.arg)) or ():
                    self.add(s)

    def on_ReindexStmt(self, n):  # noqa: N802
        if n.kind == enums.ReindexObjectType.REINDEX_OBJECT_SCHEMA:
            self.add(n.name)
        elif n.kind != enums.ReindexObjectType.REINDEX_OBJECT_TABLE and n.kind != enums.ReindexObjectType.REINDEX_OBJECT_INDEX:
            self.unclear("REINDEX SYSTEM / DATABASE cannot be checked")

    def on_CreateSchemaStmt(self, n):  # noqa: N802
        self.add(n.schemaname)

    def on_DoStmt(self, n):  # noqa: N802
        self.body(n, _option(n.args, "language") or "plpgsql", _option(n.args, "as"))

    def on_CreateFunctionStmt(self, n):  # noqa: N802
        self.support(n.options)
        if n.sql_body is None:
            self.body(n, _option(n.options, "language") or "sql", _option(n.options, "as"))

    # ---- bodies
    def body(self, node, language: str, source) -> None:
        language = language.lower()
        if source is None:
            return
        if language == "sql":
            texts = [s.sval for s in (source if isinstance(source, tuple) else (source,)) if isinstance(s, ast.String)]
            for t in texts[:1]:           # AS 'body' (a second item is a C link symbol, not SQL)
                try:
                    stmts = pglast.parse_sql(t)
                except pglast.parser.ParseError as exc:
                    self.unclear(f"the function body cannot be checked: {exc}")
                    continue
                for raw in stmts:
                    self.statement(raw.stmt)
        elif language == "plpgsql":
            try:
                tree = pglast.parse_plpgsql(RawStream()(node))
            except Exception as exc:  # noqa: BLE001 - any failure to read the body is a refusal, not a crash
                self.unclear(f"the plpgsql body cannot be checked ({exc}); write qualified names in SQL statements "
                             "rather than in declarations")
                return
            self.plpgsql(tree)
        else:
            self.unclear(f"a body in language {language} cannot be checked")

    def plpgsql(self, tree) -> None:
        """Walk libpg_query's plpgsql tree. Every expression in any statement kind is a PLpgSQL_expr (conditions,
        loop bounds, RAISE parameters and options, CASE selectors, cursor arguments, assignment targets and
        values, ...): each is parsed and checked. A node kind not in PLPGSQL_NODES cannot be checked."""
        if isinstance(tree, list):
            for x in tree:
                self.plpgsql(x)
            return
        if not isinstance(tree, dict):
            return
        for key, value in tree.items():
            if key in ("PLpgSQL_stmt_dynexecute", "PLpgSQL_stmt_dynfors") or key == "dynquery":
                self.unclear(_DYNAMIC)
            elif key.startswith("PLpgSQL_") and key not in PLPGSQL_NODES:
                self.unclear(f"the plpgsql statement {key.removeprefix('PLpgSQL_')} cannot be checked")
            elif key == "PLpgSQL_expr" and isinstance(value, dict):
                self.expression(value.get("query") or "", value.get("parseMode", 0))
            elif key == "PLpgSQL_type" and isinstance(value, dict):
                self.decl_type(value.get("typname") or "")
            self.plpgsql(value)

    def expression(self, query: str, mode: int) -> None:
        if mode in (3, 4, 5):                       # target := value: both sides are checked
            split = _split_assignment(query)
            if split is None:
                self.unclear("a plpgsql assignment cannot be checked")
                return
            target, query = split
            if not _PLAIN_TARGET.match(target):     # a[(SELECT ...)] := ..., r.f[i] := ...
                self.parsed(f"SELECT {target}", "a plpgsql assignment target")
        sql = query if mode == 0 else f"SELECT NULL::{query}" if mode == 1 else f"SELECT {query}"
        self.parsed(sql, "a plpgsql expression")

    def parsed(self, sql: str, what: str) -> None:
        try:
            stmts = pglast.parse_sql(sql)
        except pglast.parser.ParseError as exc:
            self.unclear(f"{what} cannot be checked: {exc}")
            return
        for raw in stmts:
            self.statement(raw.stmt)

    def decl_type(self, typname: str) -> None:
        name = re.sub(r"%(row)?type$", "", typname, flags=re.IGNORECASE)
        try:
            parts = _literal_name(name.split("(", 1)[0].split("[", 1)[0])
        except Unclear:
            self.unclear(f"the plpgsql declaration type {typname} cannot be checked")
            return
        offset = 3 if typname.lower().endswith("%type") else 2
        if len(parts) >= offset:
            self.add(parts[-offset])

    def statement(self, node) -> None:
        kind = type(node).__name__
        if kind not in KNOWN_STATEMENTS:
            self.unclear(f"{kind.removesuffix('Stmt')} statements cannot be checked for the databases they reach")
        self.walk(node)


# every plpgsql node kind libpg_query produces (PG 16), reviewed: their expressions are all PLpgSQL_expr nodes
# (checked wherever they sit); the others hold variable references, names of conditions or format strings.
# tests/test_access_isolation.py::test_plpgsql_node_kinds_are_all_reviewed pins it.
PLPGSQL_NODES = frozenset({
    "PLpgSQL_function", "PLpgSQL_var", "PLpgSQL_rec", "PLpgSQL_row", "PLpgSQL_recfield", "PLpgSQL_type",
    "PLpgSQL_expr", "PLpgSQL_stmt_block", "PLpgSQL_exception_block", "PLpgSQL_exception", "PLpgSQL_condition",
    "PLpgSQL_stmt_assign", "PLpgSQL_stmt_if", "PLpgSQL_if_elsif", "PLpgSQL_stmt_case", "PLpgSQL_case_when",
    "PLpgSQL_stmt_loop", "PLpgSQL_stmt_while", "PLpgSQL_stmt_fori", "PLpgSQL_stmt_fors", "PLpgSQL_stmt_forc",
    "PLpgSQL_stmt_foreach_a", "PLpgSQL_stmt_exit", "PLpgSQL_stmt_return", "PLpgSQL_stmt_return_next",
    "PLpgSQL_stmt_return_query", "PLpgSQL_stmt_raise", "PLpgSQL_raise_option", "PLpgSQL_stmt_assert",
    "PLpgSQL_stmt_execsql", "PLpgSQL_stmt_getdiag", "PLpgSQL_diag_item", "PLpgSQL_stmt_open",
    "PLpgSQL_stmt_fetch", "PLpgSQL_stmt_close", "PLpgSQL_stmt_perform", "PLpgSQL_stmt_call",
    "PLpgSQL_stmt_commit", "PLpgSQL_stmt_rollback",
})   # PLpgSQL_stmt_dynexecute / _dynfors (and any dynquery) are refused as dynamic SQL

_PLAIN_TARGET = re.compile(r'^\s*("(?:[^"]|"")+"|[A-Za-z_][\w$]*)(\s*\.\s*("(?:[^"]|"")+"|[A-Za-z_][\w$]*))*\s*$')


def _split_assignment(text: str) -> tuple[str, str] | None:
    """`target := value` (or `target = value`) split at the first assignment outside quotes and brackets."""
    depth, quote, i = 0, None, 0
    eq = None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth == 0 and text.startswith(":=", i):
            return text[:i], text[i + 2:]
        elif depth == 0 and ch == "=" and eq is None:
            eq = i
        i += 1
    return (text[:eq], text[eq + 1:]) if eq is not None else None


# (node, field) positions whose lists of identifiers are column names, labels or data, never object names: the
# fallback leaves them alone. Anything else holding a 2+ identifier list that no handler read is refused.
_NOT_NAMES = frozenset({
    ("Alias", "colnames"), ("CommonTableExpr", "aliascolnames"), ("AccessPriv", "cols"), ("JoinExpr", "usingClause"),
    ("Constraint", "keys"), ("Constraint", "fk_attrs"), ("Constraint", "pk_attrs"), ("Constraint", "including"),
    ("Constraint", "fk_del_set_cols"), ("CreateTrigStmt", "columns"), ("VacuumRelation", "va_cols"),
    ("CopyStmt", "attlist"), ("CreateEnumStmt", "vals"), ("IntoClause", "colNames"), ("ViewStmt", "aliases"),
    ("CTESearchClause", "search_col_list"), ("CTECycleClause", "cycle_col_list"), ("IndexStmt", "excludeOpNames"),
    ("CreateStatsStmt", "exprs"), ("RangeFunction", "coldeflist"), ("PublicationTable", "columns"),
    ("CreateStmt", "inhRelations"), ("TableLikeClause", "relationOids"),
})


def _option(options, name: str):
    for d in options or ():
        if isinstance(d, ast.DefElem) and d.defname == name:
            return d.arg.sval if name == "language" and isinstance(d.arg, ast.String) else d.arg
    return None


def scan(sql_or_node) -> Scan:
    """Names, catalog reads and unclear shapes of SQL text (or of one already parsed statement node).

    SQL that does not parse is unclear. Never raises for the SQL's content."""
    out = _Collector(Scan())
    if isinstance(sql_or_node, ast.Node):
        out.statement(sql_or_node)
        return out.scan
    try:
        stmts = pglast.parse_sql(sql_or_node)
    except pglast.parser.ParseError as exc:
        out.unclear(str(exc))           # the parser's own text ("syntax error at or near ...")
        out.scan.unparsable = True
        return out.scan
    for raw in stmts:
        out.statement(raw.stmt)
    return out.scan


def named_schemas(sql: str) -> set[str]:
    """Every database (schema) name the SQL names, `pg_catalog`/`information_schema` included.

    Raises Unclear when the names cannot all be determined (see the module docstring)."""
    s = scan(sql)
    if s.unclear:
        raise Unclear(s.unclear[0])
    return s.schemas


def catalog_names_leak(body: str) -> bool:
    """True when the SQL reads a system catalog relation (`pg_namespace`, `pg_class`, `pg_tables`, ...,
    `information_schema.*`), which would list database names; also True for SQL that does not parse."""
    s = scan(body)
    return s.catalog or s.unparsable


def text_problem(sql_or_node, allowed, *, is_admin: bool) -> str | None:
    """Why this SQL may not run for a user who can reach `allowed` databases, or None. Admins: always None.

    A named database outside `allowed` gives the unknown-database text, whether it exists or not."""
    if is_admin:
        return None
    s = scan(sql_or_node)
    for name in sorted(s.schemas - SYSTEM):
        if name not in allowed:
            return introspect.unknown_schema_message(name)
    if s.unclear:
        return s.unclear[0]
    if s.catalog:
        return CATALOG_REFUSAL
    return None


def check_text(body: str, allowed, *, is_admin: bool) -> None:
    """`text_problem` as a QueryError."""
    problem = text_problem(body, allowed, is_admin=is_admin)
    if problem:
        raise QueryError(problem)


VIEW_REFUSAL = ("it uses a view, table or operator whose data comes from outside the databases you can reach (or "
                "that cannot be checked)")

# every relation a relation's data can come from: view/matview rules (pg_rewrite -> pg_depend), inheritance and
# partitions both ways; then everything those rules depend on (functions, operators and the functions behind
# them, types, casts and their functions, text search configurations and dictionaries, collations)
_CLOSURE_SQL = """
WITH RECURSIVE r(oid) AS (
    SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = %(schema)s AND c.relname = %(name)s
  UNION
    SELECT x.oid FROM r, LATERAL (
        SELECT d.refobjid FROM pg_rewrite w JOIN pg_depend d ON d.classid = 'pg_rewrite'::regclass
            AND d.objid = w.oid AND d.refclassid = 'pg_class'::regclass WHERE w.ev_class = r.oid
        UNION SELECT i.inhrelid FROM pg_inherits i WHERE i.inhparent = r.oid
        UNION SELECT i.inhparent FROM pg_inherits i WHERE i.inhrelid = r.oid
    ) x(oid)
), deps AS (
    SELECT DISTINCT d.refclassid, d.refobjid FROM r JOIN pg_rewrite w ON w.ev_class = r.oid
        JOIN pg_depend d ON d.classid = 'pg_rewrite'::regclass AND d.objid = w.oid
)
SELECT 'relation' AS what, n.nspname AS schema, c.relkind::text AS name FROM r JOIN pg_class c ON c.oid = r.oid
    JOIN pg_namespace n ON n.oid = c.relnamespace
UNION ALL
SELECT 'function', n.nspname, p.proname FROM deps JOIN pg_proc p ON deps.refclassid = 'pg_proc'::regclass
    AND p.oid = deps.refobjid JOIN pg_namespace n ON n.oid = p.pronamespace
UNION ALL
SELECT 'operator', n.nspname, o.oprname FROM deps JOIN pg_operator o ON deps.refclassid = 'pg_operator'::regclass
    AND o.oid = deps.refobjid JOIN pg_namespace n ON n.oid = o.oprnamespace
UNION ALL
SELECT 'function', n.nspname, p.proname FROM deps JOIN pg_operator o ON deps.refclassid = 'pg_operator'::regclass
    AND o.oid = deps.refobjid JOIN pg_proc p ON p.oid = o.oprcode JOIN pg_namespace n ON n.oid = p.pronamespace
UNION ALL
SELECT 'type', n.nspname, t.typname FROM deps JOIN pg_type t ON deps.refclassid = 'pg_type'::regclass
    AND t.oid = deps.refobjid JOIN pg_namespace n ON n.oid = t.typnamespace
UNION ALL
SELECT 'function', n.nspname, p.proname FROM deps JOIN pg_cast k ON deps.refclassid = 'pg_cast'::regclass
    AND k.oid = deps.refobjid JOIN pg_proc p ON p.oid = k.castfunc JOIN pg_namespace n ON n.oid = p.pronamespace
UNION ALL
SELECT 'other', n.nspname, '' FROM deps JOIN pg_ts_config x ON deps.refclassid = 'pg_ts_config'::regclass
    AND x.oid = deps.refobjid JOIN pg_namespace n ON n.oid = x.cfgnamespace
UNION ALL
SELECT 'other', n.nspname, '' FROM deps JOIN pg_ts_dict x ON deps.refclassid = 'pg_ts_dict'::regclass
    AND x.oid = deps.refobjid JOIN pg_namespace n ON n.oid = x.dictnamespace
UNION ALL
SELECT 'other', n.nspname, '' FROM deps JOIN pg_collation x ON deps.refclassid = 'pg_collation'::regclass
    AND x.oid = deps.refobjid JOIN pg_namespace n ON n.oid = x.collnamespace
UNION ALL
SELECT 'unknown', '', deps.refclassid::regclass::text FROM deps
    WHERE deps.refclassid NOT IN ('pg_class'::regclass, 'pg_proc'::regclass, 'pg_operator'::regclass,
        'pg_type'::regclass, 'pg_cast'::regclass, 'pg_ts_config'::regclass, 'pg_ts_dict'::regclass,
        'pg_collation'::regclass)
"""

# the definitions of the views and materialized views among them: built-in (pinned) catalog functions and types
# are never recorded in pg_depend, so a rule calling query_to_xml or casting to regclass is found by reading it
_VIEWDEFS_SQL = _CLOSURE_SQL.split("), deps AS (")[0] + """)
SELECT pg_get_viewdef(c.oid) AS def FROM r JOIN pg_class c ON c.oid = r.oid WHERE c.relkind IN ('v', 'm')
"""

# operators a statement uses by symbol, in the schemas it can resolve them in, with the function behind each
_OPERATOR_SQL = """
SELECT n.nspname AS schema, p.proname AS function, pn.nspname AS function_schema
FROM pg_operator o JOIN pg_namespace n ON n.oid = o.oprnamespace
    JOIN pg_proc p ON p.oid = o.oprcode JOIN pg_namespace pn ON pn.oid = p.pronamespace
WHERE o.oprname = %(symbol)s AND n.nspname = ANY(%(schemas)s)
"""

_NAME_TYPES = REG_TYPES | {"oidvector"}


def _closure_row_problem(row, allowed: set[str]) -> bool:
    what, schema, name = row["what"], row["schema"], row["name"]
    if what == "relation":
        return name == "f" or schema not in allowed            # a foreign table cannot be checked
    if what == "function":
        return schema not in allowed | {"pg_catalog"} or closure_risky_function(name)
    if what == "type":
        return schema not in allowed | SYSTEM or (schema == "pg_catalog" and name in _NAME_TYPES)
    if what in ("operator", "other"):
        return schema not in allowed | {"pg_catalog"}
    return True                                                 # a dependency of a kind not followed here


def objects_problem(conn, scan_: Scan, default_schema: str, allowed) -> str | None:
    """VIEW_REFUSAL when what a statement names is, or is built on, something outside `allowed`, checked in the
    catalog on `conn`:

    - each named relation (unqualified ones resolve in `default_schema`) and everything its data can come from: a
      view or materialized view over another database, an inheritance relative or partition there, a foreign
      table, or a rule using a function, operator (and the function behind it), type, cast, text search
      configuration or collation of another database, or a risky catalog function (`closure_risky_function`);
    - each user-defined operator used (in the database it is qualified with, else the default one): one whose
      function is in another database or risky (an operator can hide `format_type` or `ts_rewrite`). Built-in
      pg_catalog operators are not looked up; an operator qualified with another database was refused by name.

    Only objects in `allowed` databases (plus pg_catalog for operators) are looked up, and callers refuse other
    names first, so the answer never depends on whether a hidden database exists. The text names nothing. A
    function in an allowed database is not followed into its body (the accepted gap)."""
    allowed = set(allowed)
    for sch, name in sorted({(sch or default_schema, name) for sch, name in scan_.relations},
                            key=lambda x: (x[0] or "", x[1])):
        if sch not in allowed:
            continue
        for row in conn.execute(_CLOSURE_SQL, {"schema": sch, "name": name}):
            if _closure_row_problem(row, allowed):
                return VIEW_REFUSAL
        for row in conn.execute(_VIEWDEFS_SQL, {"schema": sch, "name": name}):
            inner = scan(row["def"] or "")
            if inner.unclear or inner.catalog or (inner.schemas - SYSTEM - allowed) or "set_config" in inner.names:
                return VIEW_REFUSAL
    for sch, symbol in sorted(scan_.operators, key=lambda x: (x[0] or "", x[1])):
        # built-in (pg_catalog) operators are comparisons and arithmetic: only user-defined ones can hide a function
        schemas = [sch] if sch else [default_schema]
        if schemas == ["pg_catalog"] or not set(schemas) <= allowed:
            continue
        for row in conn.execute(_OPERATOR_SQL, {"symbol": symbol, "schemas": schemas}):
            if (row["function_schema"] not in allowed | {"pg_catalog"}
                    or closure_risky_function(row["function"])):
                return VIEW_REFUSAL
    return None


def closure_problem(scan_: Scan, default_schema: str, allowed) -> str | None:
    """`objects_problem` on a fresh read-only connection (raw SQL and view queries are not planned here)."""
    from .conn import ro_connection

    if not scan_.relations and not scan_.operators:
        return None
    with ro_connection() as conn:
        return objects_problem(conn, scan_, default_schema, allowed)


def _level_schemas(user, level: str) -> set[str]:
    from accounts import access

    return {e["name"] for e in access.effective_schemas(user) if access.can(user, e["name"], level)}


def reach_problem(user, sql_or_node, level: str = "editor", *, schema: str = "master") -> tuple[str, str] | None:
    """For SQL that will run as the read/write role (raw SQL, view queries) with `search_path` = `schema`:
    `(code, message)` when it names a database where `user` lacks `level`, reads the catalogs, cannot be checked,
    or reads a relation whose data comes from elsewhere (`closure_problem`); None when fine. Admins: None.

    code "invalid" with the unknown-database text for a database they cannot reach (hidden equals missing: the
    same text whether it exists or not), "forbidden" with the role text for one they can read but not at `level`.
    """
    from accounts import access

    s = scan(sql_or_node)
    if "set_config" in s.names:           # for everyone, in any position (a call, an aggregate's SFUNC, ...)
        return "invalid", "set_config is not allowed: the role, the limits and where names resolve are fixed"
    if access.is_admin(user):
        return None
    for name in sorted(s.schemas - SYSTEM):
        try:
            access.require(user, name, level)
        except introspect.UnknownSchema as exc:
            return "invalid", f"{exc} Call list_schemas to see the databases."
        except access.AccessDenied as exc:
            return "forbidden", str(exc)
    if s.unclear:
        return "invalid", f"{s.unclear[0]}; only an admin can propose it"
    if s.catalog:
        return "invalid", CATALOG_REFUSAL
    problem = closure_problem(s, schema, _level_schemas(user, level))
    if problem:
        return "invalid", problem
    return None


def view_queries(ops):
    """`(where, query)` for each create_view operation's query (the SQL a change carries besides raw SQL)."""
    if not isinstance(ops, list):
        return []
    return [(f"operation {i + 1} (create_view)", o["query"]) for i, o in enumerate(ops)
            if isinstance(o, dict) and o.get("op") == "create_view" and isinstance(o.get("query"), str)]


def _plan_nodes(tree):
    if isinstance(tree, list):
        for x in tree:
            yield from _plan_nodes(x)
    elif isinstance(tree, dict):
        yield tree
        for v in tree.values():
            if isinstance(v, (list, dict)):
                yield from _plan_nodes(v)


def plan_problem(plan, allowed) -> str | None:
    """Why this EXPLAIN (FORMAT JSON) plan reads outside `allowed`, or None. Walks every node of the tree."""
    for node in _plan_nodes(plan):
        if "Relation Name" in node:
            schema = node.get("Schema")
            if schema not in allowed:
                return CATALOG_REFUSAL if schema in SYSTEM or (schema or "").startswith("pg_") else PLAN_REFUSAL
        elif node.get("Node Type") in ("Foreign Scan", "Custom Scan"):
            return PLAN_REFUSAL                    # a scan without a named relation: cannot be checked
        if "Function Name" in node and (node.get("Schema") not in set(allowed) | {"pg_catalog"}
                                        or closure_risky_function(node["Function Name"])):
            return PLAN_REFUSAL
    return None


def check_plan(conn, body: str, allowed, *, is_admin: bool) -> None:
    """Plan `body` on `conn` (search_path and timeout already set) and raise QueryError if any relation of the
    plan is outside `allowed`. Admins reach everything: nothing is planned for them."""
    if is_admin:
        return
    # the planner expands views but not what their rules call (operators, functions, types): follow those first
    default = conn.execute("SELECT current_schema() AS s").fetchone()["s"]
    if objects_problem(conn, scan(body), default, set(allowed)):
        raise QueryError(PLAN_REFUSAL)
    row = conn.execute("EXPLAIN (VERBOSE, FORMAT JSON) " + body).fetchone()
    value = next(iter(row.values())) if isinstance(row, dict) else row[0]
    plan = json.loads(value) if isinstance(value, str) else value
    problem = plan_problem(plan, set(allowed))
    if problem:
        raise QueryError(problem)

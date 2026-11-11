"""A pg_dump 18 style dump for scale tests, and the small mixed sample the parser tests share."""
from __future__ import annotations

HEADER = """--
-- PostgreSQL database dump
--

\\restrict AbCdEf123

-- Dumped from database version 18.0
-- Dumped by pg_dump version 18.0

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;
SET default_tablespace = '';
SET default_table_access_method = heap;

"""

PROBE = HEADER + """CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;
COMMENT ON EXTENSION pg_trgm IS 'text similarity';

CREATE SCHEMA master;
ALTER SCHEMA master OWNER TO postgres;

CREATE TABLE master.province (
    province_id integer NOT NULL,
    name_th character varying(100) NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);
ALTER TABLE master.province OWNER TO postgres;
CREATE SEQUENCE master.province_province_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE master.province_province_id_seq OWNED BY master.province.province_id;
ALTER TABLE ONLY master.province ALTER COLUMN province_id SET DEFAULT nextval('master.province_province_id_seq'::regclass);
GRANT ALL ON TABLE master.province TO app;
CREATE FUNCTION master.f() RETURNS integer LANGUAGE sql AS $$ SELECT 1; SELECT 2 $$;
CREATE TRIGGER t BEFORE INSERT ON master.province FOR EACH ROW EXECUTE FUNCTION master.f();

COPY master.province (province_id, name_th, created_at) FROM stdin;
10\tกรุงเทพมหานคร\t2020-01-01 00:00:00+00
\\.

SELECT pg_catalog.setval('master.province_province_id_seq', 10, true);

ALTER TABLE ONLY master.province
    ADD CONSTRAINT province_pkey PRIMARY KEY (province_id);

\\unrestrict AbCdEf123
"""

_THAI = "ภาษาไทยทดสอบข้อมูลหลัก"


def write_dump(path, tables: int = 80, rows: int = 192_000, target_bytes: int = 250 * 2**20) -> dict:
    """Write a dump with `tables` tables and `rows` COPY rows of about `target_bytes` in total."""
    per = [rows // tables + (1 if i < rows % tables else 0) for i in range(tables)]
    fixed = 40 + len(_THAI.encode()) + 30
    filler = max(10, target_bytes // max(rows, 1) - fixed)
    note = ("x" * (filler // 2)) + _THAI * ((filler // 2) // (len(_THAI) * 3) + 1)
    note = note[: filler // 2 + (filler // 2) // 3 * 1]  # characters, not bytes: close enough for a size target
    total = 0
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(HEADER + "CREATE SCHEMA master;\n\n")
        for t in range(tables):
            f.write(f"CREATE TABLE master.t{t:02d} (\n    t{t:02d}_id character varying(20) NOT NULL,\n"
                    "    name_th text,\n    name_en text,\n    note text\n);\n\n")
        for t in range(tables):
            f.write(f"COPY master.t{t:02d} (t{t:02d}_id, name_th, name_en, note) FROM stdin;\n")
            for i in range(per[t]):
                f.write(f"K{t:02d}-{i:06d}\t{_THAI}{i}\tName {t}-{i}\t{note}\n")
                total += 1
            f.write("\\.\n\n")
        for t in range(tables):
            f.write(f"ALTER TABLE ONLY master.t{t:02d}\n    ADD CONSTRAINT t{t:02d}_pkey PRIMARY KEY (t{t:02d}_id);\n")
        f.write("\n\\unrestrict AbCdEf123\n")
    import os
    return {"bytes": os.path.getsize(path), "tables": tables, "rows": total}

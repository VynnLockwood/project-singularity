# Managed schemas: owner migration runbook

**DO NOT RUN until the owner confirms.** Nothing in this repository runs this migration automatically.

## What it does

`db/migrations/2026-11-09-managed-schemas.sql` adds three `SECURITY DEFINER` functions to the `singularity` schema:

- `singularity.create_managed_schema(text)`
- `singularity.rename_managed_schema(text, text)`
- `singularity.drop_managed_schema(text)`

It also grants `singularity_master_rw` USAGE on `singularity` and EXECUTE on the three functions. The app role never gets CREATE on the database; schema DDL goes only through these functions. Fresh installs get the same statements from `db/init/01-schemas-roles.sh`, so this is only for an existing database.

## Who runs it

The database owner, once:

```
psql -U <owner> -d <db> -f db/migrations/2026-11-09-managed-schemas.sql
```

`<owner>` must be a superuser, or the owner of the `singularity` schema. A non-superuser owner also needs CREATE on the database and membership in `singularity_master_rw` (the functions create schemas `AUTHORIZATION singularity_master_rw`), and must be allowed to create `SECURITY DEFINER` functions there. The functions are owned by whoever runs the file, so run it as the owner and not as an app role. The file is one transaction and uses `CREATE OR REPLACE`, so running it twice is harmless.

## Check queries (after the migration)

```sql
SELECT proname FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'singularity' AND proname LIKE '%managed_schema';
-- expected: 3 rows (create_managed_schema, drop_managed_schema, rename_managed_schema)

SELECT has_database_privilege('singularity_master_rw', current_database(), 'CREATE');
-- expected: false

SELECT proname, proowner::regrole, prosecdef FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'singularity' AND proname LIKE '%managed_schema';
-- expected: owner = the migration owner (not singularity_master_rw), prosecdef = true for all 3
```

## App behaviour before and after

- Before: only `master` exists and works. Everything on `master` is unchanged. A `create_schema` change can be proposed and approved, but applying it fails: the change ends in status `failed` with the database message `permission denied for schema singularity` (the app role has no USAGE on `singularity` until the migration grants it; once it does, a missing function reads `function singularity.create_managed_schema(...) does not exist`). Nothing is created and `master` is untouched (covered by `test_schema_ops_fail_readably_without_the_migration`). `rename_schema` and `drop_schema` fail the same way.
- After: approvers can create, rename and drop managed schemas through the normal propose/approve flow; table, row, query, export, duplicate, search and change operations take a schema (default `master`). Script intake validates and loads into one schema per script (`schema` on the validate endpoint and the MCP/chat script tools, default `master`); a script cannot create or rename schemas. Limits: `limits.maxSchemas` (default 20, counts `master`). `master` cannot be renamed or dropped. `drop_schema` works only on an empty schema and needs a reason.

## Rollback

Safe only while no managed schema other than `master` exists (drop or empty them through the app first, or the app loses the way to manage them):

```sql
DROP FUNCTION singularity.create_managed_schema(text);
DROP FUNCTION singularity.rename_managed_schema(text, text);
DROP FUNCTION singularity.drop_managed_schema(text);
REVOKE USAGE ON SCHEMA singularity FROM singularity_master_rw;
```

Existing managed schemas keep working for tables and data after the functions are gone, but they can then no longer be renamed or dropped from the app.

## Accepted limits

As owner of its schemas, `singularity_master_rw` can, if someone connects as that role directly:

- run `DROP SCHEMA ... CASCADE` on any schema it owns, and
- `GRANT` privileges on those schemas to other roles.

The gate never emits either statement, and the executor only calls the three functions (`drop_managed_schema()` refuses non-empty schemas, unmanaged schemas and `master`). The direct paths are outside the app and are accepted.

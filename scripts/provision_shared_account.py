"""Provision a restricted role/schema in the existing database; no data migration.

Requires DATABASE_URL and KAYHAP_DATABASE_PASSWORD in the shell environment.
Never print credentials or connection URLs. Run with --apply only after review.
"""
import argparse
import os

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url


def provision(connection, password):
    if len(password) < 32:
        raise ValueError("A separate password of at least 32 characters is required")
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user")
            database, owner = cursor.fetchone()
            cursor.execute("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = 'p2p_kayhap')")
            if cursor.fetchone()[0]:
                raise ValueError("Role already exists; verify its permissions before reusing")
            cursor.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = 'kayhap')")
            if cursor.fetchone()[0]:
                raise ValueError("Schema already exists; this script never overwrites existing data")
            cursor.execute(sql.SQL("CREATE ROLE p2p_kayhap LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT").format(sql.Literal(password)))
            cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO p2p_kayhap").format(sql.Identifier(database)))
            cursor.execute("CREATE SCHEMA kayhap AUTHORIZATION p2p_kayhap")
            cursor.execute("ALTER ROLE p2p_kayhap SET search_path TO kayhap")
            cursor.execute(sql.SQL("SELECT EXISTS(SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND has_table_privilege('p2p_kayhap', format('%I.%I', schemaname, tablename), 'SELECT,INSERT,UPDATE,DELETE'))"))
            if cursor.fetchone()[0]:
                raise ValueError("PUBLIC grants expose original tables; refusing provisioning")
    return "Created restricted role p2p_kayhap and schema kayhap. Existing tables untouched."


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print("Preview: create role p2p_kayhap and schema kayhap in the existing database; no deletes or updates.")
        return
    try:
        url = make_url(os.environ["DATABASE_URL"])
        with psycopg.connect(host=url.host, port=url.port or 5432, dbname=url.database,
                             user=url.username, password=url.password, **dict(url.query)) as connection:
            print(provision(connection, os.environ["KAYHAP_DATABASE_PASSWORD"]))
    except Exception as exc:
        # Driver errors and URLs may contain credentials; emit only a safe type.
        print("Provisioning did not complete: " + type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

import sys

from alembic import command
from sqlalchemy import Connection, create_engine
from sqlalchemy.pool import NullPool

from app.database import (
    Settings,
    current_revisions,
    expected_revision,
    migration_config,
)

ROLE_BASELINE = """
SELECT
    (SELECT nspowner = 'qa_migrate'::regrole FROM pg_namespace WHERE nspname = 'qa')
    AND (SELECT count(*) = 2 AND bool_and(
           rolcanlogin AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
           AND has_database_privilege(oid, current_database(), 'CONNECT')
           AND NOT has_database_privilege(oid, current_database(), 'CONNECT WITH GRANT OPTION')
           AND NOT has_database_privilege(oid, current_database(), 'CREATE')
           AND NOT has_database_privilege(oid, current_database(), 'TEMPORARY')
           AND has_schema_privilege(oid, 'qa', 'USAGE')
           AND has_schema_privilege(oid, 'qa', 'CREATE') = (rolname = 'qa_migrate')
           AND has_schema_privilege(oid, 'qa', 'USAGE WITH GRANT OPTION') = (rolname = 'qa_migrate')
           AND NOT has_schema_privilege(oid, 'public', 'CREATE'))
         FROM pg_roles WHERE rolname IN ('qa_api', 'qa_migrate'))
    AND NOT EXISTS (
        SELECT FROM pg_auth_members
        WHERE member IN ('qa_api'::regrole, 'qa_migrate'::regrole)
           OR roleid IN ('qa_api'::regrole, 'qa_migrate'::regrole)
    )
    AND NOT EXISTS (
        SELECT FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) acl
        WHERE n.nspname='qa' AND acl.grantee NOT IN ('qa_api'::regrole, 'qa_migrate'::regrole)
    )
    AND NOT EXISTS (
        SELECT FROM pg_database d
        CROSS JOIN LATERAL aclexplode(coalesce(d.datacl, acldefault('d',d.datdba))) acl
        WHERE d.datname=current_database() AND acl.grantee=0
          AND acl.privilege_type IN ('CONNECT','CREATE','TEMPORARY')
    )
"""

KT2_REVISION = "0001_runs"
FIXTURES_REVISION = "0002_las_fixtures"
API_GRANTS = {
    "runs": ("SELECT", "INSERT"),
    "alembic_version": ("SELECT",),
    "las_fixture_runs": (),
    "las_run_clients": (),
    "las_orders": ("SELECT",),
    "las_delivery_dates": ("SELECT",),
}
REVISION_TABLES = {
    (): (),
    (KT2_REVISION,): ("runs", "alembic_version"),
    (FIXTURES_REVISION,): tuple(API_GRANTS),
}
TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
    "MAINTAIN",
)
COLUMN_PRIVILEGES = {"SELECT", "INSERT", "UPDATE", "REFERENCES"}

COLUMNS = {
    "runs": (
        ("run_id", "uuid", None, None),
        ("schema_version", "integer", None, None),
        ("created_at", "timestamp with time zone", None, "clock_timestamp()"),
        ("status", "text", "default", None),
    ),
    "alembic_version": (("version_num", "character varying(32)", "default", None),),
    "las_fixture_runs": (
        ("run_id", "uuid", None, None),
        ("fixture_version", "text", "default", None),
        ("content_sha256", "text", "default", None),
    ),
    "las_run_clients": (
        ("run_id", "uuid", None, None),
        ("client_id", "text", "C", None),
    ),
    "las_orders": (
        ("run_id", "uuid", None, None),
        ("client_id", "text", "C", None),
        ("order_id", "text", "C", None),
        ("status", "text", "default", None),
        ("scheduled_delivery_date", "date", None, None),
    ),
    "las_delivery_dates": (
        ("run_id", "uuid", None, None),
        ("client_id", "text", "C", None),
        ("order_id", "text", "C", None),
        ("delivery_date", "date", None, None),
    ),
}
PRIMARY_KEYS = {
    "runs": ("runs_pkey", "run_id"),
    "alembic_version": ("alembic_version_pkc", "version_num"),
    "las_fixture_runs": ("las_fixture_runs_pkey", "run_id"),
    "las_run_clients": ("las_run_clients_pkey", "run_id, client_id"),
    "las_orders": ("las_orders_pkey", "run_id, client_id, order_id"),
    "las_delivery_dates": (
        "las_delivery_dates_pkey",
        "run_id, client_id, order_id, delivery_date",
    ),
}
OTHER_CONSTRAINTS = {
    "runs": {
        ("runs_positive_schema_version", "CHECK ((schema_version > 0))"),
        ("runs_created_status", "CHECK ((status = 'CREATED'::text))"),
    },
    "las_fixture_runs": {
        ("las_fixture_runs_run_fk", "FOREIGN KEY (run_id) REFERENCES qa.runs(run_id)"),
        (
            "las_fixture_runs_sha256",
            "CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text))",
        ),
    },
    "las_run_clients": {
        (
            "las_run_clients_run_fk",
            "FOREIGN KEY (run_id) REFERENCES qa.las_fixture_runs(run_id)",
        ),
    },
    "las_orders": {
        (
            "las_orders_client_fk",
            "FOREIGN KEY (run_id, client_id) REFERENCES qa.las_run_clients(run_id, client_id)",
        ),
        (
            "las_orders_status",
            "CHECK ((status = ANY (ARRAY['ACTIVE'::text, 'TRANSFER_BLOCKED'::text])))",
        ),
    },
    "las_delivery_dates": {
        (
            "las_delivery_dates_order_fk",
            "FOREIGN KEY (run_id, client_id, order_id) REFERENCES qa.las_orders(run_id, client_id, order_id)",
        ),
    },
}


def _validate_catalog(connection: Connection, tables: tuple[str, ...]) -> None:
    objects = connection.exec_driver_sql("""
        SELECT relname, relkind,
               relowner = 'qa_migrate'::regrole AND relpersistence='p'
               AND NOT relrowsecurity AND NOT relforcerowsecurity
        FROM pg_class WHERE relnamespace = 'qa'::regnamespace
    """)
    expected = {(table, "r", True) for table in tables}
    expected.update((PRIMARY_KEYS[table][0], "i", True) for table in tables)
    if set(map(tuple, objects)) != expected:
        raise RuntimeError("unexpected PostgreSQL objects or ownership")
    columns = connection.exec_driver_sql("""
        SELECT c.relname, a.attname, format_type(a.atttypid,a.atttypmod), coll.collname,
               pg_get_expr(d.adbin,d.adrelid), a.attnotnull
        FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
        LEFT JOIN pg_collation coll ON coll.oid=a.attcollation
        LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        WHERE c.relnamespace='qa'::regnamespace AND c.relkind='r'
          AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum
    """)
    expected_columns = [
        (table, *col, True) for table in sorted(tables) for col in COLUMNS[table]
    ]
    if list(map(tuple, columns)) != expected_columns:
        raise RuntimeError("unexpected PostgreSQL table columns")
    constraints = connection.exec_driver_sql("""
        SELECT c.relname, k.conname, pg_get_constraintdef(k.oid), k.convalidated
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
        WHERE c.relnamespace='qa'::regnamespace AND k.contype IN ('p','f','u','c','x')
    """)
    expected_constraints = set()
    for table in tables:
        name, columns_sql = PRIMARY_KEYS[table]
        expected_constraints.add((table, name, f"PRIMARY KEY ({columns_sql})", True))
        expected_constraints.update(
            (table, name, sql, True) for name, sql in OTHER_CONSTRAINTS.get(table, ())
        )
    if set(map(tuple, constraints)) != expected_constraints:
        raise RuntimeError("unexpected PostgreSQL table constraints")
    inheritance = connection.exec_driver_sql("""
        SELECT EXISTS (
            SELECT FROM pg_inherits i
            JOIN pg_class parent ON parent.oid=i.inhparent
            JOIN pg_class child ON child.oid=i.inhrelid
            WHERE 'qa'::regnamespace IN (parent.relnamespace, child.relnamespace)
        )
    """).scalar_one()
    if inheritance:
        raise RuntimeError("unexpected PostgreSQL table inheritance")
    extra_behavior = connection.exec_driver_sql("""
        SELECT EXISTS (
            SELECT FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            WHERE c.relnamespace='qa'::regnamespace
              AND (NOT t.tgisinternal OR t.tgenabled <> 'O')
            UNION ALL
            SELECT FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class
            WHERE c.relnamespace='qa'::regnamespace
            UNION ALL
            SELECT FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
            WHERE c.relnamespace='qa'::regnamespace
            UNION ALL
            SELECT FROM pg_proc p WHERE p.pronamespace='qa'::regnamespace
        )
    """).scalar_one()
    if extra_behavior:
        raise RuntimeError(
            "unexpected PostgreSQL triggers, rules, policies or functions"
        )


def _check_source(connection: Connection, head: str) -> None:
    if (
        connection.exec_driver_sql("SHOW session_replication_role").scalar_one()
        != "origin"
    ):
        raise RuntimeError("unexpected PostgreSQL session replication role")
    if connection.exec_driver_sql(ROLE_BASELINE).scalar_one() is not True:
        raise RuntimeError("unexpected PostgreSQL role or schema configuration")
    source = current_revisions(connection)
    if head != FIXTURES_REVISION or source not in REVISION_TABLES:
        raise RuntimeError("unsupported source revision")
    _validate_catalog(connection, REVISION_TABLES[source])
    _validate_grants(connection, REVISION_TABLES[source])


def _execute_grants(connection: Connection) -> None:
    for table, grants in API_GRANTS.items():
        if grants:
            connection.exec_driver_sql(
                f"GRANT {', '.join(grants)} ON qa.{table} TO qa_api"
            )


def _validate_grants(connection: Connection, tables: tuple[str, ...]) -> None:
    unexpected_grants = connection.exec_driver_sql("""
        SELECT EXISTS (
            SELECT FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) acl
            WHERE c.relnamespace='qa'::regnamespace AND c.relkind='r'
              AND acl.grantee NOT IN ('qa_api'::regrole, 'qa_migrate'::regrole)
            UNION ALL
            SELECT FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
            CROSS JOIN LATERAL aclexplode(a.attacl) acl
            WHERE c.relnamespace='qa'::regnamespace AND c.relkind='r' AND a.attnum>0
        )
    """).scalar_one()
    if unexpected_grants:
        raise RuntimeError("unexpected PostgreSQL ACL entries")
    for table in tables:
        rights = connection.exec_driver_sql(
            """
            SELECT role_name, privilege,
                   has_table_privilege(role_name, %(table)s, privilege),
                   has_table_privilege(role_name, %(table)s, privilege || ' WITH GRANT OPTION'),
                   CASE WHEN privilege = ANY(%(column)s)
                        THEN has_any_column_privilege(role_name, %(table)s, privilege) END,
                   CASE WHEN privilege = ANY(%(column)s)
                        THEN has_any_column_privilege(role_name, %(table)s, privilege || ' WITH GRANT OPTION') END
            FROM unnest(ARRAY['qa_api','qa_migrate']) roles(role_name)
            CROSS JOIN unnest(%(privileges)s::text[]) privileges(privilege)
        """,
            {
                "table": f"qa.{table}",
                "column": sorted(COLUMN_PRIVILEGES),
                "privileges": list(TABLE_PRIVILEGES),
            },
        )
        for (
            role,
            privilege,
            table_allowed,
            table_grant,
            column_allowed,
            column_grant,
        ) in rights:
            owner = role == "qa_migrate"
            allowed = owner or privilege in API_GRANTS[table]
            expected_columns = (
                (allowed, owner) if privilege in COLUMN_PRIVILEGES else (None, None)
            )
            if (table_allowed, table_grant) != (allowed, owner) or (
                column_allowed,
                column_grant,
            ) != expected_columns:
                raise RuntimeError("unexpected effective PostgreSQL privileges")


def upgrade() -> None:
    settings = Settings.from_env()
    if not settings.password:
        raise RuntimeError("migration credentials unavailable")
    config = migration_config()
    head = expected_revision()
    engine = create_engine(
        settings.url("qa_migrate"),
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={
            "connect_timeout": settings.connect_timeout,
            "options": "-c lock_timeout=3000 -c statement_timeout=15000",
            "application_name": "qa-migrate",
        },
    )
    try:
        with engine.begin() as connection:
            _check_source(connection, head)
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            _execute_grants(connection)
            if current_revisions(connection) != (head,):
                raise RuntimeError("unexpected target revision")
            _validate_catalog(connection, REVISION_TABLES[(head,)])
            _validate_grants(connection, REVISION_TABLES[(head,)])
    finally:
        engine.dispose()


def main() -> int:
    try:
        upgrade()
    except Exception as error:
        print(
            f"migration failed: PostgreSQL configuration, revision or transaction ({type(error).__name__})",
            file=sys.stderr,
        )
        return 1
    print("migration committed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

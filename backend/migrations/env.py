from alembic import context
from sqlalchemy import Connection

from app.database import SCHEMA

connection = context.config.attributes.get("connection")
if not isinstance(connection, Connection) or not connection.in_transaction():
    raise RuntimeError("migrations require the caller's PostgreSQL transaction")

context.configure(connection=connection, version_table_schema=SCHEMA)
with context.begin_transaction():
    context.run_migrations()

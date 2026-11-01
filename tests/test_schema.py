"""A clean Alembic deployment must contain every table/column used by the ORM."""

from sqlalchemy import inspect

from app.database import models  # noqa: F401
from app.database.base import Base, engine


async def test_migrations_cover_runtime_models():
    def missing_objects(connection):
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        missing = []
        for name, table in Base.metadata.tables.items():
            if name not in tables:
                missing.append(name)
                continue
            columns = {column["name"] for column in inspector.get_columns(name)}
            missing.extend(
                f"{name}.{column.name}" for column in table.columns if column.name not in columns
            )
        return missing

    async with engine.connect() as connection:
        assert await connection.run_sync(missing_objects) == []

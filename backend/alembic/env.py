from sqlalchemy import create_engine, pool

from alembic import context
from app.auth.models import Base
from app.db import database_url, get_settings


def include_object(obj, name, type_, reflected, compare_to):
    # Unrelated pre-existing tables belong to other features, never propose their removal.
    if type_ == "table" and reflected and name not in Base.metadata.tables:
        return False
    return True


if context.is_offline_mode():
    context.configure(
        url=database_url(get_settings()), target_metadata=Base.metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(
        database_url(get_settings()), poolclass=pool.NullPool, hide_parameters=True
    )
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=Base.metadata, include_object=include_object
        )
        with context.begin_transaction():
            context.run_migrations()

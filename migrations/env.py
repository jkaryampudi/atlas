import os
from alembic import context
from sqlalchemy import create_engine

url = os.environ.get("ATLAS_DATABASE_URL",
                     "postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas")

def run_migrations_online() -> None:
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()

run_migrations_online()

# Thin derived image on top of the published apache/superset image.
#
# Why this file exists: the published "lean" image ships NO PostgreSQL driver
# at all — no psycopg2, no pg8000, no psycopg (verified by importing all three
# inside the image) — and it has no C compiler, so `pip install psycopg2`
# cannot build one.  Two consequences:
#
#   * Superset's own metadata database cannot be Postgres, so it silently falls
#     back to SQLite inside the container (that is exactly what went wrong in
#     the previous deployment);
#   * `superset/db_engine_specs/postgres.py::get_datatype()` hard-imports
#     `psycopg2.extensions`, so ANY chart on a Postgres dataset fails with
#     "No module named 'psycopg2'" even though pg8000 could run the SQL.
#
# psycopg2-binary is a prebuilt manylinux wheel: no compiler needed.
# pg8000 is a pure-python fallback, kept so SQLAlchemy-based connections still
# work if the binary wheel is unavailable for a future base image.
#
# The virtualenv has no pip of its own; the image ships uv, which is what the
# official bootstrap scripts use as well.
ARG SUPERSET_BASE=6.1.0
FROM apache/superset:${SUPERSET_BASE}

USER root
RUN uv pip install --python /app/.venv/bin/python --no-cache \
        "psycopg2-binary==2.9.10" \
        "pg8000==1.31.5" \
    && /app/.venv/bin/python -c "import psycopg2, pg8000; print('drivers ok', psycopg2.__version__, pg8000.__version__)" \
    && /app/.venv/bin/python -c "from psycopg2.extensions import binary_types, string_types; print('datatype helpers ok')"
USER superset

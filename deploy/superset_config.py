# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Superset runtime configuration for the local Docker stack.

Everything that differs between machines is read from the environment
(see ``.env.example``).  Nothing secret is hardcoded here.
"""

import os

from celery.schedules import crontab

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]

# Metadata database (Superset's own storage: users, charts, dashboards...).
# A database of its own (superset_metadata), created by
# docker-entrypoint-initdb.d on the first start — not the database that holds
# the data being charted.
SQLALCHEMY_DATABASE_URI = os.environ.get(
    "SUPERSET_METADATA_DB_URI",
    "postgresql+psycopg2://superset:superset@db:5432/superset_metadata",
)
SQLALCHEMY_TRACK_MODIFICATIONS = False
# Recycle connections before Postgres/pgbouncer closes them.
SQLALCHEMY_ENGINE_OPTIONS = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

# ---------------------------------------------------------------------------
# Cache / Celery / async queries (Redis)
# ---------------------------------------------------------------------------
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))

CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 300,
    "CACHE_KEY_PREFIX": "superset_",
    "CACHE_REDIS_HOST": REDIS_HOST,
    "CACHE_REDIS_PORT": REDIS_PORT,
    "CACHE_REDIS_DB": 1,
    "CACHE_REDIS_URL": f"redis://{REDIS_HOST}:{REDIS_PORT}/1",
}

DATA_CACHE_CONFIG = {
    **CACHE_CONFIG,
    "CACHE_KEY_PREFIX": "superset_data_",
    "CACHE_REDIS_DB": 2,
    "CACHE_REDIS_URL": f"redis://{REDIS_HOST}:{REDIS_PORT}/2",
    "CACHE_DEFAULT_TIMEOUT": 60 * 60 * 24,
}

FILTER_STATE_CACHE_CONFIG = {
    **CACHE_CONFIG,
    "CACHE_KEY_PREFIX": "superset_filter_",
    "CACHE_REDIS_DB": 3,
    "CACHE_REDIS_URL": f"redis://{REDIS_HOST}:{REDIS_PORT}/3",
}

EXPLORE_FORM_DATA_CACHE_CONFIG = {
    **CACHE_CONFIG,
    "CACHE_KEY_PREFIX": "superset_form_",
    "CACHE_REDIS_DB": 4,
    "CACHE_REDIS_URL": f"redis://{REDIS_HOST}:{REDIS_PORT}/4",
}

# Flask-Limiter uses this for rate limiting.  Without it Superset warns
# "Using the in-memory storage for tracking rate limits" and every gunicorn
# worker keeps its own counters, so limits are effectively multiplied.
RATELIMIT_STORAGE_URI = f"redis://{REDIS_HOST}:{REDIS_PORT}/5"

# Async SQL Lab / alerts & reports.  Superset 6 ships celery with the image.
class CeleryConfig:  # noqa: N801 - Superset expects this name
    broker_url = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
    result_backend = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
    worker_prefetch_multiplier = 1
    task_acks_late = True
    task_annotations = {"sql_lab.get_sql_results": {"rate_limit": "100/s"}}
    beat_schedule = {
        "reports.scheduler": {
            "task": "reports.scheduler",
            "schedule": crontab(minute="*", hour="*"),
        },
        "reports.prune_log": {
            "task": "reports.prune_log",
            "schedule": crontab(minute=0, hour=0),
        },
    }


CELERY_CONFIG = CeleryConfig
RESULTS_BACKEND = None
# Never expose the async endpoints without running the worker above.
FEATURE_FLAGS = {
    "ALERT_REPORTS": False,
    "ASYNC_QUERIES": False,
    "DASHBOARD_RBAC": False,
    "EMBEDDED_SUPERSET": False,
    "ESTIMATE_QUERY_COST": False,
    "HORIZONTAL_FILTER_BAR": True,
    "TAGGING_SYSTEM": True,
    "THUMBNAILS": False,
}

# ---------------------------------------------------------------------------
# Web server / security
# ---------------------------------------------------------------------------
# Fixes "Too many open files" on dashboards with many charts under gunicorn.
ENABLE_PROXY_FIX = True
PROXY_FIX_CONFIG = {"x_for": 1, "x_proto": 1, "x_host": 0, "x_port": 0, "x_prefix": 0}

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = os.environ.get("SUPERSET_COOKIE_SECURE", "false").lower() == "true"
TALISMAN_ENABLED = SESSION_COOKIE_SECURE
WTF_CSRF_ENABLED = True
WTF_CSRF_TIME_LIMIT = None

# Anonymous read-only dashboards.  "Public" selects the curated Public-role
# permission set (dashboards and charts only, no SQL Lab and no Security), so an
# unauthenticated visitor opens a dashboard link without a login form.  Which
# dashboards become visible is decided by the Public role's datasource grants:
# only published dashboards whose datasets the role may read show up.
PUBLIC_ROLE_LIKE = "Public"

# Uploaded CSV/Excel files land here (backed by the superset_home volume).
UPLOAD_FOLDER = os.environ.get("SUPERSET_UPLOAD_FOLDER", "/app/superset_home/uploads/")

# Turning this on removes the "recent activity" widget and the Forbidden
# banner it produces when the user cannot read recent activity.
DASHBOARD_RECENT_ACTIVITY = False

# Row limit safety net so a careless SELECT cannot take the whole box down.
DEFAULT_SQLLAB_LIMIT = 1000
SQL_MAX_ROW = 100000
SQLLAB_TIMEOUT = 300

# Localisation
BABEL_DEFAULT_LOCALE = os.environ.get("SUPERSET_DEFAULT_LOCALE", "en")
LANGUAGES = {
    "en": {"flag": "us", "name": "English"},
    "ru": {"flag": "ru", "name": "Russian"},
}

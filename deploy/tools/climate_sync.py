#!/usr/bin/env python3
"""Copy the "Климат дома" data from the climate service into Superset's data DB.

The Kimi dashboard reads four automations that proxy one HTTP service
(``CLIMATE_API_URL``, default ``http://144.124.255.55:8765``):

* ``/api/health``            - liveness, server clock
* ``/api/artifact/climate``  - current readings, 48 h series, 7-day aggregates
* ``/api/artifact/ai``       - DeepSeek decision, applied changes, decision log

The service keeps ~3900 readings in its own store but only exposes the last
48 hours, so this script is the history keeper: every run upserts the series
into Postgres, which makes the tables grow into the full history over time.
Superset cannot query SQLite/HTTP directly, hence the copy.

Idempotent: all writes are ``INSERT ... ON CONFLICT DO UPDATE`` keyed on the
reading timestamp, so re-running never duplicates rows.

Usage (from ``deploy/``)::

    make climate-sync                              # runs inside the container
    ./tools/climate_sync.py --dry-run              # fetch only, print a summary

Environment:

* ``CLIMATE_API_KEY``   - required, the service's ``X-Api-Key``
* ``CLIMATE_API_URL``   - optional, defaults to the value above
* ``SUPERSET_METADATA_DB_URI`` - reused for credentials; the database name is
  swapped to ``CLIMATE_PG_DATABASE`` (default ``superset``, the data database)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

SERVICE = os.environ.get("CLIMATE_API_URL", "http://144.124.255.55:8765")
DATA_DB = os.environ.get("CLIMATE_PG_DATABASE", "superset")

DDL = """
CREATE TABLE IF NOT EXISTS climate_readings (
    ts            timestamptz PRIMARY KEY,
    room_temp     numeric(5,2),
    room_humidity numeric(5,2),
    ac_on         boolean,
    ac_ambient    numeric(5,2),
    ac_target     numeric(5,2),
    outdoor_temp  numeric(5,2)
);

CREATE TABLE IF NOT EXISTS climate_daily (
    day           date PRIMARY KEY,
    room_temp_min numeric(5,2),
    room_temp_max numeric(5,2),
    room_temp_avg numeric(5,2),
    room_hum_avg  numeric(5,2),
    ac_on_hours   numeric(5,2),
    samples       integer
);

CREATE TABLE IF NOT EXISTS climate_ai_decisions (
    ts      timestamptz PRIMARY KEY,
    summary text,
    reason  text
);

CREATE TABLE IF NOT EXISTS climate_ai_state (
    ts              timestamptz PRIMARY KEY,
    room_temp       numeric(5,2),
    room_humidity   numeric(5,2),
    ac_on           boolean,
    ac_temp         numeric(5,2),
    ac_mode         text,
    ac_fan          text,
    ac_swing        text,
    humidifier_on   boolean,
    humidifier_fan  text,
    reason          text,
    guardrail_note  text,
    control_enabled boolean
);

CREATE TABLE IF NOT EXISTS climate_service_status (
    checked_at         timestamptz PRIMARY KEY,
    online             boolean,
    latency_ms         integer,
    server_ts          timestamptz,
    climate_updated_at timestamptz,
    total_rows         integer,
    error              text
);

CREATE TABLE IF NOT EXISTS climate_current (
    id                        smallint PRIMARY KEY DEFAULT 1,
    updated_at                timestamptz,
    room_temp                 numeric(5,2),
    room_humidity             numeric(5,2),
    ac_on                     boolean,
    ac_ambient                numeric(5,2),
    ac_target                 numeric(5,2),
    outdoor_temp              numeric(5,2),
    window_advice_action      text,
    window_advice_text        text,
    control_enabled           boolean,
    control_reason            text,
    ac_alert                  text,
    ai_ac_on                  boolean,
    ai_ac_temp                numeric(5,2),
    ai_ac_mode                text,
    ai_ac_fan                 text,
    ai_ac_swing               text,
    ai_humidifier_on          boolean,
    ai_humidifier_fan         text,
    ai_reason                 text,
    ai_guardrail_note         text,
    evening_light_window      text,
    evening_light_dim_pct     integer,
    evening_light_warm_k      integer,
    stats_room_temp_min24h    numeric(5,2),
    stats_room_temp_max24h    numeric(5,2),
    stats_room_temp_avg24h    numeric(5,2),
    stats_room_hum_avg24h     numeric(5,2),
    stats_ac_on_hours24h      numeric(5,2),
    synced_at                 timestamptz DEFAULT now()
);
"""


def fetch(path: str, api_key: str, timeout: int = 30) -> dict:
    request = urllib.request.Request(
        SERVICE + path, headers={"X-Api-Key": api_key}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def connection_params() -> dict:
    """Credentials from the Superset metadata URI, database name swapped."""
    uri = os.environ.get("SUPERSET_METADATA_DB_URI", "")
    if uri:
        parsed = urlparse(uri)
        return {
            "host": parsed.hostname or "db",
            "port": parsed.port or 5432,
            "user": parsed.username,
            "password": parsed.password,
            "dbname": DATA_DB,
        }
    return {
        "host": os.environ.get("PGHOST", "db"),
        "port": int(os.environ.get("PGPORT", 5432)),
        "user": os.environ.get("PGUSER", "superset"),
        "password": os.environ.get("PGPASSWORD", ""),
        "dbname": DATA_DB,
    }


def readings_rows(climate: dict) -> list[tuple]:
    current = climate.get("current") or {}
    newest = max((p.get("t") for p in climate.get("series") or []), default=None)
    rows = []
    for point in climate.get("series") or []:
        # outdoorTemp is only published for the newest sample
        outdoor = current.get("outdoorTemp") if point.get("t") == newest else None
        rows.append((
            point.get("t"),
            point.get("roomTemp"),
            point.get("roomHumidity"),
            point.get("acOn"),
            point.get("acAmbient"),
            point.get("acTarget"),
            outdoor,
        ))
    return rows


def upsert(cur, table: str, columns: list[str], rows: list[tuple], key: str) -> int:
    if not rows:
        return 0
    from psycopg2.extras import execute_values

    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c != key)
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES %s "
        f"ON CONFLICT ({key}) DO UPDATE SET {updates}"
        if updates
        else f"INSERT INTO {table} ({', '.join(columns)}) VALUES %s "
        f"ON CONFLICT ({key}) DO NOTHING"
    )
    execute_values(cur, sql, rows, page_size=500)
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and summarise without writing to the database")
    args = parser.parse_args()

    api_key = os.environ.get("CLIMATE_API_KEY")
    if not api_key:
        print("CLIMATE_API_KEY is not set", file=sys.stderr)
        return 2

    started = time.monotonic()
    status: dict = {"checked_at": None, "online": False, "latency_ms": None,
                    "server_ts": None, "error": None}
    try:
        health = fetch("/api/health", api_key)
        status.update(online=bool(health.get("ok")), server_ts=health.get("ts"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        status["error"] = str(exc)[:200]
        print(f"service unreachable: {exc}", file=sys.stderr)
    status["latency_ms"] = int((time.monotonic() - started) * 1000)

    climate = fetch("/api/artifact/climate", api_key)
    ai = fetch("/api/artifact/ai", api_key)
    status["checked_at"] = climate.get("updatedAt")
    status["climate_updated_at"] = climate.get("updatedAt")

    series = readings_rows(climate)
    daily = [
        (d.get("date"), d.get("roomTempMin"), d.get("roomTempMax"), d.get("roomTempAvg"),
         d.get("roomHumAvg"), d.get("acOnHours"), d.get("samples"))
        for d in climate.get("daily") or []
    ]
    decisions = [
        (d.get("t"), d.get("summary"), d.get("reason"))
        for d in (ai.get("recent") or [])
    ]
    decision = ai.get("decision") or {}
    readings = ai.get("readings") or {}
    ai_state = [(
        ai.get("updatedAt"), readings.get("roomTemp"), readings.get("roomHumidity"),
        decision.get("acOn"), decision.get("acTemp"), decision.get("acMode"),
        decision.get("acFan"), decision.get("acSwing"), decision.get("humidifierOn"),
        decision.get("humidifierFan"), decision.get("reason"), ai.get("guardrailNote"),
        (ai.get("control") or {}).get("enabled"),
    )]
    current = climate.get("current") or {}
    advice = climate.get("windowAdvice") or {}
    control = climate.get("control") or {}
    lights = climate.get("eveningLights") or {}
    stats = climate.get("stats") or {}
    current_row = [(
        1, climate.get("updatedAt"), current.get("roomTemp"), current.get("roomHumidity"),
        current.get("acOn"), current.get("acAmbient"), current.get("acTarget"),
        current.get("outdoorTemp"), advice.get("action"), advice.get("text"),
        control.get("enabled"), control.get("reason"),
        json.dumps(climate.get("acAlert"), ensure_ascii=False) if climate.get("acAlert") else None,
        decision.get("acOn"), decision.get("acTemp"), decision.get("acMode"),
        decision.get("acFan"), decision.get("acSwing"), decision.get("humidifierOn"),
        decision.get("humidityFan") or decision.get("humidifierFan"), decision.get("reason"),
        ai.get("guardrailNote"), lights.get("window"), lights.get("dimPct"),
        lights.get("warmK"), stats.get("roomTempMin24h"), stats.get("roomTempMax24h"),
        stats.get("roomTempAvg24h"), stats.get("roomHumAvg24h"), stats.get("acOnHours24h"),
    )]

    print(f"service  : {SERVICE} online={status['online']} latency={status['latency_ms']}ms")
    print(f"updated  : {climate.get('updatedAt')} (server total rows: {climate.get('totalRows')})")
    print(f"series   : {len(series)} points, {series[0][0] if series else '-'} .. "
          f"{series[-1][0] if series else '-'}")
    print(f"daily    : {len(daily)} days, ai decisions: {len(decisions)}")
    print(f"current  : {current.get('roomTemp')}°C / {current.get('roomHumidity')}% "
          f"ac_on={current.get('acOn')} target={current.get('acTarget')}")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    import psycopg2

    params = connection_params()
    print(f"\nwriting to Postgres {params['host']}:{params['port']}/{params['dbname']}")
    with psycopg2.connect(**params) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            written = {
                "climate_readings": upsert(
                    cur, "climate_readings",
                    ["ts", "room_temp", "room_humidity", "ac_on", "ac_ambient",
                     "ac_target", "outdoor_temp"], series, "ts"),
                "climate_daily": upsert(
                    cur, "climate_daily",
                    ["day", "room_temp_min", "room_temp_max", "room_temp_avg",
                     "room_hum_avg", "ac_on_hours", "samples"], daily, "day"),
                "climate_ai_decisions": upsert(
                    cur, "climate_ai_decisions", ["ts", "summary", "reason"],
                    decisions, "ts"),
                "climate_ai_state": upsert(
                    cur, "climate_ai_state",
                    ["ts", "room_temp", "room_humidity", "ac_on", "ac_temp", "ac_mode",
                     "ac_fan", "ac_swing", "humidifier_on", "humidifier_fan", "reason",
                     "guardrail_note", "control_enabled"], ai_state, "ts"),
                "climate_service_status": upsert(
                    cur, "climate_service_status",
                    ["checked_at", "online", "latency_ms", "server_ts",
                     "climate_updated_at", "total_rows", "error"],
                    [(status["checked_at"], status["online"], status["latency_ms"],
                      status["server_ts"], status["climate_updated_at"],
                      climate.get("totalRows"), status["error"])], "checked_at"),
                "climate_current": upsert(
                    cur, "climate_current",
                    ["id", "updated_at", "room_temp", "room_humidity", "ac_on",
                     "ac_ambient", "ac_target", "outdoor_temp", "window_advice_action",
                     "window_advice_text", "control_enabled", "control_reason", "ac_alert",
                     "ai_ac_on", "ai_ac_temp", "ai_ac_mode", "ai_ac_fan", "ai_ac_swing",
                     "ai_humidifier_on", "ai_humidifier_fan", "ai_reason",
                     "ai_guardrail_note", "evening_light_window", "evening_light_dim_pct",
                     "evening_light_warm_k", "stats_room_temp_min24h",
                     "stats_room_temp_max24h", "stats_room_temp_avg24h",
                     "stats_room_hum_avg24h", "stats_ac_on_hours24h"],
                    current_row, "id"),
            }
        conn.commit()
        with conn.cursor() as cur:
            for table in written:
                cur.execute(f"SELECT count(*) FROM {table}")
                print(f"  {table:<24} +{written[table]:>4} rows  (total {cur.fetchone()[0]})")
    print("\nsync complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())

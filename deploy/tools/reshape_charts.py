"""Rebuild chart params in the exact per-viz shape Superset's UI stores.

Why not a generic rewrite: Superset's explore form keeps the historical field
names (`groupby`, `granularity_sqla`, `metric`) even in 6.x — the worked example
charts in this very instance use them — and each viz type reads its own fields.
A generic "columns/metrics" rewrite produces charts the backend can execute but
the frontend cannot render.

This tool maps the legacy charts onto that per-viz contract:

  echarts_timeseries_line / bar -> x_axis + time_grain_sqla + groupby + metrics
  pie                           -> groupby + metric (a single object)
  table / big_number            -> queryFields + all_columns

Metrics become SIMPLE ad-hoc objects with the aggregate expression as label, and
a saved `count` metric stays a string, which is what the examples do and what
keeps the generated SQL valid.

Run after importing legacy charts:

    /app/.venv/bin/python /tmp/reshape_charts.py [--chart-id N ...] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os

os.environ.setdefault("SUPERSET_SECRET_KEY", "unused-for-this-path")

from superset.app import create_app  # noqa: E402

app = create_app()


def unique(items):
    seen, out = set(), []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def dimension_names(value) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    names = []
    for item in items:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            column = item.get("column") or item.get("col")
            if isinstance(column, dict):
                column = column.get("column_name")
            names.append(item.get("column_name") or column)
    return [n for n in names if isinstance(n, str)]


def as_adhoc_metric(entry, saved_metrics: dict[str, str]):
    """Return a metric the explore form accepts: a name string or an object."""
    if isinstance(entry, str):
        return entry  # saved metric name
    if not isinstance(entry, dict):
        return None
    if entry.get("sqlExpression"):
        return {
            "expressionType": "SQL",
            "sqlExpression": entry["sqlExpression"],
            "label": entry["sqlExpression"],
        }
    column = entry.get("column") or entry.get("col")
    column_name = column.get("column_name") if isinstance(column, dict) else column
    if not column_name:
        return None
    aggregate = entry.get("aggregate") or "SUM"
    return {
        "expressionType": "SIMPLE",
        "column": {"column_name": column_name},
        "aggregate": aggregate,
        "label": f"{aggregate}({column_name})",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chart-id", type=int, action="append")
    parser.add_argument("--all", action="store_true", help="every chart")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with app.app_context():
        from superset import db
        from superset.charts.schemas import ChartDataQueryContextSchema
        from superset.connectors.sqla.models import SqlaTable
        from superset.models.slice import Slice

        charts = db.session.query(Slice)
        if args.chart_id and not args.all:
            charts = charts.filter(Slice.id.in_(args.chart_id))

        for chart in charts.all():
            datasource = (
                db.session.query(SqlaTable)
                .filter(SqlaTable.id == chart.datasource_id)
                .one_or_none()
            )
            if datasource is None:
                continue
            columns = {c.column_name for c in datasource.columns}
            temporal = [c.column_name for c in datasource.columns if c.is_dttm]
            saved_metrics = {m.metric_name: m.expression for m in datasource.metrics}
            old = chart.params_dict or {}
            # The slice column is authoritative: an earlier generic rewrite had
            # written "table" into params and that shadowed the real viz type.
            viz = chart.viz_type or old.get("viz_type") or "table"

            # ---- dimensions -------------------------------------------------
            dimensions = unique(
                dimension_names(old.get("groupby"))
                + dimension_names(old.get("series"))
                + dimension_names(old.get("columns"))
                + dimension_names(old.get("x_axis"))
                + dimension_names(old.get("entity"))
                + dimension_names(old.get("dimension"))
            )
            dimensions = [d for d in dimensions if d in columns]

            # ---- temporal axis ---------------------------------------------
            granularity = old.get("granularity") or old.get("granularity_sqla")
            if granularity not in columns:
                granularity = datasource.main_dttm_col or (temporal or [None])[0]
            grain = old.get("time_grain") or old.get("time_grain_sqla")

            # A time series names its temporal axis via x_axis and must not
            # repeat it inside groupby; a categorical bar chart uses x_axis for
            # the first dimension instead.
            x_axis = None
            groupby = list(dimensions)
            if viz in ("echarts_timeseries_line", "echarts_timeseries_bar", "line", "bar"):
                if granularity:
                    x_axis = granularity
                    groupby = [d for d in groupby if d != granularity]
                    grain = grain or "P1M"
                    if viz in ("bar", "echarts_timeseries_bar") and groupby:
                        x_axis = groupby.pop(0)
                        grain = None
            elif viz in ("pie", "pie_v2"):
                x_axis = None
            else:
                x_axis = None

            # ---- metrics ----------------------------------------------------
            raw = old.get("metrics") or old.get("metric") or old.get("y_axis") or []
            if not isinstance(raw, list):
                raw = [raw]
            metrics = []
            for entry in raw:
                metric = as_adhoc_metric(entry, saved_metrics)
                if metric:
                    metrics.append(metric)
            if not metrics and "count" in saved_metrics:
                metrics = ["count"]

            # ---- assemble ---------------------------------------------------
            form = {k: v for k, v in old.items()
                    if k not in ("y_axis", "metric", "series", "entity", "dimension")}
            form["viz_type"] = viz
            form["datasource"] = f"{chart.datasource_id}__table"
            form["annotation_layers"] = []
            form["adhoc_filters"] = [
                f for f in (old.get("adhoc_filters") or []) if isinstance(f, dict)
            ]
            form["row_limit"] = old.get("row_limit") or 1000
            form["time_range"] = old.get("time_range") or "No filter"
            if x_axis:
                form["x_axis"] = x_axis
            else:
                form.pop("x_axis", None)
            if grain:
                form["time_grain_sqla"] = grain
            else:
                form.pop("time_grain_sqla", None)
            if granularity:
                form["granularity_sqla"] = granularity

            if viz in ("pie", "pie_v2"):
                form.pop("groupby", None)
                form["groupby"] = groupby
                form["metric"] = metrics[0] if metrics else "count"
                form.pop("metrics", None)
            elif viz in ("table", "table_v2"):
                form["queryFields"] = {"groupby": "groupby", "metrics": "metrics"}
                form["groupby"] = groupby
                form.pop("metrics", None)
                form["all_columns"] = groupby
            else:
                form["groupby"] = groupby
                form["metrics"] = metrics
                form.pop("metric", None)
                form["queryFields"] = {"groupby": "groupby", "metrics": "metrics"}

            # ---- query object for query_context ------------------------------
            query = {
                "row_limit": form["row_limit"],
                "time_range": form["time_range"],
            }
            if granularity:
                query["granularity"] = granularity
            if grain:
                query["time_grain"] = grain
            query["columns"] = groupby
            query["metrics"] = [
                m["label"] if isinstance(m, dict) else m for m in metrics
            ] or ["COUNT(*)"]
            if form["adhoc_filters"]:
                query["filters"] = form["adhoc_filters"]

            print(f"  + [{chart.id}] {chart.slice_name} ({viz})")
            print(f"      x_axis={x_axis} grain={grain} groupby={groupby}")
            print(f"      metrics={query['metrics']}")
            if args.dry_run:
                continue

            chart.params = json.dumps(form)
            try:
                context = ChartDataQueryContextSchema().load(
                    {
                        "datasource": {"id": chart.datasource_id, "type": "table"},
                        "queries": [query],
                        "form_data": form,
                        "result_type": "full",
                        "result_format": "json",
                        "force": False,
                    }
                )
                stored = ChartDataQueryContextSchema().dump(context)
                stored.pop("form_data", None)
                stored.pop("custom_cache_timeout", None)
                for item in stored.get("queries") or []:
                    item.pop("datasource", None)
                chart.query_context = json.dumps(stored)
            except Exception as exc:  # noqa: BLE001
                print(f"      query_context FAILED: {type(exc).__name__}: {exc}")
            db.session.add(chart)

        if args.dry_run:
            print("dry run: nothing written")
        else:
            db.session.commit()
            print("committed")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

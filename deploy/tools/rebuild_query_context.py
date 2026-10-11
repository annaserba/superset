"""Rebuild `query_context` for charts that do not have one.

Why this is needed
------------------
The five charts in this instance were saved through the legacy explore
endpoint: their `params` use the old field names (`granularity_sqla`,
`x_axis`, `y_axis`, `series`, `metric`) and `query_context` is NULL.  Superset
6 renders a chart from `query_context`, so those charts fail with

    Chart's query context does not exist

and their dashboard tiles stay empty.  The UI repairs this on the next save,
but nothing does it for imported/legacy charts.  This script converts the
legacy params into the modern query object, builds the query context through
Superset's own schema and stores it.

Idempotent: charts that already have a query_context are skipped unless
--force is passed.

Usage (inside the superset container):
    /app/.venv/bin/python /tmp/rebuild_query_context.py [--force] [--chart-id N]
"""

from __future__ import annotations

import argparse
import json
import os

os.environ.setdefault("SUPERSET_SECRET_KEY", "unused-for-this-path")

from superset.app import create_app  # noqa: E402

app = create_app()


def as_admin():
    """Run the rest of the function with an admin identity in Flask's globals.

    Building a query context goes through the security manager
    (`can_access_all_datasources`), which reads `flask.g.user`; a bare CLI run
    has no user and fails with `AttributeError: user`.  This is a maintenance
    script executed by the operator against their own instance, so acting as the
    admin is the intended privilege level.
    """
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        from flask import g

        from superset import db, security_manager

        with app.app_context():
            admin = db.session.query(security_manager.user_model).first()
            if admin is None:
                raise RuntimeError("no user in the metadata database")
            try:
                g.user = admin
            except Exception:  # noqa: BLE001 - g may be read-only in some setups
                pass
            yield

    return _ctx()


def metric_reference(metric, columns: dict, saved_metrics: set[str]) -> str:
    """Return a metric reference Superset accepts in a query object.

    The query-object schema wants either a saved-metric name or a string
    expression; an ad-hoc dict without `label` raises KeyError.

    Charts created through the UI often carry ad-hoc metrics that were never
    saved on the dataset, and an asset export does not preserve them.  A bare
    name that does not exist on the dataset is therefore converted back into an
    expression instead of being passed through (which would fail with
    "Metric 'X' does not exist").
    """
    if isinstance(metric, str):
        return metric if metric in saved_metrics else metric
    if isinstance(metric, dict):
        # A label is only useful when it names a metric that exists on the
        # dataset; ad-hoc labels like "Revenue" must become an expression.
        label = (
            metric.get("label")
            or metric.get("metric_name")
            or metric.get("verbose_name")
        )
        if label and label in saved_metrics:
            return str(label)
        column = metric.get("column") or metric.get("col")
        column_name = column.get("column_name") if isinstance(column, dict) else column
        aggregate = metric.get("aggregate") or "SUM"
        if column_name:
            return f"{aggregate}({column_name})"
        if metric.get("sqlExpression") or metric.get("sql_expression"):
            return str(metric.get("sqlExpression") or metric.get("sql_expression"))
        if label:
            return str(label)
    return str(metric)


def resolve_metrics(raw_metrics, columns: dict, saved_metrics: set[str]) -> list[str]:
    """Resolve chart metrics, preferring saved metric names over expressions.

    A saved metric named the same as the chart metric is the better reference:
    it keeps the chart tied to the dataset definition.  Everything else has to
    become an expression, otherwise the query fails with "Metric ... does not
    exist".
    """
    resolved = []
    for metric in raw_metrics:
        if isinstance(metric, str) and metric in saved_metrics:
            resolved.append(metric)
            continue
        reference = metric_reference(metric, columns, saved_metrics)
        if reference not in saved_metrics and isinstance(metric, dict):
            # The metric was ad-hoc and is gone from the dataset: rebuild it
            # from the aggregate + column the chart stored.
            column = metric.get("column") or metric.get("col")
            column_name = (
                column.get("column_name") if isinstance(column, dict) else column
            )
            aggregate = metric.get("aggregate") or "SUM"
            if column_name:
                reference = f"{aggregate}({column_name})"
        resolved.append(reference)
    return resolved


# The explore form stores filters in the legacy shape (subject/comparator/
# operator/clause); the query-object schema wants (col/op/val).
LEGACY_OPERATORS = {
    "==": "==", "=": "==", "!=": "!=", "<>": "!=", ">": ">", "<": "<",
    ">=": ">=", "<=": "<=", "IN": "IN", "NOT IN": "NOT IN",
    "IS NULL": "IS NULL", "IS NOT NULL": "IS NOT NULL",
    "IS TRUE": "IS TRUE", "IS FALSE": "IS FALSE",
    "LIKE": "LIKE", "NOT LIKE": "NOT LIKE", "ILIKE": "ILIKE", "NOT ILIKE": "NOT ILIKE",
    "TEMPORAL_RANGE": "TEMPORAL_RANGE", "TEMPORAL_RANGE_2020": "TEMPORAL_RANGE",
}


def modern_filter(item, columns: set[str]) -> dict | None:
    """Convert one stored adhoc filter into the modern filter object."""
    if not isinstance(item, dict):
        return None
    # already modern
    if "col" in item and "op" in item:
        return item
    subject = item.get("subject")
    operator = LEGACY_OPERATORS.get(str(item.get("operator") or "").upper()) \
        or LEGACY_OPERATORS.get(str(item.get("operator") or "")) \
        or "=="
    comparator = item.get("comparator")
    if subject is None:
        return None
    # a plain value filter that compares a temporal column against "No filter"
    if operator == "TEMPORAL_RANGE":
        comparator = comparator or "No filter"
    filt: dict = {"col": subject, "op": operator}
    if operator not in ("IS NULL", "IS NOT NULL", "IS TRUE", "IS FALSE"):
        if operator in ("IN", "NOT IN") and isinstance(comparator, str):
            filt["val"] = [part.strip() for part in comparator.split(",") if part.strip()]
        else:
            filt["val"] = comparator
    return filt


def temporal_columns(datasource) -> list[str]:
    temporal = []
    for column in datasource.columns:
        if getattr(column, "is_dttm", False):
            temporal.append(column.column_name)
    return temporal


def build_query(form: dict, datasource) -> dict:
    columns = {c.column_name for c in datasource.columns}
    saved_metrics = {m.metric_name for m in datasource.metrics}
    query: dict = {"row_limit": form.get("row_limit") or 1000}

    time_column = (
        form.get("granularity_sqla")
        or form.get("granularity")
        or (temporal_columns(datasource) or [None])[0]
    )
    if time_column and time_column in columns:
        query["granularity"] = time_column
    query["time_range"] = form.get("time_range") or "No filter"

    # Raw tables ("query_mode: raw") select columns verbatim and carry no
    # metrics.  Building the aggregate query below for them would silently turn
    # the table into one COUNT(*) column per group-by, so they get their own
    # query object.
    if form.get("query_mode") == "raw" or form.get("all_columns"):
        query["columns"] = [c for c in (form.get("all_columns") or []) if c in columns]
        query["metrics"] = []
        query["is_timeseries"] = False
        orderby = []
        for item in form.get("order_by_cols") or []:
            try:
                column_name, ascending = (
                    json.loads(item) if isinstance(item, str) else item
                )
            except (ValueError, TypeError):
                continue
            if column_name in columns:
                orderby.append([column_name, bool(ascending)])
        if orderby:
            query["orderby"] = orderby
        filters = [
            f
            for f in (
                modern_filter(item, columns)
                for item in form.get("adhoc_filters") or []
            )
            if f
        ]
        if filters:
            query["filters"] = filters
        return query

    # group-by dimension: new name first, then the legacy per-viz fields.
    # A bar/line chart with a *categorical* x_axis groups by that column (only a
    # temporal x_axis becomes the query granularity), so it counts as a dimension.
    x_axis = form.get("x_axis")
    categorical_x = (
        x_axis
        if isinstance(x_axis, str)
        and x_axis in columns
        and x_axis not in temporal_columns(datasource)
        else None
    )
    groupby = (
        form.get("groupby")
        or form.get("columns")
        or form.get("series")
        or form.get("entity")
        or form.get("dimension")
        or ([categorical_x] if categorical_x else None)
    )
    if isinstance(groupby, str):
        groupby = [groupby]
    query["columns"] = [c for c in (groupby or []) if c in columns]

    # metrics.  Gantt charts use `y_axis` as a categorical row dimension, so it
    # must not be turned into a metric there ("Metric 'status' does not exist").
    viz_type = form.get("viz_type") or ""
    if viz_type == "gantt_chart":
        raw_metrics = form.get("metrics") or form.get("metric") or []
        y_axis = form.get("y_axis")
        if isinstance(y_axis, str) and y_axis in columns and y_axis not in query["columns"]:
            query["columns"].append(y_axis)
    else:
        raw_metrics = (
            form.get("metrics")
            or form.get("metric")
            or form.get("y_axis")
            or form.get("size")
            or []
        )
    if not isinstance(raw_metrics, list):
        raw_metrics = [raw_metrics]
    metrics = resolve_metrics([m for m in raw_metrics if m], columns, saved_metrics)
    if not metrics:
        metrics = ["count" if "count" in saved_metrics else "COUNT(*)"]
    query["metrics"] = metrics

    if form.get("time_grain_sqla") or form.get("time_grain"):
        query["time_grain"] = form.get("time_grain_sqla") or form.get("time_grain")
    if form.get("order_desc") is not None:
        query["order_desc"] = bool(form.get("order_desc"))
    # `orderby` must be (column, ascending) tuples in the modern schema; a bare
    # column name from the legacy params is dropped rather than guessed.
    order_by = form.get("order_by")
    if isinstance(order_by, (list, tuple)) and order_by and isinstance(
        order_by[0], (list, tuple)
    ):
        query["orderby"] = [tuple(item) for item in order_by]
    raw_filters = form.get("adhoc_filters") or []
    filters = [f for f in (modern_filter(item, columns) for item in raw_filters) if f]
    if filters:
        query["filters"] = filters
    if form.get("is_timeseries") is not None:
        query["is_timeseries"] = bool(form["is_timeseries"])
    return query


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--chart-id", type=int, action="append")
    args = parser.parse_args()

    with as_admin():
        from superset import db
        from superset.charts.schemas import ChartDataQueryContextSchema
        from superset.connectors.sqla.models import SqlaTable
        from superset.models.slice import Slice

        charts = db.session.query(Slice)
        if args.chart_id:
            charts = charts.filter(Slice.id.in_(args.chart_id))
        elif not args.force:
            charts = charts.filter(Slice.query_context.is_(None))

        fixed, failed, skipped = [], [], []
        for chart in charts.all():
            datasource = (
                db.session.query(SqlaTable)
                .filter(SqlaTable.id == chart.datasource_id)
                .one_or_none()
            )
            if datasource is None:
                skipped.append((chart.id, chart.slice_name, "no datasource"))
                continue
            form = chart.params_dict or {}
            query = build_query(form, datasource)
            payload = {
                "datasource": {"id": chart.datasource_id, "type": "table"},
                "queries": [query],
                "form_data": form,
                "result_type": "full",
                "result_format": "json",
                "force": False,
            }
            try:
                context = ChartDataQueryContextSchema().load(payload)
                stored = ChartDataQueryContextSchema().dump(context)
                stored.pop("form_data", None)
                stored.pop("custom_cache_timeout", None)
                # QueryObjectFactory.create() receives the datasource both as an
                # explicit argument and inside each query object, so a stored
                # per-query `datasource` breaks every later read of the context
                # ("got multiple values for keyword argument 'datasource'").
                for query in stored.get("queries") or []:
                    query.pop("datasource", None)
                chart.query_context = json.dumps(stored)
                db.session.add(chart)
                fixed.append((chart.id, chart.slice_name, json.dumps(query)))
            except Exception as exc:  # noqa: BLE001
                failed.append((chart.id, chart.slice_name, f"{type(exc).__name__}: {exc}"))
        db.session.commit()

        print(f"updated {len(fixed)} chart(s)")
        for chart_id, name, query in fixed:
            print(f"  + [{chart_id}] {name}\n      {query}")
        if skipped:
            print(f"skipped {len(skipped)}")
            for item in skipped:
                print("  ?", item)
        if failed:
            print(f"failed {len(failed)}")
            for item in failed:
                print("  -", item)
        return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Modernise chart parameters saved by the legacy explore endpoint.

The five charts in this instance were saved with the pre-v1 field names
(`granularity_sqla`, `x_axis`, `y_axis`, `series`, `metric`) and a string
`adhoc_filters`.  Superset 6 still renders some of that, but the dashboard
metadata endpoint `GET /api/v1/dashboard/<id>/datasets` iterates the filters and
crashes on entries that are not objects:

    AttributeError: 'str' object has no attribute 'get'
    (superset/connectors/sqla/models.py, data_for_slices)

so the dashboard cannot load its datasets.  Rewriting the stored params into
the modern shape fixes the dashboard and keeps the chart config in the format
the current frontend expects.

Also rewrites `query_context` for the affected charts, because the query object
is derived from these fields.

Usage (inside the superset container):
    /app/.venv/bin/python /tmp/modernize_charts.py [--chart-id N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os

os.environ.setdefault("SUPERSET_SECRET_KEY", "unused-for-this-path")

from superset.app import create_app  # noqa: E402

app = create_app()

# legacy field -> modern field, for group-by style dimensions
DIMENSION_FIELDS = ("series", "entity", "dimension", "x_axis")
METRIC_FIELDS = ("metric", "y_axis", "size", "secondary_metric")


def normalize_adhoc_filters(value):
    """Legacy charts stored filters as a stringified list of objects."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def as_metric(metric):
    """Convert a metric entry into an ad-hoc metric object.

    Handles both historic spellings seen in this instance:

        {"column": "revenue", "aggregate": "SUM", "label": "Revenue"}   # v0/v1
        {"column": {"column_name": "revenue"}, "expressionType": "SIMPLE", ...}
        "SUM(revenue)"                                                 # saved metric
    """
    if not isinstance(metric, dict):
        return metric
    column = metric.get("column") or metric.get("col")
    column_name = (
        column.get("column_name") if isinstance(column, dict) else column
    )
    aggregate = metric.get("aggregate") or "SUM"
    if not column_name:
        # SQL metric: keep the expression, but normalise the expressionType.
        if metric.get("sqlExpression"):
            return {
                "expressionType": "SQL",
                "sqlExpression": metric["sqlExpression"],
                "label": metric.get("label") or metric.get("name") or metric["sqlExpression"],
            }
        return metric
    return {
        "expressionType": "SIMPLE",
        "column": {"column_name": column_name},
        "aggregate": aggregate,
        "label": f"{aggregate}({column_name})",
    }


def modernize(params: dict) -> tuple[dict, list[str]]:
    form = dict(params)
    changes = []

    if "adhoc_filters" in form and not isinstance(form["adhoc_filters"], list):
        form["adhoc_filters"] = normalize_adhoc_filters(form["adhoc_filters"])
        changes.append("adhoc_filters -> list")

    metrics = []
    metric_source = None
    for field in ("metrics",) + METRIC_FIELDS:
        raw = form.get(field)
        if raw:
            metric_source = field
            metrics = [as_metric(m) for m in (raw if isinstance(raw, list) else [raw])]
            break
    if metrics:
        if form.get("metrics") != metrics:
            changes.append(f"{metric_source} -> normalised metrics")
        form["metrics"] = metrics

    def dimension_name(dimension):
        if isinstance(dimension, dict):
            return (
                dimension.get("column_name")
                or (dimension.get("column") or {}).get("column_name")
                if isinstance(dimension.get("column"), dict)
                else dimension.get("column")
            )
        return dimension

    if not form.get("columns"):
        for field in DIMENSION_FIELDS:
            raw = form.get(field)
            if raw:
                items = raw if isinstance(raw, list) else [raw]
                names = [dimension_name(item) for item in items]
                form["columns"] = [name for name in names if isinstance(name, str)]
                changes.append(f"{field} -> columns")
                break

    if not form.get("granularity") and form.get("granularity_sqla"):
        form["granularity"] = form["granularity_sqla"]
        changes.append("granularity_sqla -> granularity")

    if not form.get("time_grain") and form.get("time_grain_sqla"):
        form["time_grain"] = form["time_grain_sqla"]
        changes.append("time_grain_sqla -> time_grain")

    return form, changes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chart-id", type=int, action="append")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with app.app_context():
        from superset import db
        from superset.charts.schemas import ChartDataQueryContextSchema
        from superset.connectors.sqla.models import SqlaTable
        from superset.models.slice import Slice

        charts = db.session.query(Slice)
        if args.chart_id:
            charts = charts.filter(Slice.id.in_(args.chart_id))

        updated = 0
        for chart in charts.all():
            raw = chart.params or "{}"
            try:
                original = json.loads(raw)
            except json.JSONDecodeError:
                print(f"  ! [{chart.id}] params are not JSON, skipped")
                continue
            form, changes = modernize(original)
            if not changes:
                print(f"  = [{chart.id}] {chart.slice_name}: already modern")
                continue
            print(f"  + [{chart.id}] {chart.slice_name}: {', '.join(changes)}")
            if args.dry_run:
                continue
            chart.params = json.dumps(form)

            # query_context is derived from params, so rebuild it too.
            datasource = (
                db.session.query(SqlaTable)
                .filter(SqlaTable.id == chart.datasource_id)
                .one_or_none()
            )
            if datasource is None:
                continue
            saved_metrics = {m.metric_name for m in datasource.metrics}
            columns = {c.column_name for c in datasource.columns}
            temporal = [c.column_name for c in datasource.columns if c.is_dttm]
            query: dict = {"row_limit": form.get("row_limit") or 1000}
            granularity = form.get("granularity") or (temporal or [None])[0]
            if granularity in columns:
                query["granularity"] = granularity
            query["time_range"] = form.get("time_range") or "No filter"
            groupby = form.get("columns") or []
            query["columns"] = [
                c if isinstance(c, str)
                else (c.get("column_name") if isinstance(c, dict) else None)
                for c in groupby
            ]
            query["columns"] = [c for c in query["columns"] if c in columns]
            metrics = []
            for metric in form.get("metrics") or []:
                if isinstance(metric, str):
                    metrics.append(metric)
                elif isinstance(metric, dict):
                    label = metric.get("label")
                    if label and label in saved_metrics:
                        metrics.append(label)
                    else:
                        column = metric.get("column") or {}
                        column_name = (
                            column.get("column_name")
                            if isinstance(column, dict)
                            else column
                        )
                        aggregate = metric.get("aggregate") or "SUM"
                        metrics.append(
                            f"{aggregate}({column_name})" if column_name else "COUNT(*)"
                        )
            query["metrics"] = metrics or ["COUNT(*)"]
            if form.get("time_grain"):
                query["time_grain"] = form["time_grain"]
            if form.get("adhoc_filters"):
                query["filters"] = form["adhoc_filters"]
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
                # Keep the same keys Superset itself stores; `form_data` inside
                # the context makes QueryContextFactory.create() fail later with
                # "multiple values for keyword argument 'datasource'".
                stored.pop("form_data", None)
                stored.pop("custom_cache_timeout", None)
                # QueryObjectFactory.create() receives the datasource both as an
                # explicit argument and inside each query object, so a stored
                # per-query `datasource` breaks every later read of the context
                # ("got multiple values for keyword argument 'datasource'").
                for query in stored.get("queries") or []:
                    query.pop("datasource", None)
                chart.query_context = json.dumps(stored)
            except Exception as exc:  # noqa: BLE001
                print(f"      query_context not rebuilt: {type(exc).__name__}: {exc}")
            db.session.add(chart)
            updated += 1

        if args.dry_run:
            print("dry run: no changes written")
        else:
            db.session.commit()
            print(f"updated {updated} chart(s)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Rewrite the migrated charts' params into a valid v1 form_data.

The five charts came from the legacy explore endpoint and their stored params
carry defects that only surface when the charts are actually rendered:

  * `granularity_sqla` holds a time grain ("P1M") instead of a temporal column,
    so the generated SQL contains `"P1M"` and Postgres answers
    `column "P1M" does not exist`;
  * the dimensions are duplicated across `groupby` and `x_axis`, and Superset
    rejects the query with `Duplicate column/metric labels: "product"`;
  * metrics are stored without a unique label, and mixing a saved metric name
    with an expression of the same name breaks the query.

This tool builds one canonical v1 form_data per chart from the dataset metadata
plus the chart's own intent, stores it, and rebuilds the matching
`query_context`.  Run it after importing legacy charts.

Usage (inside the superset container):
    /app/.venv/bin/python /tmp/rewrite_chart_params.py [--chart-id N ...] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os

os.environ.setdefault("SUPERSET_SECRET_KEY", "unused-for-this-path")

from superset.app import create_app  # noqa: E402

app = create_app()

# viz type -> (temporal x-axis?, aggregate default)
AGGREGATE_MAP = {
    "SUM": "SUM",
    "AVG": "AVG",
    "COUNT": "COUNT",
    "COUNT_DISTINCT": "COUNT_DISTINCT",
    "MIN": "MIN",
    "MAX": "MAX",
}


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
            names.append(item.get("column_name") or column or item.get("label"))
    return [name for name in names if isinstance(name, str)]


def build_metric(entry, saved_metrics: dict[str, str]) -> dict | None:
    """Return a canonical ad-hoc metric with a unique label."""
    if isinstance(entry, str):
        expression = saved_metrics.get(entry, entry)
        return {"expressionType": "SQL", "sqlExpression": expression, "label": expression}
    if not isinstance(entry, dict):
        return None

    label = entry.get("label") or entry.get("name")
    sql_expression = entry.get("sqlExpression")
    column = entry.get("column") or entry.get("col")
    column_name = column.get("column_name") if isinstance(column, dict) else column
    aggregate = entry.get("aggregate") or "SUM"
    if sql_expression:
        return {
            "expressionType": "SQL",
            "sqlExpression": sql_expression,
            "label": sql_expression,
        }
    if column_name:
        return {
            "expressionType": "SIMPLE",
            "column": {"column_name": column_name},
            "aggregate": AGGREGATE_MAP.get(aggregate, aggregate),
            "label": f"{aggregate}({column_name})",
        }
    return None


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

        for chart in charts.all():
            datasource = (
                db.session.query(SqlaTable)
                .filter(SqlaTable.id == chart.datasource_id)
                .one_or_none()
            )
            if datasource is None:
                print(f"  ! [{chart.id}] no datasource, skipped")
                continue
            column_names = {c.column_name for c in datasource.columns}
            temporal = [c.column_name for c in datasource.columns if c.is_dttm]
            saved_metrics = {m.metric_name: m.expression for m in datasource.metrics}
            old = chart.params_dict or {}

            form = dict(old)
            # 1. dimensions: keep the order the chart used, drop duplicates.
            dimensions = unique(
                dimension_names(old.get("groupby"))
                + dimension_names(old.get("series"))
                + dimension_names(old.get("columns"))
                + dimension_names(old.get("x_axis"))
                + dimension_names(old.get("entity"))
                + dimension_names(old.get("dimension"))
            )
            granularity_candidate = old.get("granularity") or old.get("granularity_sqla")
            if granularity_candidate not in column_names:
                granularity_candidate = datasource.main_dttm_col or None
            # The temporal axis is expressed through `granularity`; keeping it in
            # `columns` would add a second, redundant group-by.
            dimensions = [d for d in dimensions if d in column_names]
            if granularity_candidate in dimensions and len(dimensions) > 1:
                dimensions = [d for d in dimensions if d != granularity_candidate]
            form["columns"] = dimensions

            # 2. metrics: canonical ad-hoc objects with unique labels.
            raw_metrics = old.get("metrics") or old.get("metric") or old.get("y_axis") or []
            if not isinstance(raw_metrics, list):
                raw_metrics = [raw_metrics]
            metrics, labels = [], set()
            for entry in raw_metrics:
                metric = build_metric(entry, saved_metrics)
                if not metric:
                    continue
                while metric["label"] in labels:
                    metric["label"] = f"{metric['label']} "
                labels.add(metric["label"])
                metrics.append(metric)
            form["metrics"] = metrics

            # 3. temporal axis: a granularity must be a column, never a grain.
            granularity = old.get("granularity") or old.get("granularity_sqla")
            if granularity not in column_names:
                granularity = datasource.main_dttm_col or (temporal or [None])[0]
            if granularity:
                form["granularity"] = granularity
                form["granularity_sqla"] = granularity
            grain = old.get("time_grain") or old.get("time_grain_sqla")
            if grain:
                form["time_grain"] = grain
                form["time_grain_sqla"] = grain

            # Drop the legacy field names: the frontend builds its query from
            # this object too, and leaving `groupby` next to `x_axis` produces
            # "Duplicate column/metric labels".
            for legacy in (
                "groupby", "series", "y_axis", "metric", "size", "secondary_metric",
                "granularity_sqla", "time_grain_sqla", "entity", "dimension",
            ):
                form.pop(legacy, None)
            if granularity:
                form["x_axis"] = granularity
            elif old.get("x_axis") in column_names:
                form["x_axis"] = old["x_axis"]

            form["adhoc_filters"] = [
                f for f in (old.get("adhoc_filters") or []) if isinstance(f, dict)
            ]
            form["row_limit"] = old.get("row_limit") or 1000
            form["time_range"] = old.get("time_range") or "No filter"
            form["viz_type"] = old.get("viz_type")

            # 4. query object, built from the cleaned form
            query = {
                "row_limit": form["row_limit"],
                "time_range": form["time_range"],
                "columns": dimensions,
                "metrics": [m["label"] for m in metrics],
            }
            if granularity:
                query["granularity"] = granularity
            if grain:
                query["time_grain"] = grain
            if form["adhoc_filters"]:
                query["filters"] = form["adhoc_filters"]

            print(f"  + [{chart.id}] {chart.slice_name}")
            print(f"      columns={dimensions} metrics={[m['label'] for m in metrics]}")
            print(f"      granularity={granularity} grain={grain}")
            if args.dry_run:
                continue

            chart.params = json.dumps(form)
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
                # Keep the key set Superset itself stores; form_data and the
                # per-query datasource break later reads of the context.
                stored.pop("form_data", None)
                stored.pop("custom_cache_timeout", None)
                for item in stored.get("queries") or []:
                    item.pop("datasource", None)
                chart.query_context = json.dumps(stored)
                print("      query_context rebuilt")
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

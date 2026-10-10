"""Make every chart's stored query_context resolvable by the API.

Superset's chart API (`PUT /api/v1/chart/warm_up_cache`,
`GET /api/v1/chart/<id>/data/`) reads the *stored* `query_context` and looks up
each metric by name.  When a query object carries an ad-hoc metric as a plain
expression string — which is how a context serialised from form data looks —
that lookup fails:

    Error: Metric 'SUM(revenue)' does not exist

The UI does not hit this because it builds the query from `params`, so a chart
can render on a dashboard while the same chart fails through the API, exports,
alerts and "view query".  This tool rewrites those string entries into proper
ad-hoc metric objects (`expressionType: SIMPLE/SQL` + `label`), which the query
factory resolves without a name lookup.

Idempotent.  Charts that already use saved metric names or ad-hoc objects are
left alone.

Usage (inside the superset container):
    /app/.venv/bin/python /tmp/fix_api_query_contexts.py [--chart-id N ...] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os

os.environ.setdefault("SUPERSET_SECRET_KEY", "unused-for-this-path")

from superset.app import create_app  # noqa: E402

app = create_app()

AGGREGATES = ("SUM", "AVG", "COUNT", "COUNT_DISTINCT", "MIN", "MAX", "STDDEV", "VAR")


def parse_expression(expression: str) -> dict | None:
    """Turn 'SUM(revenue)' into a SIMPLE ad-hoc metric; None if not an aggregate."""
    if not isinstance(expression, str) or "(" not in expression:
        return None
    head, _, tail = expression.partition("(")
    aggregate = head.strip().upper()
    column = tail.rstrip(")").strip()
    if aggregate not in AGGREGATES or not column:
        return None
    return {
        "expressionType": "SIMPLE",
        "column": {"column_name": column},
        "aggregate": aggregate,
        "label": expression,
    }


def to_adhoc(metric, saved_metrics: dict[str, str], by_column=None) -> dict | None:
    """Return an ad-hoc metric object for a stored metric entry, or None."""
    if isinstance(metric, dict):
        return None  # already an object
    if not isinstance(metric, str):
        return None
    if metric in saved_metrics:
        return None  # a real saved metric: keep the name reference
    parsed = parse_expression(metric)
    if parsed:
        # Prefer a saved metric that aggregates the same column: some legacy
        # charts reference metric names that were never saved on the dataset
        # (e.g. "status"), and the saved COUNT(<column>) is the intended metric.
        saved_name = (by_column or {}).get(parsed["column"]["column_name"])
        if saved_name:
            return {"expressionType": "SQL",
                    "sqlExpression": saved_metrics[saved_name],
                    "label": saved_metrics[saved_name]}
        return parsed
    if metric.upper().lstrip("(").startswith(("SELECT", "WITH")) or " " in metric:
        return {"expressionType": "SQL", "sqlExpression": metric, "label": metric}
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chart-id", type=int, action="append")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with app.app_context():
        from superset import db
        from superset.connectors.sqla.models import SqlaTable
        from superset.models.slice import Slice

        charts = db.session.query(Slice)
        if args.chart_id:
            charts = charts.filter(Slice.id.in_(args.chart_id))

        updated = skipped = failed = 0
        for chart in charts.all():
            raw = chart.query_context
            if not raw:
                skipped += 1
                continue
            datasource = (
                db.session.query(SqlaTable)
                .filter(SqlaTable.id == chart.datasource_id)
                .one_or_none()
            )
            if datasource:
                saved = {m.metric_name: m.expression for m in datasource.metrics}
                # column -> saved metric name, for metrics that were lost with
                # the chart's ad-hoc definition
                by_column = {}
                import re as _re

                for name, expression in saved.items():
                    match = _re.match(r"^\s*(\w+)\s*\(\s*(\w+)\s*\)", expression or "")
                    if match:
                        by_column.setdefault(match.group(2), name)
            else:
                saved, by_column = {}, {}
            try:
                context = json.loads(raw)
            except json.JSONDecodeError:
                failed += 1
                print(f"  ! [{chart.id}] query_context is not JSON")
                continue

            changed = False
            for query in context.get("queries") or []:
                metrics = query.get("metrics")
                if not isinstance(metrics, list):
                    continue
                rebuilt = []
                for metric in metrics:
                    adhoc = to_adhoc(metric, saved, by_column)
                    if adhoc:
                        changed = True
                    rebuilt.append(adhoc or metric)
                query["metrics"] = rebuilt

            if not changed:
                skipped += 1
                continue
            print(f"  + [{chart.id}] {chart.slice_name}")
            if args.dry_run:
                updated += 1
                continue
            chart.query_context = json.dumps(context)
            db.session.add(chart)
            updated += 1

        if args.dry_run:
            print(f"dry run: {updated} chart(s) would be rewritten, "
                  f"{skipped} already fine")
        else:
            db.session.commit()
            print(f"updated {updated}, already fine {skipped}, failed {failed}")
        return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

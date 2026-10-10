"""Final, explicit configuration of the five migrated charts.

The earlier generic rewrites lost information (metrics ended up as `count`,
one viz type got overwritten), so this tool states each chart's intent
explicitly — derived from the legacy params that shipped with the instance —
and writes both the explore form data and the matching query context.

Charts (dataset "Test Postgres"):
  104 Revenue by Month (by Region)    line: x=sale_date (P1M), series=region, SUM(revenue)
  105 Revenue by Product (by Channel) bar:  x=product, series=channel, SUM(revenue)
  106 Units Share by Region           pie:  region, SUM(units)
  107 Avg Salary by Role (by Region)  bar:  x=role, series=region, AVG(salary)
  108 Employees by Region             pie:  region, COUNT(id)

Usage (inside the superset container):
    /app/.venv/bin/python /tmp/configure_charts.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os

os.environ.setdefault("SUPERSET_SECRET_KEY", "unused-for-this-path")

from superset.app import create_app  # noqa: E402

app = create_app()

CHARTS = {
    104: {
        "viz_type": "echarts_timeseries_line",
        "dimension": "region",
        "metric": {"column": "revenue", "aggregate": "SUM"},
        "temporal": "sale_date",
        "time_grain": "P1M",
        "series": "region",
    },
    105: {
        "viz_type": "echarts_timeseries_bar",
        "x_axis": "product",
        "series": "channel",
        "metric": {"column": "revenue", "aggregate": "SUM"},
        "temporal": "sale_date",
    },
    106: {
        "viz_type": "pie",
        "dimension": "region",
        "metric": {"column": "units", "aggregate": "SUM"},
        "temporal": "sale_date",
    },
    107: {
        "viz_type": "echarts_timeseries_bar",
        "x_axis": "role",
        "series": "region",
        "metric": {"column": "salary", "aggregate": "AVG"},
        "temporal": "hired_date",
    },
    108: {
        "viz_type": "pie",
        "dimension": "region",
        "metric": {"column": "id", "aggregate": "COUNT"},
        "temporal": "hired_date",
    },
}


def adhoc(metric: dict) -> dict:
    return {
        "expressionType": "SIMPLE",
        "column": {"column_name": metric["column"]},
        "aggregate": metric["aggregate"],
        "label": f"{metric['aggregate']}({metric['column']})",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with app.app_context():
        from superset import db
        from superset.charts.schemas import ChartDataQueryContextSchema
        from superset.models.slice import Slice

        for chart_id, spec in CHARTS.items():
            chart = db.session.get(Slice, chart_id)
            if chart is None:
                print(f"  ! chart {chart_id} not found")
                continue

            metric = adhoc(spec["metric"])
            dims = [d for d in (spec.get("dimension"),) if d]
            groupby = []
            if spec.get("x_axis"):
                groupby.append(spec["x_axis"])
            if spec.get("series") and spec["series"] not in groupby:
                groupby.append(spec["series"])
            groupby += [d for d in dims if d not in groupby]

            form = {
                "viz_type": spec["viz_type"],
                "datasource": f"{chart.datasource_id}__table",
                "annotation_layers": [],
                "adhoc_filters": [],
                "row_limit": 1000,
                "time_range": "No filter",
                "granularity_sqla": spec["temporal"],
                "queryFields": {"groupby": "groupby", "metrics": "metrics"}
                if spec["viz_type"] != "pie"
                else {"groupby": "groupby", "metric": "metrics"},
                "color_scheme": "supersetColors",
                "show_legend": True,
            }
            query = {
                "row_limit": 1000,
                "time_range": "No filter",
                "granularity": spec["temporal"],
            }

            if spec["viz_type"] == "pie":
                form["groupby"] = groupby
                form["metric"] = metric
                form["labels_outside"] = True
                form["label_type"] = "key"
                query["columns"] = groupby
                query["metrics"] = [metric["label"]]
            else:
                if spec["viz_type"] == "echarts_timeseries_line":
                    form["x_axis"] = spec["temporal"]
                    form["time_grain_sqla"] = spec["time_grain"]
                    form["groupby"] = groupby
                    query["time_grain"] = spec["time_grain"]
                else:
                    form["x_axis"] = spec["x_axis"]
                    form["groupby"] = [d for d in groupby if d != spec["x_axis"]]
                form["metrics"] = [metric]
                form["x_axis_sort_asc"] = True
                form["y_axis_format"] = "SMART_NUMBER"
                query["columns"] = groupby
                query["metrics"] = [metric["label"]]

            print(f"  ~ [{chart_id}] {chart.slice_name} ({spec['viz_type']})")
            print(f"      groupby={form.get('groupby')} x_axis={form.get('x_axis')} "
                  f"metric={metric['label']} grain={form.get('time_grain_sqla')}")
            if args.dry_run:
                continue

            chart.viz_type = spec["viz_type"]
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
                print("      query_context OK")
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

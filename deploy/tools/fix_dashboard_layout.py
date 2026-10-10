"""Give the Sales Overview dashboard a valid, readable grid.

Two problems with the layout that the asset importer produced:

  * the chart nodes had no `meta.width` / `meta.height`, so the dashboard
    crashed with `TypeError: Cannot read properties of undefined (reading
    'width')`;
  * every chart ended up in one row, so the dashboard was ~2600 px wide and two
    of the five charts sat off-screen.

This writes the node shape Superset itself uses (see any example dashboard) and
lays the five charts out in two rows, three columns max.

Usage (inside the superset container, POSTGRES_PASSWORD in the environment):
    /app/.venv/bin/python /tmp/fix_dashboard_layout.py [--dashboard-id 10]
"""

from __future__ import annotations

import argparse
import json
import os

import psycopg2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dashboard-id", type=int, default=10)
    parser.add_argument("--title", default="Sales Overview")
    args = parser.parse_args()

    conn = psycopg2.connect(
        host="db",
        dbname="superset_metadata",
        user=os.environ.get("POSTGRES_USER", "superset"),
        password=os.environ["POSTGRES_PASSWORD"],
    )
    cur = conn.cursor()
    cur.execute(
        """
        select s.id, s.slice_name, s.uuid
        from slices s
        join dashboard_slices ds on ds.slice_id = s.id
        where ds.dashboard_id = %s
        order by s.id
        """,
        (args.dashboard_id,),
    )
    charts = cur.fetchall()
    cur.execute("select position_json from dashboards where id = %s", (args.dashboard_id,))
    position = json.loads(cur.fetchone()[0])

    for key in [k for k in position if k.startswith(("CHART-", "ROW-")) or k in
                ("c1", "c2", "c3", "c4", "c5")]:
        position.pop(key, None)

    per_row = 3
    rows = [charts[i:i + per_row] for i in range(0, len(charts), per_row)]
    grid_children = []
    for row_index, row in enumerate(rows, start=1):
        row_id = f"ROW-sales-{row_index}"
        width = max(1, 12 // len(row))
        position[row_id] = {
            "children": [],
            "id": row_id,
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
            "parents": ["ROOT_ID", "GRID_ID"],
            "type": "ROW",
        }
        for chart_id, title, uuid in row:
            node_id = f"CHART-{uuid}"
            position[node_id] = {
                "children": [],
                "id": node_id,
                "meta": {
                    "chartId": chart_id,
                    "height": 50,
                    "sliceName": title,
                    "uuid": str(uuid),
                    "width": width,
                },
                "parents": ["ROOT_ID", "GRID_ID", row_id],
                "type": "CHART",
            }
            position[row_id]["children"].append(node_id)
        grid_children.append(row_id)

    position["GRID_ID"] = {
        "children": grid_children,
        "id": "GRID_ID",
        "parents": ["ROOT_ID"],
        "type": "GRID",
    }
    position["ROOT_ID"] = {"children": ["GRID_ID"], "id": "ROOT_ID", "type": "ROOT"}
    position["HEADER_ID"] = {
        "id": "HEADER_ID",
        "meta": {"text": args.title},
        "type": "HEADER",
    }
    position["DASHBOARD_VERSION_KEY"] = "v2"

    cur.execute(
        "update dashboards set position_json = %s where id = %s",
        (json.dumps(position), args.dashboard_id),
    )
    conn.commit()
    print(
        f"dashboard {args.dashboard_id}: {len(charts)} chart(s) in {len(rows)} row(s), "
        f"{len(position)} nodes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

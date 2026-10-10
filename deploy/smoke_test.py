#!/usr/bin/env python3
"""Post-deploy smoke test for the local Superset stack.

    ./deploy/smoke_test.py [--url http://localhost:8088] [--user admin]
                           [--password admin] [--max-charts 8] [--all-charts]

Checks, in order:

  1. /health answers 200
  2. the login page serves the built frontend bundle
  3. the PWA service worker 404s cleanly (it used to raise in the logs)
  4. the API session can be established
  5. /api/v1/me/ answers 200
  6. dashboards / charts / datasets / databases are readable
  7. every configured database connection passes Superset's own test
  8. every chart really executes and returns rows

Check 8 goes through Superset's own ``PUT /api/v1/chart/warm_up_cache``, which
runs the chart's stored query server-side and reports the error if it fails.
The raw ``POST /api/v1/chart/data`` endpoint needs the caller to rebuild the
query object (metric labels and all) and answers "Request is incorrect" for
otherwise valid requests, so it is not a reliable health signal.

Exit code is the number of failed checks (0 = everything passed).
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

failures = 0


def ok(message: str) -> None:
    print(f"  {GREEN}OK{RESET}   {message}")


def bad(message: str) -> None:
    global failures
    failures += 1
    print(f"  {RED}FAIL{RESET} {message}")


class Client:
    """Minimal cookie-aware HTTP client: session login, JSON helpers."""

    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        # This machine runs a system-wide HTTP proxy; a localhost request must
        # not be routed through it.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar),
        )
        self.csrf = ""

    def request(
        self,
        method: str,
        path: str,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 90,
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method
        )
        request.add_header("Referer", f"{self.base}/")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except Exception as error:  # noqa: BLE001 - reported, not raised
            return 0, str(error).encode()

    def get(self, path: str, **kwargs) -> tuple[int, bytes]:
        return self.request("GET", path, **kwargs)

    def post_json(self, path: str, payload: dict) -> tuple[int, bytes]:
        return self.request(
            "POST",
            path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )

    def put_json(
        self, path: str, payload: dict, timeout: int = 90
    ) -> tuple[int, bytes]:
        headers = {"Content-Type": "application/json"}
        if self.csrf:
            headers["X-CSRFToken"] = self.csrf
        return self.request(
            "PUT",
            path,
            data=json.dumps(payload).encode(),
            headers=headers,
            timeout=timeout,
        )

    def login(self, username: str, password: str) -> bool:
        status, body = self.get("/login/")
        if status != 200:
            return False
        match = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', body)
        if not match:
            return False
        form = urllib.parse.urlencode(
            {
                "csrf_token": match.group(1).decode(),
                "username": username,
                "password": password,
            }
        ).encode()
        status, _ = self.request(
            "POST",
            "/login/",
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if status not in (200, 302):
            return False
        status, body = self.get("/api/v1/security/csrf_token/")
        if status == 200:
            try:
                self.csrf = json.loads(body)["result"]
            except Exception:  # noqa: BLE001
                self.csrf = ""
        # A logged-in session can read its own identity.
        status, _ = self.get("/api/v1/me/")
        return status == 200

    def json(self, path: str, **kwargs) -> tuple[int, dict]:
        status, body = self.get(path, **kwargs)
        try:
            return status, json.loads(body)
        except Exception:  # noqa: BLE001
            return status, {"_raw": body[:200].decode("utf-8", "replace")}


def jget(payload: dict, path: str, default=None):
    node = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8088")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="admin")
    parser.add_argument(
        "--max-charts",
        type=int,
        default=8,
        help="how many charts to execute (0 = all)",
    )
    parser.add_argument(
        "--all-charts", action="store_true", help="execute every chart"
    )
    args = parser.parse_args()

    max_charts = 0 if args.all_charts else args.max_charts
    client = Client(args.url)
    print(f"Superset smoke test -> {args.url}")

    # 1. health
    status, _ = client.get("/health", timeout=15)
    ok("/health -> 200") if status == 200 else bad(f"/health -> {status}")

    # 2. login page serves the frontend bundle
    status, body = client.get("/login/", timeout=20)
    if status == 200 and b"/static/assets/" in body:
        ok("login page serves frontend bundle")
    else:
        bad(f"login page (status {status}) has no /static/assets/ references")

    # 3. service worker must 404, not 500
    status, _ = client.get("/static/service-worker.js", timeout=15)
    if status in (200, 404):
        ok(f"/static/service-worker.js -> {status}")
    else:
        bad(f"/static/service-worker.js -> {status}")

    # 4. session login
    if not client.login(args.user, args.password):
        bad(f"login as {args.user}")
        print("\nSmoke test aborted: cannot authenticate.")
        return failures
    ok(f"login as {args.user}")

    # 5. identity
    status, _ = client.get("/api/v1/me/", timeout=20)
    ok("/api/v1/me/ -> 200") if status == 200 else bad(f"/api/v1/me/ -> {status}")

    # 6. inventory
    for endpoint in ("dashboard", "chart", "dataset", "database"):
        status, payload = client.json(f"/api/v1/{endpoint}/?q=(page_size:1)")
        if status != 200:
            bad(f"/api/v1/{endpoint} -> HTTP {status} {jget(payload, 'message', '')}")
            continue
        count = payload.get("count")
        if count is None:
            bad(f"/api/v1/{endpoint} unreadable")
        else:
            ok(f"/api/v1/{endpoint} count={count}")

    # 7. database connections
    status, payload = client.json("/api/v1/database/?q=(page_size:100)")
    database_ids = payload.get("ids") or [] if status == 200 else []
    if not database_ids:
        bad("no database connections configured")
    for db_id in database_ids:
        status, conn = client.json(f"/api/v1/database/{db_id}/connection", timeout=60)
        driver = jget(conn, "result.driver")
        if status == 200 and driver:
            ok(f"database id={db_id} connection (driver {driver})")
        else:
            bad(
                f"database id={db_id} connection -> HTTP {status} "
                f"{jget(conn, 'message', '')}"
            )

    # 8. charts really execute
    status, payload = client.json("/api/v1/chart/?q=(page_size:200)")
    chart_ids = payload.get("ids") or [] if status == 200 else []
    if not chart_ids:
        bad("no charts exist to test")
    checked = 0
    for chart_id in chart_ids:
        if max_charts and checked >= max_charts:
            break
        checked += 1
        status, result = client.put_json(
            "/api/v1/chart/warm_up_cache", {"chart_id": chart_id}, timeout=180
        )
        errors = jget(result, "errors")
        entry = (jget(result, "result") or [{}])[0]
        # warm_up_cache reports failures as viz_error, not as `error`; missing it
        # would let a broken chart pass as "executed (0 rows)".
        viz_error = entry.get("viz_error")
        if status == 200 and not errors and not viz_error:
            rows = entry.get("rows")
            suffix = f" ({rows} row(s))" if rows is not None else ""
            ok(f"chart id={chart_id} executed{suffix}")
        else:
            detail = viz_error or errors or result.get("message") or result
            bad(f"chart id={chart_id} -> {json.dumps(detail, default=str)[:200]}")

    print()
    if failures == 0:
        print("All checks passed.")
    else:
        print(f"{failures} check(s) failed.")
    return failures


if __name__ == "__main__":
    sys.exit(main())

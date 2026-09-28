import io
import json
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import aztree  # noqa: E402

SUBS = [
    {"id": "aaaa-1", "name": "acme-prod", "state": "Enabled"},
    {"id": "bbbb-2", "name": "acme-dev", "state": "Enabled"},
    {"id": "cccc-3", "name": "old-sandbox", "state": "Disabled"},
]


def quiet(fn, *args, **kwargs):
    with redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


class TokenTest(unittest.TestCase):
    def test_env_var_wins_over_cli(self):
        def run(*a, **k):
            raise AssertionError("az should not be called")

        self.assertEqual(aztree.get_token({"AZURE_ACCESS_TOKEN": "t0k"}, run=run), "t0k")

    def test_cli_token_for_arm(self):
        calls = []

        def run(cmd, **k):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="abc\n", stderr="")

        self.assertEqual(aztree.get_token({}, run=run, az_path="az"), "abc")
        self.assertIn("get-access-token", calls[0])
        self.assertIn("https://management.azure.com/", calls[0])

    def test_cli_failure_explains_login(self):
        def run(cmd, **k):
            return SimpleNamespace(returncode=1, stdout="", stderr="Please run 'az login' to setup account.")

        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            aztree.get_token({}, run=run, az_path="az")
        self.assertIn("az login", err.getvalue())


class PickSubscriptionsTest(unittest.TestCase):
    def test_default_is_current_cli_subscription(self):
        got = aztree.pick_subscriptions(SUBS, wanted=[], all_=False, current={"id": "bbbb-2", "name": "acme-dev"})
        self.assertEqual(got, [{"id": "bbbb-2", "name": "acme-dev"}])

    def test_by_name_is_case_insensitive_and_by_id(self):
        got = aztree.pick_subscriptions(SUBS, wanted=["ACME-PROD", "bbbb-2"], all_=False, current=None)
        self.assertEqual([s["id"] for s in got], ["aaaa-1", "bbbb-2"])

    def test_duplicates_are_dropped(self):
        got = aztree.pick_subscriptions(SUBS, wanted=["acme-prod", "aaaa-1"], all_=False, current=None)
        self.assertEqual(len(got), 1)

    def test_all_means_enabled_only(self):
        got = aztree.pick_subscriptions(SUBS, wanted=[], all_=True, current=None)
        self.assertEqual([s["name"] for s in got], ["acme-prod", "acme-dev"])

    def test_unknown_subscription_dies_listing_known_ones(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            aztree.pick_subscriptions(SUBS, wanted=["nope"], all_=False, current=None)
        self.assertIn("acme-prod", err.getvalue())

    def test_no_default_without_cli_dies(self):
        with self.assertRaises(SystemExit):
            quiet(aztree.pick_subscriptions, SUBS, wanted=[], all_=False, current=None)


class FakeSend:
    """Stands in for the HTTP layer: returns canned (status, headers, body) tuples and records requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, data, headers):
        self.calls.append({"method": method, "url": url, "body": json.loads(data) if data else None, "headers": headers})
        return self.responses.pop(0)


def page(columns, rows, next_link=None, headers=None):
    body = {"properties": {"columns": [{"name": c, "type": "String"} for c in columns], "rows": rows, "nextLink": next_link}}
    return 200, headers or {}, json.dumps(body).encode()


def error(status, code="Error", message="boom", headers=None):
    return status, headers or {}, json.dumps({"error": {"code": code, "message": message}}).encode()


def client(send, **kw):
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("log", lambda *a: None)
    return aztree.Azure("tok", send=send, **kw)


class QueryTest(unittest.TestCase):
    COLS = ["UsageDate", "ServiceName", "Cost", "Meter", "Currency", "CostUSD"]

    def run_query(self, send, **kw):
        return aztree.query(client(send), "/subscriptions/s1", "2026-07-01", "2026-08-29",
                            ["ServiceName", "Meter"], "ActualCost", **kw)

    def test_rows_are_mapped_by_column_name(self):
        send = FakeSend(page(self.COLS, [[20260701, "Storage", 1.5, "Hot LRS Data Stored", "USD", 1.5]]))
        rows = self.run_query(send)
        self.assertEqual(rows, [{"UsageDate": 20260701, "ServiceName": "Storage", "Cost": 1.5,
                                 "Meter": "Hot LRS Data Stored", "Currency": "USD", "CostUSD": 1.5}])

    def test_request_shape(self):
        send = FakeSend(page(self.COLS, []))
        self.run_query(send)
        call = send.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://management.azure.com/subscriptions/s1/providers/"
                                      "Microsoft.CostManagement/query?api-version=" + aztree.API_VERSION)
        self.assertEqual(call["headers"]["Authorization"], "Bearer tok")
        body = call["body"]
        self.assertEqual(body["type"], "ActualCost")
        self.assertEqual(body["timeframe"], "Custom")
        self.assertEqual(body["timePeriod"], {"from": "2026-07-01T00:00:00Z", "to": "2026-08-29T23:59:59Z"})
        self.assertEqual(body["dataset"]["granularity"], "Daily")
        self.assertEqual(body["dataset"]["grouping"], [{"type": "Dimension", "name": "ServiceName"},
                                                       {"type": "Dimension", "name": "Meter"}])
        self.assertEqual(sorted(a["name"] for a in body["dataset"]["aggregation"].values()), ["Cost", "CostUSD"])

    def test_follows_next_link_with_the_same_body(self):
        nxt = "https://management.azure.com/subscriptions/s1/providers/Microsoft.CostManagement/query?api-version=x&$skiptoken=abc"
        send = FakeSend(page(self.COLS, [[20260701, "A", 1, "m", "USD", 1]], next_link=nxt),
                        page(self.COLS, [[20260702, "B", 2, "m", "USD", 2]]))
        rows = self.run_query(send)
        self.assertEqual([r["ServiceName"] for r in rows], ["A", "B"])
        self.assertEqual(send.calls[1]["url"], nxt)
        self.assertEqual(send.calls[1]["method"], "POST")
        self.assertEqual(send.calls[1]["body"], send.calls[0]["body"])

    def test_too_many_pages_raises(self):
        send = FakeSend(*[page(self.COLS, [], next_link=f"https://management.azure.com/p{i}") for i in range(3)])
        with self.assertRaises(aztree.TooManyPages):
            self.run_query(send, max_pages=2)

    def test_throttle_waits_for_the_largest_retry_after_header(self):
        waits = []
        throttled = error(429, headers={
            "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after": "30",
            "x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after": "0",
            "x-ms-ratelimit-microsoft.costmanagement-QPU-retry-after": "12",
        })
        send = FakeSend(throttled, page(self.COLS, []))
        aztree.query(client(send, sleep=waits.append), "/subscriptions/s1", "2026-07-01", "2026-07-02",
                     ["ServiceName", "Meter"], "ActualCost")
        self.assertEqual(waits, [30])
        self.assertEqual(len(send.calls), 2)

    def test_throttle_without_header_still_backs_off(self):
        waits = []
        send = FakeSend(error(429), page(self.COLS, []))
        aztree.query(client(send, sleep=waits.append), "/subscriptions/s1", "2026-07-01", "2026-07-02",
                     ["ServiceName", "Meter"], "ActualCost")
        self.assertEqual(len(waits), 1)
        self.assertGreaterEqual(waits[0], 1)

    def test_gives_up_after_repeated_throttling(self):
        send = FakeSend(*[error(429) for _ in range(aztree.MAX_TRIES)])
        with self.assertRaises(aztree.AzureError) as ctx:
            self.run_query(send)
        self.assertEqual(ctx.exception.status, 429)

    def test_http_error_carries_status_and_message(self):
        send = FakeSend(error(403, "RBACAccessDenied", "The client does not have authorization"))
        with self.assertRaises(aztree.AzureError) as ctx:
            self.run_query(send)
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("does not have authorization", str(ctx.exception))

    def test_verbose_logs_qpu_consumed(self):
        lines = []
        send = FakeSend(page(self.COLS, [], headers={"x-ms-ratelimit-microsoft.costmanagement-qpu-consumed": "2"}))
        aztree.query(client(send, verbose=True, log=lines.append), "/subscriptions/s1", "2026-07-01", "2026-07-02",
                     ["ServiceName", "Meter"], "ActualCost")
        self.assertTrue(any("qpu" in line.lower() and "2" in line for line in lines))


class ListSubscriptionsTest(unittest.TestCase):
    def test_lists_every_page_from_arm(self):
        def subs_page(items, nxt=None):
            return 200, {}, json.dumps({"value": items, "nextLink": nxt}).encode()

        send = FakeSend(
            subs_page([{"subscriptionId": "aaaa-1", "displayName": "acme-prod", "state": "Enabled"}],
                      "https://management.azure.com/subscriptions?page=2"),
            subs_page([{"subscriptionId": "bbbb-2", "displayName": "acme-dev", "state": "Disabled"}]),
        )
        got = aztree.list_subscriptions(client(send))
        self.assertEqual(got, [{"id": "aaaa-1", "name": "acme-prod", "state": "Enabled"},
                               {"id": "bbbb-2", "name": "acme-dev", "state": "Disabled"}])
        self.assertTrue(send.calls[0]["url"].startswith("https://management.azure.com/subscriptions?api-version="))
        self.assertEqual(send.calls[0]["method"], "GET")


TODAY = aztree.dt.date(2026, 9, 28)  # with days=3 the window is Sep 22..27, split after Sep 24


class Router:
    """Fake Cost Management: answers each query from `tables[(dim1, dim2)][scope]`, a list of
    (usage_date, value1, value2, cost, currency, cost_usd) tuples."""

    def __init__(self, tables, explode=(), reject_usd=False):
        self.tables, self.explode, self.reject_usd = tables, explode, reject_usd
        self.bodies = []

    def __call__(self, method, url, data, headers):
        body = json.loads(data)
        self.bodies.append(body)
        dims = tuple(g["name"] for g in body["dataset"]["grouping"])
        aggs = [a["name"] for a in body["dataset"]["aggregation"].values()]
        if self.reject_usd and "CostUSD" in aggs:
            return error(400, "BadRequest", "Invalid aggregation CostUSD")
        scope = url.split("/providers/Microsoft.CostManagement")[0][len(aztree.ARM):]
        if dims in self.explode:
            return page(["Cost"], [], next_link=url.split("&")[0] + "&$skiptoken=more")
        cols = aggs + ["UsageDate", *dims, "Currency"]
        rows = []
        for date, a, b, cost, cur, usd in self.tables.get(dims, {}).get(scope, []):
            vals = {"Cost": cost, "CostUSD": usd if usd is not None else cost, "UsageDate": date,
                    dims[0]: a, dims[1]: b, "Currency": cur}
            rows.append([vals[c] for c in cols])
        return page(cols, rows)


RID = "/subscriptions/aaaa-1/resourcegroups/rg-app/providers/microsoft.web/sites/shop"
ONE_SUB = {
    ("ServiceName", "Meter"): {"/subscriptions/aaaa-1": [
        (20260922, "Storage", "Hot LRS Data Stored", 1.0, "USD", None),
        (20260927, "Storage", "Hot LRS Data Stored", 2.0, "USD", None),
        (20260925, "Azure App Service", "P1 v3 App", 5.0, "USD", None),
        (20260901, "Azure App Service", "P1 v3 App", 99.0, "USD", None),  # outside the window
    ]},
    ("ResourceGroupName", "ResourceId"): {"/subscriptions/aaaa-1": [
        (20260925, "rg-app", RID, 5.0, "USD", None),
        (20260927, "", "/subscriptions/aaaa-1/providers/microsoft.visualstudio/account/x", 2.0, "USD", None),
    ]},
    ("ResourceLocation", "ServiceName"): {"/subscriptions/aaaa-1": [
        (20260925, "us central", "Azure App Service", 5.0, "USD", None),
    ]},
}
PROD = {"id": "aaaa-1", "name": "acme-prod"}
DEV = {"id": "bbbb-2", "name": "acme-dev"}


def fetch(tables, targets=(PROD,), **kw):
    router = Router(tables, **{k: kw.pop(k) for k in ("explode", "reject_usd") if k in kw})
    data = aztree.fetch(client(router), [aztree.subscription_target(t) for t in targets], 3, "ActualCost",
                        advisor=False, log=lambda *a: None, today=TODAY)
    return data, router


def rows_of(data, view):
    return {tuple(r["k"]): r["d"] for r in data["views"][view]["rows"]}


class FetchTest(unittest.TestCase):
    def test_window_is_two_periods_ending_yesterday(self):
        data, router = fetch(ONE_SUB)
        self.assertEqual(data["days"], ["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-27"])
        self.assertEqual(data["split"], 3)
        self.assertEqual(router.bodies[0]["timePeriod"], {"from": "2026-09-22T00:00:00Z", "to": "2026-09-27T23:59:59Z"})

    def test_service_view_places_costs_on_their_day(self):
        data, _ = fetch(ONE_SUB)
        rows = rows_of(data, "service")
        self.assertEqual(rows[("Storage", "Hot LRS Data Stored")], [1.0, 0, 0, 0, 0, 2.0])
        self.assertEqual(rows[("Azure App Service", "P1 v3 App")], [0, 0, 0, 5.0, 0, 0])  # the Sep 1 row is dropped
        self.assertEqual(data["views"]["service"]["dims"], ["ServiceName", "Meter"])

    def test_subscription_view_comes_from_the_service_query(self):
        data, router = fetch(ONE_SUB)
        self.assertEqual(sorted(rows_of(data, "subscription")), [("aaaa-1", "Azure App Service"), ("aaaa-1", "Storage")])
        self.assertEqual(data["views"]["subscription"]["names"], {"aaaa-1": "acme-prod"})
        self.assertEqual(len(router.bodies), 3)  # service, resource and region queries only

    def test_region_view(self):
        data, _ = fetch(ONE_SUB)
        self.assertEqual(list(rows_of(data, "region")), [("us central", "Azure App Service")])

    def test_resource_view_keys_groups_by_their_arm_id(self):
        data, _ = fetch(ONE_SUB)
        view = data["views"]["resource"]
        rows = rows_of(data, "resource")
        self.assertIn(("/subscriptions/aaaa-1/resourcegroups/rg-app", RID), rows)
        self.assertEqual(view["names"]["/subscriptions/aaaa-1/resourcegroups/rg-app"], "rg-app")
        self.assertEqual(view["names"]["/subscriptions/aaaa-1"], "(no resource group)")

    def test_same_group_name_in_two_subscriptions_stays_apart(self):
        tables = {
            ("ServiceName", "Meter"): {},
            ("ResourceLocation", "ServiceName"): {},
            ("ResourceGroupName", "ResourceId"): {
                "/subscriptions/aaaa-1": [(20260925, "rg-app", RID, 1.0, "USD", None)],
                "/subscriptions/bbbb-2": [(20260925, "rg-app", RID.replace("aaaa-1", "bbbb-2"), 1.0, "USD", None)],
            },
        }
        data, _ = fetch(tables, targets=(PROD, DEV))
        names = data["views"]["resource"]["names"]
        self.assertEqual(names["/subscriptions/aaaa-1/resourcegroups/rg-app"], "rg-app · acme-prod")
        self.assertEqual(names["/subscriptions/bbbb-2/resourcegroups/rg-app"], "rg-app · acme-dev")

    def test_resource_view_falls_back_to_services_when_paging_explodes(self):
        tables = dict(ONE_SUB)
        tables[("ResourceGroupName", "ServiceName")] = {"/subscriptions/aaaa-1": [
            (20260925, "rg-app", "Azure App Service", 5.0, "USD", None)]}
        data, _ = fetch(tables, explode={("ResourceGroupName", "ResourceId")})
        self.assertEqual(list(rows_of(data, "resource")), [("/subscriptions/aaaa-1/resourcegroups/rg-app", "Azure App Service")])
        self.assertEqual(data["resource_fallback"], ["acme-prod"])

    def test_one_currency_is_drawn_in_that_currency(self):
        tables = {("ServiceName", "Meter"): {"/subscriptions/aaaa-1": [(20260925, "Storage", "LRS", 10.0, "EUR", 11.0)]}}
        data, _ = fetch(tables)
        self.assertEqual(data["currency"], "EUR")
        self.assertEqual(rows_of(data, "service")[("Storage", "LRS")][3], 10.0)

    def test_mixed_currencies_are_drawn_in_usd(self):
        tables = {("ServiceName", "Meter"): {
            "/subscriptions/aaaa-1": [(20260925, "Storage", "LRS", 10.0, "EUR", 11.0)],
            "/subscriptions/bbbb-2": [(20260925, "Storage", "LRS", 5.0, "USD", 5.0)],
        }}
        data, _ = fetch(tables, targets=(PROD, DEV))
        self.assertEqual(data["currency"], "USD")
        self.assertEqual(rows_of(data, "service")[("Storage", "LRS")][3], 16.0)
        self.assertEqual({s["name"]: s["currency"] for s in data["subscriptions"]}, {"acme-prod": "EUR", "acme-dev": "USD"})

    def test_rejected_costusd_is_dropped_once(self):
        data, router = fetch(ONE_SUB, reject_usd=True)
        aggs = [[a["name"] for a in b["dataset"]["aggregation"].values()] for b in router.bodies]
        self.assertEqual(aggs[0], ["Cost", "CostUSD"])
        self.assertTrue(all(a == ["Cost"] for a in aggs[1:]))
        self.assertIn(("Storage", "Hot LRS Data Stored"), rows_of(data, "service"))


class ExplainTest(unittest.TestCase):
    def test_403_mentions_cost_management_reader(self):
        self.assertIn("Cost Management Reader", aztree.explain(aztree.AzureError(403, "denied")))

    def test_401_mentions_login(self):
        self.assertIn("az login", aztree.explain(aztree.AzureError(401, "expired")))


if __name__ == "__main__":
    unittest.main()

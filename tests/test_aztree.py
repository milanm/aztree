import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import aztree  # noqa: E402

SUBS = [
    {"id": "aaaa-1", "name": "acme-prod", "state": "Enabled"},
    {"id": "bbbb-2", "name": "acme-dev", "state": "Enabled"},
    {"id": "cccc-3", "name": "old-sandbox", "state": "Disabled"},
]


def scratch_dir(test):
    """A temp folder removed when the test ends."""
    d = Path(tempfile.mkdtemp(prefix="aztree-test-"))
    test.addCleanup(shutil.rmtree, d, True)
    return d


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
        self.assertEqual(got, [{"id": "bbbb-2", "name": "acme-dev", "tenant": None}])

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


    def test_a_tag_grouping_goes_out_as_given(self):
        send = FakeSend(page(self.COLS, []))
        aztree.query(client(send), "/subscriptions/s1", "2026-07-01", "2026-08-29",
                     [{"type": "TagKey", "name": "env"}, "ServiceName"], "ActualCost")
        self.assertEqual(send.calls[0]["body"]["dataset"]["grouping"],
                         [{"type": "TagKey", "name": "env"}, {"type": "Dimension", "name": "ServiceName"}])


class ListSubscriptionsTest(unittest.TestCase):
    def test_lists_every_page_from_arm(self):
        def subs_page(items, nxt=None):
            return 200, {}, json.dumps({"value": items, "nextLink": nxt}).encode()

        send = FakeSend(
            subs_page([{"subscriptionId": "aaaa-1", "displayName": "acme-prod", "state": "Enabled", "tenantId": "t-one"}],
                      "https://management.azure.com/subscriptions?page=2"),
            subs_page([{"subscriptionId": "bbbb-2", "displayName": "acme-dev", "state": "Disabled"}]),
        )
        got = aztree.list_subscriptions(client(send))
        self.assertEqual(got, [{"id": "aaaa-1", "name": "acme-prod", "state": "Enabled", "tenant": "t-one"},
                               {"id": "bbbb-2", "name": "acme-dev", "state": "Disabled", "tenant": None}])
        self.assertTrue(send.calls[0]["url"].startswith("https://management.azure.com/subscriptions?api-version="))
        self.assertEqual(send.calls[0]["method"], "GET")


TODAY = aztree.dt.date(2026, 9, 29)  # with days=3 the window is Sep 22..27 (ends the day before yesterday)


class Router:
    """Fake ARM. Cost Management queries are answered from `tables[(dim1, dim2)][scope]`, a list of
    (usage_date, value1, value2, cost, currency, cost_usd) tuples; a tag grouping is keyed ("tag:KEY", dim2).
    Tag names, forecasts and Resource Graph answer from `tag_names`, `forecasts` and `findings`, empty by default."""

    def __init__(self, tables, explode=(), reject_usd=False, reject=(), reject_for=None, fail=None,
                 tag_names=None, forecasts=None, findings=None):
        self.tables, self.explode = tables, explode
        self.reject = set(reject) | ({"CostUSD"} if reject_usd else set())
        self.reject_for = reject_for or {}  # scope -> aggregations only that scope refuses
        self.fail = fail or {}  # (dim1, dim2) -> error message answered with HTTP 400
        self.tag_names = tag_names or {}  # subscription id -> [(tag name, resource count)], or an HTTP status
        self.forecasts = forecasts or {}  # scope -> [(date, "Actual" or "Forecast", cost, currency)], or an HTTP status
        self.findings = findings  # Resource Graph pages (lists of findings), or an HTTP status
        self.bodies, self.calls, self.other = [], [], []

    def __call__(self, method, url, data, headers):
        if "/tagNames" in url:
            return self.tag_names_page(url)
        if "/Microsoft.CostManagement/forecast" in url:
            return self.forecast_page(url, json.loads(data))
        if "/Microsoft.ResourceGraph/" in url:
            return self.graph_page(json.loads(data), headers)
        body = json.loads(data)
        self.bodies.append(body)
        dims = tuple(("tag:" if g["type"] == "TagKey" else "") + g["name"] for g in body["dataset"].get("grouping", []))
        aggs = [a["name"] for a in body["dataset"]["aggregation"].values()]
        scope = url.split("/providers/Microsoft.CostManagement")[0][len(aztree.ARM):]
        self.calls.append((scope, dims, aggs))
        if dims in self.fail:
            return error(400, "BadRequest", self.fail[dims])
        refused = (self.reject | set(self.reject_for.get(scope, ()))) & set(aggs)
        if refused:
            return error(400, "BadRequest", f"Invalid aggregation {sorted(refused)}")
        daily = body["dataset"].get("granularity") == "Daily"
        if dims in self.explode and daily:
            return page(["Cost"], [], next_link=url.split("&")[0] + "&$skiptoken=more")
        if not daily:  # no granularity: Azure answers one row per group, summed over the whole time period
            return self.totals(body, dims, aggs, scope)
        tagged = bool(dims) and dims[0].startswith("tag:")  # Azure answers a tag grouping as TagKey and TagValue
        cols = aggs + ["UsageDate", *(["TagKey", "TagValue", dims[1]] if tagged else dims), "Currency"]
        rows = []
        for date, a, b, cost, cur, usd in self.tables.get(dims, {}).get(scope, []):
            usd = usd if usd is not None else cost
            vals = {"Cost": cost, "CostUSD": usd, "PreTaxCost": cost, "PreTaxCostUSD": usd, "UsageDate": date,
                    dims[0]: a, dims[1]: b, "Currency": cur, "TagKey": dims[0][4:], "TagValue": a}
            rows.append([vals[c] for c in cols])
        return page(cols, rows)

    def totals(self, body, dims, aggs, scope):
        start, end = (int(body["timePeriod"][k][:10].replace("-", "")) for k in ("from", "to"))
        sums = {}
        for date, a, b, cost, cur, usd in self.tables.get(dims, {}).get(scope, []):
            if start <= date <= end:
                acc = sums.setdefault((a, b, cur), [0.0, 0.0])
                acc[0] += cost
                acc[1] += usd if usd is not None else cost
        cols = aggs + [*dims, "Currency"]
        rows = []
        for (a, b, cur), (cost, usd) in sums.items():
            vals = {"Cost": cost, "CostUSD": usd, "PreTaxCost": cost, "PreTaxCostUSD": usd, dims[0]: a, dims[1]: b, "Currency": cur}
            rows.append([vals[c] for c in cols])
        return page(cols, rows)

    def tag_names_page(self, url):
        sub = url.split("/subscriptions/")[1].split("/")[0]
        self.other.append(("tagNames", sub))
        got = self.tag_names.get(sub, [])
        if isinstance(got, int):
            return error(got, "Failed", "no tags for you")
        value = [{"tagName": n, "count": {"type": "Total", "value": c}, "values": []} for n, c in got]
        return 200, {}, json.dumps({"value": value}).encode()

    def forecast_page(self, url, body):
        scope = url.split("/providers/Microsoft.CostManagement")[0][len(aztree.ARM):]
        self.other.append(("forecast", scope, body))
        got = self.forecasts.get(scope, [])
        if isinstance(got, int):
            return error(got, "Failed", "no forecast")
        col = next(iter(body["dataset"]["aggregation"].values()))["name"]
        return page([col, "UsageDate", "CostStatus", "Currency"], [[c, d, s, cur] for d, s, c, cur in got])

    def graph_page(self, body, headers):
        self.other.append(("graph", body, headers["Authorization"]))
        if isinstance(self.findings, int):
            return error(self.findings, "AuthorizationFailed", "no Resource Graph for you")
        pages = self.findings or [[]]
        i = int(body["options"].get("$skipToken") or 0)
        out = {"totalRecords": sum(map(len, pages)), "count": len(pages[i]), "data": pages[i],
               "facets": [], "resultTruncated": "false"}
        if i + 1 < len(pages):
            out["$skipToken"] = str(i + 1)
        return 200, {}, json.dumps(out).encode()


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


ROUTER_ARGS = ("explode", "reject_usd", "reject", "reject_for", "fail", "tag_names", "forecasts", "findings")


def fetch(tables, targets=(PROD,), log=None, **kw):
    """aztree.fetch() against the fake ARM. Router options go to the Router, the rest (tag=, graph=) to fetch()."""
    router = Router(tables, **{k: kw.pop(k) for k in ROUTER_ARGS if k in kw})
    data = aztree.fetch(client(router), [aztree.subscription_target(t) for t in targets], 3, "ActualCost",
                        advisor=False, log=log or (lambda *a: None), today=TODAY, **kw)
    return data, router


def rows_of(data, view):
    return {tuple(r["k"]): r["d"] for r in data["views"][view]["rows"]}


class FetchTest(unittest.TestCase):
    def test_window_is_two_periods_ending_the_day_before_yesterday(self):
        # yesterday is still arriving (Azure takes 8-24 h), so a partial day would drag down every comparison
        data, router = fetch(ONE_SUB)
        self.assertEqual(data["days"][-1], (TODAY - aztree.dt.timedelta(2)).isoformat())
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

    def test_resource_view_falls_back_to_period_totals_when_paging_explodes(self):
        tables = dict(ONE_SUB)
        tables[("ResourceGroupName", "ResourceId")] = {"/subscriptions/aaaa-1": [
            (20260922, "rg-app", RID, 4.0, "USD", None),   # previous period
            (20260925, "rg-app", RID, 5.0, "USD", None),   # current period
            (20260927, "rg-app", RID, 1.0, "USD", None)]}
        data, router = fetch(tables, explode={("ResourceGroupName", "ResourceId")})
        # every resource stays, with the right totals for each period: previous on its first day, current on its first day
        self.assertEqual(rows_of(data, "resource")[("/subscriptions/aaaa-1/resourcegroups/rg-app", RID)], [4.0, 0, 0, 6.0, 0, 0])
        self.assertEqual(data["resource_fallback"], ["acme-prod"])
        totals = [b["timePeriod"] for b in router.bodies if "granularity" not in b["dataset"]]
        self.assertEqual(totals, [{"from": "2026-09-22T00:00:00Z", "to": "2026-09-24T23:59:59Z"},
                                  {"from": "2026-09-25T00:00:00Z", "to": "2026-09-27T23:59:59Z"}])

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


class TagViewTest(unittest.TestCase):
    TAGS = {"aaaa-1": [("hidden-link: /app-insights-resource-id", 300), ("environment", 109), ("owner", 40)]}
    TAGGED = {**ONE_SUB, ("tag:environment", "ServiceName"): {"/subscriptions/aaaa-1": [
        (20260922, "prod", "Storage", 1.0, "USD", None),
        (20260927, None, "Storage", 1.5, "USD", None),  # untagged spend: Azure sends TagValue null
        (20260927, "", "Storage", 0.5, "USD", None),
        (20260925, "prod", "Azure App Service", 5.0, "USD", None),
    ]}}

    def choose(self, tags, targets=(PROD,), wanted=None):
        router = Router({}, tag_names=tags)
        got = aztree.choose_tag(client(router), [aztree.subscription_target(t) for t in targets], wanted, log=lambda *a: None)
        return got, router

    def test_the_tag_on_most_resources_is_picked(self):
        self.assertEqual(self.choose(self.TAGS)[0], "environment")  # Azure's hidden-* tags don't count

    def test_counts_add_up_across_subscriptions_whatever_the_case(self):
        tags = {"aaaa-1": [("env", 10), ("owner", 30)], "bbbb-2": [("Env", 25)]}
        self.assertEqual(self.choose(tags, targets=(PROD, DEV))[0], "env")

    def test_the_tag_flag_wins_without_asking_azure(self):
        got, router = self.choose(self.TAGS, wanted="costcenter")
        self.assertEqual((got, router.other), ("costcenter", []))

    def test_other_scopes_need_the_tag_flag(self):
        scope = aztree.scope_target("/providers/Microsoft.Billing/billingAccounts/1")
        self.assertIsNone(aztree.choose_tag(client(Router({})), [scope], None, log=lambda *a: None))

    def test_no_tags_means_no_tag_view(self):
        data, _ = fetch(ONE_SUB)
        self.assertNotIn("tag", data["views"])

    def test_a_tag_names_error_is_logged_and_means_no_tag_view(self):
        lines = []
        data, _ = fetch(ONE_SUB, tag_names={"aaaa-1": 403}, log=lines.append)
        self.assertNotIn("tag", data["views"])
        self.assertTrue(any("tags" in line and "403" in line for line in lines), lines)

    def test_the_tag_view_splits_the_bill_by_value(self):
        data, router = fetch(self.TAGGED, tag_names=self.TAGS)
        view = data["views"]["tag"]
        self.assertEqual((view["dims"], view["tag"], view["names"]), (["TagValue", "ServiceName"], "environment", {}))
        rows = rows_of(data, "tag")
        self.assertEqual(sum(rows[("", "Storage")]), 2.0)  # null and empty values are both untagged
        self.assertEqual(sum(rows[("prod", "Azure App Service")]), 5.0)
        bill = sum(sum(d) for d in rows_of(data, "service").values())
        self.assertEqual(sum(sum(d) for d in rows.values()), bill)
        body = next(b for b in router.bodies if b["dataset"]["grouping"][0]["type"] == "TagKey")
        self.assertEqual(body["dataset"]["grouping"],
                         [{"type": "TagKey", "name": "environment"}, {"type": "Dimension", "name": "ServiceName"}])

    def test_the_tag_flag_skips_the_tag_names_call(self):
        data, router = fetch(self.TAGGED, tag="environment")
        self.assertIn("tag", data["views"])
        self.assertFalse([c for c in router.other if c[0] == "tagNames"])

    def test_a_failed_tag_query_drops_the_view_but_not_the_run(self):
        lines = []
        data, _ = fetch(ONE_SUB, tag="environment", fail={("tag:environment", "ServiceName"): "Invalid tag key"},
                        log=lines.append)
        self.assertNotIn("tag", data["views"])
        self.assertIn(("Storage", "Hot LRS Data Stored"), rows_of(data, "service"))
        self.assertTrue(any("tag view" in line for line in lines), lines)


class ForecastTest(unittest.TestCase):
    SEP = [(20260901, "Actual", 100.0, "USD"), (20260927, "Actual", 50.0, "USD"),
           (20260928, "Forecast", 40.0, "USD"), (20260930, "Forecast", 45.0, "USD")]
    TWO = {("ServiceName", "Meter"): {"/subscriptions/aaaa-1": [(20260925, "Storage", "LRS", 1.0, "USD", None)],
                                      "/subscriptions/bbbb-2": [(20260925, "Storage", "LRS", 1.0, "USD", None)]}}

    def test_asks_for_this_calendar_month(self):
        _, router = fetch(ONE_SUB, forecasts={"/subscriptions/aaaa-1": self.SEP})
        ((_, scope, body),) = [c for c in router.other if c[0] == "forecast"]
        self.assertEqual(scope, "/subscriptions/aaaa-1")
        self.assertEqual(body["timePeriod"], {"from": "2026-09-01T00:00:00Z", "to": "2026-09-30T23:59:59Z"})
        self.assertEqual((body["type"], body["includeActualCost"], body["includeFreshPartialCost"]), ("ActualCost", True, False))
        self.assertEqual(body["dataset"]["aggregation"], {"totalCost": {"name": "Cost", "function": "Sum"}})

    def test_december_ends_on_the_31st(self):
        router = Router({}, forecasts={"/subscriptions/aaaa-1": self.SEP})
        aztree.month_forecast(client(router), aztree.subscription_target(PROD), "ActualCost", aztree.dt.date(2026, 12, 5))
        self.assertEqual(router.other[0][2]["timePeriod"]["to"], "2026-12-31T23:59:59Z")

    def test_sums_what_is_billed_and_what_is_to_come(self):
        data, _ = fetch(ONE_SUB, forecasts={"/subscriptions/aaaa-1": self.SEP})
        self.assertEqual(data["forecast"], {"month": "2026-09", "actual": 150.0, "forecast": 85.0, "total": 235.0})
        self.assertIsNone(data["forecast_note"])

    def test_adds_up_across_subscriptions(self):
        data, _ = fetch(self.TWO, targets=(PROD, DEV),
                        forecasts={"/subscriptions/aaaa-1": self.SEP, "/subscriptions/bbbb-2": self.SEP})
        self.assertEqual(data["forecast"]["total"], 470.0)

    def test_a_subscription_without_a_forecast_means_none(self):
        data, _ = fetch(self.TWO, targets=(PROD, DEV),
                        forecasts={"/subscriptions/aaaa-1": self.SEP, "/subscriptions/bbbb-2": 403})
        self.assertIsNone(data["forecast"])
        self.assertIn("acme-dev (HTTP 403)", data["forecast_note"])

    def test_an_empty_answer_means_none(self):
        data, _ = fetch(ONE_SUB)  # the fake answers no rows unless told otherwise
        self.assertIsNone(data["forecast"])
        self.assertIn("acme-prod", data["forecast_note"])

    def test_a_forecast_in_another_currency_is_left_out(self):
        eur = [(d, s, c, "EUR") for d, s, c, _ in self.SEP]
        data, _ = fetch(ONE_SUB, forecasts={"/subscriptions/aaaa-1": eur})  # the bill is in USD
        self.assertIsNone(data["forecast"])
        self.assertIn("currency", data["forecast_note"])

    def test_rows_without_a_currency_count_as_the_bill_currency(self):
        bare = [(d, s, c, None) for d, s, c, _ in self.SEP]
        data, _ = fetch(ONE_SUB, forecasts={"/subscriptions/aaaa-1": bare})
        self.assertEqual(data["forecast"]["total"], 235.0)

    def test_the_column_the_queries_settled_on_is_used(self):
        _, router = fetch(ONE_SUB, reject={"Cost", "CostUSD"}, forecasts={"/subscriptions/aaaa-1": self.SEP})
        body = next(c[2] for c in router.other if c[0] == "forecast")
        self.assertEqual(body["dataset"]["aggregation"]["totalCost"]["name"], "PreTaxCost")


IDLE_DISK = {"check": "unattached-disk", "id": RID.rsplit("/providers/", 1)[0] + "/providers/microsoft.compute/disks/d1",
             "name": "d1", "resourceGroup": "rg-app", "subscriptionId": "aaaa-1"}


class GraphTest(unittest.TestCase):
    def graph_calls(self, router):
        return [c for c in router.other if c[0] == "graph"]

    def test_one_query_for_the_subscriptions_of_a_tenant(self):
        tables = {("ServiceName", "Meter"): {"/subscriptions/aaaa-1": [], "/subscriptions/bbbb-2": []}}
        data, router = fetch(tables, targets=(PROD, DEV), findings=[[IDLE_DISK]])
        ((_, body, _),) = self.graph_calls(router)
        self.assertEqual(body["subscriptions"], ["aaaa-1", "bbbb-2"])
        self.assertIn("unattached-disk", body["query"])
        self.assertEqual((data["graph"], data["graph_error"]), ([IDLE_DISK], None))

    def test_follows_skip_tokens(self):
        other = {**IDLE_DISK, "id": IDLE_DISK["id"] + "2", "name": "d2"}
        data, router = fetch(ONE_SUB, findings=[[IDLE_DISK], [other]])
        self.assertEqual([f["name"] for f in data["graph"]], ["d1", "d2"])
        self.assertEqual(self.graph_calls(router)[1][1]["options"]["$skipToken"], "1")

    def test_stops_at_the_row_cap(self):
        rows = [{**IDLE_DISK, "name": f"d{i}"} for i in range(3)]
        with mock.patch.object(aztree, "MAX_GRAPH_ROWS", 2):
            data, router = fetch(ONE_SUB, findings=[rows[:2], rows[2:]])
        self.assertEqual((len(data["graph"]), len(self.graph_calls(router))), (2, 1))

    def test_each_tenant_is_asked_with_its_own_token(self):
        router = Router({}, findings=[[]])
        az = aztree.Azure(lambda tenant: f"tok-{tenant}", send=router, sleep=lambda s: None, log=lambda *a: None)
        targets = [aztree.subscription_target({"id": "aaaa-1", "name": "a", "tenant": "t-one"}),
                   aztree.subscription_target({"id": "bbbb-2", "name": "b", "tenant": "t-two"})]
        aztree.graph_findings(az, targets)
        self.assertEqual([(c[1]["subscriptions"], c[2]) for c in self.graph_calls(router)],
                         [(["aaaa-1"], "Bearer tok-t-one"), (["bbbb-2"], "Bearer tok-t-two")])

    def test_without_access_the_run_goes_on_and_keeps_only_the_status(self):
        lines = []
        data, _ = fetch(ONE_SUB, findings=403, log=lines.append)
        self.assertEqual((data["graph"], data["graph_error"]), ([], "HTTP 403"))
        self.assertTrue(any("--no-graph" in line for line in lines), lines)
        self.assertIn(("Storage", "Hot LRS Data Stored"), rows_of(data, "service"))

    def test_no_graph_makes_no_call(self):
        data, router = fetch(ONE_SUB, findings=[[IDLE_DISK]], graph=False)
        self.assertIsNone(data["graph"])
        self.assertEqual(self.graph_calls(router), [])

    def test_other_scopes_are_not_asked(self):
        router = Router({})
        data = aztree.fetch(client(router), [aztree.scope_target("/providers/Microsoft.Billing/billingAccounts/1")], 3,
                            "ActualCost", advisor=False, log=lambda *a: None, today=TODAY)
        self.assertIsNone(data["graph"])
        self.assertEqual(self.graph_calls(router), [])


class Batch4ReviewTest(unittest.TestCase):
    """Findings from the batch 4 review, each reproduced before it was fixed."""

    def test_one_tenant_without_access_keeps_the_other_tenants_findings(self):
        router = Router({}, findings=[[IDLE_DISK]])

        def send(method, url, data, headers):
            if "ResourceGraph" in url and headers["Authorization"] == "Bearer tok-t-two":
                return error(403, "AuthorizationFailed", "no Resource Graph in this tenant")
            return router(method, url, data, headers)

        az = aztree.Azure(lambda tenant: f"tok-{tenant}", send=send, sleep=lambda s: None, log=lambda *a: None)
        targets = [aztree.subscription_target({"id": "aaaa-1", "name": "a", "tenant": "t-one"}),
                   aztree.subscription_target({"id": "bbbb-2", "name": "b", "tenant": "t-two"})]
        data = aztree.fetch(az, targets, 3, "ActualCost", advisor=False, log=lambda *a: None, today=TODAY)
        self.assertEqual((data["graph"], data["graph_error"]), ([IDLE_DISK], "HTTP 403"))

    def test_a_rejected_tag_query_does_not_change_the_cost_column(self):
        # the columns were settled by the first query; a later 400 that mentions a column is that query's own problem
        data, router = fetch(ONE_SUB, tag="environment", forecasts={"/subscriptions/aaaa-1": ForecastTest.SEP},
                             fail={("tag:environment", "ServiceName"): "Invalid query definition: Invalid column name 'TagKey'"})
        self.assertEqual(len([c for c in router.calls if c[1] == ("tag:environment", "ServiceName")]), 1)
        body = next(c[2] for c in router.other if c[0] == "forecast")
        self.assertEqual(body["dataset"]["aggregation"]["totalCost"]["name"], "Cost")
        self.assertNotIn("tag", data["views"])

    def test_a_forecast_without_the_cost_column_is_no_forecast(self):
        router = Router(ONE_SUB)

        def send(method, url, data, headers):
            if "/forecast" in url:  # asked for Cost, answered with another column: not a $0 forecast
                return page(["PreTaxCost", "UsageDate", "CostStatus", "Currency"], [[100.0, 20260901, "Actual", "USD"]])
            return router(method, url, data, headers)

        data = aztree.fetch(client(send), [aztree.subscription_target(PROD)], 3, "ActualCost", advisor=False,
                            log=lambda *a: None, today=TODAY)
        self.assertIsNone(data["forecast"])
        self.assertIn("acme-prod", data["forecast_note"])

    def test_the_ai_instructions_keep_their_spacing(self):
        self.assertIn("Please: 1) explain", aztree.AI_INSTRUCTIONS)


# (service, meter, words expected in the reason, or None for "no flag"). Meter names are from real bills.
PIT_CASES = [
    ("Log Analytics", "Analytics Logs Data Ingestion", "Log Analytics ingestion"),
    ("Azure Monitor", "Basic Logs Data Ingestion", None),  # already the cheap tier: "use Basic logs" would be wrong
    ("Azure Front Door Service", "Premium Base Fees", "Front Door Premium"),
    ("Azure Front Door Service", "Standard Base Fees", None, 35),  # one profile: nothing to merge
    ("Azure Front Door Service", "Standard Base Fees", "about 10 Front Door Standard profiles", 346),
    ("SQL Database", "eDTUs", "DTU", 184),       # a pool: worth a look
    ("SQL Database", "S2 DTUs", None, 74),       # small tier: cheaper than any vCore option
    ("Azure DevOps", "Microsoft-hosted CI/CD Concurrent Job", "Azure DevOps"),
    ("Azure DevOps", "Basic User", "Azure DevOps"),
    ("Azure DevOps", "Standard Data Stored", None),
    ("Virtual Network", "Standard Private Endpoint", "private endpoints"),
    ("Azure Cosmos DB", "100 RU/s", "provisioned"),
    ("Azure Cosmos DB", "Data Stored", None),
    ("Azure Monitor", "Alerts System Log Monitored at 1 Minute Frequency", "1-minute alert"),
    ("Azure Monitor", "Alerts System Log Monitored at 5 Minute Frequency", None),
    ("Virtual Machines", "D2 v2 Promo", "1 May 2028"),
    ("Virtual Machines", "GS3", "15 Nov 2028"),
    ("Virtual Machines", "L8s", "1 May 2028"),
    ("Virtual Machines", "L8s v2", "15 Nov 2028"),
    ("Virtual Machines", "E8s v3", "reservations"),
    ("Virtual Machines", "B2s v2", None),
    ("Log Analytics", "Auxiliary Logs Data Ingestion", None),
    ("Redis Cache", "C1 Cache Instance", "30 Sep 2028"),
    ("Azure App Service", "S2 App", "can't be reserved"),
    ("Azure App Service", "B1 App", None),
    ("Virtual Machines", "L8s v3", None),
    ("Virtual Machines", "NV6ads A10 v5", None),
    ("Storage", "Standard Page Blob v2 Snapshots", "snapshots"),
    ("Azure Monitor", "Standard Web Test Execution", None),
    ("Bandwidth", "Standard Data Transfer Out", "data transfer"),
    ("Bandwidth", "Inter Continent Data Transfer Out - NAM or EU To Any", "data transfer"),
    ("Bandwidth", "Data Transfer In", None),
    ("NAT Gateway", "Standard Data Processed", "NAT"),
    ("NAT Gateway", "Standard Gateway", None),
    ("Azure Firewall", "Standard Data Processed", "Firewall"),
    ("Azure Firewall", "Premium Deployment", "Firewall"),
    ("Azure Firewall", "Standard Deployment", None),
    ("Virtual Network", "Basic IPv4 Static Public IP", "Basic public IP"),
    ("Virtual Network", "Standard IPv4 Static Public IP", "public IPs"),
    ("Storage", "LRS Snapshots", "snapshots"),
    ("Storage", "P10 LRS Disk", None),
    ("Virtual Machines", "D2 v2", "1 May 2028"),
    ("Virtual Machines", "D2 v2/DS2 v2", "1 May 2028"),
    ("Virtual Machines", "DS3 v2 Spot", "1 May 2028"),
    ("Virtual Machines", "D11 v2", "1 May 2028"),
    ("Virtual Machines", "A1 v2", "15 Nov 2028"),
    ("Virtual Machines", "Basic.A2", "15 Nov 2028"),
    ("Virtual Machines", "F4", "15 Nov 2028"),
    ("Virtual Machines", "F2s", "15 Nov 2028"),
    ("Virtual Machines", "F2s v2", "15 Nov 2028"),
    ("Virtual Machines", "D4s v5", None),
    ("Virtual Machines", "D2 v3", "reservations"),
    ("Virtual Machines", "D2as v4", None),
    ("Virtual Machines", "B2s", "15 Nov 2028"),
    ("Azure App Service", "P1 v2 App", "Premium v2"),
    ("Azure App Service", "P2v2 App", "Premium v2"),
    ("Azure App Service", "P1 v3 App", None),
    ("Azure App Service", "P0v3 App", None),
    ("Windows Server", "Extended Security Updates - Windows Server 2012", "Extended Security Updates"),
    ("SQL Database", "vCore", None),
]


class PitsTest(unittest.TestCase):
    def test_rules_against_real_meter_names(self):
        # a case may name the meter's monthly spend: some rules only matter above a floor
        for service, meter, expected, *monthly in PIT_CASES:
            with self.subTest(service=service, meter=meter, monthly=monthly):
                why = aztree.pit(service, meter, *monthly)
                if expected is None:
                    self.assertIsNone(why)
                else:
                    self.assertIsNotNone(why)
                    self.assertIn(expected.lower(), why.lower())

    def test_summarize_passes_the_monthly_pace(self):
        rows = [("SQL Database", "S2 DTUs", [2.5] * 6), ("SQL Database", "eDTUs", [6] * 6), ("Storage", "LRS", [1] * 6)]
        self.assertEqual([f["meter"] for f in aztree.summarize(make_data(rows))["flags"]], ["eDTUs"])


def advisor_item(problem, resource, value, sku=None, term=None, savings=None, rtype="t-1", field="Microsoft.Subscriptions/subscriptions"):
    ext = {"annualSavingsAmount": str(savings), "savingsCurrency": "USD"} if savings is not None else {}
    if sku:
        ext["displaySKU"] = sku
    if term:
        ext["term"] = term
    return {"properties": {"category": "Cost", "impact": "High", "impactedField": field, "impactedValue": value,
                           "recommendationTypeId": rtype, "shortDescription": {"problem": problem, "solution": problem},
                           "extendedProperties": ext, "resourceMetadata": {"resourceId": resource}}}


class AdvisorTest(unittest.TestCase):
    SUB = "/subscriptions/aaaa-1"

    def recs(self, items):
        send = FakeSend((200, {}, json.dumps({"value": items}).encode()))
        return aztree.advisor_recs(client(send), aztree.subscription_target(PROD)), send

    def test_reads_cost_recommendations_for_the_subscription(self):
        _, send = self.recs([])
        url = send.calls[0]["url"]
        self.assertIn("/subscriptions/aaaa-1/providers/Microsoft.Advisor/recommendations", url)
        self.assertIn("Category%20eq%20%27Cost%27", url)

    def test_reservation_variants_collapse_to_the_best_one(self):
        cosmos = "Consider Cosmos DB reserved instance"
        items = [advisor_item(cosmos, self.SUB, "aaaa-1", "100 RU/s", term, s, rtype="cosmos")
                 for term, s in (("P1Y", 747), ("P3Y", 1182), ("P1Y", 787), ("P3Y", 1143))]
        items.append(advisor_item("Consider SQL PaaS DB reserved instance", self.SUB, "aaaa-1", "SQL GP Gen5", "P1Y", 4396, rtype="sql"))
        recs, _ = self.recs(items)
        self.assertEqual([(r["sku"], r["annual_savings"], r["term"]) for r in recs],
                         [("SQL GP Gen5", 4396.0, "P1Y"), ("100 RU/s", 1182.0, "P3Y")])

    def test_resource_level_recommendation(self):
        rid = "/subscriptions/aaaa-1/resourceGroups/RG-App/providers/Microsoft.Compute/virtualMachines/vm1"
        recs, _ = self.recs([advisor_item("Right-size or shutdown underutilized virtual machines", rid, "vm1",
                                          savings=840, field="Microsoft.Compute/virtualMachines")])
        rec = recs[0]
        self.assertEqual(rec["resource"], rid.lower())
        self.assertEqual(rec["resource_name"], "vm1")
        self.assertEqual(rec["subscription"], "acme-prod")
        self.assertEqual(rec["currency"], "USD")

    def test_subscription_level_recommendation_is_named_after_the_subscription(self):
        recs, _ = self.recs([advisor_item("Consider a savings plan", self.SUB, "aaaa-1", "Compute_Savings_Plan", "P1Y", 2947)])
        self.assertEqual(recs[0]["resource_name"], "acme-prod")

    def test_no_savings_figure_sorts_last(self):
        recs, _ = self.recs([advisor_item("Disable health probes", "/x", "fd", savings=None),
                             advisor_item("Buy reservation", self.SUB, "aaaa-1", "sku", "P1Y", 10)])
        self.assertEqual([r["annual_savings"] for r in recs], [10.0, None])

    def test_fetch_survives_missing_advisor_access(self):
        router = Router(ONE_SUB)
        real = router.__call__

        def send(method, url, data, headers):
            if "Microsoft.Advisor" in url:
                return error(403, "AuthorizationFailed", "no access to Advisor")
            return real(method, url, data, headers)

        lines = []
        data = aztree.fetch(client(send), [aztree.subscription_target(PROD)], 3, "ActualCost", advisor=True,
                            log=lines.append, today=TODAY)
        self.assertEqual(data["advisor"], [])
        self.assertIn("403", data["advisor_error"])
        self.assertTrue(any("Advisor" in line for line in lines))
        self.assertIn(("Storage", "Hot LRS Data Stored"), rows_of(data, "service"))

    def test_fetch_collects_advisor_when_asked(self):
        router = Router(ONE_SUB)

        def send(method, url, data, headers):
            if "Microsoft.Advisor" in url:
                return 200, {}, json.dumps({"value": [advisor_item("Buy reservation", self.SUB, "aaaa-1", "sku", "P1Y", 10)]}).encode()
            return router(method, url, data, headers)

        data = aztree.fetch(client(send), [aztree.subscription_target(PROD)], 3, "ActualCost", advisor=True,
                            log=lambda *a: None, today=TODAY)
        self.assertEqual(len(data["advisor"]), 1)
        self.assertIsNone(data["advisor_error"])


DAYS6 = ["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-27"]
RG = "/subscriptions/aaaa-1/resourcegroups/rg-app"


def make_data(service_rows, resource_rows=(), advisor=None, **extra):
    """A minimal data dict in the shape fetch() returns. Rows are (k1, k2, [daily values]): 6 values are Sep 22-27,
    split after day 3; longer series get consecutive days ending Sep 27, split in the middle."""
    def view(dims, rows, names=None):
        return {"dims": dims, "names": names or {}, "rows": [{"k": [a, b], "d": d} for a, b, d in rows]}

    n = len(service_rows[0][2]) if service_rows else 6
    days = DAYS6 if n == 6 else [(aztree.dt.date(2026, 9, 27) - aztree.dt.timedelta(n - 1 - i)).isoformat() for i in range(n)]
    sub_rows = {}
    for svc, _, d in service_rows:
        acc = sub_rows.setdefault(svc, [0.0] * n)
        for i, v in enumerate(d):
            acc[i] += v
    data = {
        "days": days, "split": n // 2, "currency": "USD", "metric": "ActualCost", "generated": "2026-09-28 09:00",
        "subscriptions": [{"id": "aaaa-1", "name": "acme-prod", "currency": "USD"}],
        "views": {
            "service": view(["ServiceName", "Meter"], service_rows),
            "subscription": view(["SubscriptionId", "ServiceName"], [("aaaa-1", s, d) for s, d in sub_rows.items()],
                                 {"aaaa-1": "acme-prod"}),
            "region": view(["ResourceLocation", "ServiceName"], [("us central", s, d) for s, d in sub_rows.items()]),
            "resource": view(["ResourceGroupName", "ResourceId"], resource_rows, {RG: "rg-app"}),
        },
        "advisor": advisor, "advisor_error": None, "resource_fallback": [], "demo": False,
    }
    data.update(extra)
    return data


BASIC = make_data([
    ("SQL Database", "vCore", [10, 10, 10, 10, 10, 10]),
    ("Log Analytics", "Analytics Logs Data Ingestion", [2, 2, 2, 6, 6, 6]),
    ("Storage", "Hot LRS Data Stored", [1, 1, 1, 1, 1, 1]),
], resource_rows=[(RG, RG + "/providers/microsoft.sql/servers/db1", [10, 10, 10, 10, 10, 10])])


class SummarizeTest(unittest.TestCase):
    def test_totals_compare_the_two_periods(self):
        t = aztree.summarize(BASIC)["totals"]
        self.assertEqual((t["current"], t["previous"], t["change"]), (51.0, 39.0, 12.0))
        self.assertEqual(t["daily_avg"], 17.0)
        self.assertEqual(t["monthly_pace"], round(17.0 * 30.4, 2))

    def test_periods_are_named(self):
        p = aztree.summarize(BASIC)["period"]
        self.assertEqual(p["current"], {"start": "2026-09-25", "end": "2026-09-27", "days": 3})
        self.assertEqual(p["previous"], {"start": "2026-09-22", "end": "2026-09-24", "days": 3})

    def test_line_items_name_service_and_meter(self):
        top = aztree.summarize(BASIC)["line_items"][0]
        self.assertEqual((top["service"], top["meter"], top["current"]), ("SQL Database", "vCore", 30.0))

    def test_growers_and_flags(self):
        s = aztree.summarize(BASIC)
        self.assertEqual([g["meter"] for g in s["top_growers"]], ["Analytics Logs Data Ingestion"])
        self.assertEqual(s["top_growers"][0]["change_pct"], 200.0)
        self.assertEqual([f["meter"] for f in s["flags"]], ["Analytics Logs Data Ingestion"])
        self.assertIn("Log Analytics", s["flags"][0]["reason"])

    def test_breakdowns_by_subscription_region_and_resource_group(self):
        s = aztree.summarize(BASIC)
        self.assertEqual(s["by_subscription"][0]["name"], "acme-prod")
        self.assertEqual(s["by_subscription"][0]["current"], 51.0)
        self.assertEqual(s["by_region"][0]["region"], "us central")
        rg = s["by_resource_group"][0]
        self.assertEqual((rg["resource_group"], rg["id"]), ("rg-app", RG))
        self.assertEqual(rg["resources"][0]["resource_id"], RG + "/providers/microsoft.sql/servers/db1")

    def test_big_resource_groups_list_their_top_resources_only(self):
        rows = [(RG, f"{RG}/providers/microsoft.web/sites/app{i}", [0, 0, 0, i + 1, 0, 0]) for i in range(25)]
        rg = aztree.summarize(make_data([("App", "m", [1] * 6)], rows))["by_resource_group"][0]
        self.assertEqual(len(rg["resources"]), 20)
        self.assertEqual(rg["other_resources"], {"count": 5, "current": 15.0})

    def test_carries_currency_subscriptions_and_advisor(self):
        rec = {"problem": "Buy a reservation", "annual_savings": 100.0}
        s = aztree.summarize(make_data([("A", "m", [1] * 6)], advisor=[rec]))
        self.assertEqual(s["currency"], "USD")
        self.assertEqual(s["subscriptions"][0]["name"], "acme-prod")
        self.assertEqual(s["advisor"], [{**rec, "covers": [], "covers_monthly": 0.0}])  # not a commitment: covers nothing
        self.assertEqual(s["tool"], "aztree")

    def test_instructions_speak_azure(self):
        text = aztree.summarize(BASIC)["instructions_for_ai"]
        for word in ("Azure", "reservations", "savings plans", "Hybrid Benefit", "dev/test", "commitment tier", "tier"):
            self.assertIn(word, text)
        self.assertNotIn("AWS", text)

    def test_export_writes_utf8_json(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = aztree.export(make_data([("Storage", "Snapshots · Zürich", [1] * 6)]), Path(d) / "x.json")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["line_items"][0]["meter"], "Snapshots · Zürich")


class IdleHintsTest(unittest.TestCase):
    DISK, DISK2 = RG + "/providers/microsoft.compute/disks/d1", RG + "/providers/microsoft.compute/disks/d2"
    IP = RG + "/providers/microsoft.network/publicipaddresses/ip1"

    def data(self, graph, **extra):
        rows = [(RG, self.DISK, [1, 1, 1, 2, 2, 2]), (RG, self.DISK2, [1] * 6), (RG, self.IP, [0.1] * 6)]
        return make_data([("Storage", "LRS", [5] * 6)], resource_rows=rows, graph=graph, **extra)

    def finding(self, check, rid):
        return {"check": check, "id": rid, "name": rid.rsplit("/", 1)[-1], "resourceGroup": "rg-app", "subscriptionId": "aaaa-1"}

    def idle(self, data):
        return [h for h in aztree.summarize(data)["hints"] if h["kind"] == "idle"]

    def test_one_hint_per_check_with_what_it_cost(self):
        (h,) = self.idle(self.data([self.finding("unattached-disk", self.DISK), self.finding("unattached-disk", self.DISK2)]))
        self.assertEqual((h["check"], h["label"], h["amount"], h["current"]), ("unattached-disk", "2 unattached disks", 9.0, 9.0))
        self.assertEqual(h["resources"], [{"id": self.DISK, "name": "d1", "group": RG, "current": 6.0},
                                          {"id": self.DISK2, "name": "d2", "group": RG, "current": 3.0}])
        self.assertIn("d1", h["reason"])  # the biggest: where a click goes

    def test_a_single_resource_is_named(self):
        (h,) = self.idle(self.data([self.finding("unattached-disk", self.DISK)]))
        self.assertEqual(h["label"], "d1")
        self.assertTrue(h["reason"].startswith("unattached disk: "), h["reason"])

    def test_what_costs_nothing_is_left_out(self):
        free = RG + "/providers/microsoft.web/serverfarms/free-plan"  # no cost row: a free plan isn't money
        self.assertEqual(self.idle(self.data([self.finding("empty-plan", free)])), [])

    def test_pennies_are_not_a_hint(self):
        self.assertEqual(self.idle(self.data([self.finding("unused-ip", self.IP)])), [])  # $0.30 this period

    def test_unknown_checks_are_ignored(self):
        self.assertEqual(self.idle(self.data([self.finding("something-new", self.DISK)])), [])

    def test_they_sort_with_the_other_to_dos(self):
        data = self.data([self.finding("unattached-disk", self.DISK)])
        data["views"]["service"]["rows"].append({"k": ["Log Analytics", "Analytics Logs Data Ingestion"], "d": [1] * 6})
        kinds = [h["kind"] for h in aztree.summarize(data)["hints"] if h["kind"] in ("pit", "idle")]
        self.assertEqual(kinds, ["idle", "pit"])  # $6 of disk before $3 of ingestion

    def test_old_saved_data_has_no_idle_hints(self):
        self.assertEqual(self.idle(BASIC), [])  # runs saved by 0.3.0 have no "graph" key


class Batch4ExportTest(unittest.TestCase):
    def test_by_tag_names_the_tag_and_its_untagged_spend(self):
        data = make_data([("Storage", "LRS", [5] * 6)])
        data["views"]["tag"] = {"dims": ["TagValue", "ServiceName"], "names": {}, "tag": "environment",
                                "rows": [{"k": ["prod", "Storage"], "d": [4] * 6}, {"k": ["", "Storage"], "d": [1] * 6}]}
        s = aztree.summarize(data)
        self.assertEqual(s["tag"], "environment")
        self.assertEqual([(t["value"], t["current"]) for t in s["by_tag"]], [("prod", 12.0), ("(untagged)", 3.0)])
        self.assertEqual(s["by_tag"][0]["services"][0]["service"], "Storage")

    def test_forecast_and_graph_status_are_passed_on(self):
        f = {"month": "2026-09", "actual": 150.0, "forecast": 85.0, "total": 235.0}
        s = aztree.summarize(make_data([("Storage", "LRS", [5] * 6)], forecast=f, graph_error="HTTP 403"))
        self.assertEqual((s["forecast"], s["graph_error"]), (f, "HTTP 403"))

    def test_old_saved_data_still_exports(self):
        s = aztree.summarize(BASIC)
        self.assertEqual((s["tag"], s["by_tag"], s["forecast"], s["graph_error"]), (None, None, None, None))

    def test_instructions_explain_the_new_fields(self):
        for word in ("by_tag", "(untagged)", "forecast", "Resource Graph", "idle"):
            self.assertIn(word, aztree.AI_INSTRUCTIONS)


TEMPLATE = (REPO / "aztree" / "viewer.html").read_text(encoding="utf-8")

# Runs the viewer's script in Node against a bare-bones DOM: enough to prove each view draws boxes
# and fills the side panel, without a browser.
FAKE_DOM = r"""
const vm = require("vm"), fs = require("fs");
const [code, view, click] = JSON.parse(fs.readFileSync(0, "utf8"));
const made = [], byId = {}, docOn = {}, winOn = {}, calls = [], animations = [], pressed = [];
class El {
  constructor(tag) {
    Object.assign(this, { tag, children: [], dataset: {}, className: "", innerHTML: "", value: "", checked: false,
      isConnected: true, clientWidth: 1100, clientHeight: 700, offsetWidth: 100, offsetHeight: 30, on: {},
      scrollTop: 0, scrollHeight: 2000 });
    this.style = { setProperty() {} };
    const el = this, has = c => el.className.split(" ").includes(c);
    this.classList = {  // edits className, so tests can read the classes
      add: c => { if (!has(c)) el.className = (el.className + " " + c).trim(); },
      remove: c => { el.className = el.className.split(" ").filter(x => x && x !== c).join(" "); },
      toggle: (c, on) => ((on ?? !has(c)) ? el.classList.add(c) : el.classList.remove(c)),
      contains: has,
    };
  }
  append(...c) { this.children.push(...c); }
  replaceChildren(...c) { this.children = c; }
  addEventListener(type, fn) { (this.on[type] = this.on[type] || []).push(fn); }
  closest() { return null; } focus() {} blur() {} click() {} dispatchEvent() {} scrollIntoView() {}
  animate(...a) { animations.push(a); return { cancel() {} }; }
}
const body = new El("body");
Object.assign(globalThis, {
  document: {
    body,
    querySelector: s => byId[s] || (byId[s] = new El(s)), querySelectorAll: () => [],
    createElement: t => { const e = new El(t); made.push(e); return e; },
    createDocumentFragment: () => new El("#fragment"),
    addEventListener: (type, fn) => (docOn[type] = docOn[type] || []).push(fn),
  },
  location: { hash: "#" + view },
  history: { state: null,
    pushState(s, _, url) { this.state = s; calls.push(["push", s, url]); },
    replaceState(s, _, url) { this.state = s; calls.push(["replace", s, url]); } },
  addEventListener: (type, fn) => (winOn[type] = winOn[type] || []).push(fn),
  matchMedia: () => ({ matches: false }),
  ResizeObserver: class { observe() {} }, requestAnimationFrame: () => 0, cancelAnimationFrame() {},
  innerWidth: 1400, innerHeight: 900,
});
vm.runInThisContext(code);
const cells = () => (byId["#map"].children[0]?.children || []).filter(e => e.className.split(" ").includes("cell"));
const box = name => cells().filter(e => e._n && e._n.name === name).pop();
const fire = (list, ev) => { for (const fn of list || []) fn(ev); };
for (const a of Array.isArray(click) ? click : click ? [click] : []) {
  if (typeof a === "string") {  // "[data-rec]:0" clicks the first Advisor tip in the side panel
    const [sel, value] = a.split(":");
    fire(byId["#side"].on.click, { target: { closest: s => (s === sel ? { dataset: { [sel.slice(6, -1)]: value } } : null) } });
  } else if (a.map === "dblclick") {  // a real double click is click, click, dblclick
    const b = cells().find(e => e.className.split(" ").includes("group") && e._n.key === a.key);
    for (const t of ["click", "click", "dblclick"]) fire(byId["#map"].on[t], { target: { closest: () => b } });
  } else if (a.map === "click") {
    const b = a.name === null ? null : box(a.name);
    fire(byId["#map"].on.click, { target: { closest: () => b } });
  } else if (a.press) {  // {"press": "Tab", "from": "map"}; {"press": "Enter", "row": "[data-hint]:0"} presses on a panel row
    let target = a.from ? byId["#" + a.from] : body;
    if (a.row) {
      const [sel, value] = a.row.split(":");
      const hit = { closest: s => (s === sel ? { dataset: { [sel.slice(6, -1)]: value } } : null) };
      target = { matches: s => s.includes("role=button"), click: () => fire(byId["#side"].on.click, { target: hit }) };
    }
    const ev = { key: a.press, shiftKey: !!a.shift, target, prevented: false, preventDefault() { ev.prevented = true; } };
    fire(docOn.keydown, ev);
    pressed.push(ev.prevented);
  } else if (a.type !== undefined) {
    byId["#filter"].value = a.type;
    fire(byId["#filter"].on.input, { target: byId["#filter"] });
  } else if (a.filterKey) {
    fire(docOn.keydown, { key: a.filterKey, target: byId["#filter"], preventDefault() {} });
  } else if (a.pop !== undefined) {
    fire(winOn.popstate, { state: a.pop });
  } else if (a.hover) {
    fire(byId["#map"].on.mousemove, { target: { closest: () => box(a.hover) }, clientX: 10, clientY: 10 });
  }
}
console.log(JSON.stringify({
  leaves: made.filter(e => e.className.split(" ").includes("leaf")).length,
  boxes: made.map(e => e.innerHTML).join("\n"),
  side: byId["#side"].innerHTML, sub: byId["#sub"].innerHTML, meta: byId["#meta"].innerHTML,
  crumbs: byId["#crumbs"].innerHTML,
  views: byId["#views"]?.innerHTML ?? "", viewkeys: byId["#viewkeys"]?.innerHTML ?? "",  // only what the page asked for exists
  tip: byId["#tip"]?.innerHTML ?? "",
  drawn: cells().map(e => ({ cls: e.className, name: e._n?.name })),
  history: calls, animations: animations.length, pressed,
  leafBoxes: made.filter(e => e.className.split(" ").includes("leaf"))
    .map(e => ({ cls: e.className, name: e._n.name, w: parseFloat(e.style.width), h: parseFloat(e.style.height) })),
}));
"""


class ViewerTest(unittest.TestCase):
    def render(self, data):
        out = scratch_dir(self) / "aztree.html"
        aztree.render(data, out)
        return out

    def test_data_is_embedded_without_breaking_out_of_the_script(self):
        html = self.render(make_data([("Storage", "</script><!-- x", [1] * 6)])).read_text(encoding="utf-8")
        self.assertNotIn("__AZTREE_DATA__", html)
        self.assertEqual(html.count("</script>"), 1)
        self.assertNotIn("<!--", html)

    def test_page_loads_nothing_from_the_network(self):
        for pattern in (r"<script[^>]+src", r"<link", r"<img", r"<iframe", r"(?<![a-z])url\(", r"@import", r"fetch\(",
                        r"XMLHttpRequest", r"WebSocket", r"sendBeacon", r"import\("):
            self.assertIsNone(aztree.re.search(pattern, TEMPLATE, aztree.re.I), pattern)

    def test_no_aws_vocabulary_left(self):
        for word in ("Amazon", "USAGE_TYPE", "LINKED_ACCOUNT", "AWSTREE", "UnblendedCost", "awstree-export"):
            self.assertFalse(word in TEMPLATE, word)
        self.assertIn("__AZTREE_DATA__", TEMPLATE)

    def run_page(self, data, view, click=None):
        html = self.render(data).read_text(encoding="utf-8")
        code = html.split("<script>", 1)[1].rsplit("</script>", 1)[0]
        out = aztree.subprocess.run(["node", "-e", FAKE_DOM], input=json.dumps([code, view, click]),
                                    capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        return json.loads(out.stdout)

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_every_view_draws_boxes_and_the_side_panel(self):
        rec = {"problem": "Right-size underused VMs", "solution": "", "resource": RG + "/providers/microsoft.sql/servers/db1",
               "resource_name": "db1", "sku": None, "term": None, "annual_savings": 840.0, "currency": "USD",
               "subscription": "acme-prod", "impact": "High", "resource_type": "x"}
        data = make_data([
            ("SQL Database", "vCore", [10] * 6),
            ("Log Analytics", "Analytics Logs Data Ingestion", [2, 2, 2, 6, 6, 6]),
        ], resource_rows=[(RG, RG + "/providers/microsoft.sql/servers/db1", [10] * 6)], advisor=[rec])
        for view, box, count in (("service", "vCore", "2 services · 2 meters"),
                                 ("subscription", "acme-prod", "1 subscriptions · 2 services"),
                                 ("region", "us central", "1 regions · 2 services"),
                                 ("resource", "db1", "1 resource groups · 1 resources")):
            with self.subTest(view=view):
                page = self.run_page(data, view)
                self.assertGreater(page["leaves"], 0, "no boxes drawn")
                self.assertIn(box, page["boxes"])
                self.assertIn(count, page["sub"])
                self.assertIn("Worth a look", page["side"])
                self.assertIn("Right-size underused VMs", page["side"])
                self.assertIn("$840/yr", page["side"])
                self.assertIn("acme-prod", page["meta"])

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_amounts_use_the_billing_currency(self):
        page = self.run_page(make_data([("Storage", "LRS", [100] * 6)], currency="EUR"), "service")
        self.assertIn("€300", page["sub"])

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_advisor_without_access_says_so(self):
        page = self.run_page(make_data([("Storage", "LRS", [1] * 6)], advisor=[], advisor_error="HTTP 403: denied"), "service")
        self.assertIn("needs Reader", page["side"])


class MainTest(unittest.TestCase):
    def setUp(self):
        self.dir = scratch_dir(self)
        self.saved = self.dir / "aztree-data.json"
        self.saved.write_text(json.dumps(BASIC), encoding="utf-8")

        def no_azure(*a, **k):
            raise AssertionError("must not call Azure")

        self.patches = {"get_token": aztree.get_token, "fetch": aztree.fetch}
        aztree.get_token = aztree.fetch = no_azure

    def tearDown(self):
        for name, fn in self.patches.items():
            setattr(aztree, name, fn)

    def main(self, *argv):
        out = io.StringIO()
        from contextlib import redirect_stdout
        with redirect_stdout(out):
            aztree.main(list(argv))
        return out.getvalue()

    def test_from_saved_data_writes_the_page_without_azure(self):
        page = self.dir / "page.html"
        self.main("--from", str(self.saved), "--out", str(page), "--no-open")
        self.assertIn('"tool":"aztree"', page.read_text(encoding="utf-8"))

    def test_export_writes_the_summary_instead_of_the_page(self):
        target = self.dir / "summary.json"
        self.main("--from", str(self.saved), "--export", str(target))
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["totals"]["current"], 51.0)

    def test_days_out_of_range_dies(self):
        with self.assertRaises(SystemExit):
            quiet(self.main, "--from", str(self.saved), "--days", "0")
        with self.assertRaises(SystemExit):
            quiet(self.main, "--from", str(self.saved), "--days", "184")

    def run_module(self, *args, cwd=None):
        env = dict(os.environ, PYTHONPATH=str(REPO))
        return aztree.subprocess.run([sys.executable, "-m", "aztree", *args], capture_output=True, text=True,
                                     encoding="utf-8", cwd=cwd or REPO, env=env)

    def test_runs_as_a_module(self):
        target = self.dir / "summary.json"
        r = self.run_module("--from", str(self.saved), "--export", str(target))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(target.exists())

    def test_template_loads_from_any_directory(self):
        page = self.dir / "page.html"
        r = self.run_module("--from", str(self.saved), "--out", str(page), "--no-open", cwd=self.dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"tool":"aztree"', page.read_text(encoding="utf-8"))

    def test_version(self):
        out = io.StringIO()
        from contextlib import redirect_stdout
        with redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            aztree.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"aztree {aztree.__version__}")
        self.assertRegex(aztree.__version__, r"^\d+\.\d+\.\d+$")

    def test_scope_and_subscription_do_not_mix(self):
        with self.assertRaises(SystemExit):
            quiet(self.main, "--scope", "/providers/Microsoft.Billing/billingAccounts/1", "--subscription", "x")


class TargetsTest(unittest.TestCase):
    def args(self, **kw):
        return SimpleNamespace(**{"scope": None, "subscription": [], "all": False, **kw})

    def test_scope_is_read_as_given(self):
        targets = aztree.resolve_targets(self.args(scope="providers/Microsoft.Billing/billingAccounts/123/"), az=None)
        self.assertEqual(targets, [{"id": "/providers/Microsoft.Billing/billingAccounts/123",
                                    "name": "/providers/Microsoft.Billing/billingAccounts/123",
                                    "scope": "/providers/Microsoft.Billing/billingAccounts/123", "tenant": None}])

    def test_subscriptions_are_resolved_against_arm(self):
        send = FakeSend((200, {}, json.dumps({"value": [
            {"subscriptionId": "aaaa-1", "displayName": "acme-prod", "state": "Enabled"}]}).encode()))
        targets = aztree.resolve_targets(self.args(subscription=["acme-prod"]), az=client(send))
        self.assertEqual(targets, [{"id": "aaaa-1", "name": "acme-prod", "scope": "/subscriptions/aaaa-1", "tenant": None}])


class DemoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = aztree.demo(30, today=TODAY)
        cls.summary = aztree.summarize(cls.data)

    def test_same_shape_as_real_data(self):
        self.assertEqual(set(self.data["views"]), set(aztree.VIEWS) | {"tag"})  # the demo shows the tag view too
        for v, dims in aztree.VIEWS.items():
            self.assertEqual(self.data["views"][v]["dims"], dims)
        self.assertEqual(len(self.data["days"]), 60)
        self.assertEqual(self.data["days"][-1], "2026-09-27")
        self.assertTrue(self.data["demo"])

    def test_every_view_adds_up_to_the_same_bill(self):
        totals = {v: round(sum(sum(r["d"]) for r in view["rows"]), 2) for v, view in self.data["views"].items()}
        self.assertEqual(len(set(totals.values())), 1, totals)

    def test_ids_are_obviously_fake(self):
        for s in self.data["subscriptions"]:
            self.assertRegex(s["id"], r"^(\d)\1{7}-")

    def test_has_growers_over_forty_percent(self):
        big = [g for g in self.summary["top_growers"] if (g["change_pct"] or 0) >= 40]
        self.assertGreaterEqual(len({g["service"] for g in big}), 2)

    def test_trips_the_azure_rules(self):
        flagged = {f["meter"] for f in self.summary["flags"]}
        for meter in ("D2 v2", "Basic IPv4 Static Public IP", "Analytics Logs Data Ingestion", "P1 v2 App", "LRS Snapshots"):
            self.assertIn(meter, flagged)

    def test_has_advisor_tips_pointing_at_demo_resources(self):
        recs = self.data["advisor"]
        self.assertGreaterEqual(len(recs), 3)
        leaves = {r["k"][1] for r in self.data["views"]["resource"]["rows"]}
        self.assertTrue(any(r["resource"] in leaves for r in recs))

    def test_is_repeatable(self):
        self.assertEqual(aztree.demo(30, today=TODAY), self.data)

    def test_main_demo_needs_no_azure(self):
        page = scratch_dir(self) / "demo.html"
        real = aztree.get_token
        aztree.get_token = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call Azure"))
        try:
            with redirect_stderr(io.StringIO()):
                from contextlib import redirect_stdout
                with redirect_stdout(io.StringIO()):
                    aztree.main(["--demo", "--out", str(page), "--no-open"])
        finally:
            aztree.get_token = real
        self.assertIn('"demo":true', page.read_text(encoding="utf-8"))


class Batch4DemoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = aztree.demo(30, today=TODAY)
        cls.summary = aztree.summarize(cls.data)

    def test_has_an_environment_tag_with_untagged_spend(self):
        view = self.data["views"]["tag"]
        self.assertEqual((view["tag"], view["dims"]), ("environment", ["TagValue", "ServiceName"]))
        self.assertEqual({r["k"][0] for r in view["rows"]}, {"production", "staging", "dev", ""})

    def test_has_a_forecast_for_this_month(self):
        f = self.data["forecast"]
        self.assertEqual(f["month"], "2026-09")
        self.assertGreater(f["actual"], 0)
        self.assertGreater(f["forecast"], 0)
        self.assertEqual(f["total"], round(f["actual"] + f["forecast"], 2))

    def test_has_idle_resources_that_cost_something(self):
        checks = {h["check"] for h in self.summary["hints"] if h["kind"] == "idle"}
        self.assertEqual(checks, {"unattached-disk", "old-snapshot", "unused-ip"})


class Batch4CliTest(unittest.TestCase):
    def run_main(self, *argv):
        from contextlib import redirect_stdout
        seen = {}

        def fake_fetch(az, targets, days, metric, **kw):
            seen.update(kw)
            return make_data([("Storage", "LRS", [1] * 6)])

        with mock.patch.object(aztree, "fetch", fake_fetch), \
                mock.patch.object(aztree, "resolve_targets", lambda *a, **k: [aztree.subscription_target(PROD)]), \
                mock.patch.dict(os.environ, {"AZTREE_HOME": str(scratch_dir(self))}), redirect_stdout(io.StringIO()):
            aztree.main([*argv, "--no-open"])
        return seen

    def test_tag_and_no_graph_reach_fetch(self):
        seen = self.run_main("--tag", "costcenter", "--no-graph")
        self.assertEqual((seen["tag"], seen["graph"]), ("costcenter", False))

    def test_by_default_the_tag_is_picked_and_the_graph_is_read(self):
        seen = self.run_main()
        self.assertEqual((seen["tag"], seen["graph"]), (None, True))


class ReviewFixesTest(unittest.TestCase):
    """Findings from the final review, each reproduced before it was fixed."""

    def test_network_errors_are_retried(self):
        waits, calls = [], []

        def send(method, url, data, headers):
            calls.append(url)
            if len(calls) == 1:
                raise aztree.urllib.error.URLError(ConnectionResetError("connection reset"))
            return page(["UsageDate", "ServiceName", "Meter", "Cost"], [[20260925, "Storage", "LRS", 1.0]])

        rows = aztree.query(client(send, sleep=waits.append), "/subscriptions/s1", "2026-09-22", "2026-09-27",
                            ["ServiceName", "Meter"], "ActualCost")
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(waits), 1)

    def test_network_error_that_persists_is_an_azure_error(self):
        def send(method, url, data, headers):
            raise aztree.urllib.error.URLError("no route to host")

        with self.assertRaises(aztree.AzureError):
            aztree.query(client(send), "/subscriptions/s1", "2026-09-22", "2026-09-27", ["ServiceName", "Meter"], "ActualCost")

    def test_malformed_advisor_data_does_not_lose_the_run(self):
        router = Router(ONE_SUB)

        def send(method, url, data, headers):
            if "Microsoft.Advisor" in url:
                bad = advisor_item("Buy reservation", "/subscriptions/aaaa-1", "aaaa-1", "sku", "P1Y", "N/A")
                return 200, {}, json.dumps({"value": [bad]}).encode()
            return router(method, url, data, headers)

        data = aztree.fetch(client(send), [aztree.subscription_target(PROD)], 3, "ActualCost", advisor=True,
                            log=lambda *a: None, today=TODAY)
        self.assertEqual(data["advisor"], [])
        self.assertTrue(data["advisor_error"])
        self.assertIn(("Storage", "Hot LRS Data Stored"), rows_of(data, "service"))

    def test_falls_back_to_pretaxcost(self):
        data, router = fetch(ONE_SUB, reject={"Cost", "CostUSD"})
        self.assertEqual(rows_of(data, "service")[("Storage", "Hot LRS Data Stored")], [1.0, 0, 0, 0, 0, 2.0])
        aggs = [a["name"] for a in router.bodies[-1]["dataset"]["aggregation"].values()]
        self.assertIn("PreTaxCost", aggs)

    def test_response_without_a_cost_column_is_an_error(self):
        send = FakeSend(page(["UsageDate", "ServiceName", "Meter"], [[20260925, "Storage", "LRS"]]))
        with self.assertRaises(aztree.AzureError):
            aztree.query(client(send), "/subscriptions/s1", "2026-09-22", "2026-09-27", ["ServiceName", "Meter"], "ActualCost")

    def test_ambiguous_subscription_name_dies_listing_ids(self):
        subs = [{"id": "aaaa-1", "name": "Pay-As-You-Go", "state": "Enabled"},
                {"id": "bbbb-2", "name": "Pay-As-You-Go", "state": "Enabled"}]
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            aztree.pick_subscriptions(subs, wanted=["pay-as-you-go"], all_=False, current=None)
        self.assertIn("aaaa-1", err.getvalue())
        self.assertIn("bbbb-2", err.getvalue())

    def test_same_named_subscriptions_get_telling_labels(self):
        tables = {("ServiceName", "Meter"): {"/subscriptions/aaaa-1": [(20260925, "Storage", "LRS", 1.0, "USD", None)],
                                             "/subscriptions/bbbb-2": [(20260925, "Storage", "LRS", 1.0, "USD", None)]},
                  ("ResourceGroupName", "ResourceId"): {
                      "/subscriptions/aaaa-1": [(20260925, "rg-app", RID, 1.0, "USD", None)],
                      "/subscriptions/bbbb-2": [(20260925, "rg-app", RID.replace("aaaa-1", "bbbb-2"), 1.0, "USD", None)]}}
        twins = ({"id": "aaaa-1", "name": "Pay-As-You-Go"}, {"id": "bbbb-2", "name": "Pay-As-You-Go"})
        data, _ = fetch(tables, targets=twins)
        labels = list(data["views"]["subscription"]["names"].values())
        self.assertEqual(len(set(labels)), 2, labels)
        self.assertEqual(len(set(data["views"]["resource"]["names"].values())), 2)

    def test_non_subscription_scope_has_no_advisor_panel(self):
        scope = aztree.scope_target("/providers/Microsoft.Billing/billingAccounts/1")
        data = aztree.fetch(client(Router({})), [scope], 3, "ActualCost", advisor=True, log=lambda *a: None, today=TODAY)
        self.assertIsNone(data["advisor"])

    def test_missing_service_name_does_not_crash(self):
        tables = {("ServiceName", "Meter"): {"/subscriptions/aaaa-1": [(20260925, None, "Something", 1.0, "USD", None)]},
                  ("ResourceLocation", "ServiceName"): {"/subscriptions/aaaa-1": [(20260925, "us central", None, 1.0, "USD", None)]}}
        data, _ = fetch(tables)
        self.assertIn(("(no service)", "Something"), rows_of(data, "service"))
        self.assertIn(("aaaa-1", "(no service)"), rows_of(data, "subscription"))
        self.assertIn(("us central", "(no service)"), rows_of(data, "region"))
        aztree.summarize(data)

    def test_advisor_error_keeps_only_the_status(self):
        router = Router(ONE_SUB)

        def send(method, url, data, headers):
            if "Microsoft.Advisor" in url:
                return error(403, "AuthorizationFailed", "The client 'milan@contoso.com' with object id '0000-1111' "
                                                         "does not have authorization")
            return router(method, url, data, headers)

        data = aztree.fetch(client(send), [aztree.subscription_target(PROD)], 3, "ActualCost", advisor=True,
                            log=lambda *a: None, today=TODAY)
        self.assertEqual(data["advisor_error"], "HTTP 403")
        self.assertNotIn("contoso", json.dumps(aztree.summarize(data)))

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_subscription_level_tip_jumps_to_the_subscription(self):
        rec = {"problem": "Consider a savings plan", "solution": "", "resource": "/subscriptions/aaaa-1",
               "resource_name": "acme-prod", "sku": "Compute_Savings_Plan", "term": "P1Y", "annual_savings": 3120.0,
               "currency": "USD", "subscription": "acme-prod", "impact": "High", "resource_type": None}
        page = ViewerTest.run_page(self, make_data([("SQL Database", "vCore", [10] * 6)], advisor=[rec]), "service",
                                   click="[data-rec]:0")
        self.assertIn("all subscriptions", page["crumbs"])
        self.assertIn('<div class="sel-name">acme-prod</div>', page["side"])

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_a_subscription_links_to_the_portal(self):
        sub = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
        rec = {"problem": "Consider a savings plan", "solution": "", "resource": f"/subscriptions/{sub}",
               "resource_name": "acme-prod", "sku": None, "term": None, "annual_savings": 3120.0,
               "currency": "USD", "subscription": "acme-prod", "impact": "High", "resource_type": None}
        data = make_data([("SQL Database", "vCore", [10] * 6)], advisor=[rec])
        data["views"]["subscription"] = {"dims": ["SubscriptionId", "ServiceName"], "names": {sub: "acme-prod"},
                                         "rows": [{"k": [sub, "SQL Database"], "d": [10] * 6}]}
        page = ViewerTest.run_page(self, data, "service", click="[data-rec]:0")
        self.assertIn(f'<div class="sel-name"><a href="{aztree.PORTAL}/#resource/subscriptions/{sub}"', page["side"])

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_advisor_errors_other_than_access_say_what_happened(self):
        page = ViewerTest.run_page(self, make_data([("Storage", "LRS", [1] * 6)], advisor=[], advisor_error="HTTP 500"), "service")
        self.assertNotIn("needs Reader", page["side"])
        self.assertIn("HTTP 500", page["side"])

    render = ViewerTest.render


class SecondReviewTest(unittest.TestCase):
    """Findings from the second review, each reproduced before it was fixed."""

    def test_usd_is_used_only_when_every_row_has_it(self):
        # prod can't report USD (so it falls back to Cost); dev then refuses Cost and answers in PreTaxCost/PreTaxCostUSD
        tables = {("ServiceName", "Meter"): {
            "/subscriptions/aaaa-1": [(20260925, "Storage", "LRS", 100.0, "EUR", None)],
            "/subscriptions/bbbb-2": [(20260925, "Storage", "LRS", 200.0, "USD", None)],
        }}
        data, _ = fetch(tables, targets=(PROD, DEV),
                        reject_for={"/subscriptions/aaaa-1": {"CostUSD"}, "/subscriptions/bbbb-2": {"Cost"}})
        self.assertEqual(rows_of(data, "service")[("Storage", "LRS")][3], 300.0)

    def test_refund_that_cancels_an_earlier_charge_is_kept(self):
        tables = {("ServiceName", "Meter"): {"/subscriptions/aaaa-1": [
            (20260922, "Azure Cosmos DB", "Reserved 100 RU/s", 100.0, "USD", None),   # previous period: bought
            (20260927, "Azure Cosmos DB", "Reserved 100 RU/s", -100.0, "USD", None),  # this period: refunded
            (20260925, "Storage", "LRS", 5.0, "USD", None),
        ]}}
        data, _ = fetch(tables)
        self.assertIn(("Azure Cosmos DB", "Reserved 100 RU/s"), rows_of(data, "service"))
        totals = aztree.summarize(data)["totals"]
        self.assertEqual((totals["previous"], totals["current"]), (100.0, -95.0))

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_the_no_region_group_opens(self):
        data = make_data([("Storage", "LRS", [1] * 6)])
        data["views"]["region"]["rows"][0]["k"][0] = ""  # Azure leaves ResourceLocation empty for some charges
        page = ViewerTest.run_page(self, data, "region", click={"map": "dblclick", "key": ""})
        self.assertIn('<span class="cur">(no region)</span>', page["crumbs"])

    def test_cli_lists_subscriptions_from_every_tenant(self):
        listing = [{"id": "aaaa-1", "name": "acme-prod", "state": "Enabled", "tenantId": "t-one"},
                   {"id": "bbbb-2", "name": "client-x", "state": "Enabled", "tenantId": "t-two"}]
        calls = []

        def run(cmd, **k):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout=json.dumps(listing), stderr="")

        got = aztree.cli_subscriptions(run=run, az_path="az")
        self.assertEqual([(s["id"], s["tenant"]) for s in got], [("aaaa-1", "t-one"), ("bbbb-2", "t-two")])
        self.assertEqual(calls[0][1:3], ["account", "list"])

    def test_token_for_a_tenant_asks_the_cli_for_that_tenant(self):
        calls = []

        def run(cmd, **k):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="t\n", stderr="")

        aztree.get_token({}, run=run, az_path="az", tenant="t-two")
        self.assertEqual(calls[0][calls[0].index("--tenant") + 1], "t-two")

    def test_picked_subscriptions_keep_their_tenant(self):
        subs = [{"id": "bbbb-2", "name": "client-x", "state": "Enabled", "tenant": "t-two"}]
        picked = aztree.pick_subscriptions(subs, wanted=["client-x"], all_=False, current=None)
        self.assertEqual(aztree.subscription_target(picked[0])["tenant"], "t-two")

    def test_all_reads_the_cli_list_when_given_one(self):
        args = SimpleNamespace(scope=None, subscription=[], all=True)
        cli = lambda: [{"id": "bbbb-2", "name": "client-x", "state": "Enabled", "tenant": "t-two"}]  # noqa: E731
        self.assertEqual(aztree.resolve_targets(args, az=None, cli_list=cli)[0]["tenant"], "t-two")

    def test_each_tenant_gets_its_own_token(self):
        router, seen, asked = Router(ONE_SUB), [], []

        def send(method, url, data, headers):
            seen.append((url.split("/providers/")[0], headers["Authorization"]))
            return router(method, url, data, headers)

        def token(tenant):
            asked.append(tenant)
            return f"tok-{tenant}"

        az = aztree.Azure(token, send=send, sleep=lambda s: None, log=lambda *a: None)
        targets = [aztree.subscription_target({"id": "aaaa-1", "name": "acme-prod", "tenant": "t-one"}),
                   aztree.subscription_target({"id": "bbbb-2", "name": "client-x", "tenant": "t-two"})]
        aztree.fetch(az, targets, 3, "ActualCost", advisor=False, log=lambda *a: None, today=TODAY)
        self.assertEqual(asked, ["t-one", "t-two"])  # once per tenant, then cached
        self.assertEqual({auth for scope, auth in seen if scope.endswith("aaaa-1")}, {"Bearer tok-t-one"})
        self.assertEqual({auth for scope, auth in seen if scope.endswith("bbbb-2")}, {"Bearer tok-t-two"})

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_long_tail_folds_into_one_more_box(self):
        meters = [("Storage", "Hot LRS Data Stored", [1000] * 6)] + [("Storage", f"meter {i}", [0.1] * 6) for i in range(30)]
        page = ViewerTest.run_page(self, make_data(meters), "service")
        self.assertEqual(sorted(b["name"] for b in page["leafBoxes"]), ["+30 more", "Hot LRS Data Stored"])
        self.assertIn("more", next(b["cls"] for b in page["leafBoxes"] if b["name"] == "+30 more"))

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_boxes_too_small_for_padding_drop_it(self):
        page = ViewerTest.run_page(self, make_data([("Storage", "Big", [1000] * 6), ("Storage", "Small", [0.05] * 6)]), "service")
        small = [b for b in page["leafBoxes"] if b["w"] < 14 or b["h"] < 10]
        self.assertTrue(small, page["leafBoxes"])
        for b in small:
            self.assertIn("tiny", b["cls"])
        self.assertRegex(TEMPLATE, r"\.leaf\.tiny\s*\{[^}]*padding:\s*0")

    def test_a_selected_group_stays_under_its_boxes(self):
        # groups and their leaves are siblings; lifting a selected group would cover its leaves
        for rule in aztree.re.findall(r"([^{}]*\.sel[^{}]*)\{([^}]*)\}", TEMPLATE):
            if "z-index" in rule[1]:
                self.assertIn(".leaf.sel", rule[0])

    render = ViewerTest.render


class Batch1Test(unittest.TestCase):
    def test_an_unrelated_400_is_not_retried_with_other_columns(self):
        router = Router(ONE_SUB, fail={("ResourceGroupName", "ResourceId"): "Grouping by ResourceId is not supported here"})
        with self.assertRaises(aztree.AzureError):
            aztree.fetch(client(router), [aztree.subscription_target(PROD)], 3, "ActualCost", advisor=False,
                         log=lambda *a: None, today=TODAY)
        tries = [c for c in router.calls if c[1] == ("ResourceGroupName", "ResourceId")]
        self.assertEqual(len(tries), 1)

    def test_each_subscription_starts_from_the_best_columns(self):
        tables = {("ServiceName", "Meter"): {"/subscriptions/aaaa-1": [(20260925, "Storage", "LRS", 1.0, "USD", None)],
                                             "/subscriptions/bbbb-2": [(20260925, "Storage", "LRS", 1.0, "USD", None)]}}
        _, router = fetch(tables, targets=(PROD, DEV), reject_for={"/subscriptions/aaaa-1": {"CostUSD"}})
        first_dev = next(c for c in router.calls if c[0] == "/subscriptions/bbbb-2")
        self.assertEqual(first_dev[2], ["Cost", "CostUSD"])


    REFUNDED = [("Storage", "LRS", [5] * 6), ("Azure Cosmos DB", "Reservation refund", [0, 0, 0, -2, 0, 0])]

    def test_export_totals_name_credits_and_refunds(self):
        self.assertEqual(aztree.summarize(make_data(self.REFUNDED))["totals"]["credits_and_refunds"], -2.0)
        self.assertEqual(aztree.summarize(BASIC)["totals"]["credits_and_refunds"], 0)
        self.assertIn("credits_and_refunds", aztree.AI_INSTRUCTIONS)

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_page_says_what_credits_the_total_includes(self):
        page = ViewerTest.run_page(self, make_data(self.REFUNDED), "service")
        self.assertIn("-$2.00 credits &amp; refunds", page["sub"])
        self.assertIn("-$2.00 credits &amp; refunds", page["side"])
        plain = ViewerTest.run_page(self, make_data([("Storage", "LRS", [5] * 6)]), "service")
        self.assertNotIn("credits", plain["sub"])

    DROPPED = [("Storage", "Hot LRS Data Stored", [5] * 6), ("Storage", "Old disk", [10, 10, 10, 0, 0, 0]),
               ("SQL Database", "vCore", [20, 20, 20, 5, 5, 5])]

    def test_export_lists_the_biggest_drops(self):
        drops = aztree.summarize(make_data(self.DROPPED))["top_drops"]
        self.assertEqual([(d["meter"], d["change"]) for d in drops], [("vCore", -45.0), ("Old disk", -30.0)])

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_page_lists_the_biggest_drops_even_when_gone(self):
        page = ViewerTest.run_page(self, make_data(self.DROPPED), "service")
        self.assertIn("Biggest drops", page["side"])
        self.assertIn("vCore", page["side"])
        self.assertIn("Old disk", page["side"])  # fell to zero, so it has no box, but it's the drop you want to see
        self.assertIn("down 75%", page["side"])

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_clicking_a_gone_drop_shows_its_numbers(self):
        page = ViewerTest.run_page(self, make_data(self.DROPPED), "service", click="[data-drop]:1")
        self.assertIn('<div class="sel-name">Old disk</div>', page["side"])

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_totals_only_resource_view_says_so_instead_of_a_chart(self):
        data = make_data([("Storage", "LRS", [5] * 6)], resource_rows=[(RG, RG + "/providers/x/y/z", [3, 0, 0, 4, 0, 0])],
                         resource_fallback=["acme-prod"])
        page = ViewerTest.run_page(self, data, "resource")
        self.assertIn("period totals", page["side"])
        self.assertNotIn("<svg", page["side"])
        self.assertIn("period totals", page["sub"])
        self.assertIn("<svg", ViewerTest.run_page(self, data, "service")["side"])  # other views keep their chart

    def test_verbose_counts_pages(self):
        lines = []
        nxt = "https://management.azure.com/subscriptions/s1/providers/Microsoft.CostManagement/query?x=1&$skiptoken=a"
        send = FakeSend(page(["Cost"], [], next_link=nxt), page(["Cost"], []))
        aztree.query(client(send, verbose=True, log=lines.append), "/subscriptions/s1", "2026-09-22", "2026-09-27",
                     ["ResourceGroupName", "ResourceId"], "ActualCost")
        self.assertTrue(any("2 pages" in line for line in lines), lines)

    render = ViewerTest.render


class HomeTest(unittest.TestCase):
    """Output lives in ~/.aztree/ (or AZTREE_HOME), never next to the code."""

    def setUp(self):
        self.home = scratch_dir(self) / "Milanović" / ".aztree"  # doesn't exist yet, non-ASCII on purpose
        patcher = mock.patch.dict(os.environ, {"AZTREE_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def main(self, *argv):
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            aztree.main(list(argv))
        return out.getvalue()

    def test_home_follows_the_environment(self):
        self.assertEqual(aztree.home(), self.home)

    def test_live_run_saves_under_home(self):
        with mock.patch.object(aztree, "resolve_targets", lambda *a, **k: [aztree.subscription_target(PROD)]), \
             mock.patch.object(aztree, "fetch", lambda *a, **k: json.loads(json.dumps(BASIC))):
            printed = self.main("--no-open")
        self.assertTrue((self.home / "aztree-data.json").exists())
        self.assertTrue((self.home / "aztree.html").exists())
        self.assertIn("--from", printed)

    def test_from_without_a_path_reopens_the_last_run(self):
        self.home.mkdir(parents=True)
        (self.home / "aztree-data.json").write_text(json.dumps(BASIC), encoding="utf-8")
        self.main("--from", "--no-open")
        self.assertIn('"tool":"aztree"', (self.home / "aztree.html").read_text(encoding="utf-8"))

    def test_from_without_saved_data_says_where_it_looked(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            self.main("--from")
        self.assertIn(str(self.home / "aztree-data.json"), err.getvalue())

    def test_from_after_a_demo_explains_demo_runs_are_not_saved(self):
        # the README's quick start runs --demo first; the hint must not claim nothing was run
        page = self.home.parent / "demo.html"
        self.main("--demo", "--no-open", "--out", str(page))
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            self.main("--from")
        self.assertIn("--demo runs aren't saved", err.getvalue())


class Batch1ReviewTest(unittest.TestCase):
    """Findings from the batch 1 review, each reproduced before it was fixed."""
    render = ViewerTest.render
    # a reservation refund in the current period, netted inside Cosmos DB in every view but the service view
    REFUND_IN_SERVICE = [("Azure Cosmos DB", "100 RU/s", [0, 0, 0, 50, 50, 50]),
                         ("Azure Cosmos DB", "Reserved 100 RU/s", [0, 0, 0, -100, 0, 0]),
                         ("Storage", "LRS", [5] * 6)]

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_credits_note_is_the_same_in_every_view(self):
        for view in ("service", "subscription", "region"):
            with self.subTest(view=view):
                page = ViewerTest.run_page(self, make_data(self.REFUND_IN_SERVICE), view)
                self.assertIn("-$100 credits &amp; refunds", page["sub"])

    def test_a_refund_is_not_a_drop(self):
        rows = [("Azure Cosmos DB", "Reserved 100 RU/s", [100, 0, 0, -100, 0, 0]), ("Storage", "LRS", [5] * 6)]
        self.assertEqual(aztree.summarize(make_data(rows))["top_drops"], [])

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_a_refund_is_not_a_drop_on_the_page(self):
        rows = [("Azure Cosmos DB", "Reserved 100 RU/s", [100, 0, 0, -100, 0, 0]), ("Storage", "LRS", [5] * 6)]
        self.assertNotIn("Biggest drops", ViewerTest.run_page(self, make_data(rows), "service")["side"])

    def test_errors_that_only_name_cost_management_are_not_retried(self):
        for message in ("Cost Management is not supported for subscription offer type MS-AZR-0145P",
                        "The subscription is not registered for Microsoft.CostManagement"):
            with self.subTest(message=message):
                router = Router(ONE_SUB, fail={("ResourceGroupName", "ResourceId"): message})
                with self.assertRaises(aztree.AzureError):
                    aztree.fetch(client(router), [aztree.subscription_target(PROD)], 3, "ActualCost", advisor=False,
                                 log=lambda *a: None, today=TODAY)
                self.assertEqual(len([c for c in router.calls if c[1] == ("ResourceGroupName", "ResourceId")]), 1)

    def test_demo_shows_drops_and_credits(self):
        s = aztree.summarize(aztree.demo(30, today=TODAY))
        self.assertTrue(s["top_drops"])
        self.assertLess(s["totals"]["credits_and_refunds"], 0)

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_clicking_a_gone_drop_opens_its_service(self):
        page = ViewerTest.run_page(self, make_data(Batch1Test.DROPPED), "service", click="[data-drop]:1")
        self.assertIn('<span class="cur">Storage</span>', page["crumbs"])
        self.assertIn('<div class="sel-name">Old disk</div>', page["side"])


class HintsTest(unittest.TestCase):
    """Every worth-a-look hint is computed once, in summarize(); the page and the AI export both read it."""
    GROWING = [("Log Analytics", "Analytics Logs Data Ingestion", [2, 2, 2, 6, 6, 6]),  # a pit that also grows
               ("SQL Database", "RA-GRS Data Stored", [5, 5, 5, 10, 10, 10]),         # a grower, no rule
               ("Storage", "Hot LRS Data Stored", [1] * 6)]

    def test_a_meter_is_either_a_pit_or_a_grower(self):
        hints = aztree.summarize(make_data(self.GROWING))["hints"]
        self.assertEqual([(h["kind"], h["meter"]) for h in hints],
                         [("grower", "RA-GRS Data Stored"), ("pit", "Analytics Logs Data Ingestion")])
        grower = hints[0]
        self.assertEqual((grower["current"], grower["previous"], grower["change"]), (30.0, 15.0, 15.0))
        self.assertIn("Log Analytics", hints[1]["reason"])

    def test_news_and_to_dos_take_turns(self):
        rows = [("SQL Database", "RA-GRS Data Stored", [5, 5, 5, 20, 20, 20]),        # grower +45
                ("Storage", "Hot LRS Write Operations", [2, 2, 2, 6, 6, 6]),          # grower +12
                ("Log Analytics", "Analytics Logs Data Ingestion", [30] * 6),         # pit 90
                ("Bandwidth", "Standard Data Transfer Out", [10] * 6)]                # pit 30
        kinds = [(h["kind"], h["meter"]) for h in aztree.summarize(make_data(rows))["hints"]]
        self.assertEqual(kinds, [("grower", "RA-GRS Data Stored"), ("pit", "Analytics Logs Data Ingestion"),
                                 ("grower", "Hot LRS Write Operations"), ("pit", "Standard Data Transfer Out")])

    def test_the_page_no_longer_carries_the_rules(self):
        self.assertNotIn("DATA.pits", TEMPLATE)
        out = scratch_dir(self) / "page.html"
        aztree.render(make_data(self.GROWING), out)
        self.assertNotIn('"pits":', out.read_text(encoding="utf-8"))

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_the_page_shows_the_exported_hints(self):
        page = ViewerTest.run_page(self, make_data(self.GROWING), "service")
        self.assertIn("up 100% (+$15.00) vs previous 3d", page["side"])
        self.assertIn("Log Analytics ingestion", page["side"])
        self.assertEqual(page["side"].count('data-hint="'), 2)

    render = ViewerTest.render


def series(n=60, base=5.0, at=None):
    """n daily values of `base`, with {day index: value} overrides."""
    d = [base] * n
    for i, v in (at or {}).items():
        d[i] = v
    return d


class SpikeTest(unittest.TestCase):
    STEADY = ("Storage", "Hot LRS Data Stored", series(base=20))

    def spikes(self, *rows):
        return [h for h in aztree.summarize(make_data([self.STEADY, *rows]))["hints"] if h["kind"] == "spike"]

    def test_a_one_day_charge_is_a_spike(self):
        (spike,) = self.spikes(("Azure Data Factory v2", "Cloud Data Movement", series(at={45: 60.0})))
        data = make_data([self.STEADY])
        self.assertEqual((spike["meter"], spike["date"], spike["day_cost"], spike["usual"], spike["amount"]),
                         ("Cloud Data Movement", data["days"][45], 60.0, 5.0, 55.0))

    def test_a_charge_on_a_meter_that_is_usually_zero_is_a_spike(self):
        (spike,) = self.spikes(("Support", "Professional Direct", series(base=0.0, at={40: 80.0})))
        self.assertEqual(spike["usual"], 0.0)

    def test_a_step_change_is_a_grower_not_a_spike(self):
        hints = aztree.summarize(make_data([self.STEADY, ("SQL Database", "vCore", [5.0] * 35 + [50.0] * 25)]))["hints"]
        self.assertEqual([h["kind"] for h in hints if h["meter"] == "vCore"], ["grower"])

    def test_a_monthly_charge_is_not_a_spike(self):
        self.assertEqual(self.spikes(("Azure DevOps", "Basic Plan", series(base=0.0, at={10: 100.0, 40: 100.0}))), [])

    def test_small_wobbles_are_not_spikes(self):
        self.assertEqual(self.spikes(("Key Vault", "Operations", series(base=0.01, at={45: 0.05}))), [])

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_the_page_says_when_and_how_much(self):
        data = make_data([self.STEADY, ("Azure Data Factory v2", "Cloud Data Movement", series(at={45: 60.0}))])
        page = ViewerTest.run_page(self, data, "service")
        self.assertIn("spiked on Sep 13: $60.00 vs a usual $5.00/day", page["side"])

    def test_the_demo_has_one(self):
        self.assertTrue([h for h in aztree.summarize(aztree.demo(30, today=TODAY))["hints"] if h["kind"] == "spike"])

    render = ViewerTest.render


class DevTestTest(unittest.TestCase):
    DEV = "/subscriptions/aaaa-1/resourcegroups/tms-dev-rg"

    def data(self, group_label="tms-dev-rg", plan=(5,) * 14, group=None, **extra):
        group = group or self.DEV
        rows = [(group, f"{group}/providers/microsoft.web/serverfarms/asp-dev", list(plan)),
                (group, f"{group}/providers/microsoft.sql/servers/s1/elasticpools/pool", [4] * 14),
                (group, f"{group}/providers/microsoft.storage/storageaccounts/stdev", [2] * 14),  # not compute
                (RG, f"{RG}/providers/microsoft.web/serverfarms/asp-prod", [9] * 14)]              # not dev/test
        data = make_data([("Storage", "LRS", [1] * 14)], resource_rows=rows, **extra)
        data["views"]["resource"]["names"][group] = group_label
        return data

    def devtest(self, data):
        return [h for h in aztree.summarize(data)["hints"] if h["kind"] == "devtest"]

    def test_always_on_compute_in_a_dev_group(self):
        (h,) = self.devtest(self.data())
        self.assertEqual((h["group"], h["label"], h["resources"], h["amount"]), (self.DEV, "tms-dev-rg", 2, 63.0))

    def test_something_that_stops_on_some_days_is_not_always_on(self):
        (h,) = self.devtest(self.data(plan=(5,) * 13 + (0,)))
        self.assertEqual(h["resources"], 1)  # only the pool

    def test_dev_must_be_a_word_in_the_name(self):
        group = "/subscriptions/aaaa-1/resourcegroups/rg-devices"
        self.assertEqual(self.devtest(self.data(group_label="rg-devices", group=group)), [])

    def test_a_dev_test_subscription_counts_too(self):
        group = "/subscriptions/aaaa-1/resourcegroups/rg-app2"
        data = self.data(group_label="rg-app2", group=group)
        data["subscriptions"] = [{"id": "aaaa-1", "name": "acme-staging", "currency": "USD"}]
        self.assertEqual(len(self.devtest(data)), 2)  # both groups sit in the staging subscription

    def test_not_judged_from_period_totals(self):
        self.assertEqual(self.devtest(self.data(resource_fallback=["acme-prod"])), [])

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_the_page_names_the_group_and_opens_it(self):
        page = ViewerTest.run_page(self, self.data(), "service")
        self.assertIn("tms-dev-rg", page["side"])
        self.assertIn("always-on", page["side"])
        page = ViewerTest.run_page(self, self.data(), "service", click="[data-hint]:0")
        self.assertIn("all resource groups", page["crumbs"])
        self.assertIn(f'<a href="{aztree.PORTAL}/#resource/subscriptions/aaaa-1/resourcegroups/tms-dev-rg" '
                      'target="_blank" rel="noopener noreferrer" title="open in the Azure portal">tms-dev-rg</a></div>',
                      page["side"])

    render = ViewerTest.render


def tip(problem, sku=None, savings=100.0, resource="/subscriptions/aaaa-1"):
    return {"problem": problem, "solution": "", "impact": "High", "resource": resource, "resource_name": "acme-prod",
            "resource_type": None, "sku": sku, "term": "P1Y", "annual_savings": savings, "currency": "USD",
            "subscription": "acme-prod"}


RESERVE_SQL = "Consider SQL PaaS DB reserved instance to save over the pay-as-you-go costs"
RESERVE_APP = "Consider App Service reserved instance to save over the on-demand costs"
RESERVE_COSMOS = "Consider Cosmos DB reserved instance to save over the pay-as-you-go costs"
SAVINGS_PLAN = "Consider purchasing a savings plan to unlock lower prices"
BILL = [("SQL Database", "vCore", [40] * 6), ("SQL Database", "eDTUs", [5] * 6),
        ("Azure App Service", "P1 v3 App", [20] * 6), ("Azure App Service", "P0v3 App", [4] * 6),
        ("Azure App Service", "S2 App", [5] * 6), ("Azure App Service", "B1 App", [4] * 6),
        ("Functions", "Premium vCPU Duration", [12] * 6), ("Azure Cosmos DB", "100 RU/s", [11] * 6),
        ("Virtual Machines", "D4s v5", [9] * 6), ("Storage", "Hot LRS Data Stored", [5] * 6)]


class AdvisorLinkTest(unittest.TestCase):
    def covers(self, rec):
        (linked,) = aztree.summarize(make_data(BILL, advisor=[rec]))["advisor"]
        return [c["meter"] for c in linked["covers"]]

    def test_a_reservation_tip_names_the_meters_it_covers(self):
        self.assertEqual(self.covers(tip(RESERVE_SQL, "SQL DB Single/Elastic Pool - General Purpose - Gen 5")), ["vCore"])
        self.assertEqual(self.covers(tip(RESERVE_APP, "Standard_P1_v3_Windows")), ["P1 v3 App", "P0v3 App"])
        self.assertEqual(self.covers(tip(RESERVE_COSMOS, "100 RU/s")), ["100 RU/s"])

    def test_a_savings_plan_covers_what_its_sku_says(self):
        self.assertEqual(self.covers(tip(SAVINGS_PLAN, "Compute_Savings_Plan")),
                         ["P1 v3 App", "Premium vCPU Duration", "D4s v5", "P0v3 App"])
        self.assertEqual(self.covers(tip(SAVINGS_PLAN, "Database_Savings_Plan")), ["vCore", "100 RU/s"])

    def test_other_tips_cover_nothing(self):
        self.assertEqual(self.covers(tip("Disable health probes when there's only one origin in an origin group", savings=None)), [])

    def test_the_covered_amount_is_a_monthly_pace(self):
        (linked,) = aztree.summarize(make_data(BILL, advisor=[tip(RESERVE_SQL)]))["advisor"]
        self.assertEqual(linked["covers_monthly"], round(120 / 3 * 30.4, 2))

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_the_page_says_what_a_tip_covers_and_goes_there(self):
        data = make_data(BILL, advisor=[tip(RESERVE_SQL, "SQL DB")])
        page = ViewerTest.run_page(self, data, "service")
        self.assertIn("covers SQL Database · vCore ($1,216/mo)", page["side"])
        page = ViewerTest.run_page(self, data, "service", click="[data-rec]:0")
        self.assertIn('<span class="cur">SQL Database</span>', page["crumbs"])
        self.assertIn('<div class="sel-name">vCore</div>', page["side"])

    render = ViewerTest.render


class SteadyTest(unittest.TestCase):
    def steady(self, data):
        return [(h["service"], h["monthly"]) for h in aztree.summarize(data)["hints"] if h["kind"] == "steady"]

    def test_steady_reservable_spend_without_advisor(self):
        found = dict(self.steady(make_data(weeks(BILL), advisor=None)))
        self.assertEqual(found["SQL Database"], round(280 / 7 * 30.4, 2))  # vCore only: DTUs can't be reserved
        self.assertEqual(found["Azure App Service"], round(168 / 7 * 30.4, 2))  # P1 v3 + P0v3, not S2 or B1
        self.assertNotIn("Storage", found)

    def test_hidden_when_advisor_has_commitment_tips(self):
        self.assertEqual(self.steady(make_data(weeks(BILL), advisor=[tip(SAVINGS_PLAN, "Compute_Savings_Plan")])), [])

    def test_shown_when_advisor_failed(self):
        self.assertTrue(self.steady(make_data(weeks(BILL), advisor=[], advisor_error="HTTP 403")))

    def test_spend_that_moves_is_not_steady(self):
        rows = [("SQL Database", "vCore", [40] * 7 + [40, 20, 60, 30, 50, 20, 60])]
        self.assertEqual(self.steady(make_data(rows, advisor=None)), [])

    def test_small_spend_is_not_worth_committing(self):
        self.assertEqual(self.steady(make_data([("SQL Database", "vCore", [3] * 14)], advisor=None)), [])  # ~$91/mo

    def test_amortized_cost_hides_it(self):
        # reserved usage already looks flat under AmortizedCost; Advisor knows what's reserved, this can't
        self.assertEqual(self.steady(make_data(weeks(BILL), advisor=None, metric="AmortizedCost")), [])

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_the_page_shows_it_and_opens_the_service(self):
        data = make_data([("SQL Database", "vCore", [40] * 14)], advisor=None)
        page = ViewerTest.run_page(self, data, "service")
        self.assertIn("steady", page["side"])
        page = ViewerTest.run_page(self, data, "service", click="[data-hint]:0")
        self.assertIn('<div class="sel-name">SQL Database</div>', page["side"])

    render = ViewerTest.render


def weeks(rows, n=14):
    """The same flat rows over n days (split in the middle): steady and dev/test need a week of current days."""
    return [(s, m, [d[0]] * n) for s, m, d in rows]


class Batch3ReviewTest(unittest.TestCase):
    """Findings from the batch 3 review, each reproduced before it was fixed."""

    def test_steady_hints_rank_by_the_same_dollars_as_other_to_dos(self):
        rows = [("SQL Database", "vCore", [40] * 14), ("Log Analytics", "Analytics Logs Data Ingestion", [50] * 14)]
        todos = [h["kind"] for h in aztree.summarize(make_data(rows, advisor=None))["hints"]]
        self.assertEqual(todos, ["pit", "steady"])  # $350 of ingestion this week before $280 of vCore

    def test_a_synapse_reservation_is_not_sql_database(self):
        rec = tip("Consider Azure Synapse Analytics (formerly SQL DW) reserved instance to save over the pay-as-you-go costs")
        (linked,) = aztree.summarize(make_data(BILL, advisor=[rec]))["advisor"]
        self.assertEqual(linked["covers"], [])

    def test_spot_vms_are_not_covered_by_commitments(self):
        rows = BILL + [("Virtual Machines", "D2s v5 Spot", [3] * 6), ("Virtual Machines", "D2 v3 Low Priority", [2] * 6)]
        (linked,) = aztree.summarize(make_data(rows, advisor=[tip(SAVINGS_PLAN, "Compute_Savings_Plan")]))["advisor"]
        self.assertNotIn("D2s v5 Spot", [c["meter"] for c in linked["covers"]])
        self.assertNotIn("D2 v3 Low Priority", [c["meter"] for c in linked["covers"]])

    def test_no_spike_on_a_meter_that_nets_to_nothing(self):
        rows = [SpikeTest.STEADY, ("Azure Cosmos DB", "Reserved 100 RU/s", series(base=0.0, at={10: 5.0, 45: 60.0, 50: -60.0}))]
        self.assertEqual([h for h in aztree.summarize(make_data(rows))["hints"] if h["kind"] == "spike"], [])

    def test_short_periods_make_no_steady_or_dev_test_hints(self):
        kinds = {h["kind"] for h in aztree.summarize(make_data(BILL, advisor=None))["hints"]}  # 3 current days
        self.assertFalse(kinds & {"steady", "devtest"})

    def test_more_dev_test_names(self):
        for name in ("rg-devtest", "testing-rg", "rg-development", "rg-non-prod", "rg-preprod", "rg-pre-prod"):
            self.assertTrue(aztree.DEV_TEST.search(name), name)
        for name in ("rg-devices", "contest", "rg-devops", "latest", "pentest"):
            self.assertFalse(aztree.DEV_TEST.search(name), name)

    def test_the_demo_has_a_dev_test_group(self):
        hints = aztree.summarize(aztree.demo(30, today=TODAY))["hints"]
        self.assertTrue([h for h in hints if h["kind"] == "devtest"])

    def test_covers_is_explained_for_multi_subscription_runs(self):
        self.assertIn("across the whole bill", aztree.AI_INSTRUCTIONS)

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_dev_test_text_claims_only_what_was_measured(self):
        page = ViewerTest.run_page(self, DevTestTest().data(), "service")
        self.assertNotIn("168 hours", page["side"])
        self.assertIn("every day", page["side"])

    render = ViewerTest.render


class LedgerStyleTest(unittest.TestCase):
    """The side panel reads like a statement: plain rows, hairline dividers, a small kind tag, no accent bars."""

    def test_no_accent_bars_on_rows_or_the_selection_title(self):
        for rule in (".hint", ".sel-name"):
            block = aztree.re.search(aztree.re.escape(rule) + r"\s*\{([^}]*)\}", TEMPLATE)
            self.assertIsNotNone(block, rule)
            self.assertNotIn("border-left", block.group(1), rule)

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_each_row_says_what_kind_of_hint_it_is(self):
        page = ViewerTest.run_page(self, make_data(HintsTest.GROWING), "service")
        self.assertIn('<span class="tag">grew</span>', page["side"])
        self.assertIn('<span class="tag">fix</span>', page["side"])
        self.assertNotIn('class="hb"', page["side"])  # no share bars in the rows either

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_drops_say_fell_or_gone(self):
        page = ViewerTest.run_page(self, make_data(Batch1Test.DROPPED), "service")
        self.assertIn('<span class="tag">fell</span>', page["side"])
        self.assertIn('<span class="tag">gone</span>', page["side"])

    render = ViewerTest.render


@unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
class ShowAllTest(unittest.TestCase):
    render = ViewerTest.render
    run_page = ViewerTest.run_page
    # nine flagged meters, all big enough to show: more than the panel's first screen
    PITS9 = [("Log Analytics", "Analytics Logs Data Ingestion", [30] * 6), ("Azure Cosmos DB", "100 RU/s", [20] * 6),
             ("Azure Front Door Service", "Premium Base Fees", [11] * 6), ("SQL Database", "eDTUs", [10] * 6),
             ("SQL Database", "S2 DTUs", [9] * 6), ("Azure DevOps", "Basic User", [8] * 6),
             ("Virtual Network", "Standard Private Endpoint", [7] * 6), ("Bandwidth", "Standard Data Transfer Out", [6] * 6),
             ("Azure Monitor", "Alerts System Log Monitored at 1 Minute Frequency", [5] * 6)]
    RECS = [{"problem": f"Tip {i}", "solution": "", "resource": "/subscriptions/aaaa-1", "resource_name": "acme-prod",
             "sku": None, "term": None, "annual_savings": 100.0 - i, "currency": "USD", "subscription": "acme-prod"}
            for i in range(8)]

    def test_worth_a_look_shows_six_then_all(self):
        # with an Advisor commitment tip there's no steady-spend hint, so the nine pits are the whole list
        data = make_data(self.PITS9, advisor=[tip(SAVINGS_PLAN, "Database_Savings_Plan")])
        page = self.run_page(data, "service")
        self.assertEqual(page["side"].count('data-hint="'), 6)
        self.assertIn("show all 9", page["side"])
        page = self.run_page(data, "service", click="[data-more]:hints")
        self.assertEqual(page["side"].count('data-hint="'), 9)
        self.assertIn("show fewer", page["side"])

    def test_advisor_shows_five_then_all(self):
        page = self.run_page(make_data([("Storage", "LRS", [5] * 6)], advisor=self.RECS), "service")
        self.assertEqual(page["side"].count('data-rec="'), 5)
        self.assertIn("show all 8", page["side"])
        page = self.run_page(make_data([("Storage", "LRS", [5] * 6)], advisor=self.RECS), "service", click="[data-more]:recs")
        self.assertEqual(page["side"].count('data-rec="'), 8)


@unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
class ViewerEdgesTest(unittest.TestCase):
    render = ViewerTest.render
    run_page = ViewerTest.run_page

    def test_drops_are_formatted_like_rises(self):
        page = self.run_page(make_data([("Storage", "LRS", [2, 2, 2, 1, 1, 1])]), "service")
        self.assertIn("-50%", page["side"])
        self.assertNotIn("-50.0%", page["side"])

    def test_odd_url_hashes_fall_back_to_the_service_view(self):
        for view in ("constructor", "__proto__", "%E0"):
            with self.subTest(view=view):
                page = self.run_page(make_data([("Storage", "LRS", [1] * 6)]), view)
                self.assertIn("all services", page["crumbs"])

    def test_unknown_term_is_not_looked_up_on_the_prototype(self):
        rec = {"problem": "Buy a reservation", "solution": "", "resource": "/subscriptions/aaaa-1", "resource_name": "acme-prod",
               "sku": "x", "term": "constructor", "annual_savings": 10.0, "currency": "USD", "subscription": "acme-prod"}
        page = self.run_page(make_data([("Storage", "LRS", [1] * 6)], advisor=[rec]), "service")
        self.assertNotIn("native code", page["side"])


@unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
class Batch4PageTest(unittest.TestCase):
    render = ViewerTest.render
    run_page = ViewerTest.run_page
    F = {"month": "2026-09", "actual": 150.0, "forecast": 85.0, "total": 235.0}

    def tagged(self, tag="environment"):
        data = make_data([("Storage", "LRS", [5] * 6), ("SQL Database", "vCore", [10] * 6)])
        data["views"]["tag"] = {"dims": ["TagValue", "ServiceName"], "names": {}, "tag": tag,
                                "rows": [{"k": ["prod", "SQL Database"], "d": [10] * 6}, {"k": ["", "Storage"], "d": [5] * 6}]}
        return data

    def test_the_tag_view_has_a_button_named_after_the_tag(self):
        page = self.run_page(self.tagged(), "service")
        self.assertIn('<button data-v="tag">environment</button>', page["views"])
        self.assertIn("<kbd>5</kbd>", page["viewkeys"])
        plain = self.run_page(make_data([("Storage", "LRS", [5] * 6)]), "service")
        self.assertNotIn('data-v="tag"', plain["views"])
        self.assertIn('<button data-v="resource">Resource</button>', plain["views"])
        self.assertNotIn("<kbd>5</kbd>", plain["viewkeys"])

    def test_key_5_and_the_hash_open_the_tag_view(self):
        for page in (self.run_page(self.tagged(), "service", click={"press": "5"}), self.run_page(self.tagged(), "tag")):
            self.assertIn("all environment values", page["crumbs"])
            self.assertIn("(untagged)", page["boxes"])
            self.assertIn("untagged $15.00 (33% of bill)", page["sub"])

    def test_without_a_tag_view_key_5_does_nothing(self):
        page = self.run_page(make_data([("Storage", "LRS", [5] * 6)]), "service", click={"press": "5"})
        self.assertIn("all services", page["crumbs"])

    def test_a_tag_name_cannot_inject_html(self):
        page = self.run_page(self.tagged(tag='<img src=x onerror="alert(1)">'), "tag")
        for part in ("views", "crumbs", "sub"):
            self.assertNotIn("<img", page[part], part)
        self.assertIn("&lt;img", page["crumbs"])

    def test_the_whole_bill_shows_this_months_forecast(self):
        page = self.run_page(make_data([("Storage", "LRS", [5] * 6)], forecast=self.F), "service")
        self.assertIn("<span>Sep so far</span>$150", page["side"])
        self.assertIn("<span>Sep forecast</span>$235", page["side"])
        self.assertNotIn("whole bill", page["side"])

    def test_a_selection_shows_its_own_kind_instead(self):
        data = make_data([("Log Analytics", "Analytics Logs Data Ingestion", [2, 2, 2, 6, 6, 6])], forecast=self.F)
        page = self.run_page(data, "service", click="[data-hint]:0")
        self.assertNotIn("so far", page["side"])
        self.assertIn("<span>kind</span>meter", page["side"])

    def test_no_forecast_keeps_the_old_cells(self):
        page = self.run_page(make_data([("Storage", "LRS", [5] * 6)]), "service")
        self.assertIn("<span>kind</span>whole bill", page["side"])

    def idle_data(self):
        disk = RG + "/providers/microsoft.compute/disks/d1"
        graph = [{"check": "unattached-disk", "id": disk, "name": "d1", "resourceGroup": "rg-app", "subscriptionId": "aaaa-1"}]
        return make_data([("Storage", "P10 LRS Disk", [3] * 6)], resource_rows=[(RG, disk, [3] * 6)], graph=graph)

    def test_an_idle_resource_is_a_row_that_opens_it(self):
        page = self.run_page(self.idle_data(), "service")
        self.assertIn('<span class="tag">idle</span>unattached disk: attached to no VM', page["side"])
        self.assertIn('title="' + RG + '/providers/microsoft.compute/disks/d1">d1</span>', page["side"])
        page = self.run_page(self.idle_data(), "service", click="[data-hint]:0")
        self.assertIn("all resource groups", page["crumbs"])
        self.assertIn(f'<div class="sel-name"><a href="{aztree.PORTAL}/#resource' + RG
                      + '/providers/microsoft.compute/disks/d1" target="_blank"', page["side"])
        self.assertIn('title="open in the Azure portal">d1</a></div>', page["side"])

    def test_graph_errors_get_a_note(self):
        page = self.run_page(make_data([("Storage", "LRS", [5] * 6)], graph=[], graph_error="HTTP 403"), "service")
        self.assertIn("Resource Graph checks need Reader", page["side"])
        page = self.run_page(make_data([("Storage", "LRS", [5] * 6)], graph=[], graph_error="HTTP 500"), "service")
        self.assertIn("didn't run everywhere (HTTP 500)", page["side"])  # a tenant may have answered


@unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
class MapLookTest(unittest.TestCase):
    render = ViewerTest.render
    run_page = ViewerTest.run_page

    def test_every_hued_category_shares_one_lightness_and_chroma(self):
        found = dict(aztree.re.findall(r"--(\w+):\s*oklch\(([^)]*)\)", TEMPLATE))
        hued = [found[k].split() for k in ("compute", "storage", "database", "network", "ai", "analytics", "ops")]
        self.assertEqual({(l, c) for l, c, _ in hued}, {("0.72", "0.11")})
        self.assertEqual(len({h for _, _, h in hued}), 7)
        self.assertIn("other", found)

    def test_boxes_have_no_outline_and_the_selection_is_amber(self):
        leaf = aztree.re.search(r"\n\.leaf \{([^}]*)\}", TEMPLATE).group(1)
        self.assertNotIn("border", leaf)
        self.assertRegex(TEMPLATE, r"\.cell\.sel \{[^}]*outline: 2px solid var\(--accent\)")
        self.assertNotRegex(TEMPLATE, r"\.leaf:hover \{[^}]*background:")  # the shorthand would wipe a hatch

    def test_to_dos_are_hatched_where_their_row_jumps(self):
        # a pit (Log Analytics ingestion) and steady spend (SQL vCore, no Advisor) in the service view
        data = make_data([("Log Analytics", "Analytics Logs Data Ingestion", [30] * 14), ("SQL Database", "vCore", [40] * 14),
                          ("Storage", "Hot LRS Data Stored", [5] * 14)], advisor=None)
        drawn = {d["name"]: d["cls"] for d in self.run_page(data, "service")["drawn"]}
        self.assertIn("todo", drawn["Analytics Logs Data Ingestion"].split())
        self.assertIn("todo", drawn["vCore"].split())
        self.assertNotIn("todo", drawn["Hot LRS Data Stored"].split())
        self.assertNotIn("todo", drawn["SQL Database"].split())  # groups aren't hatched, their boxes are
        region = self.run_page(data, "region")["drawn"]
        self.assertFalse([d for d in region if "todo" in d["cls"].split()])  # no meters there to match

    def test_idle_and_dev_test_boxes_are_hatched_in_the_resource_view(self):
        page = self.run_page(Batch4PageTest.idle_data(self), "resource")
        self.assertIn("todo", {d["name"]: d["cls"] for d in page["drawn"]}["d1"].split())
        dev = self.run_page(DevTestTest().data(), "resource")["drawn"]
        hatched = {d["name"] for d in dev if "todo" in d["cls"].split()}
        self.assertEqual(hatched, {"asp-dev", "pool", "stdev"})  # every box of the dev/test group

    def test_the_more_box_is_never_hatched(self):
        meters = [("Log Analytics", "Analytics Logs Data Ingestion", [1000] * 6)] + [("Log Analytics", f"m {i}", [0.1] * 6) for i in range(30)]
        more = [d for d in self.run_page(make_data(meters), "service")["drawn"] if d["name"] == "+30 more"]
        self.assertEqual(len(more), 1)
        self.assertNotIn("todo", more[0]["cls"].split())

    def test_the_legend_has_a_to_do_swatch_in_both_colour_modes(self):
        data = make_data([("Log Analytics", "Analytics Logs Data Ingestion", [30] * 6)])
        self.assertIn('<i class="todo"></i>to-do', self.run_page(data, "service")["sub"])
        self.assertIn('<i class="todo"></i>to-do', self.run_page(data, "service", click={"press": "c"})["sub"])
        self.assertNotIn("to-do", self.run_page(data, "region")["sub"])  # nothing hatched there


@unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
class FilterDimTest(unittest.TestCase):
    render = ViewerTest.render
    run_page = ViewerTest.run_page
    DATA = [("SQL Database", "vCore", [10] * 6), ("Storage", "Hot LRS Data Stored", [5] * 6),
            ("Storage", "LRS Snapshots", [2] * 6)]

    def cls(self, page):
        return {d["name"]: d["cls"].split() for d in page["drawn"]}

    def test_typing_dims_what_does_not_match_and_keeps_it_drawn(self):
        cls = self.cls(self.run_page(make_data(self.DATA), "service", click={"type": "snap"}))
        self.assertIn("dim", cls["vCore"])
        self.assertIn("dim", cls["SQL Database"])
        self.assertNotIn("dim", cls["LRS Snapshots"])
        self.assertNotIn("dim", cls["Storage"])  # holds a match
        self.assertIn("dim", cls["Hot LRS Data Stored"])

    def test_the_readout_counts_matches_in_the_views_own_words(self):
        page = self.run_page(make_data(self.DATA), "service", click={"type": "lrs"})
        self.assertIn("filter “lrs” · 2 meters · $21.00 · 41% of bill", page["sub"])
        page = self.run_page(make_data(self.DATA), "subscription", click={"type": "stor"})
        self.assertIn("1 service ·", page["sub"])
        page = self.run_page(make_data(self.DATA), "service", click={"type": "zzz"})
        self.assertIn("no match", page["sub"])

    def test_enter_keeps_only_the_matches_and_esc_clears(self):
        page = self.run_page(make_data(self.DATA), "service", click=[{"type": "snap"}, {"filterKey": "Enter"}])
        self.assertEqual([d["name"] for d in page["drawn"]], ["Storage", "LRS Snapshots"])
        page = self.run_page(make_data(self.DATA), "service", click=[{"type": "snap"}, {"filterKey": "Enter"}, {"filterKey": "Escape"}])
        self.assertEqual(len(page["drawn"]), 5)
        self.assertFalse([d for d in page["drawn"] if "dim" in d["cls"].split()])

    def test_the_selection_survives_typing(self):
        page = self.run_page(make_data(self.DATA), "service", click=[{"map": "click", "name": "vCore"}, {"type": "snap"}])
        self.assertIn('<div class="sel-name">vCore</div>', page["side"])


@unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
class NavKeysTest(unittest.TestCase):
    render = ViewerTest.render
    run_page = ViewerTest.run_page
    DATA = FilterDimTest.DATA

    def sel(self, page):
        return aztree.re.search(r'<div class="sel-name">([^<]*)</div>', page["side"]).group(1)

    def test_clicking_the_selected_box_opens_it(self):
        page = self.run_page(make_data(self.DATA), "service", click=[{"map": "click", "name": "Storage"}] * 2)
        self.assertIn('<span class="cur">Storage</span>', page["crumbs"])

    def test_no_double_click_listener_opens_twice(self):
        self.assertNotIn('addEventListener("dblclick"', TEMPLATE)

    def test_tab_walks_by_size_and_shift_tab_back(self):
        tab = {"press": "Tab", "from": "map"}  # the map walks its boxes when it has focus
        page = self.run_page(make_data(self.DATA), "service", click=[tab])
        self.assertEqual(self.sel(page), "SQL Database")  # the largest box
        page = self.run_page(make_data(self.DATA), "service", click=[tab] * 2)
        self.assertEqual(self.sel(page), "Storage")
        page = self.run_page(make_data(self.DATA), "service", click=[tab] * 2 + [{**tab, "shift": True}])
        self.assertEqual(self.sel(page), "SQL Database")
        page = self.run_page(make_data(self.DATA), "service", click=[{"map": "click", "name": "Hot LRS Data Stored"}, tab])
        self.assertEqual(self.sel(page), "LRS Snapshots")  # among its siblings

    def test_enter_with_nothing_selected_picks_the_largest(self):
        self.assertEqual(self.sel(self.run_page(make_data(self.DATA), "service", click={"press": "Enter"})), "SQL Database")

    def test_escape_clears_the_selection_then_goes_up(self):
        opened = [{"map": "click", "name": "Storage"}] * 2 + [{"map": "click", "name": "LRS Snapshots"}]
        page = self.run_page(make_data(self.DATA), "service", click=opened + [{"press": "Escape"}])
        self.assertIn('<span class="cur">Storage</span>', page["crumbs"])  # still inside
        self.assertIn('<div class="sel-name">Storage</div>', page["side"])  # the zoomed group, not the leaf
        page = self.run_page(make_data(self.DATA), "service", click=opened + [{"press": "Escape"}] * 2)
        self.assertIn("all services", page["crumbs"])


@unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
class HistoryTest(unittest.TestCase):
    render = ViewerTest.render
    run_page = ViewerTest.run_page
    DATA = FilterDimTest.DATA

    def test_zooms_and_jumps_push_and_selecting_replaces(self):
        page = self.run_page(make_data(self.DATA), "service", click=[{"map": "click", "name": "Storage"}] * 2)
        self.assertEqual([c[0] for c in page["history"]], ["replace", "replace", "push"])  # load, select, open
        self.assertEqual(page["history"][-1][1], {"view": "service", "zoom": "Storage", "more": 0, "sel": ["Storage", None]})
        self.assertEqual(page["history"][-1][2], "#service")

    def test_back_restores_view_zoom_and_selection(self):
        state = {"view": "service", "zoom": "Storage", "sel": ["Storage", "LRS Snapshots"]}
        page = self.run_page(make_data(self.DATA), "region", click={"pop": state})
        self.assertIn('<span class="cur">Storage</span>', page["crumbs"])
        self.assertIn('<div class="sel-name">LRS Snapshots</div>', page["side"])

    def test_the_empty_zoom_key_survives_the_round_trip(self):
        data = make_data([("Storage", "LRS", [1] * 6)])
        data["views"]["region"]["rows"] = [{"k": ["", "Storage"], "d": [1] * 6}]
        page = self.run_page(data, "service", click={"pop": {"view": "region", "zoom": "", "sel": None}})
        self.assertIn('<span class="cur">(no region)</span>', page["crumbs"])

    def test_a_zoom_that_is_gone_falls_back_to_the_top(self):
        page = self.run_page(make_data(self.DATA), "service", click={"pop": {"view": "service", "zoom": "Nope", "sel": ["Nope", "x"]}})
        self.assertIn("all services", page["crumbs"])
        self.assertIn('<div class="sel-name">Everything</div>', page["side"])

    def test_a_hint_jump_is_a_step_back_can_undo(self):
        data = make_data([("Log Analytics", "Analytics Logs Data Ingestion", [30] * 6)])
        page = self.run_page(data, "resource", click="[data-hint]:0")
        self.assertEqual(page["history"][-1][0], "push")
        self.assertEqual(page["history"][-1][1]["view"], "service")


@unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
class ZoomAnimationTest(unittest.TestCase):
    render = ViewerTest.render
    run_page = ViewerTest.run_page

    def test_opening_and_going_up_animate_the_map(self):
        data = make_data(FilterDimTest.DATA)
        opened = [{"map": "click", "name": "Storage"}] * 2
        self.assertEqual(self.run_page(data, "service", click=opened)["animations"], 1)
        self.assertEqual(self.run_page(data, "service", click=opened + [{"press": "Backspace"}])["animations"], 2)
        self.assertEqual(self.run_page(data, "service", click="[data-hint]:0")["animations"], 0)  # a jump doesn't

    def test_reduced_motion_and_resizing_stop_it(self):
        self.assertIn("prefers-reduced-motion: reduce", TEMPLATE)
        self.assertRegex(TEMPLATE, r"new ResizeObserver\(\(\) => \{ anim\?\.cancel\(\);")


@unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
class PanelTest(unittest.TestCase):
    render = ViewerTest.render
    run_page = ViewerTest.run_page

    def test_the_advisor_header_sticks_to_the_bottom_of_the_panel(self):
        side = self.run_page(make_data([("Storage", "LRS", [5] * 6)], advisor=ShowAllTest.RECS), "service")["side"]
        i = side.index('class="advh')
        self.assertEqual(side[:i].count("<section"), side[:i].count("</section>"))  # a direct child of the aside
        self.assertRegex(TEMPLATE, r"\.advh \{[^}]*position: sticky; bottom: 0")
        self.assertRegex(TEMPLATE, r"@media \(min-height: 900px\) \{[^}]*#side > section:first-child \{[^}]*position: sticky")

    def test_the_tooltip_gives_share_of_parent_for_a_box(self):
        page = self.run_page(make_data(FilterDimTest.DATA), "service", click={"hover": "LRS Snapshots"})
        self.assertIn("29% of Storage", page["tip"])


@unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
class Batch5ReviewTest(unittest.TestCase):
    """Findings from the batch 5 review, each reproduced before it was fixed."""
    render = ViewerTest.render
    run_page = ViewerTest.run_page
    DATA = FilterDimTest.DATA

    def test_a_wrapper_clips_the_map_while_it_animates(self):
        # the transform is on #map, so #map's own overflow moves with it; going up would paint over the panel
        self.assertIn('<div id="mapbox"><div id="map" tabindex="0"></div></div>', TEMPLATE)
        self.assertRegex(TEMPLATE, r"#mapbox \{[^}]*overflow: hidden")

    def test_tab_after_opening_walks_the_opened_group(self):
        page = self.run_page(make_data(self.DATA), "service", click=[{"map": "click", "name": "Storage"}] * 2 + [{"press": "Tab", "from": "map"}])
        self.assertIn('<div class="sel-name">Hot LRS Data Stored</div>', page["side"])

    def test_nothing_shows_under_the_sticky_advisor_header(self):
        self.assertRegex(TEMPLATE, r"\naside \{[^}]*padding: 14px 16px 0;")
        self.assertRegex(TEMPLATE, r"#side > section:last-child \{[^}]*padding-bottom: 14px")

    def test_a_more_box_holding_a_match_is_not_dimmed(self):
        meters = [("Log Analytics", "Analytics Logs Data Ingestion", [1000] * 6)] + [("Log Analytics", f"m {i}", [0.1] * 6) for i in range(30)]
        more = [d for d in self.run_page(make_data(meters), "service", click={"type": "m 3"})["drawn"] if d["name"] == "+30 more"]
        self.assertNotIn("dim", more[0]["cls"].split())

    def test_the_legend_drops_whole_entries_instead_of_cutting_a_word(self):
        self.assertRegex(TEMPLATE, r"\.legend \{[^}]*flex-wrap: wrap")


class DeferredMinorsTest(unittest.TestCase):
    """The small findings deferred from the batch 4 and 5 reviews, each reproduced before it was fixed."""
    render = ViewerTest.render
    run_page = ViewerTest.run_page

    def test_the_demo_forecast_on_the_first_counts_the_months_days(self):
        data = aztree.demo(30, today=aztree.dt.date(2026, 10, 1))  # the demo's last day is Sep 29
        daily = [sum(r["d"][i] for r in data["views"]["service"]["rows"]) for i in range(len(data["days"]))]
        f = data["forecast"]
        self.assertEqual((f["month"], f["actual"]), ("2026-10", 0))
        self.assertEqual(f["forecast"], round(sum(daily[-7:]) / 7 * 31, 2))  # Oct 1-31, not Sep 30 too

    def test_an_expired_login_is_told_to_log_in_not_to_pass_a_tag(self):
        lines = []
        aztree.choose_tag(client(Router({}, tag_names={"aaaa-1": 401})), [aztree.subscription_target(PROD)], None,
                          log=lines.append)
        self.assertTrue(any("az login" in line for line in lines), lines)
        self.assertFalse(any("--tag" in line for line in lines), lines)

    def test_untagged_spend_is_marked_apart_from_a_value_named_untagged(self):
        data = make_data([("Storage", "LRS", [5] * 6)])
        data["views"]["tag"] = {"dims": ["TagValue", "ServiceName"], "names": {}, "tag": "env",
                                "rows": [{"k": ["(untagged)", "Storage"], "d": [4] * 6}, {"k": ["", "Storage"], "d": [1] * 6}]}
        rows = {(r["value"], bool(r.get("untagged"))): r["current"] for r in aztree.summarize(data)["by_tag"]}
        self.assertEqual(rows, {("(untagged)", False): 12.0, ("(untagged)", True): 3.0})

    DROPPED = [("Storage", "LRS", [5] * 6), ("Storage", "Old disk", [10, 10, 10, 0, 0, 0]),
               ("Old Service", "Old meter", [30, 30, 30, 0, 0, 0])]

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_enter_on_a_drop_whose_service_is_gone_does_not_zoom(self):
        page = self.run_page(make_data(self.DROPPED), "service", click=["[data-drop]:0", {"press": "Enter"}])
        self.assertIn("all services", page["crumbs"])
        self.assertFalse([c for c in page["history"] if c[1]["zoom"] == "Old Service"])

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_back_after_a_gone_drop_jump_remembers_the_zoom(self):
        page = self.run_page(make_data(self.DROPPED), "service", click="[data-drop]:1")  # Old disk: Storage is still there
        self.assertIn('<span class="cur">Storage</span>', page["crumbs"])
        self.assertEqual(page["history"][-1][1]["zoom"], "Storage")


class CleanupTest(unittest.TestCase):
    """Older review notes, checked against the code and reproduced before each fix."""
    render = ViewerTest.render
    run_page = ViewerTest.run_page

    def test_the_export_says_which_subscriptions_have_period_totals(self):
        s = aztree.summarize(make_data([("Storage", "LRS", [5] * 6)], resource_fallback=["acme-prod"]))
        self.assertEqual(s["resource_fallback"], ["acme-prod"])
        self.assertIn("resource_fallback", aztree.AI_INSTRUCTIONS)

    def test_reservations_for_two_regions_stay_apart(self):
        items = [advisor_item("Buy reserved instance", "/subscriptions/aaaa-1", "aaaa-1", "D4s v5", "P1Y", savings)
                 for savings in (100, 200)]
        items[0]["properties"]["extendedProperties"]["region"] = "eastus"
        items[1]["properties"]["extendedProperties"]["region"] = "westeurope"
        send = FakeSend((200, {}, json.dumps({"value": items}).encode()))
        self.assertEqual(len(aztree.advisor_recs(client(send), aztree.subscription_target(PROD))), 2)

    def test_cli_output_is_read_as_utf8(self):
        # the Azure CLI writes UTF-8; a cp1252 Windows locale would turn "Milanović" into "MilanoviÄ‡"
        seen = {}

        def run(cmd, **kw):
            seen.update(kw)
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        aztree.az_cli(["account", "show"], run=run, az_path="az")
        self.assertEqual(seen.get("encoding"), "utf-8")

    def test_the_demo_has_no_retired_gpu_series(self):
        meters = {r["k"][1] for r in aztree.demo(30, today=TODAY)["views"]["service"]["rows"]}
        self.assertNotIn("NC6s v3", meters)  # NCv3 retired on 30 Sep 2025

    def test_server_errors_are_not_called_throttling(self):
        lines = []
        send = FakeSend(error(503, "ServiceUnavailable", "try later"), page(QueryTest.COLS, []))
        aztree.query(client(send, log=lines.append), "/subscriptions/s1", "2026-09-22", "2026-09-27", ["ServiceName", "Meter"], "ActualCost")
        self.assertTrue(any("503" in line for line in lines), lines)
        self.assertFalse(any("throttl" in line for line in lines), lines)

    def test_constrained_and_isolated_sizes_match_the_rules(self):
        self.assertIn("D, Ds, Dv2, Dsv2", aztree.pit("Virtual Machines", "DS13-4 v2"))
        for meter in ("E64i v3", "E64is v3", "E16-4s v3", "D8-2s v3"):
            with self.subTest(meter=meter):
                self.assertIn("Dv3 and Ev3", aztree.pit("Virtual Machines", meter) or "")
        self.assertIsNone(aztree.pit("Virtual Machines", "E64s v5"))

    def test_a_spike_names_its_days_cost_plainly(self):
        (h,) = [h for h in aztree.summarize(aztree.demo(30, today=TODAY))["hints"] if h["kind"] == "spike"]
        self.assertIn("day_cost", h)
        self.assertNotIn("day", h)  # read like a day index

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_a_refund_bigger_than_the_usage_keeps_the_positive_meters_on_the_map(self):
        data = make_data([("Azure Cosmos DB", "100 RU/s", [10] * 6), ("Azure Cosmos DB", "Reservation refund", [0, 0, 0, -50, 0, 0]),
                          ("Storage", "LRS", [5] * 6)])
        names = [d["name"] for d in self.run_page(data, "service")["drawn"]]
        self.assertIn("100 RU/s", names)  # Cosmos DB nets to -$20, but it still ran $30 of RU/s

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_only_fallback_subscriptions_lose_the_daily_chart(self):
        other = "/subscriptions/bbbb-2/resourcegroups/rg-other"
        data = make_data([("Storage", "LRS", [5] * 6)],
                         resource_rows=[(RG, RG + "/providers/x/y/a", [3, 0, 0, 4, 0, 0]), (other, other + "/providers/x/y/b", [1] * 6)],
                         resource_fallback=["acme-prod"],
                         subscriptions=[{"id": "aaaa-1", "name": "acme-prod", "currency": "USD"},
                                        {"id": "bbbb-2", "name": "acme-dev", "currency": "USD"}])
        data["views"]["resource"]["names"][other] = "rg-other"
        page = self.run_page(data, "resource", click={"map": "click", "name": "rg-other"})
        self.assertIn("<svg", page["side"])
        page = self.run_page(data, "resource", click={"map": "click", "name": "rg-app"})
        self.assertIn("period totals", page["side"])


class V051ReviewTest(unittest.TestCase):
    """An outside review of 0.5.1: each finding reproduced before it was fixed."""
    render = ViewerTest.render
    run_page = ViewerTest.run_page
    MIXED = {("ServiceName", "Meter"): {"/subscriptions/aaaa-1": [(20260925, "Storage", "LRS", 100.0, "EUR", None)],
                                        "/subscriptions/bbbb-2": [(20260925, "Storage", "LRS", 200.0, "USD", None)]}}
    MIXED_REJECT = {"/subscriptions/aaaa-1": {"CostUSD"}, "/subscriptions/bbbb-2": {"Cost"}}

    # 1. mixed currencies
    def test_unconverted_currencies_are_flagged_not_summed_silently(self):
        data, _ = fetch(self.MIXED, targets=(PROD, DEV), reject_for=self.MIXED_REJECT)
        self.assertEqual(data["mixed_currencies"], ["EUR", "USD"])
        s = aztree.summarize(data)
        self.assertEqual((s["currency"], s["currencies"]), ("mixed", ["EUR", "USD"]))
        self.assertIn("mixed", aztree.AI_INSTRUCTIONS)

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_the_page_says_the_totals_mix_currencies(self):
        page = self.run_page(make_data([("Storage", "LRS", [5] * 6)], currency="EUR", mixed_currencies=["EUR", "USD"]), "service")
        self.assertIn("mix EUR and USD", page["sub"])
        self.assertNotIn("€", page["sub"])

    # 2. dollar rules on other currencies
    def test_the_bills_exchange_rate_is_read(self):
        tables = {("ServiceName", "Meter"): {"/subscriptions/aaaa-1": [(20260925, "Storage", "LRS", 15000.0, "JPY", 100.0)]}}
        data, _ = fetch(tables)
        self.assertEqual((data["currency"], data["usd_rate"]), ("JPY", 150.0))

    def test_dollar_rules_use_the_bills_exchange_rate(self):
        # ¥4,256 a month of Front Door Standard base fees is about $28: under one profile's $35, so no "122 profiles"
        yen = make_data([("Azure Front Door Service", "Standard Base Fees", [140] * 6)], currency="JPY", usd_rate=150.0)
        self.assertEqual(aztree.summarize(yen)["flags"], [])
        usd = make_data([("Azure Front Door Service", "Standard Base Fees", [140] * 6)])
        self.assertIn("about 122 Front Door Standard profiles", aztree.summarize(usd)["flags"][0]["reason"])

    # 3. tiny charges
    def test_a_thousand_tiny_charges_still_add_up(self):
        rows = {("rg", f"/subscriptions/a/resourcegroups/rg/providers/x/y/r{i}"): [0.001, 0, 0, 0.003, 0, 0] for i in range(1000)}
        packed = aztree.pack(rows)
        self.assertEqual([r["k"] for r in packed], [["rg", "(under a cent each)"]])
        self.assertAlmostEqual(sum(sum(r["d"]) for r in packed), 4.0, places=2)

    # 6. subscription and resource-group scopes
    def test_a_subscription_scope_asks_resource_graph_for_its_id(self):
        for scope in ("/subscriptions/aaaa-1", "/subscriptions/aaaa-1/resourceGroups/rg-app"):
            with self.subTest(scope=scope):
                router = Router({}, findings=[[]])
                aztree.fetch(client(router), [aztree.scope_target(scope)], 3, "ActualCost", advisor=False,
                             log=lambda *a: None, today=TODAY)
                (call,) = [c for c in router.other if c[0] == "graph"]
                self.assertEqual(call[1]["subscriptions"], ["aaaa-1"])

    # 4. keyboard
    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_tab_from_the_page_is_left_to_the_browser(self):
        page = self.run_page(make_data(FilterDimTest.DATA), "service", click={"press": "Tab"})
        self.assertEqual(page["pressed"], [False])
        self.assertIn('<div class="sel-name">Everything</div>', page["side"])

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_tab_on_the_map_walks_the_boxes_and_lets_go_after_the_last(self):
        page = self.run_page(make_data(FilterDimTest.DATA), "service", click=[{"press": "Tab", "from": "map"}] * 3)
        self.assertEqual(page["pressed"], [True, True, False])  # SQL Database, Storage, then on to the next control
        self.assertIn('<div class="sel-name">Storage</div>', page["side"])
        self.assertIn('id="map" tabindex="0"', TEMPLATE)

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_panel_rows_are_keyboard_controls(self):
        data = make_data([("Log Analytics", "Analytics Logs Data Ingestion", [30] * 6)])
        page = self.run_page(data, "service")
        self.assertIn('data-hint="0" role="button" tabindex="0"', page["side"])
        page = self.run_page(data, "resource", click={"press": "Enter", "row": "[data-hint]:0"})
        self.assertIn('<div class="sel-name">Analytics Logs Data Ingestion</div>', page["side"])

    # 5. "+N more" inside a zoomed group
    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_the_more_box_opens_inside_a_zoomed_group(self):
        meters = [("Log Analytics", "Analytics Logs Data Ingestion", [1000] * 6)] + [("Log Analytics", f"m {i}", [0.1] * 6) for i in range(30)]
        clicks = [{"map": "click", "name": "Log Analytics"}] * 2 + [{"map": "click", "name": "+30 more"}] * 2
        page = self.run_page(make_data(meters), "service", click=clicks)
        self.assertIn("m 0", {d["name"] for d in page["drawn"]})
        self.assertIn("30 smaller meters", page["crumbs"])
        page = self.run_page(make_data(meters), "service", click=clicks + [{"press": "Backspace"}])
        self.assertIn("+30 more", {d["name"] for d in page["drawn"]})
        self.assertIn('<span class="cur">Log Analytics</span>', page["crumbs"])


class ExplainTest(unittest.TestCase):
    def test_bad_response_is_not_blamed_on_the_network(self):
        send = FakeSend(page(["UsageDate", "ServiceName", "Meter"], [[20260925, "Storage", "LRS"]]))
        with self.assertRaises(aztree.AzureError) as ctx:
            aztree.query(client(send), "/subscriptions/s1", "2026-09-22", "2026-09-27", ["ServiceName", "Meter"], "ActualCost")
        self.assertNotIn("network", aztree.explain(ctx.exception))

    def test_403_mentions_cost_management_reader(self):
        self.assertIn("Cost Management Reader", aztree.explain(aztree.AzureError(403, "denied")))

    def test_401_mentions_login(self):
        self.assertIn("az login", aztree.explain(aztree.AzureError(401, "expired")))


if __name__ == "__main__":
    unittest.main()

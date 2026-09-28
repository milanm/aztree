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


TODAY = aztree.dt.date(2026, 9, 28)  # with days=3 the window is Sep 22..27, split after Sep 24


class Router:
    """Fake Cost Management: answers each query from `tables[(dim1, dim2)][scope]`, a list of
    (usage_date, value1, value2, cost, currency, cost_usd) tuples."""

    def __init__(self, tables, explode=(), reject_usd=False, reject=(), reject_for=None):
        self.tables, self.explode = tables, explode
        self.reject = set(reject) | ({"CostUSD"} if reject_usd else set())
        self.reject_for = reject_for or {}  # scope -> aggregations only that scope refuses
        self.bodies = []

    def __call__(self, method, url, data, headers):
        body = json.loads(data)
        self.bodies.append(body)
        dims = tuple(g["name"] for g in body["dataset"]["grouping"])
        aggs = [a["name"] for a in body["dataset"]["aggregation"].values()]
        scope = url.split("/providers/Microsoft.CostManagement")[0][len(aztree.ARM):]
        refused = (self.reject | set(self.reject_for.get(scope, ()))) & set(aggs)
        if refused:
            return error(400, "BadRequest", f"Invalid aggregation {sorted(refused)}")
        if dims in self.explode:
            return page(["Cost"], [], next_link=url.split("&")[0] + "&$skiptoken=more")
        cols = aggs + ["UsageDate", *dims, "Currency"]
        rows = []
        for date, a, b, cost, cur, usd in self.tables.get(dims, {}).get(scope, []):
            usd = usd if usd is not None else cost
            vals = {"Cost": cost, "CostUSD": usd, "PreTaxCost": cost, "PreTaxCostUSD": usd, "UsageDate": date,
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
    router = Router(tables, **{k: kw.pop(k) for k in ("explode", "reject_usd", "reject", "reject_for") if k in kw})
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


# (service, meter, words expected in the reason, or None for "no flag"). Meter names are from real bills.
PIT_CASES = [
    ("Log Analytics", "Analytics Logs Data Ingestion", "Log Analytics ingestion"),
    ("Azure Monitor", "Basic Logs Data Ingestion", "Log Analytics ingestion"),
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
    ("Virtual Network", "Standard Private Endpoint", None),
    ("Storage", "LRS Snapshots", "snapshots"),
    ("Storage", "P10 LRS Disk", None),
    ("Virtual Machines", "D2 v2", "previous-gen"),
    ("Virtual Machines", "D2 v2/DS2 v2", "previous-gen"),
    ("Virtual Machines", "DS3 v2 Spot", "previous-gen"),
    ("Virtual Machines", "D11 v2", "previous-gen"),
    ("Virtual Machines", "A1 v2", "previous-gen"),
    ("Virtual Machines", "Basic.A2", "previous-gen"),
    ("Virtual Machines", "F4", "previous-gen"),
    ("Virtual Machines", "F2s", "previous-gen"),
    ("Virtual Machines", "F2s v2", None),
    ("Virtual Machines", "D4s v5", None),
    ("Virtual Machines", "D2 v3", None),
    ("Virtual Machines", "D2as v4", None),
    ("Virtual Machines", "B2s", None),
    ("Azure App Service", "P1 v2 App", "Premium v2"),
    ("Azure App Service", "P2v2 App", "Premium v2"),
    ("Azure App Service", "P1 v3 App", None),
    ("Azure App Service", "P0v3 App", None),
    ("Windows Server", "Extended Security Updates - Windows Server 2012", "Extended Security Updates"),
    ("SQL Database", "vCore", None),
]


class PitsTest(unittest.TestCase):
    def test_rules_against_real_meter_names(self):
        for service, meter, expected in PIT_CASES:
            with self.subTest(service=service, meter=meter):
                why = aztree.pit(service, meter)
                if expected is None:
                    self.assertIsNone(why)
                else:
                    self.assertIsNotNone(why)
                    self.assertIn(expected.lower(), why.lower())

    @unittest.skipUnless(aztree.shutil.which("node"), "node not installed")
    def test_javascript_reads_the_rules_the_same_way(self):
        # The viewer runs the same patterns with JavaScript's RegExp; both engines must agree.
        script = (
            "const [pits, cases] = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
            "const rules = pits.map(([s, m, why]) => [new RegExp(s), new RegExp(m), why]);"
            "console.log(JSON.stringify(cases.map(([s, m]) => { const r = rules.find(([rs, rm]) => rs.test(s) && rm.test(m)); return r ? r[2] : null; })));"
        )
        payload = json.dumps([aztree.PITS, [[s, m] for s, m, _ in PIT_CASES]])
        out = aztree.subprocess.run(["node", "-e", script], input=payload, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [aztree.pit(s, m) for s, m, _ in PIT_CASES])


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
    """A minimal data dict in the shape fetch() returns. Rows are (k1, k2, [6 daily values]); split after day 3."""
    def view(dims, rows, names=None):
        return {"dims": dims, "names": names or {}, "rows": [{"k": [a, b], "d": d} for a, b, d in rows]}

    sub_rows = {}
    for svc, _, d in service_rows:
        acc = sub_rows.setdefault(svc, [0.0] * 6)
        for i, v in enumerate(d):
            acc[i] += v
    data = {
        "days": DAYS6, "split": 3, "currency": "USD", "metric": "ActualCost", "generated": "2026-09-28 09:00",
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
        self.assertEqual(s["advisor"], [rec])
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


TEMPLATE = (Path(__file__).resolve().parent.parent / "viewer.html").read_text(encoding="utf-8")

# Runs the viewer's script in Node against a bare-bones DOM: enough to prove each view draws boxes
# and fills the side panel, without a browser.
FAKE_DOM = r"""
const vm = require("vm"), fs = require("fs");
const [code, view, click] = JSON.parse(fs.readFileSync(0, "utf8"));
class El {
  constructor(tag) {
    Object.assign(this, { tag, children: [], dataset: {}, className: "", innerHTML: "", value: "", checked: false,
      isConnected: true, clientWidth: 1100, clientHeight: 700, offsetWidth: 100, offsetHeight: 30, on: {} });
    this.style = { setProperty() {} };
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  append(...c) { this.children.push(...c); }
  replaceChildren(...c) { this.children = c; }
  addEventListener(type, fn) { (this.on[type] = this.on[type] || []).push(fn); }
  closest() { return null; } focus() {} blur() {} click() {} dispatchEvent() {}
}
const byId = {}, made = [];
Object.assign(globalThis, {
  document: {
    querySelector: s => byId[s] || (byId[s] = new El(s)), querySelectorAll: () => [],
    createElement: t => { const e = new El(t); made.push(e); return e; },
    createDocumentFragment: () => new El("#fragment"), addEventListener() {},
  },
  location: { hash: "#" + view }, history: { replaceState() {} },
  ResizeObserver: class { observe() {} }, requestAnimationFrame: () => 0, cancelAnimationFrame() {},
  innerWidth: 1400, innerHeight: 900,
});
vm.runInThisContext(code);
if (click && typeof click === "object") {  // {"map": "dblclick", "key": ""} double-clicks that group's box
  const box = made.find(e => e.className === "cell group" && e._n.key === click.key);
  for (const fn of byId["#map"].on[click.map] || []) fn({ target: { closest: () => box } });
} else if (click) {  // "[data-rec]:0" clicks the first Advisor tip in the side panel
  const [sel, value] = click.split(":");
  const target = { closest: s => (s === sel ? { dataset: { [sel.slice(6, -1)]: value } } : null) };
  for (const fn of byId["#side"].on.click || []) fn({ target });
}
console.log(JSON.stringify({
  leaves: made.filter(e => e.className === "cell leaf").length,
  boxes: made.map(e => e.innerHTML).join("\n"),
  side: byId["#side"].innerHTML, sub: byId["#sub"].innerHTML, meta: byId["#meta"].innerHTML,
  crumbs: byId["#crumbs"].innerHTML,
}));
"""


class ViewerTest(unittest.TestCase):
    def render(self, data):
        import tempfile
        d = tempfile.mkdtemp()
        out = Path(d) / "aztree.html"
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
        import tempfile
        self.dir = Path(tempfile.mkdtemp())
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

    def test_entry_point_is_the_last_statement(self):
        # Run as a script, main() only sees what is defined above the __main__ guard.
        import ast
        last = ast.parse(Path(aztree.__file__).read_text(encoding="utf-8")).body[-1]
        self.assertIsInstance(last, ast.If)
        self.assertIn("__main__", ast.unparse(last.test))

    def test_runs_as_a_script(self):
        target = self.dir / "summary.json"
        r = aztree.subprocess.run([sys.executable, aztree.__file__, "--from", str(self.saved), "--export", str(target)],
                                  capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(target.exists())

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
        self.assertEqual(set(self.data["views"]), set(aztree.VIEWS))
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
        import tempfile
        page = Path(tempfile.mkdtemp()) / "demo.html"
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

    def test_a_selected_group_stays_under_its_boxes(self):
        # groups and their leaves are siblings; lifting a selected group would cover its leaves
        for rule in aztree.re.findall(r"([^{}]*\.sel[^{}]*)\{([^}]*)\}", TEMPLATE):
            if "z-index" in rule[1]:
                self.assertIn(".leaf.sel", rule[0])

    render = ViewerTest.render


class ExplainTest(unittest.TestCase):
    def test_403_mentions_cost_management_reader(self):
        self.assertIn("Cost Management Reader", aztree.explain(aztree.AzureError(403, "denied")))

    def test_401_mentions_login(self):
        self.assertIn("az login", aztree.explain(aztree.AzureError(401, "expired")))


if __name__ == "__main__":
    unittest.main()

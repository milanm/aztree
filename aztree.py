#!/usr/bin/env python3
"""aztree: see where your Azure money goes, as a disktree-style treemap.

    python3 aztree.py            # read your Azure costs (last 30 days) and open the map
    python3 aztree.py --demo     # fake data, no Azure needed

Needs a logged-in Azure CLI (`az login`), or a token in AZURE_ACCESS_TOKEN. No other dependencies.
"""
import argparse
import datetime as dt
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "viewer.html"
OUT = HERE / "out"  # everything generated goes here (git-ignored: it contains your subscription IDs and costs)

ARM = "https://management.azure.com"
API_VERSION = "2025-03-01"  # Microsoft.CostManagement/query
MAX_TRIES = 8  # per request, when Cost Management throttles us
MAX_RESOURCE_PAGES = 10  # past this, the resource view drops from single resources to services
# What to sum, in order of preference. Some scopes reject USD columns, and older offers only know PreTaxCost.
AGGREGATIONS = [["Cost", "CostUSD"], ["Cost"], ["PreTaxCost", "PreTaxCostUSD"], ["PreTaxCost"]]

# Each view is outer box -> inner box. A query can group by two dimensions at most, so the
# subscription view is not queried: every query runs per subscription and call 1 feeds it too.
VIEWS = {
    "service": ["ServiceName", "Meter"],
    "subscription": ["SubscriptionId", "ServiceName"],
    "region": ["ResourceLocation", "ServiceName"],
    "resource": ["ResourceGroupName", "ResourceId"],
}


def die(msg):
    print(f"\naztree: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- auth and subscriptions

def az_cli(args, run=subprocess.run, az_path=None):
    """Run the Azure CLI and return stdout. `az` is az.cmd on Windows, so resolve the full path first."""
    path = az_path or shutil.which("az")
    if not path:
        die("need the Azure CLI (https://aka.ms/azcli), or a token in AZURE_ACCESS_TOKEN.")
    r = run([path, *args], capture_output=True, text=True)
    if r.returncode:
        err = r.stderr.strip()
        hint = "\n  -> Log in first: `az login`." if "login" in err.lower() or "expired" in err.lower() else ""
        die(f"Azure CLI error: {err}{hint}")
    return r.stdout


def get_token(env=os.environ, run=subprocess.run, az_path=None):
    if env.get("AZURE_ACCESS_TOKEN"):
        return env["AZURE_ACCESS_TOKEN"]
    out = az_cli(["account", "get-access-token", "--resource", ARM + "/", "--query", "accessToken", "-o", "tsv"],
                 run=run, az_path=az_path)
    return out.strip()


def list_subscriptions(az):
    """Every subscription the token can see, straight from ARM, so AZURE_ACCESS_TOKEN works without the CLI."""
    subs, url = [], "/subscriptions?api-version=2022-12-01"
    while url:
        page = az.call("GET", url)
        subs += [{"id": s["subscriptionId"], "name": s["displayName"], "state": s["state"]} for s in page.get("value", [])]
        url = page.get("nextLink")
    return subs


def current_subscription(run=subprocess.run):
    """The subscription `az account show` points at, or None without the CLI."""
    if not shutil.which("az"):
        return None
    return json.loads(az_cli(["account", "show", "--query", "{id:id, name:name}", "-o", "json"], run=run))


def pick_subscriptions(available, wanted, all_, current):
    """Choose which subscriptions to read: --all, --subscription (id or name), or the CLI's current one."""
    if all_:
        return [{"id": s["id"], "name": s["name"]} for s in available if s["state"] == "Enabled"]
    if not wanted:
        if not current:
            die("no default subscription. Pass --subscription ID_OR_NAME or --all.")
        return [{"id": current["id"], "name": current["name"]}]
    picked = []
    for w in wanted:
        hits = [s for s in available if w.lower() in (s["id"].lower(), s["name"].lower())]
        if not hits:
            known = "\n    ".join(f"{s['name']}  ({s['id']})" for s in available) or "(none)"
            die(f"no subscription matches '{w}'. Subscriptions you can read:\n    {known}")
        if len(hits) > 1:
            ids = "\n    ".join(s["id"] for s in hits)
            die(f"{len(hits)} subscriptions are named '{w}'. Pass the one you mean by id:\n    {ids}")
        hit = hits[0]
        if all(p["id"] != hit["id"] for p in picked):
            picked.append({"id": hit["id"], "name": hit["name"]})
    return picked


# ---------------------------------------------------------------- Azure REST client

class AzureError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class TooManyPages(Exception):
    pass


def http_send(method, url, data, headers):
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def retry_after(headers):
    """Cost Management throttles per scope, per client type and per QPU, each with its own
    *-retry-after header. Wait for the longest one."""
    waits = [int(v) for k, v in headers.items() if k.lower().endswith("retry-after") and str(v).strip().isdigit()]
    return max(waits) if waits and max(waits) > 0 else 5


class Azure:
    def __init__(self, token, send=http_send, sleep=time.sleep, verbose=False, log=print):
        self.token, self.send, self.sleep, self.verbose, self.log = token, send, sleep, verbose, log
        self.requests = 0

    def call(self, method, url, body=None):
        url = url if url.startswith("https://") else ARM + url
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        for attempt in range(1, MAX_TRIES + 1):
            try:
                status, resp_headers, raw = self.send(method, url, data, headers)
            except OSError as e:  # URLError, timeouts, resets: the network, not Azure, said no
                reason = getattr(e, "reason", e)
                if attempt == MAX_TRIES:
                    raise AzureError(0, f"network error: {reason}") from e
                self.log(f"    network error ({reason}); retrying in {5 * attempt}s ...")
                self.sleep(5 * attempt)
                continue
            self.requests += 1
            if self.verbose:
                qpu = {k.lower(): v for k, v in resp_headers.items()}.get("x-ms-ratelimit-microsoft.costmanagement-qpu-consumed")
                if qpu:
                    self.log(f"    ({qpu} QPU consumed)")
            if (status == 429 or status >= 500) and attempt < MAX_TRIES:
                wait = retry_after(resp_headers)
                self.log(f"    Azure is throttling us (HTTP {status}); waiting {wait}s ...")
                self.sleep(wait)
                continue
            if status >= 400:
                raise AzureError(status, error_message(raw))
            return json.loads(raw) if raw else {}


def error_message(raw):
    try:
        err = json.loads(raw)["error"]
        return f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")
    except (ValueError, KeyError, TypeError):
        return raw.decode(errors="replace")[:300] if isinstance(raw, bytes) else str(raw)[:300]


def explain(e):
    hints = []
    if e.status == 401:
        hints.append("Your token is missing or expired. Run `az login` (or refresh AZURE_ACCESS_TOKEN).")
    if e.status == 403:
        hints.append("You need the Cost Management Reader (or Reader) role on the subscription or scope.")
    if e.status == 429:
        hints.append("Cost Management kept throttling. Wait a minute and try again, or read fewer subscriptions.")
    if e.status == 0:
        hints.append("Check your network connection and try again.")
    return f"Azure error: {e}" + ("\n  -> " + "\n  -> ".join(hints) if hints else "")


def query(az, scope, start, end, groupings, metric, aggs=("Cost", "CostUSD"), max_pages=None):
    """One Cost Management query at daily granularity. `start` and `end` are ISO dates, both inclusive.
    Returns every row of every page as a dict keyed by column name."""
    body = {
        "type": metric,
        "timeframe": "Custom",
        "timePeriod": {"from": f"{start}T00:00:00Z", "to": f"{end}T23:59:59Z"},
        "dataset": {
            "granularity": "Daily",
            "aggregation": {f"total{a}": {"name": a, "function": "Sum"} for a in aggs},
            "grouping": [{"type": "Dimension", "name": g} for g in groupings],
        },
    }
    url = f"{scope}/providers/Microsoft.CostManagement/query?api-version={API_VERSION}"
    rows, pages = [], 0
    while url:
        pages += 1
        if max_pages and pages > max_pages:
            raise TooManyPages(f"more than {max_pages} pages")
        props = az.call("POST", url, body).get("properties", {})
        cols = [c["name"] for c in props.get("columns", [])]
        if cols and aggs[0] not in cols:
            raise AzureError(0, f"Cost Management answered without a {aggs[0]} column (columns: {', '.join(cols)})")
        rows += [dict(zip(cols, r)) for r in props.get("rows", [])]
        url = props.get("nextLink")
    return rows


# ---------------------------------------------------------------- reading costs

def subscription_target(sub):
    return {"id": sub["id"], "name": sub["name"], "scope": f"/subscriptions/{sub['id']}"}


def scope_target(scope):
    scope = "/" + scope.strip("/")
    return {"id": scope, "name": scope, "scope": scope}


def usage_day(v):
    """UsageDate arrives as 20260930 (a number) or as '2026-09-30T00:00:00'. Return 'yyyymmdd'."""
    s = str(v)
    return s[:10].replace("-", "") if "-" in s else s[:8]


def group_key(target, rg, resource_id):
    """The resource group's ARM id, or the subscription's for resources outside any group.
    Keying on the id keeps two `rg-app` groups in different subscriptions apart."""
    m = re.match(r"/subscriptions/[^/]+", resource_id or "", re.I)
    sub = m.group(0).lower() if m else target["scope"].lower()
    return f"{sub}/resourcegroups/{rg.lower()}" if rg else sub


def fetch(az, targets, days, metric, advisor=True, log=print, today=None):
    end = (today or dt.date.today()) - dt.timedelta(1)  # through yesterday: today is still arriving
    start = end - dt.timedelta(2 * days - 1)  # current window + previous window, for the "vs prev" deltas
    dates = [(start + dt.timedelta(i)).isoformat() for i in range(2 * days)]
    index = {d.replace("-", ""): i for i, d in enumerate(dates)}
    raw = {v: [] for v in VIEWS}  # (key, day index, cost, cost in USD), folded once the currency is known
    names = {v: {} for v in VIEWS}
    aggs = list(AGGREGATIONS)  # aggs[0] is what this run asks for; a scope that rejects it moves us down the list
    subs, fallback = [], []
    twins = Counter(t["name"] for t in targets)  # "Pay-As-You-Go" twice needs the id to tell them apart
    targets = [{**t, "name": f"{t['name']} ({t['id'][:8]})"} if twins[t["name"]] > 1 else t for t in targets]

    def run(target, groupings, **kw):
        while True:
            try:
                return query(az, target["scope"], dates[0], dates[-1], groupings, metric, aggs[0], **kw)
            except AzureError as e:
                if e.status != 400 or len(aggs) == 1:
                    raise
                aggs.pop(0)

    def add(view, key, r):
        i = index.get(usage_day(r.get("UsageDate")))
        if i is not None:
            cost, cost_usd = r.get("Cost", r.get("PreTaxCost")), r.get("CostUSD", r.get("PreTaxCostUSD"))
            raw[view].append((key, i, cost or 0.0, cost_usd))

    for t in targets:
        log(f"  {t['name']}: services ...")
        currencies = Counter()
        for r in run(t, VIEWS["service"]):
            currencies[r.get("Currency")] += 1
            add("service", (service_of(r), r.get("Meter") or "(no meter)"), r)
            add("subscription", (t["id"], service_of(r)), r)
        names["subscription"][t["id"]] = t["name"]

        log(f"  {t['name']}: resources ...")
        try:
            rows, leaf = run(t, VIEWS["resource"], max_pages=MAX_RESOURCE_PAGES), "ResourceId"
        except TooManyPages:
            log(f"  {t['name']}: too many resources to list one by one, grouping them by service instead")
            rows, leaf = run(t, ["ResourceGroupName", "ServiceName"]), "ServiceName"
            fallback.append(t["name"])
        for r in rows:
            rg, rid = r.get("ResourceGroupName") or "", (r.get("ResourceId") or "").lower()
            key = group_key(t, rg, rid)
            label = rg or "(no resource group)"
            names["resource"][key] = f"{label} · {t['name']}" if len(targets) > 1 else label
            add("resource", (key, (rid or "(no resource)") if leaf == "ResourceId" else service_of(r)), r)

        log(f"  {t['name']}: regions ...")
        for r in run(t, VIEWS["region"]):
            add("region", ((r.get("ResourceLocation") or "").lower(), service_of(r)), r)
        subs.append({"id": t["id"], "name": t["name"], "currency": next((c for c, _ in currencies.most_common() if c), None)})

    recs, advisor_error = None, None
    with_advisor = [t for t in targets if t["scope"].lower().startswith("/subscriptions/")]  # Advisor is per subscription
    if advisor and with_advisor:
        recs = []
        for t in with_advisor:
            log(f"  {t['name']}: Advisor ...")
            try:
                recs += advisor_recs(az, t)
            except Exception as e:  # Advisor needs Reader; cost data alone is still worth a page
                # keep only the status: Azure's 403 text names the caller, and this lands in the AI export
                advisor_error = f"HTTP {e.status}" if isinstance(e, AzureError) else type(e).__name__
                log(f"  {t['name']}: skipped Advisor ({e}). Reader on the subscription fixes access errors; --no-advisor hides this.")
        recs.sort(key=lambda r: -(r["annual_savings"] or -1))

    found = {s["currency"] for s in subs if s["currency"]}
    usd = len(found) > 1 and any(a.endswith("USD") for a in aggs[0])
    if len(found) > 1 and not usd:
        log("  warning: these subscriptions bill in different currencies and Azure won't convert them; totals mix currencies")
    log(f"  done: {az.requests} requests (Cost Management queries are free)")
    return {
        "days": dates, "split": days,
        "views": {v: {"dims": dims, "names": names[v], "rows": fold(raw[v], len(dates), usd)} for v, dims in VIEWS.items()},
        "currency": "USD" if usd or not found else min(found),
        "subscriptions": subs, "resource_fallback": fallback,
        "advisor": recs, "advisor_error": advisor_error, "demo": False,
    }


def service_of(row):
    return row.get("ServiceName") or "(no service)"


def fold(entries, n, usd):
    """(key, day, cost, cost_usd) entries -> packed rows of daily totals, in one currency."""
    rows = {}
    for key, i, cost, cost_usd in entries:
        amount = (cost_usd or 0.0) if usd else cost
        if amount:
            rows.setdefault(key, [0.0] * n)[i] += amount
    return pack(rows)


def pack(rows):
    out = [{"k": list(k), "d": [round(v, 4) for v in d]} for k, d in rows.items()]
    return [r for r in out if abs(sum(r["d"])) >= 0.005]


# ---------------------------------------------------------------- demo data

P, S, D, X = (f"{c * 8}-{c * 4}-{c * 4}-{c * 4}-{c * 12}" for c in "1234")
DEMO_SUBS = {P: "acme-prod", S: "acme-staging", D: "acme-data", X: "sandbox-dev"}
VM, WEB, SQL, STG = ("microsoft.compute/virtualmachines", "microsoft.web/serverfarms", "microsoft.sql/servers",
                     "microsoft.storage/storageaccounts")

# service, meter, $/day, growth over the 60 days, [(subscription, region, resource group, provider/type/name, share)]
DEMO = [
    ("Virtual Machines", "D4s v5", 58, 0.05, [(P, "us east", "rg-app-prod", f"{VM}/vm-app-01", .5),
                                             (P, "us east", "rg-app-prod", f"{VM}/vm-app-02", .5)]),
    ("Virtual Machines", "D8s v5", 44, 0.1, [(P, "us east", "mc_rg-aks-prod_aks-prod_eastus",
                                             "microsoft.compute/virtualmachinescalesets/aks-nodepool1-vmss", 1)]),
    ("Virtual Machines", "E8s v5", 38, 0, [(D, "eu west", "rg-etl", f"{VM}/vm-etl-worker", 1)]),
    ("Virtual Machines", "NC6s v3", 22, 0.3, [(D, "us east", "rg-ml", f"{VM}/vm-gpu-train", 1)]),
    ("Virtual Machines", "D2 v2", 9, 0, [(S, "us east", "rg-legacy", f"{VM}/vm-legacy-ftp", 1)]),
    ("Azure Kubernetes Service", "Standard Uptime SLA", 2.4, 0, [(P, "us east", "rg-aks-prod",
                                                                  "microsoft.containerservice/managedclusters/aks-prod", 1)]),
    ("SQL Database", "vCore", 41, 0, [(P, "us east", "rg-data-prod", f"{SQL}/sql-prod/databases/orders", .7),
                                      (S, "us east", "rg-data-staging", f"{SQL}/sql-staging/databases/orders", .3)]),
    ("SQL Database", "RA-GRS Data Stored", 6, 0.03, [(P, "us east", "rg-data-prod", f"{SQL}/sql-prod/databases/orders", 1)]),
    ("Azure Cosmos DB", "100 RU/s", 19, 1.2, [(P, "us east", "rg-data-prod",
                                              "microsoft.documentdb/databaseaccounts/cosmos-catalog", 1)]),
    ("Azure Cosmos DB", "Data Stored", 3, 0.05, [(P, "us east", "rg-data-prod",
                                                  "microsoft.documentdb/databaseaccounts/cosmos-catalog", 1)]),
    ("Redis Cache", "C1 Cache Instance", 3.3, 0, [(P, "us east", "rg-app-prod", "microsoft.cache/redis/redis-sessions", 1)]),
    ("Storage", "Hot LRS Data Stored", 16, 0.04, [(D, "eu west", "rg-lake", f"{STG}/stlakeraw", .8),
                                                  (P, "us east", "rg-app-prod", f"{STG}/stappassets", .2)]),
    ("Storage", "Hot LRS Write Operations", 3, 0.1, [(D, "eu west", "rg-lake", f"{STG}/stlakeraw", 1)]),
    ("Storage", "P30 LRS Disk", 11, 0, [(P, "us east", "rg-app-prod", "microsoft.compute/disks/vm-app-01-data", 1)]),
    ("Storage", "LRS Snapshots", 7, 0.15, [(P, "us east", "rg-backup", "microsoft.compute/snapshots/snap-vm-app-01-2025", 1)]),
    ("Log Analytics", "Analytics Logs Data Ingestion", 34, 1.1, [
        (P, "us east", "rg-monitoring", "microsoft.operationalinsights/workspaces/log-prod", .85),
        (S, "us east", "rg-monitoring", "microsoft.operationalinsights/workspaces/log-staging", .15)]),
    ("Azure Monitor", "Standard Web Test Execution", 1.5, 0, [(P, "us east", "rg-monitoring",
                                                               "microsoft.insights/webtests/ping-home", 1)]),
    ("Microsoft Defender for Cloud", "Standard Node", 6, 0, [(P, "us east", "", "microsoft.security/pricings/virtualmachines", 1)]),
    ("Azure App Service", "P1 v3 App", 14, 0, [(P, "us east", "rg-app-prod", f"{WEB}/asp-web-prod", 1)]),
    ("Azure App Service", "P1 v2 App", 5, 0, [(S, "us east", "rg-app-staging", f"{WEB}/asp-web-staging", 1)]),
    ("Functions", "Premium vCPU Duration", 8, 0.1, [(P, "us east", "rg-app-prod", f"{WEB}/asp-func-prod", 1)]),
    ("Bandwidth", "Standard Data Transfer Out", 12, 0.1, [(P, "us east", "rg-app-prod", f"{VM}/vm-app-01", 1)]),
    ("Bandwidth", "Inter Continent Data Transfer Out - NAM or EU To Any", 4, 0.2, [(D, "eu west", "rg-etl", f"{VM}/vm-etl-worker", 1)]),
    ("NAT Gateway", "Standard Data Processed", 6, 0.3, [(P, "us east", "rg-network", "microsoft.network/natgateways/ng-prod", 1)]),
    ("NAT Gateway", "Standard Gateway", 1.1, 0, [(P, "us east", "rg-network", "microsoft.network/natgateways/ng-prod", 1)]),
    ("Virtual Network", "Standard Private Endpoint", 1.8, 0, [(P, "us east", "rg-network", "microsoft.network/privateendpoints/pe-sql-prod", 1)]),
    ("Virtual Network", "Standard IPv4 Static Public IP", 1.2, 0, [(P, "us east", "rg-network", "microsoft.network/publicipaddresses/pip-ng-prod", 1)]),
    ("Virtual Network", "Basic IPv4 Static Public IP", 1.4, 0, [(X, "us west 2", "rg-sandbox", "microsoft.network/publicipaddresses/pip-old-test", 1)]),
    ("Azure Front Door Service", "Standard Base Fees", 1.2, 0, [(P, "global", "rg-edge", "microsoft.cdn/profiles/afd-web", 1)]),
    ("Azure Front Door Service", "Standard Data Transfer Out", 4, 0.05, [(P, "global", "rg-edge", "microsoft.cdn/profiles/afd-web", 1)]),
    ("Foundry Models", "gpt 4.1 Inp glbl Tokens", 6, 3, [(X, "us east 2", "rg-ai-sandbox", "microsoft.cognitiveservices/accounts/oai-sandbox", 1)]),
    ("Foundry Models", "gpt 4.1 Outp glbl Tokens", 9, 3, [(X, "us east 2", "rg-ai-sandbox", "microsoft.cognitiveservices/accounts/oai-sandbox", 1)]),
    ("Azure Data Factory v2", "Cloud Data Movement", 5, 0.2, [(D, "eu west", "rg-etl", "microsoft.datafactory/factories/adf-etl", 1)]),
    ("Event Hubs", "Standard Throughput Unit", 3.6, 0, [(D, "eu west", "rg-etl", "microsoft.eventhub/namespaces/evh-ingest", 1)]),
    ("Container Registry", "Standard Registry Unit", 0.67, 0, [(P, "us east", "rg-app-prod", "microsoft.containerregistry/registries/acmecr", 1)]),
    ("Key Vault", "Operations", 0.3, 0, [(P, "us east", "rg-app-prod", "microsoft.keyvault/vaults/kv-app-prod", 1)]),
]


def demo_rid(sub, rg, path):
    return f"/subscriptions/{sub}/resourcegroups/{rg}/providers/{path}" if rg else f"/subscriptions/{sub}/providers/{path}"


def demo_advisor():
    def rec(problem, sub, path, savings, sku=None, term=None, rg=None):
        resource = demo_rid(sub, rg, path) if path else f"/subscriptions/{sub}"
        return {"problem": problem, "solution": problem, "impact": "High", "resource": resource,
                "resource_name": resource.rsplit("/", 1)[-1] if path else DEMO_SUBS[sub], "resource_type": None,
                "sku": sku, "term": term, "annual_savings": savings, "currency": "USD", "subscription": DEMO_SUBS[sub]}

    return [
        rec("Consider SQL PaaS DB reserved instance to save over the pay-as-you-go costs", P, None, 4380.0,
            "SQL DB Single/Elastic Pool - General Purpose - Gen 5", "P3Y"),
        rec("Consider purchasing a savings plan to unlock lower prices", P, None, 3120.0, "Compute_Savings_Plan", "P1Y"),
        rec("Right-size or shutdown underutilized virtual machines", D, f"{VM}/vm-etl-worker", 2150.0, "Standard_E4s_v5", rg="rg-etl"),
        rec("Consider Cosmos DB reserved instance to save over the pay-as-you-go costs", P, None, 1020.0, "100 RU/s", "P1Y"),
        rec("Right-size or shutdown underutilized virtual machines", S, f"{VM}/vm-legacy-ftp", 640.0, "Standard_B2s", rg="rg-legacy"),
    ]


def demo(days, today=None):
    rnd = random.Random(7)
    end = (today or dt.date.today()) - dt.timedelta(1)
    n = 2 * days
    dates = [(end - dt.timedelta(n - 1 - i)).isoformat() for i in range(n)]
    weekend = [dt.date.fromisoformat(d).weekday() >= 5 for d in dates]
    rows = {v: {} for v in VIEWS}
    names = {v: {} for v in VIEWS}
    names["subscription"] = dict(DEMO_SUBS)
    for service, meter, per_day, growth, spots in DEMO:
        bursty = any(w in meter for w in ("Tokens", "Data Transfer", "Ingestion", "Operations", "Processed", "Duration"))
        for sub, region, rg, path, share in spots:
            daily = []
            for i in range(n):
                m = (1 + growth * i / (n - 1)) / (1 + growth / 2)  # keep the average near per_day
                m *= 0.8 if bursty and weekend[i] else 1
                daily.append(max(0.0, per_day * share * m * rnd.gauss(1, 0.06)))
            rid = demo_rid(sub, rg, path)
            gkey = group_key({"scope": f"/subscriptions/{sub}"}, rg, rid)
            names["resource"][gkey] = f"{rg or '(no resource group)'} · {DEMO_SUBS[sub]}"
            for view, key in (("service", (service, meter)), ("subscription", (sub, service)),
                              ("region", (region, service)), ("resource", (gkey, rid))):
                acc = rows[view].setdefault(key, [0.0] * n)
                for i, v in enumerate(daily):
                    acc[i] += v
    return {
        "days": dates, "split": days,
        "views": {v: {"dims": VIEWS[v], "names": names[v], "rows": pack(rows[v])} for v in VIEWS},
        "currency": "USD",
        "subscriptions": [{"id": k, "name": v, "currency": "USD"} for k, v in DEMO_SUBS.items()],
        "resource_fallback": [], "advisor": demo_advisor(), "advisor_error": None, "demo": True,
    }


# ---------------------------------------------------------------- worth a look

# Known money pits: (service pattern, meter pattern, why). First match wins. Shared with the viewer's
# "worth a look" panel, so the patterns must mean the same in Python and JavaScript.
OLD_VM_SIZES = r"^(?:(?:Basic[ ._])?A\d+m?(?: v2)?|DS?\d+(?: v2)?|F\d+s?)(?:$|/| Low Priority| Spot)"
PITS = [
    (r"^(?:Log Analytics|Azure Monitor)$", r"Data Ingestion",
     "Log Analytics ingestion — trim noisy tables, use Basic logs, or a commitment tier past 100 GB/day"),
    (r"^Bandwidth$", r"Data Transfer Out", "data transfer out — keep traffic in one region, cache at the edge"),
    (r"^NAT Gateway$", r"Data Processed", "NAT data processing — service or private endpoints for Storage, SQL and ACR skip it"),
    (r"^Azure Firewall$", r"Data Processed|Premium", "Azure Firewall — processing and Premium add up; route only what needs inspection"),
    (r"^Virtual Network$", r"^Basic .*Public IP", "Basic public IPs — the Basic SKU retired on 30 Sep 2025, move to Standard"),
    (r"^Virtual Network$", r"Public IP|IP Address Hours", "public IPs — billed per hour each; release the ones nothing uses"),
    (r"^Storage$", r"Snapshot", "disk snapshots — prune old ones; incremental snapshots on Standard storage cost less"),
    (r"^Virtual Machines$", OLD_VM_SIZES, "previous-gen VM sizes — current generations cost less for the same work"),
    (r"^Azure App Service$", r"^P\d+ ?v2 App", "Premium v2 App Service plans — Premium v3 gives more per dollar and can be reserved"),
    (r"", r"Extended Security Update", "Extended Security Updates — upgrade the OS or SQL version to stop paying for them"),
]


def pit(service, meter):
    return next((why for s, m, why in PITS if re.search(s, service) and re.search(m, meter)), None)


def advisor_recs(az, target):
    """Azure Advisor's cost recommendations for one subscription, one per (kind, resource, SKU).
    Advisor lists each reservation once per term and look-back period; keep the biggest saving."""
    flt = urllib.parse.quote("Category eq 'Cost'")
    url = f"{target['scope']}/providers/Microsoft.Advisor/recommendations?api-version=2023-01-01&$filter={flt}"
    best = {}
    while url:
        page = az.call("GET", url)
        for item in page.get("value", []):
            key, rec = advisor_rec(item.get("properties", {}), target)
            if key not in best or (rec["annual_savings"] or 0) > (best[key]["annual_savings"] or 0):
                best[key] = rec
        url = page.get("nextLink")
    return sorted(best.values(), key=lambda r: -(r["annual_savings"] or -1))


def advisor_rec(p, target):
    ext = p.get("extendedProperties") or {}
    short = p.get("shortDescription") or {}
    resource = ((p.get("resourceMetadata") or {}).get("resourceId") or "").lower()
    savings = ext.get("annualSavingsAmount")
    name = p.get("impactedValue") or resource.rsplit("/", 1)[-1]
    rec = {
        "problem": short.get("problem", ""),
        "solution": short.get("solution", ""),
        "impact": p.get("impact"),
        "resource": resource,
        "resource_name": target["name"] if name.lower() == target["id"].lower() else name,
        "resource_type": p.get("impactedField"),
        "sku": ext.get("displaySKU") or ext.get("sku") or ext.get("targetSku"),
        "term": ext.get("term"),
        "annual_savings": float(savings) if savings not in (None, "") else None,
        "currency": ext.get("savingsCurrency"),
        "subscription": target["name"],
    }
    return (p.get("recommendationTypeId"), resource, rec["sku"]), rec


# ---------------------------------------------------------------- AI export

AI_INSTRUCTIONS = (
    "This is an Azure cost breakdown exported by aztree. Amounts are in `currency`, for the cost type in `metric` "
    "(ActualCost books reservation and savings plan purchases on the day they were bought; AmortizedCost spreads them "
    "over the term). `current` is the most recent period and `previous` is the equally long period before it. "
    "Line items are Azure meters grouped by service. `flags` are known cost traps matched on meter names. `advisor` holds "
    "Azure Advisor's cost recommendations, one per kind, resource and SKU, with the largest annual saving Advisor "
    "reported; recommendations that cover the same usage (a reservation and a savings plan, a 1-year and a 3-year term) "
    "are alternatives, not additive. "
    "Please: 1) explain what drives the cost, 2) explain notable changes vs the previous period, "
    "3) suggest concrete savings, each with an estimated monthly saving and how to verify it. "
    "Levers to consider: reservations and savings plans for steady compute and databases; Azure Hybrid Benefit for "
    "Windows Server and SQL Server licenses already owned; dev/test pricing for non-production subscriptions; "
    "right-sizing and auto-shutdown for VMs and App Service plans; blob access tiers (cool, cold, archive) and lifecycle "
    "rules; Log Analytics commitment tier pricing, Basic logs and shorter table retention; and keeping data transfer "
    "inside one region."
)
TOP_RESOURCES = 20  # per resource group in the export; the rest are summed


def summarize(data):
    """Turn the raw daily data into a compact JSON an AI agent can reason about."""
    days, split = data["days"], data["split"]
    n = len(days) - split

    def money(v):
        return round(v, 2)

    def entry(cur, prev):
        return {
            "current": money(cur), "previous": money(prev), "change": money(cur - prev),
            "change_pct": round(100 * (cur - prev) / prev, 1) if prev >= 0.01 else None,
            "share_pct": round(100 * cur / grand, 2) if grand else 0,
        }

    def keep(cur, prev):
        return abs(cur) >= 0.01 or abs(prev) >= 0.01

    def grouped(view):
        out = {}
        for r in data["views"][view]["rows"]:
            cur, prev = sum(r["d"][split:]), sum(r["d"][:split])
            g = out.setdefault(r["k"][0], {"cur": 0, "prev": 0, "items": {}})
            g["cur"] += cur
            g["prev"] += prev
            it = g["items"].setdefault(r["k"][1], [0, 0])
            it[0] += cur
            it[1] += prev
        return sorted(out.items(), key=lambda kv: -kv[1]["cur"])

    def breakdown(view, key_name, child_name, children="services", label=None, limit=None):
        names = data["views"][view].get("names", {})
        out = []
        for k, g in grouped(view):
            if not keep(g["cur"], g["prev"]):
                continue
            row = {key_name: label(k, names) if label else k}
            if label:
                row["id"] = k
            elif names.get(k):
                row["name"] = names[k]
            row.update(entry(g["cur"], g["prev"]))
            kids = sorted(({child_name: c, **entry(cc, p)} for c, (cc, p) in g["items"].items() if keep(cc, p)),
                              key=lambda x: -x["current"])
            row[children] = kids[:limit]
            if limit and len(kids) > limit:
                row["other_" + children] = {"count": len(kids) - limit, "current": money(sum(c["current"] for c in kids[limit:]))}
            out.append(row)
        return out

    services = grouped("service")
    grand = sum(g["cur"] for _, g in services)
    grand_prev = sum(g["prev"] for _, g in services)

    line_items = []
    for svc, g in services:
        for meter, (cur, prev) in g["items"].items():
            if keep(cur, prev):
                line_items.append({"service": svc, "meter": meter, **entry(cur, prev)})
    line_items.sort(key=lambda x: -x["current"])

    growers = [x for x in line_items
               if x["change"] >= max(1, grand * 0.005) and (x["change_pct"] is None or x["change_pct"] > 20)]
    growers = sorted(growers, key=lambda x: -x["change"])[:10]
    flags = []
    for x in line_items:
        why = pit(x["service"], x["meter"])
        if why and x["current"] >= grand * 0.002:
            flags.append({"service": x["service"], "meter": x["meter"], "current": x["current"], "reason": why})

    daily = [0.0] * len(days)
    for r in data["views"]["service"]["rows"]:
        for i, v in enumerate(r["d"]):
            daily[i] += v

    return {
        "tool": "aztree",
        "instructions_for_ai": AI_INSTRUCTIONS,
        "generated": data.get("generated"),
        "metric": data.get("metric", "ActualCost"),
        "currency": data.get("currency", "USD"),
        "subscriptions": data.get("subscriptions", []),
        "demo_data": bool(data.get("demo")),
        "period": {
            "current": {"start": days[split], "end": days[-1], "days": n},
            "previous": {"start": days[0], "end": days[split - 1], "days": split},
        },
        "totals": {**entry(grand, grand_prev), "daily_avg": money(grand / n), "monthly_pace": money(grand / n * 30.4)},
        "by_service": [{"service": k, **entry(g["cur"], g["prev"])} for k, g in services if keep(g["cur"], g["prev"])],
        "by_subscription": breakdown("subscription", "subscription_id", "service"),
        "by_region": breakdown("region", "region", "service"),
        "by_resource_group": breakdown("resource", "resource_group", "resource_id", "resources",
                                       label=lambda k, names: names.get(k, k), limit=TOP_RESOURCES),
        "top_growers": growers,
        "flags": flags,
        "advisor": data.get("advisor"),
        "advisor_error": data.get("advisor_error"),
        "line_items": line_items,
        "daily_totals": [{"date": d, "cost": money(v)} for d, v in zip(days, daily)],
    }


def export(data, path):
    path.write_text(json.dumps(summarize(data), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------- output

def render(data, out):
    html = TEMPLATE.read_text(encoding="utf-8")
    page = {**data, "pits": PITS, "export": summarize(data)}
    # < keeps names like "</script>" or "<!--" from ending the script block early
    blob = json.dumps(page, separators=(",", ":"), ensure_ascii=False).replace("<", "\\u003c")
    out.write_text(html.replace("__AZTREE_DATA__", blob), encoding="utf-8")


def resolve_targets(args, az):
    if args.scope:
        return [scope_target(args.scope)]
    current = None if (args.all or args.subscription) else current_subscription()
    return [subscription_target(s) for s in pick_subscriptions(list_subscriptions(az), args.subscription, args.all, current)]


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # subscription names can hold characters a Windows pipe can't encode
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="See where your Azure money goes, as a treemap.")
    ap.add_argument("--demo", action="store_true", help="use fake data (no Azure access needed)")
    who = ap.add_mutually_exclusive_group()
    who.add_argument("--subscription", action="append", default=[], metavar="ID_OR_NAME",
                     help="subscription to read, repeat for more (default: the Azure CLI's current one)")
    who.add_argument("--all", action="store_true", help="read every enabled subscription you can see")
    who.add_argument("--scope", help="any Cost Management scope, e.g. a billing account (not tested yet)")
    ap.add_argument("--days", type=int, default=30, help="period to show, compared with the period before it (default 30)")
    ap.add_argument("--metric", default="ActualCost", choices=["ActualCost", "AmortizedCost"],
                    help="AmortizedCost spreads reservation and savings plan purchases over their term")
    ap.add_argument("--no-advisor", action="store_true", help="skip Azure Advisor's cost recommendations")
    ap.add_argument("--from", dest="source", metavar="JSON", help="re-open saved data (out/aztree-data.json) without calling Azure")
    ap.add_argument("--out", default=str(OUT / "aztree.html"), help="where to write the page (default out/aztree.html)")
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    ap.add_argument("--export", nargs="?", const=str(OUT / "aztree-export.json"), metavar="FILE",
                    help="write a summary JSON for an AI agent (default out/aztree-export.json) instead of the page")
    ap.add_argument("--verbose", action="store_true", help="print the query units each request used")
    args = ap.parse_args(argv)

    if not 1 <= args.days <= 180:
        die("--days must be between 1 and 180 (aztree reads two periods, and a query spans a year at most).")

    if args.source:
        data = json.loads(Path(args.source).read_text(encoding="utf-8"))
    elif args.demo:
        data = demo(args.days)
        data.update(generated=dt.datetime.now().strftime("%Y-%m-%d %H:%M"), metric=args.metric)
    else:
        try:
            az = Azure(get_token(), verbose=args.verbose)
            targets = resolve_targets(args, az)
            who_ = targets[0]["name"] if len(targets) == 1 else f"{len(targets)} subscriptions"
            print(f"aztree: reading {who_}, last {args.days} days (+{args.days} before, for comparison)")
            data = fetch(az, targets, args.days, args.metric, advisor=not args.no_advisor)
        except AzureError as e:
            die(explain(e))
        data.update(generated=dt.datetime.now().strftime("%Y-%m-%d %H:%M"), metric=args.metric)
        OUT.mkdir(exist_ok=True)
        (OUT / "aztree-data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    if args.export:
        path = Path(args.export).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        export(data, path)
        print(f"aztree: wrote {path}  (give this file to your AI agent)")
        return

    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    render(data, out)
    print(f"aztree: wrote {out}")
    if not args.no_open:
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()

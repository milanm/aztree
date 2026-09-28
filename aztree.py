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
            status, resp_headers, raw = self.send(method, url, data, headers)
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
    aggs = ["Cost", "CostUSD"]
    subs, fallback = [], []

    def run(target, groupings, **kw):
        nonlocal aggs
        try:
            return query(az, target["scope"], dates[0], dates[-1], groupings, metric, aggs, **kw)
        except AzureError as e:
            if e.status != 400 or len(aggs) == 1:
                raise
            aggs = ["Cost"]  # this scope can't report USD: billing currency only from here on
            return query(az, target["scope"], dates[0], dates[-1], groupings, metric, aggs, **kw)

    def add(view, key, r):
        i = index.get(usage_day(r.get("UsageDate")))
        if i is not None:
            raw[view].append((key, i, r.get("Cost") or 0.0, r.get("CostUSD")))

    for t in targets:
        log(f"  {t['name']}: services ...")
        currencies = Counter()
        for r in run(t, VIEWS["service"]):
            currencies[r.get("Currency")] += 1
            add("service", (r["ServiceName"], r.get("Meter") or "(no meter)"), r)
            add("subscription", (t["id"], r["ServiceName"]), r)
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
            add("resource", (key, (rid or "(no resource)") if leaf == "ResourceId" else r["ServiceName"]), r)

        log(f"  {t['name']}: regions ...")
        for r in run(t, VIEWS["region"]):
            add("region", ((r.get("ResourceLocation") or "").lower(), r["ServiceName"]), r)
        subs.append({"id": t["id"], "name": t["name"], "currency": next((c for c, _ in currencies.most_common() if c), None)})

    recs, advisor_error = None, None
    if advisor:
        recs = []
        for t in targets:
            if not t["scope"].lower().startswith("/subscriptions/"):
                continue
            log(f"  {t['name']}: Advisor ...")
            try:
                recs += advisor_recs(az, t)
            except AzureError as e:  # Advisor needs Reader; cost data alone is still worth a page
                advisor_error = str(e)
                log(f"  {t['name']}: skipped Advisor ({e}). Reader on the subscription fixes that; --no-advisor hides this.")
        recs.sort(key=lambda r: -(r["annual_savings"] or -1))

    found = {s["currency"] for s in subs if s["currency"]}
    usd = len(found) > 1 and "CostUSD" in aggs
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
        hit = next((s for s in available if w.lower() in (s["id"].lower(), s["name"].lower())), None)
        if not hit:
            known = "\n    ".join(f"{s['name']}  ({s['id']})" for s in available) or "(none)"
            die(f"no subscription matches '{w}'. Subscriptions you can read:\n    {known}")
        if all(p["id"] != hit["id"] for p in picked):
            picked.append({"id": hit["id"], "name": hit["name"]})
    return picked

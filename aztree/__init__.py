"""aztree: see where your Azure money goes, as a disktree-style treemap.

    aztree                # read your Azure costs (last 30 days) and open the map
    aztree --demo         # fake data, no Azure needed
    python -m aztree      # the same, from a clone without installing

Needs a logged-in Azure CLI (`az login`), or a token in AZURE_ACCESS_TOKEN. No other dependencies.
"""
__version__ = "0.9.0"

import argparse
import datetime as dt
import functools
import json
import os
import random
import re
import shutil
import ssl
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter
from importlib import resources
from itertools import zip_longest
from pathlib import Path

def home():
    """Where runs keep the page and the data: ~/.aztree/ (or AZTREE_HOME), never inside a repo by accident.
    The data holds subscription IDs and costs."""
    return Path(os.environ.get("AZTREE_HOME") or Path.home() / ".aztree")


def template():
    """The page, shipped inside the package (works from a clone, a wheel or a zip)."""
    return resources.files(__name__).joinpath("viewer.html").read_text(encoding="utf-8")

ARM = "https://management.azure.com"
PORTAL = "https://portal.azure.com"
API_VERSION = "2025-03-01"  # Microsoft.CostManagement/query
SCHEMA = 1  # the saved data's and the export's format: raise it when either changes in a way readers would notice
HISTORY_KEPT = 12  # dated copies of runs kept in ~/.aztree/history/
MAX_TRIES = 8  # per request, when Cost Management throttles us
MAX_RESOURCE_PAGES = 10  # past this, the resource view reads one total per resource and period instead of daily rows
# What to sum, in order of preference. Some scopes reject USD columns, and older offers only know PreTaxCost.
AGGREGATIONS = [["Cost", "CostUSD"], ["Cost"], ["PreTaxCost", "PreTaxCostUSD"], ["PreTaxCost"]]
# A 400 about those columns names the aggregation or a column. "Cost" alone isn't enough: "Cost Management is not
# supported for this offer" and "not registered for Microsoft.CostManagement" are not about columns.
COLUMN_ERROR = re.compile(r"aggregation|column|costusd|pretaxcost|\bcost\b(?!\s*management)", re.I)

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
    r = run([path, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")  # az writes UTF-8
    if r.returncode:
        err = r.stderr.strip()
        hint = "\n  -> Log in first: `az login`." if "login" in err.lower() or "expired" in err.lower() else ""
        die(f"Azure CLI error: {err}{hint}")
    return r.stdout


def get_token(env=os.environ, run=subprocess.run, az_path=None, tenant=None):
    """A token for ARM. Tokens are per tenant, so ask for the tenant a subscription lives in."""
    if env.get("AZURE_ACCESS_TOKEN"):
        return env["AZURE_ACCESS_TOKEN"]
    args = ["account", "get-access-token", "--resource", ARM + "/", "--query", "accessToken", "-o", "tsv"]
    return az_cli(args + (["--tenant", tenant] if tenant else []), run=run, az_path=az_path).strip()


def cli_subscriptions(run=subprocess.run, az_path=None):
    """Every subscription the Azure CLI knows, across every tenant you're logged in to."""
    subs = json.loads(az_cli(["account", "list", "-o", "json"], run=run, az_path=az_path))
    return [{"id": s["id"], "name": s["name"], "state": s["state"], "tenant": s.get("tenantId")} for s in subs]


def list_subscriptions(az):
    """Every subscription a bare token can see, straight from ARM: only that token's tenant."""
    subs, url = [], "/subscriptions?api-version=2022-12-01"
    while url:
        page = az.call("GET", url)
        subs += [{"id": s["subscriptionId"], "name": s["displayName"], "state": s["state"], "tenant": s.get("tenantId")}
                 for s in page.get("value", [])]
        url = page.get("nextLink")
    return subs


def current_subscription(run=subprocess.run):
    """The subscription `az account show` points at, or None without the CLI."""
    if not shutil.which("az"):
        return None
    return json.loads(az_cli(["account", "show", "--query", "{id:id, name:name, tenant:tenantId}", "-o", "json"], run=run))


def sub_ref(s):
    return {"id": s["id"], "name": s["name"], "tenant": s.get("tenant")}


def pick_subscriptions(available, wanted, all_, current):
    """Choose which subscriptions to read: --all, --subscription (id or name), or the CLI's current one."""
    if all_:
        return [sub_ref(s) for s in available if s["state"] == "Enabled"]
    if not wanted:
        if not current:
            die("no default subscription. Pass --subscription ID_OR_NAME or --all.")
        return [sub_ref(current)]
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
            picked.append(sub_ref(hit))
    return picked


# ---------------------------------------------------------------- Azure REST client

class AzureError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class TooManyPages(Exception):
    pass


# Where operating systems keep their CA bundle (Go reads the same list): Debian and Ubuntu, Fedora and RHEL,
# RHEL 7 and CentOS, openSUSE, then macOS, Alpine and the BSDs
SYSTEM_CA_FILES = ["/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
                   "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem", "/etc/ssl/ca-bundle.pem", "/etc/ssl/cert.pem"]


def ca_files(env=os.environ, paths=None, exists=os.path.isfile, windows=sys.platform == "win32"):
    """CA bundles to trust besides Python's own. A frozen executable's OpenSSL looks where its build machine kept
    certificates (python.org's framework folder on macOS, /usr/lib/ssl on Linux), which most machines don't have: then
    the system's bundle stands in. Windows reads its certificate store. REQUESTS_CA_BUNDLE is what the Azure CLI reads
    behind a proxy that inspects HTTPS; SSL_CERT_FILE, OpenSSL's own, needs nothing from here."""
    paths = paths or ssl.get_default_verify_paths()
    found = []
    if not windows and not paths.cafile and not paths.capath:
        found += [f for f in SYSTEM_CA_FILES if exists(f)][:1]
    bundle = env.get("REQUESTS_CA_BUNDLE")
    if bundle and exists(bundle):
        found.append(bundle)
    return found


@functools.lru_cache(maxsize=None)
def tls_context():
    """One context for every request: Python's trusted certificates plus ca_files()."""
    ctx = ssl.create_default_context()
    for f in ca_files():
        ctx.load_verify_locations(cafile=f)
    return ctx


def http_send(method, url, data, headers):
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120, context=tls_context()) as r:
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
        """`token` is a token, or a function tenant -> token for runs that span tenants."""
        self.token, self.send, self.sleep, self.verbose, self.log = token, send, sleep, verbose, log
        self.tokens = {}
        self.requests = 0

    def token_for(self, tenant):
        if isinstance(self.token, str):
            return self.token
        if tenant not in self.tokens:
            self.tokens[tenant] = self.token(tenant)
        return self.tokens[tenant]

    def call(self, method, url, body=None, tenant=None):
        url = url if url.startswith("https://") else ARM + url
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": "Bearer " + self.token_for(tenant), "Content-Type": "application/json"}
        for attempt in range(1, MAX_TRIES + 1):
            try:
                status, resp_headers, raw = self.send(method, url, data, headers)
            except OSError as e:  # URLError, timeouts, resets: the network, not Azure, said no
                reason = getattr(e, "reason", e)
                if isinstance(reason, ssl.SSLCertVerificationError):  # the same certificate on every try
                    raise AzureError(0, f"untrusted certificate: {reason}") from e
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
                why = "Azure is throttling us" if status == 429 else "Azure had a server error"
                self.log(f"    {why} (HTTP {status}); waiting {wait}s ...")
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
    if e.status == 0 and "untrusted certificate" in str(e):
        hints.append("aztree found no CA certificate that vouches for Azure. Behind a proxy that inspects HTTPS, set "
                     "SSL_CERT_FILE (or REQUESTS_CA_BUNDLE, as for the Azure CLI) to a PEM file with your company's "
                     "root certificate.")
    elif e.status == 0:
        hints.append("Check your network connection and try again.")
    return f"Azure error: {e}" + ("\n  -> " + "\n  -> ".join(hints) if hints else "")


def query(az, scope, start, end, groupings, metric, aggs=("Cost", "CostUSD"), max_pages=None, tenant=None,
          granularity="Daily"):
    """One Cost Management query. `start` and `end` are ISO dates, both inclusive. With granularity=None, Azure
    sums each group over the whole period: one row per group instead of one per group per day.
    Returns every row of every page as a dict keyed by column name."""
    dataset = {
        "aggregation": {f"total{a}": {"name": a, "function": "Sum"} for a in aggs},
        "grouping": [g if isinstance(g, dict) else {"type": "Dimension", "name": g} for g in groupings],
    }
    if granularity:
        dataset["granularity"] = granularity
    body = {
        "type": metric,
        "timeframe": "Custom",
        "timePeriod": {"from": f"{start}T00:00:00Z", "to": f"{end}T23:59:59Z"},
        "dataset": dataset,
    }
    url = f"{scope}/providers/Microsoft.CostManagement/query?api-version={API_VERSION}"
    rows, pages = [], 0
    while url:
        pages += 1
        if max_pages and pages > max_pages:
            raise TooManyPages(f"more than {max_pages} pages")
        props = az.call("POST", url, body, tenant=tenant).get("properties", {})
        cols = [c["name"] for c in props.get("columns", [])]
        if cols and aggs[0] not in cols:
            raise AzureError(200, f"Cost Management answered without a {aggs[0]} column (columns: {', '.join(cols)})")
        rows += [dict(zip(cols, r)) for r in props.get("rows", [])]
        url = props.get("nextLink")
    if getattr(az, "verbose", False):
        az.log(f"    ({pages} page{'s' if pages != 1 else ''}, {len(rows)} rows)")
    return rows


TAG_NAMES_API = "2021-04-01"


def choose_tag(az, targets, wanted, log=print):
    """The tag the tag view splits the bill by: --tag, or else the tag on the most resources across the
    subscriptions. Azure's own hidden-* tags (hidden-link, hidden-title) don't count. None when there's none."""
    if wanted:
        return wanted
    counts, spelling = Counter(), {}
    for t in targets:
        sub = re.match(r"/subscriptions/([^/]+)", t["scope"], re.I)
        if not sub:
            continue  # tag names are listed per subscription; other scopes need --tag
        url = f"/subscriptions/{sub.group(1)}/tagNames?api-version={TAG_NAMES_API}"
        try:
            while url:
                page = az.call("GET", url, tenant=t.get("tenant"))
                for item in page.get("value", []):
                    name = item.get("tagName") or ""
                    if name and not name.lower().startswith("hidden-"):
                        spelling.setdefault(name.lower(), name)
                        counts[name.lower()] += (item.get("count") or {}).get("value") or 0
                url = page.get("nextLink")
        except AzureError as e:
            fix = "run `az login` again" if e.status == 401 else "pass --tag KEY to choose one"  # 401: the token, not the tags
            log(f"  {t['name']}: couldn't list its tags (HTTP {e.status}); {fix}")
    if not counts:
        return None
    return spelling[min(counts, key=lambda k: (-counts[k], k))]  # most resources, then by name


def month_forecast(az, target, metric, today, column="Cost"):
    """Azure's own forecast for this calendar month: what's billed so far and what's still to come, each summed,
    and the currencies it answered in. None when Azure has no forecast (new subscriptions, some offers)."""
    first = today.replace(day=1)
    last = (first + dt.timedelta(32)).replace(day=1) - dt.timedelta(1)
    body = {
        "type": metric, "timeframe": "Custom",
        "timePeriod": {"from": f"{first}T00:00:00Z", "to": f"{last}T23:59:59Z"},
        "dataset": {"granularity": "Daily", "aggregation": {"totalCost": {"name": column, "function": "Sum"}}},
        "includeActualCost": True, "includeFreshPartialCost": False,
    }
    url = f"{target['scope']}/providers/Microsoft.CostManagement/forecast?api-version={API_VERSION}"
    sums, currencies, rows = {"Actual": 0.0, "Forecast": 0.0}, set(), 0
    while url:
        props = az.call("POST", url, body, tenant=target.get("tenant")).get("properties", {})
        cols = [c["name"] for c in props.get("columns", [])]
        if cols and column not in cols:  # summing a missing column would show a $0 forecast as if it were real
            raise AzureError(200, f"the forecast answered without a {column} column (columns: {', '.join(cols)})")
        for values in props.get("rows", []):
            r = dict(zip(cols, values))
            if r.get("CostStatus") in sums:
                sums[r["CostStatus"]] += r.get(column) or 0.0
                currencies.add(r.get("Currency"))
                rows += 1
        url = props.get("nextLink")
    return {"actual": sums["Actual"], "forecast": sums["Forecast"], "currencies": currencies} if rows else None


GRAPH_API = "2022-10-01"
MAX_GRAPH_ROWS = 5000
# Resources that bill while doing nothing. Most need one look at the resource itself: one pass with case(), since
# Resource Graph caps how many unions and joins a query may have. Gateways and elastic pools are idle only when
# no other resource points at them, so they take a second query with a join. Several checks follow the FinOps
# toolkit's optimization workbooks (github.com/microsoft/finops-toolkit, MIT).
IDLE_QUERIES = ["""resources
| extend check = case(
    type =~ 'microsoft.compute/virtualmachines' and tostring(properties.extended.instanceView.powerState.code) =~ 'PowerState/stopped', 'stopped-vm',
    type =~ 'microsoft.compute/disks' and tostring(properties.diskState) =~ 'Unattached', 'unattached-disk',
    type =~ 'microsoft.compute/disks' and tostring(properties.diskState) =~ 'Reserved' and tostring(sku.name) startswith 'Premium' and todatetime(properties.LastOwnershipUpdateTime) < ago(30d), 'premium-disk-off',
    type =~ 'microsoft.network/publicipaddresses' and isempty(tostring(properties.ipConfiguration.id)) and isempty(tostring(properties.natGateway.id)), 'unused-ip',
    type =~ 'microsoft.compute/snapshots' and todatetime(properties.timeCreated) < ago(90d), 'old-snapshot',
    type =~ 'microsoft.compute/snapshots' and tostring(sku.name) startswith 'Premium', 'premium-snapshot',
    type =~ 'microsoft.web/serverfarms' and toint(properties.numberOfSites) == 0, 'empty-plan',
    type =~ 'microsoft.network/natgateways' and coalesce(array_length(properties.subnets), 0) == 0, 'lonely-nat',
    type =~ 'microsoft.network/applicationgateways' and not(tostring(properties.backendAddressPools) has_any ('ipAddress', 'fqdn', 'ipConfigurations')), 'empty-appgw',
    type =~ 'microsoft.network/loadbalancers' and coalesce(array_length(properties.backendAddressPools), 0) == 0 and coalesce(array_length(properties.inboundNatRules), 0) == 0, 'empty-lb',
    type =~ 'microsoft.network/expressroutecircuits' and tostring(properties.serviceProviderProvisioningState) =~ 'NotProvisioned', 'unprovisioned-circuit',
    type =~ 'microsoft.network/privateendpoints' and strcat(properties.privateLinkServiceConnections[0].properties.privateLinkServiceConnectionState.status, properties.manualPrivateLinkServiceConnections[0].properties.privateLinkServiceConnectionState.status) =~ 'Disconnected', 'disconnected-endpoint',
    '')
| where check != ''
| project check, id = tolower(id), name, resourceGroup, subscriptionId""",
                """resources
| where type =~ 'microsoft.network/virtualnetworkgateways' and coalesce(array_length(properties.vpnClientConfiguration.vpnClientAddressPool.addressPrefixes), 0) == 0
| extend key = tolower(id)
| join kind=leftouter (
    resources
    | where type =~ 'microsoft.network/connections'
    | mv-expand gateway = pack_array(properties.virtualNetworkGateway1.id, properties.virtualNetworkGateway2.id) to typeof(string)
    | summarize connections = count() by key = tolower(gateway)
  ) on key
| where isnull(connections)
| extend check = 'lonely-gateway'
| union (
    resources
    | where type =~ 'microsoft.sql/servers/elasticpools'
    | extend key = tolower(id)
    | join kind=leftouter (
        resources
        | where type =~ 'microsoft.sql/servers/databases' and isnotempty(properties.elasticPoolId)
        | summarize databases = count() by key = tolower(tostring(properties.elasticPoolId))
      ) on key
    | where isnull(databases)
    | extend check = 'empty-pool')
| project check, id = tolower(id), name, resourceGroup, subscriptionId"""]


def graph_findings(az, targets, log=print):
    """What IDLE_QUERIES find in Azure Resource Graph: each query once per tenant (tokens are per tenant) for all
    of that tenant's subscriptions, at most MAX_GRAPH_ROWS rows in all. A tenant that says no is skipped, like a
    subscription without Advisor access: returns what the others found and the first failure's status, or None."""
    tenants = {}
    for t in targets:
        # a subscription or resource-group scope: Resource Graph wants the subscription's id, not the path
        sub = re.match(r"/subscriptions/([^/]+)", t["scope"], re.I)
        subs = tenants.setdefault(t.get("tenant"), [])
        if sub and sub.group(1) not in subs:
            subs.append(sub.group(1))
    found, error = [], None
    for tenant, subs in tenants.items():
        try:
            for query in IDLE_QUERIES:
                options = {"resultFormat": "objectArray", "$top": 1000}
                while len(found) < MAX_GRAPH_ROWS:
                    page = az.call("POST", f"/providers/Microsoft.ResourceGraph/resources?api-version={GRAPH_API}",
                                   {"subscriptions": subs, "query": query, "options": options}, tenant=tenant)
                    found += [{k: row.get(k) for k in ("check", "id", "name", "resourceGroup", "subscriptionId")}
                              for row in page.get("data", [])]
                    if not page.get("$skipToken"):
                        break
                    options = {**options, "$skipToken": page["$skipToken"]}
        except Exception as e:  # needs Reader, like Advisor; the cost data alone is still worth a page
            # keep only the status: Azure's 403 text names the caller, and this lands in the AI export
            error = error or (f"HTTP {e.status}" if isinstance(e, AzureError) else type(e).__name__)
            log(f"  skipped the Resource Graph checks for {len(subs)} subscription{'s' if len(subs) > 1 else ''} ({e}). "
                "Reader on the subscription fixes access errors; --no-graph hides this.")
    return found[:MAX_GRAPH_ROWS], error


# ---------------------------------------------------------------- reading costs

def subscription_target(sub):
    return {"id": sub["id"], "name": sub["name"], "scope": f"/subscriptions/{sub['id']}", "tenant": sub.get("tenant")}


def scope_target(scope):
    scope = "/" + scope.strip("/")
    return {"id": scope, "name": scope, "scope": scope, "tenant": None}


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


# Cost Management spells a region three ways: its own short names ("US East", "EU West"), ARM names ("eastus") and
# the portal's ("East US"). From the FinOps toolkit's open data (github.com/microsoft/finops-toolkit, Regions.csv, MIT).
REGIONS = ("Asia Pacific|Australia|Australia Central|Australia Central 2|Australia East|Australia Southeast|Austria East|"
           "Azure Stack|Belgium Central|Brazil South|Brazil Southeast|Canada Central|Canada East|Central India|Central US|"
           "Central US EUAP|Chile Central|China East|China East 2|China East 3|China North|China North 2|China North 3|"
           "Denmark East|East Asia|East US|East US 2|East US 2 EUAP|East US 3|Finland Central|France Central|France South|"
           "Germany Central|Germany North|Germany NorthEast|Germany West Central|Global|Greece Central|India South Central|"
           "Indonesia Central|Israel Central|Italy North|Japan|Japan East|Japan West|Jio India Central|Jio India West|"
           "Korea Central|Korea South|Malaysia South|Malaysia West|Mexico Central|New Zealand North|North Central US|"
           "North Europe|Norway East|Norway West|Poland Central|Qatar Central|Saudi Arabia East|Singapore Central|"
           "South Africa North|South Africa West|South Central US|South Central US 2|South Central US STG|South India|"
           "Southeast Asia|Southeast US|Spain Central|Sweden Central|Sweden South|Switzerland North|Switzerland West|"
           "Taiwan North|Taiwan Northwest|UAE Central|UAE North|UK North|UK South|UK South 2|UK West|US East 2 EUAP|"
           "USDoD Central|USDoD East|USGov Arizona|USGov Iowa|USGov Texas|USGov Virginia|USGov Wyoming|USSec East|"
           "USSec West|USSec West Central|United States|West Central US|West Europe|West India|West US|West US 2|"
           "West US 3").split("|")
# only the short names that aren't the portal's name without its spaces ("us gov virginia" is "usgovvirginia")
SHORT_REGIONS = {"ae central": "UAE Central", "ae north": "UAE North", "ap east": "East Asia",
                 "ap southeast": "Southeast Asia", "at east": "Austria East", "au central": "Australia Central",
                 "au central 2": "Australia Central 2", "au east": "Australia East", "au southeast": "Australia Southeast",
                 "be central": "Belgium Central", "br south": "Brazil South", "br southeast": "Brazil Southeast",
                 "ca central": "Canada Central", "ca east": "Canada East", "ch north": "Switzerland North",
                 "ch west": "Switzerland West", "cl central": "Chile Central", "cn east": "China East",
                 "cn east 2": "China East 2", "cn east 3": "China East 3", "cn north": "China North",
                 "cn north 2": "China North 2", "cn north 3": "China North 3", "de central": "Germany Central",
                 "de north": "Germany North", "de northeast": "Germany NorthEast", "de west central": "Germany West Central",
                 "es central": "Spain Central", "eu north": "North Europe", "eu west": "West Europe",
                 "fi central": "Finland Central", "fr central": "France Central", "fr south": "France South",
                 "gr central": "Greece Central", "id central": "Indonesia Central", "il central": "Israel Central",
                 "in central": "Central India", "in central jio": "Jio India Central", "in south": "South India",
                 "in south central": "India South Central", "in west": "West India", "in west jio": "Jio India West",
                 "it north": "Italy North", "ja east": "Japan East", "ja west": "Japan West", "kr central": "Korea Central",
                 "kr south": "Korea South", "mx central": "Mexico Central", "my west": "Malaysia West",
                 "no east": "Norway East", "no west": "Norway West", "nz north": "New Zealand North",
                 "pl central": "Poland Central", "qa central": "Qatar Central", "sa east": "Saudi Arabia East",
                 "se central": "Sweden Central", "se south": "Sweden South", "tw north": "Taiwan North",
                 "us central": "Central US", "us east": "East US", "us east 2": "East US 2", "us east 3": "East US 3",
                 "us gov az": "USGov Arizona", "us gov tx": "USGov Texas", "us north central": "North Central US",
                 "us south central": "South Central US", "us west": "West US", "us west 2": "West US 2",
                 "us west 3": "West US 3", "us west central": "West Central US", "za north": "South Africa North",
                 "za west": "South Africa West"}
REGION_NAMES = {**{r.lower().replace(" ", ""): r for r in REGIONS}, **SHORT_REGIONS}


def region_name(location):
    """'US East', 'eastus' or 'East US' -> 'East US', so one region is one box. A name it doesn't know stays as Azure
    sent it, in lower case like before."""
    v = (location or "").strip().lower()
    return REGION_NAMES.get(v) or REGION_NAMES.get(v.replace(" ", "")) or v


def last_full_day(today=None):
    """The day before yesterday. Azure takes 8-24 hours to post usage, so yesterday is still filling in
    and would drag down every comparison, grower and pace figure."""
    return (today or dt.date.today()) - dt.timedelta(2)


def pick_currency(found, raw, log=print):
    """The page's currency, from the billing currencies `found` and each view's (key, day, cost, cost in USD) entries
    in `raw`: one currency stays as it is, several become US dollars when every entry has a USD figure, and otherwise
    the totals mix them. Returns (currency, usd, mixed, usd_rate): whether amounts are the USD figures, the currencies
    mixed (or None), and units of the currency per US dollar (or None when unknown)."""
    # USD only if every row has a USD figure: a subscription may have answered in its billing currency alone
    usd = len(found) > 1 and all(cost_usd is not None for entries in raw.values() for *_, cost_usd in entries)
    mixed = sorted(found) if len(found) > 1 and not usd else None  # the page and export say so; nothing converts them
    if mixed:
        log("  warning: these subscriptions bill in different currencies and Azure won't convert them; totals mix currencies")
    currency = "USD" if usd or not found else min(found)
    # units of the bill's currency per US dollar, from rows that carry both: the $ thresholds in the rules use it
    paired = [(cost, cost_usd) for *_, cost, cost_usd in raw["service"] if cost_usd]
    usd_rate = None
    if currency == "USD":
        usd_rate = 1.0
    elif not mixed and sum(u for _, u in paired):
        usd_rate = round(sum(c for c, _ in paired) / sum(u for _, u in paired), 4)
    return currency, usd, mixed, usd_rate


def fetch(az, targets, days, metric, advisor=True, log=print, today=None, tag=None, graph=True):
    end = last_full_day(today)
    start = end - dt.timedelta(2 * days - 1)  # current window + previous window, for the "vs prev" deltas
    dates = [(start + dt.timedelta(i)).isoformat() for i in range(2 * days)]
    index = {d.replace("-", ""): i for i, d in enumerate(dates)}
    raw = {v: [] for v in [*VIEWS, "tag"]}  # (key, day index, cost, cost in USD), folded once the currency is known
    names = {v: {} for v in VIEWS}
    columns = {}  # target id -> cost columns that target accepts, best first; every subscription starts at the top
    settled = set()  # targets whose columns a query has already worked with
    subs, fallback = [], []
    forecasts, no_forecast = [], []
    twins = Counter(t["name"] for t in targets)  # "Pay-As-You-Go" twice needs the id to tell them apart
    targets = [{**t, "name": f"{t['name']} ({t['id'][:8]})"} if twins[t["name"]] > 1 else t for t in targets]
    tag = choose_tag(az, targets, tag, log)

    def run(target, groupings, start=None, end=None, **kw):
        aggs = columns.setdefault(target["id"], list(AGGREGATIONS))
        while True:
            try:
                rows = query(az, target["scope"], start or dates[0], end or dates[-1], groupings, metric, aggs[0],
                             tenant=target.get("tenant"), **kw)
                settled.add(target["id"])
                return rows
            except AzureError as e:
                # step down only when Azure objects to the cost columns, and only until a query has worked: after
                # that the columns are known good, and a 400 (a tag it can't group by) is that query's own problem
                about_columns = COLUMN_ERROR.search(str(e))
                if e.status != 400 or len(aggs) == 1 or not about_columns or target["id"] in settled:
                    raise
                aggs.pop(0)

    def add(view, key, r, day=None):
        """`day` is set for period totals, which have no UsageDate: they land on their period's first day."""
        i = day if day is not None else index.get(usage_day(r.get("UsageDate")))
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
            rows = [(r, None) for r in run(t, VIEWS["resource"], max_pages=MAX_RESOURCE_PAGES)]
        except TooManyPages:
            # too many resources x days: read one total per resource and period instead (two small queries)
            log(f"  {t['name']}: too many resources for daily detail, reading period totals instead")
            prev = run(t, VIEWS["resource"], start=dates[0], end=dates[days - 1], granularity=None)
            cur = run(t, VIEWS["resource"], start=dates[days], end=dates[-1], granularity=None)
            rows = [(r, 0) for r in prev] + [(r, days) for r in cur]
            fallback.append(t["name"])
        for r, day in rows:
            rg, rid = r.get("ResourceGroupName") or "", (r.get("ResourceId") or "").lower()
            key = group_key(t, rg, rid)
            label = rg or "(no resource group)"
            names["resource"][key] = f"{label} · {t['name']}" if len(targets) > 1 else label
            add("resource", (key, rid or "(no resource)"), r, day)

        log(f"  {t['name']}: regions ...")
        for r in run(t, VIEWS["region"]):
            add("region", (region_name(r.get("ResourceLocation")), service_of(r)), r)
        if tag:
            log(f"  {t['name']}: tag {tag} ...")
            try:
                for r in run(t, [{"type": "TagKey", "name": tag}, "ServiceName"]):
                    add("tag", (r.get("TagValue") or "", service_of(r)), r)  # no value: spend on untagged resources
            except AzureError as e:
                log(f"  {t['name']}: skipped the tag view ({e})")
                tag = None  # without this target's part it wouldn't add up to the bill
        log(f"  {t['name']}: forecast ...")
        try:
            f = month_forecast(az, t, metric, today or dt.date.today(), columns[t["id"]][0][0])
            why = "Azure has none"
        except AzureError as e:
            f, why = None, f"HTTP {e.status}"
        if f:
            forecasts.append(f)
        else:
            no_forecast.append(f"{t['name']} ({why})")
        subs.append({"id": t["id"], "name": t["name"], "currency": next((c for c, _ in currencies.most_common() if c), None),
                     "tenant": t.get("tenant")})  # the page's portal links open this tenant's directory

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

    findings, graph_error = None, None
    if graph and with_advisor:  # Resource Graph, like Advisor, reads subscriptions
        log("  Resource Graph ...")
        findings, graph_error = graph_findings(az, with_advisor, log)

    found = {s["currency"] for s in subs if s["currency"]}
    currency, usd, mixed, usd_rate = pick_currency(found, raw, log)
    forecast, forecast_note = None, None
    billed_in = {c for f in forecasts for c in f["currencies"] if c}
    if no_forecast:
        forecast_note = "no forecast for " + ", ".join(no_forecast)
    elif billed_in - {currency}:
        forecast_note = f"the forecast came in a different currency ({', '.join(sorted(billed_in))}) than the bill ({currency})"
    elif forecasts:
        actual, rest = (round(sum(f[k] for f in forecasts), 2) for k in ("actual", "forecast"))
        forecast = {"month": (today or dt.date.today()).strftime("%Y-%m"), "actual": actual, "forecast": rest,
                    "total": round(actual + rest, 2)}
    if forecast_note:
        log(f"  skipped this month's forecast: {forecast_note}")
    log(f"  done: {az.requests} requests (Cost Management queries are free)")
    views = {v: {"dims": dims, "names": names[v], "rows": fold(raw[v], len(dates), usd)} for v, dims in VIEWS.items()}
    if tag:
        views["tag"] = {"dims": ["TagValue", "ServiceName"], "names": {}, "tag": tag,
                        "rows": fold(raw["tag"], len(dates), usd)}
    return {
        "days": dates, "split": days,
        "views": views,
        "currency": currency,
        "subscriptions": subs, "resource_fallback": fallback,
        "advisor": recs, "advisor_error": advisor_error,
        "mixed_currencies": mixed, "usd_rate": usd_rate,
        "forecast": forecast, "forecast_note": forecast_note,
        "graph": findings, "graph_error": graph_error, "demo": False,
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


TINY = "(under a cent each)"


def pack(rows):
    """Rows too small to keep alone (under half a cent over both periods) are summed per group into one TINY row,
    so a thousand tiny charges still add up. A charge in one period and its refund in the other count in both."""
    kept, tiny = {}, {}
    for k, d in rows.items():
        if sum(abs(v) for v in d) >= 0.005:
            kept[k] = d
        else:
            acc = tiny.setdefault(k[0], [0.0] * len(d))
            for i, v in enumerate(d):
                acc[i] += v
    for group, d in tiny.items():
        if sum(abs(v) for v in d) >= 0.005:
            kept[(group, TINY)] = d
    return [{"k": list(k), "d": [round(v, 4) for v in d]} for k, d in kept.items()]


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
    ("Virtual Machines", "E8s v5", 38, -0.7, [(D, "eu west", "rg-etl", f"{VM}/vm-etl-worker", 1)]),  # scaled down: a drop
    ("Virtual Machines", "NC8as T4 v3", 22, 0.3, [(D, "us east", "rg-ml", f"{VM}/vm-gpu-train", 1)]),  # NCv3 retired in 2025
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
    # their VMs were deleted, the disks weren't: Resource Graph finds them unattached (demo_graph)
    ("Storage", "P10 LRS Disk", 0.65, 0, [(S, "us east", "rg-legacy", "microsoft.compute/disks/vm-old-ftp-osdisk", 1)]),
    ("Storage", "P20 LRS Disk", 2.41, 0, [(S, "us east", "rg-legacy", "microsoft.compute/disks/vm-old-ftp-datadisk", 1)]),
    ("Storage", "E10 LRS Disk", 0.32, 0, [(X, "us west 2", "rg-sandbox", "microsoft.compute/disks/disk-test-restore", 1)]),
    # the site-to-site VPN to the old office: the office closed, the gateway didn't (demo_graph finds it unused)
    ("VPN Gateway", "VpnGw1", 4.56, 0, [(S, "us east", "rg-network-staging",
                                        "microsoft.network/virtualnetworkgateways/vgw-staging", 1)]),
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


DEMO_TAG = "environment"
DEMO_ENV = {P: "production", S: "staging", D: "production", X: "dev"}
DEMO_UNTAGGED = {"", "rg-legacy", "rg-backup", "rg-ai-sandbox", "mc_rg-aks-prod_aks-prod_eastus"}  # the usual gaps


def demo_env(sub, rg):
    return "" if rg in DEMO_UNTAGGED else DEMO_ENV[sub]


def demo_graph():
    def found(check, sub, rg, path):
        return {"check": check, "id": demo_rid(sub, rg, path), "name": path.rsplit("/", 1)[-1],
                "resourceGroup": rg, "subscriptionId": sub}

    return [found("unattached-disk", S, "rg-legacy", "microsoft.compute/disks/vm-old-ftp-osdisk"),
            found("unattached-disk", S, "rg-legacy", "microsoft.compute/disks/vm-old-ftp-datadisk"),
            found("unattached-disk", X, "rg-sandbox", "microsoft.compute/disks/disk-test-restore"),
            found("old-snapshot", P, "rg-backup", "microsoft.compute/snapshots/snap-vm-app-01-2025"),
            found("unused-ip", X, "rg-sandbox", "microsoft.network/publicipaddresses/pip-old-test"),
            found("lonely-gateway", S, "rg-network-staging", "microsoft.network/virtualnetworkgateways/vgw-staging")]


def demo_forecast(views, dates, today):
    """This month so far from the demo's own days, and the last week's daily average for the days still to come."""
    daily = [sum(r["d"][i] for r in views["service"]["rows"]) for i in range(len(dates))]
    month = today.strftime("%Y-%m")
    last = (today.replace(day=1) + dt.timedelta(32)).replace(day=1) - dt.timedelta(1)
    actual = round(sum(v for d, v in zip(dates, daily) if d.startswith(month)), 2)
    # the days still to come this month: after the demo's last day, or all of them when that day is last month's
    since = max(dt.date.fromisoformat(dates[-1]), today.replace(day=1) - dt.timedelta(1))
    rest = round(sum(daily[-7:]) / 7 * (last - since).days, 2)
    return {"month": month, "actual": actual, "forecast": rest, "total": round(actual + rest, 2)}


def demo(days, today=None):
    today = today or dt.date.today()
    rnd = random.Random(7)
    end = last_full_day(today)
    n = 2 * days
    dates = [(end - dt.timedelta(n - 1 - i)).isoformat() for i in range(n)]
    weekend = [dt.date.fromisoformat(d).weekday() >= 5 for d in dates]
    rows = {v: {} for v in [*VIEWS, "tag"]}
    names = {v: {} for v in VIEWS}
    names["subscription"] = dict(DEMO_SUBS)
    for service, meter, per_day, growth, spots in DEMO:
        bursty = any(w in meter for w in ("Tokens", "Data Transfer", "Ingestion", "Operations", "Processed", "Duration"))
        # plans, provisioned databases, nodes and gateways bill a fixed hourly price: the same every day, like real bills
        fixed = re.search(r"App$|vCore|Instance|Node$|Uptime SLA|Unit$|Gateway$|^VpnGw|Endpoint$|Public IP$|Base Fees|Disk$", meter)
        for sub, region, rg, path, share in spots:
            daily = []
            for i in range(n):
                m = (1 + growth * i / (n - 1)) / (1 + growth / 2)  # keep the average near per_day
                m *= 0.8 if bursty and weekend[i] else 1
                noise = rnd.gauss(1, 0.06)  # drawn either way, so the other meters' numbers don't move
                daily.append(max(0.0, per_day * share * m * (1 if fixed else noise)))
            rid = demo_rid(sub, rg, path)
            gkey = group_key({"scope": f"/subscriptions/{sub}"}, rg, rid)
            names["resource"][gkey] = f"{rg or '(no resource group)'} · {DEMO_SUBS[sub]}"
            for view, key in (("service", (service, meter)), ("subscription", (sub, service)),
                              ("region", (region_name(region), service)), ("resource", (gkey, rid)),
                              ("tag", (demo_env(sub, rg), service))):
                acc = rows[view].setdefault(key, [0.0] * n)
                for i, v in enumerate(daily):
                    acc[i] += v
    # a cancelled Cosmos DB reservation refunded as one negative day: no box can show it, so the header notes it
    rid = demo_rid(P, "rg-data-prod", "microsoft.documentdb/databaseaccounts/cosmos-catalog")
    gkey = group_key({"scope": f"/subscriptions/{P}"}, "rg-data-prod", rid)
    for view, key in (("service", ("Azure Cosmos DB", "Reserved 100 RU/s")), ("subscription", (P, "Azure Cosmos DB")),
                      ("region", ("East US", "Azure Cosmos DB")), ("resource", (gkey, rid)),
                      ("tag", (demo_env(P, "rg-data-prod"), "Azure Cosmos DB"))):
        rows[view].setdefault(key, [0.0] * n)[max(days, n - 6)] -= 150.0
    # a one-off backfill: Data Factory moved a year of data in one day, a spike for "worth a look"
    rid = demo_rid(D, "rg-etl", "microsoft.datafactory/factories/adf-etl")
    gkey = group_key({"scope": f"/subscriptions/{D}"}, "rg-etl", rid)
    for view, key in (("service", ("Azure Data Factory v2", "Cloud Data Movement")), ("subscription", (D, "Azure Data Factory v2")),
                      ("region", ("West Europe", "Azure Data Factory v2")), ("resource", (gkey, rid)),
                      ("tag", (demo_env(D, "rg-etl"), "Azure Data Factory v2"))):
        rows[view].setdefault(key, [0.0] * n)[max(days, n - 9)] += 180.0
    views = {v: {"dims": VIEWS[v], "names": names[v], "rows": pack(rows[v])} for v in VIEWS}
    views["tag"] = {"dims": ["TagValue", "ServiceName"], "names": {}, "tag": DEMO_TAG, "rows": pack(rows["tag"])}
    return {
        "days": dates, "split": days,
        "views": views,
        "currency": "USD",
        "subscriptions": [{"id": k, "name": v, "currency": "USD"} for k, v in DEMO_SUBS.items()],
        "resource_fallback": [], "advisor": demo_advisor(), "advisor_error": None,
        "forecast": demo_forecast(views, dates, today), "forecast_note": None,
        "graph": demo_graph(), "graph_error": None, "demo": True,
    }


# ---------------------------------------------------------------- worth a look

# Known money pits: (service pattern, meter pattern, why, monthly floor). First match wins; a rule with a floor only
# fires once the meter runs at least that much a month. `{profiles}` in a reason is filled in from the monthly amount.
VM_END = r"(?:$|/| Low Priority| Spot| Promo)"
RETIRING_MAY_2028 = r"^(?:DS?\d+(?:-\d+)?(?: v2)?|L\d+s)" + VM_END  # D, Ds, Dv2, Dsv2 (and constrained DS13-4 v2), Ls
RETIRING_NOV_2028 = r"^(?:(?:Basic[ ._])?A\d+m?(?: v2)?|F\d+s?(?: v2)?|L\d+s v2|GS?\d+|B\d+[a-z]*)" + VM_END  # Av2, F*, Lsv2, G*, B v1
NO_NEW_RESERVATIONS = r"^[DE]\d+(?:-\d+)?(?:i|s|is)? v3" + VM_END  # Dv3, Dsv3, Ev3, Esv3, constrained E16-4s, isolated E64i/E64is
PITS = [
    (r"^(?:Log Analytics|Azure Monitor)$", r"^(?!Basic |Auxiliary ).*Data Ingestion",
     "Log Analytics ingestion — trim noisy tables, use Basic logs, or a commitment tier past 100 GB/day", 0),
    (r"^Azure Front Door Service$", r"^Premium Base Fees",
     "Front Door Premium — Standard is about $35 a month against about $330; Premium is only needed for managed "
     "WAF rules or Private Link origins", 0),
    (r"^Azure Front Door Service$", r"^Standard Base Fees",
     "about {profiles} Front Door Standard profiles at $35 a month each; one profile can hold many endpoints and domains", 70),
    (r"^SQL Database$", r"DTUs?$",  # below ~$150 a month a DTU database is cheaper than any provisioned vCore option
     "DTU databases — vCore can be reserved and use Azure Hybrid Benefit; serverless pauses when idle", 150),
    (r"^Azure DevOps$", r"Concurrent Job|Basic User",
     "Azure DevOps seats and hosted jobs — the first 5 Basic users are free; remove inactive users, check pipeline concurrency", 0),
    (r"^Virtual Network$", r"Private Endpoint", "private endpoints — $0.01 an hour each plus data; remove the ones nothing uses", 0),
    (r"^Azure Cosmos DB$", r"^100 RU/s$", "provisioned Cosmos DB throughput — autoscale or serverless costs less when load varies", 0),
    (r"^Azure Monitor$", r"at 1 Minute Frequency", "1-minute alert rules — they cost more than 5- or 15-minute ones; relax the ones "
     "where minutes don't matter", 0),
    (r"^(?:Redis Cache|Azure Cache for Redis)$", r"",
     "Azure Cache for Redis retires on 30 Sep 2028 (Enterprise tiers on 31 Mar 2027); plan the move to Azure Managed Redis", 0),
    (r"^Azure App Service$", r"^S\d App$", "Standard App Service plans can't be reserved or use a savings plan; Premium v3 plans can", 0),
    (r"^Bandwidth$", r"Data Transfer Out", "data transfer out — keep traffic in one region, cache at the edge", 0),
    (r"^NAT Gateway$", r"Data Processed", "NAT data processing — service or private endpoints for Storage, SQL and ACR skip it", 0),
    (r"^Azure Firewall$", r"Data Processed|Premium", "Azure Firewall — processing and Premium add up; route only what needs inspection", 0),
    (r"^Virtual Network$", r"^Basic .*Public IP", "Basic public IPs — the Basic SKU retired on 30 Sep 2025, move to Standard", 0),
    (r"^Virtual Network$", r"Public IP|IP Address Hours", "public IPs — billed per hour each; release the ones nothing uses", 0),
    (r"^Storage$", r"Snapshot", "snapshots — prune old ones; incremental snapshots on Standard storage cost less", 0),
    (r"^Virtual Machines$", RETIRING_MAY_2028,
     "retiring VM series — D, Ds, Dv2, Dsv2 and Ls stop on 1 May 2028; current generations cost less for the same work", 0),
    (r"^Virtual Machines$", RETIRING_NOV_2028,
     "retiring VM series — F, Fs, Fsv2, Lsv2, G, Gs, Av2 and B-series v1 stop on 15 Nov 2028; current generations cost "
     "less for the same work", 0),
    (r"^Virtual Machines$", NO_NEW_RESERVATIONS,
     "Dv3 and Ev3 sizes — new reservations stopped in July 2026; v5 and v6 sizes can still be reserved", 0),
    (r"^Azure App Service$", r"^P\d+ ?v2 App", "Premium v2 App Service plans — Premium v3 gives more per dollar and can be reserved", 0),
    (r"", r"Extended Security Update", "Extended Security Updates — upgrade the OS or SQL version to stop paying for them", 0),
]


def pit(service, meter, monthly=0.0):
    """Why this meter is worth a look, or None. `monthly` is what it runs at a month; some rules have a floor."""
    for s, m, why, floor in PITS:
        if re.search(s, service) and re.search(m, meter) and monthly >= floor:
            return why.format(profiles=round(monthly / 35))
    return None


def advisor_recs(az, target):
    """Azure Advisor's cost recommendations for one subscription, one per (kind, resource, SKU).
    Advisor lists each reservation once per term and look-back period; keep the biggest saving."""
    flt = urllib.parse.quote("Category eq 'Cost'")
    url = f"{target['scope']}/providers/Microsoft.Advisor/recommendations?api-version=2023-01-01&$filter={flt}"
    best = {}
    while url:
        page = az.call("GET", url, tenant=target.get("tenant"))
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
        "region": ext.get("region") or ext.get("location"),  # a reservation is per region
        "annual_savings": float(savings) if savings not in (None, "") else None,
        "currency": ext.get("savingsCurrency"),
        "subscription": target["name"],
    }
    return (p.get("recommendationTypeId"), resource, rec["sku"], rec["region"]), rec


# ---------------------------------------------------------------- AI export

AI_INSTRUCTIONS = (
    "This is an Azure cost breakdown exported by aztree; `schema` is the version of its format. "
    "Amounts are in `currency`, for the cost type in `metric` "
    "(ActualCost books reservation and savings plan purchases on the day they were bought; AmortizedCost spreads them "
    "over the term). When `currency` is \"mixed\", the subscriptions bill in the currencies listed in `currencies` and "
    "Azure didn't convert them, so totals add different currencies: compare amounts within one subscription only. "
    "`usd_rate` is units of `currency` per US dollar, as this bill priced them (null when unknown). "
    "`current` is the most recent period and `previous` is the equally long period before it. "
    "Line items are Azure meters grouped by service; `totals.credits_and_refunds` is the part of `current` that comes "
    "from negative line items (credits, refunds). `resource_fallback` names subscriptions with so many resources that "
    "aztree read one total per resource and period for them: their resource groups' amounts are right, but daily "
    "detail and dev/test always-on checks aren't available there. `flags` are known cost traps matched on meter names. `hints` is the "
    "list the page shows under Worth a look: news (growers, one-off spikes) taking turns with to-dos (money pits, "
    "always-on compute in dev/test groups, idle resources, steady spend worth committing). `advisor` holds "
    "Azure Advisor's cost recommendations, one per kind, resource and SKU, with the largest annual saving Advisor "
    "reported; recommendations that cover the same usage (a reservation and a savings plan, a 1-year and a 3-year term) "
    "are alternatives, not additive. A tip's `covers` lists the meters of the kind its reservation or savings plan "
    "commits to, matched by service and meter, not by SKU or region, across the whole bill: it is related spend, not "
    "what the commitment would cover. A reservation covers only its SKU family and region, so less than this; a savings "
    "plan spans SKUs and regions but only up to its hourly commitment. In a multi-subscription export it can include "
    "other subscriptions' usage. "
    "`by_tag` splits the bill by the values of the tag named in `tag`; the row marked `untagged: true` (shown as "
    "\"(untagged)\") is spend on resources without it. `forecast` is Azure's own forecast for the current calendar month: `actual` is billed so far, `forecast` is "
    "still to come and `total` is both. Hints of kind `idle` are resources Azure Resource Graph found billing while "
    "doing nothing (VMs stopped but still allocated, unattached disks, Premium disks of long-stopped VMs, old or "
    "Premium snapshots, unused public IPs, gateways, load balancers and Application Gateways that nothing uses, "
    "disconnected private endpoints, App Service plans with no apps, elastic pools with no databases), with what they "
    "cost in the current period. "
    "`source`, when present, says the data came from Cost Management FOCUS export files instead of the API, so "
    "Advisor, the idle checks and the forecast didn't run; `source.warnings` lists what the files lacked or held twice "
    "(an export run missing files, days with no rows, days in more than one file), so totals may be low or double "
    "there: say so before drawing conclusions. `totals.commitment_purchases` is the net cost of "
    "reservation and savings plan purchases in the current period (ActualCost only). The `(untagged)` row in "
    "`by_tag` has `how_to_fix` when untagged spend is a tenth of the bill or more. "
    "Please: 1) explain what drives the cost, 2) explain notable changes vs the previous period, "
    "3) suggest concrete savings, each with an estimated monthly saving and how to verify it. "
    "Levers to consider: reservations and savings plans for steady compute and databases; Azure Hybrid Benefit for "
    "Windows Server and SQL Server licenses already owned; dev/test pricing for non-production subscriptions; "
    "right-sizing and auto-shutdown for VMs and App Service plans; blob access tiers (cool, cold, archive) and lifecycle "
    "rules; Log Analytics commitment tier pricing, Basic logs and shorter table retention; and keeping data transfer "
    "inside one region."
)
TOP_RESOURCES = 20  # per resource group in the export; the rest are summed
# said on the (untagged) row of by_tag, and on the page, when untagged spend is a tenth of the bill or more
UNTAGGED_FIX = ("Tag inheritance in Cost Management (EA, MCA and partner Azure plans) copies subscription and "
                "resource-group tags onto usage from the start of the month, and an Azure Policy that denies resources "
                "without the tag keeps new ones tagged. Purchases can't be tagged.")


def spike(daily, split, floor):
    """The biggest one-off day in the current period (days from `split` on), or None. A spike is at least `floor`
    above the meter's usual day (the median of the 14 days before it) and at least 3x it, or any amount on a meter
    that's usually zero. More than 3 such days is a trend, which the growers cover. A charge with one half its size
    27-31 days earlier is a monthly bill, not a spike."""
    found = []
    for i in range(split, len(daily)):
        prior = daily[max(0, i - 14):i]
        if len(prior) < 7:
            continue
        usual = statistics.median(prior)
        excess = daily[i] - usual
        if excess >= floor and (usual <= 0 or daily[i] >= 3 * usual):
            found.append({"day": i, "usual": usual, "excess": excess})
    if not 1 <= len(found) <= 3:
        return None
    best = max(found, key=lambda s: s["excess"])
    i = best["day"]
    if any(v >= 0.5 * daily[i] for v in daily[max(0, i - 31):max(0, i - 26)]):
        return None
    return best


# Dev/test by name: a whole word (tms-dev-rg, acme-staging), not a substring (devices, contest).
DEV_TEST = re.compile(r"(?:^|[-_ .])(?:dev|devtest|development|tst|test|testing|stg|staging|stage|qa|uat|sbx|sandbox"
                      r"|non-?prod|pre-?prod)(?:[-_ .0-9]|$)", re.I)
MIN_PATTERN_DAYS = 7  # "flat" or "steady" means little over fewer days than a week
# Compute that bills by the hour whether used or not, and can be scaled down, stopped or made serverless.
ALWAYS_ON = re.compile(r"/providers/microsoft\.(?:compute/(?:virtualmachines|virtualmachinescalesets)|web/serverfarms"
                       r"|sql/servers/[^/]+/(?:databases|elasticpools)|documentdb/databaseaccounts"
                       r"|containerservice/managedclusters)/", re.I)


def always_on(data, floor):
    """Dev/test resource groups whose compute runs flat all period (lowest day at least 90% of the highest):
    one hint per group. Needs daily data: a subscription read as period totals has its days at zero but the first, so
    its resources never look flat, and the other subscriptions are still judged."""
    if len(data["days"]) - data["split"] < MIN_PATTERN_DAYS:
        return []
    view, split = data["views"]["resource"], data["split"]
    subs = {s["id"].lower(): s["name"] for s in data.get("subscriptions", [])}
    groups = {}
    for r in view["rows"]:
        key, rid = r["k"]
        cur = r["d"][split:]
        if not ALWAYS_ON.search(rid) or min(cur) <= 0 or min(cur) < 0.9 * max(cur):
            continue
        label = view["names"].get(key, key)
        sub = subs.get(key.split("/")[2].lower(), "") if key.startswith("/subscriptions/") else ""
        if not (DEV_TEST.search(label) or DEV_TEST.search(sub)):
            continue
        g = groups.setdefault(key, {"label": label, "current": 0.0, "resources": 0})
        g["current"] += sum(cur)
        g["resources"] += 1
    return [{"kind": "devtest", "group": k, "label": g["label"], "resources": g["resources"],
             "current": round(g["current"], 2), "amount": round(g["current"], 2)}
            for k, g in groups.items() if g["current"] >= floor]


# Meters a reservation or savings plan can cover, by kind: (service pattern, meter pattern). Advisor names a
# reservation in its wording ("Consider SQL PaaS DB reserved instance ...") and a savings plan only by its SKU.
RESERVABLE = {
    "sql": (r"^SQL (?:Database|Managed Instance)$", r"vCore"),
    "app": (r"^Azure App Service$", r"^(?:P\d+ ?m?v3|I\d+ ?v2) App"),
    "functions": (r"^Functions$", r"^Premium"),
    "cosmos": (r"^Azure Cosmos DB$", r"RU/s"),
    "vm": (r"^Virtual Machines$", r"^(?!.* (?:Spot|Low Priority)$)"),  # Spot capacity can't be reserved
    "redis": (r"^(?:Redis Cache|Azure Cache for Redis)$", r"^P\d|Enterprise"),
    "postgres": (r"^Azure Database for PostgreSQL", r"vCore"),
    "mysql": (r"^Azure Database for MySQL", r"vCore"),
}
# "SQL" alone would also catch "Azure Synapse Analytics (formerly SQL DW)", which covers no SQL Database meter
RESERVATION_WORDS = [(r"SQL (?:PaaS DB|Database|Managed Instance)", "sql"), (r"App Service", "app"), (r"Cosmos", "cosmos"),
                     (r"virtual machine", "vm"), (r"Redis", "redis"), (r"PostgreSQL", "postgres"), (r"MySQL", "mysql")]
SAVINGS_PLANS = {"Compute_Savings_Plan": ["vm", "app", "functions"], "Database_Savings_Plan": ["sql", "cosmos", "postgres", "mysql"]}


def commitment_kinds(rec):
    """What an Advisor tip would commit to: from its wording for a reservation, from its SKU for a savings plan."""
    problem = rec.get("problem") or ""
    m = re.match(r"Consider (.+?) reserved (?:instance|capacity)", problem, re.I)
    if m:
        return [kind for words, kind in RESERVATION_WORDS if re.search(words, m.group(1), re.I)]
    if "savings plan" in problem.lower():
        return SAVINGS_PLANS.get(rec.get("sku") or "", [])
    return []


def reservable(service, meter, kinds=tuple(RESERVABLE)):
    return any(re.search(RESERVABLE[k][0], service) and re.search(RESERVABLE[k][1], meter) for k in kinds)


def link_tip(rec, line_items, n):
    """An Advisor tip plus the meters of the kind its reservation or savings plan commits to (biggest first) and their
    monthly pace: related spend, matched by service and meter, not the tip's SKU or region. Other tips cover nothing."""
    kinds = commitment_kinds(rec)
    covers = [{"service": x["service"], "meter": x["meter"], "current": x["current"]} for x in line_items
              if kinds and x["current"] > 0 and reservable(x["service"], x["meter"], kinds)]
    return {**rec, "covers": covers, "covers_monthly": round(sum(c["current"] for c in covers) / n * 30.4, 2)}


def steady(data, line_keys, n, rate=1.0):
    """Reservable spend that barely moves (no zero day, day-to-day spread within 10%) and runs at least $100 a
    month, one hint per service. Only asked for when Advisor, which knows what's already reserved, isn't there.
    `amount` is the period's dollars, like every other to-do, so they sort together; `monthly` is for the text."""
    split, by_service = data["split"], {}
    if n < MIN_PATTERN_DAYS:
        return []
    for r in data["views"]["service"]["rows"]:
        svc, meter = r["k"]
        cur = r["d"][split:]
        if (svc, meter) not in line_keys or not reservable(svc, meter) or min(cur) <= 0:
            continue
        if statistics.pstdev(cur) <= 0.10 * statistics.mean(cur):
            s = by_service.setdefault(svc, {"current": 0.0, "meters": []})
            s["current"] += sum(cur)
            s["meters"].append(meter)
    return [{"kind": "steady", "service": svc, "meters": s["meters"], "current": round(s["current"], 2),
             "amount": round(s["current"], 2), "monthly": round(s["current"] / n * 30.4, 2)}
            for svc, s in by_service.items() if s["current"] / n * 30.4 >= 100 * rate]


# Resource Graph checks (see IDLE_QUERIES): what one is called, what several are called, and what to do
IDLE = {
    "stopped-vm": ("stopped VM", "stopped VMs",
                   "stopped from inside the OS but still allocated, so compute keeps billing; Stop in the portal deallocates"),
    "unattached-disk": ("unattached disk", "unattached disks", "attached to no VM; snapshot what you need, then delete"),
    "premium-disk-off": ("Premium disk of a stopped VM", "Premium disks of stopped VMs",
                         "the VM has been deallocated for over 30 days, but a Premium disk bills the same; switch it to "
                         "Standard HDD while the VM stays off"),
    "unused-ip": ("unused public IP", "unused public IPs",
                  "attached to nothing, and public IPs bill by the hour; release what nothing uses"),
    "old-snapshot": ("old snapshot", "old snapshots", "older than 90 days; delete what no restore plan needs"),
    "premium-snapshot": ("snapshot on Premium storage", "snapshots on Premium storage",
                         "a full copy at Premium prices; incremental snapshots on Standard storage cost less"),
    "empty-plan": ("empty App Service plan", "empty App Service plans",
                   "no apps, but a plan bills by the hour whether used or not; delete or scale down"),
    "lonely-nat": ("unused NAT gateway", "unused NAT gateways", "on no subnet, so routing nothing, but billed by the hour"),
    "empty-appgw": ("Application Gateway with no backends", "Application Gateways with no backends",
                    "its backend pools are empty, so it routes to nothing, but it bills by the hour; stop or delete it"),
    "empty-lb": ("load balancer with no backends", "load balancers with no backends",
                 "no backend pool or NAT rule, so it balances nothing, but it still bills; delete it"),
    "unprovisioned-circuit": ("unprovisioned ExpressRoute circuit", "unprovisioned ExpressRoute circuits",
                              "the connectivity provider hasn't set it up, or took it down, but the circuit bills; "
                              "finish the setup or delete it"),
    "disconnected-endpoint": ("disconnected private endpoint", "disconnected private endpoints",
                              "what it pointed to is gone or refused it, but it bills by the hour; delete it"),
    "lonely-gateway": ("unused VPN or ExpressRoute gateway", "unused VPN or ExpressRoute gateways",
                       "no connections and no point-to-site clients, but a gateway bills by the hour; delete what "
                       "nothing will connect to"),
    "empty-pool": ("empty SQL elastic pool", "empty SQL elastic pools",
                   "no databases, but a pool bills for its capacity; delete it or move databases in"),
}
IDLE_FLOOR = 1.0  # dollars over the period: a check that costs less isn't worth a line


def idle(data, rate=1.0):
    """Resource Graph's findings with what each cost this period (from the resource view): one hint per check,
    biggest resource first. What cost nothing is left out: a free empty plan or a $0 IP isn't money."""
    split, cost, group = data["split"], {}, {}
    for r in data["views"]["resource"]["rows"]:
        key, rid = r["k"]
        cost[rid] = cost.get(rid, 0.0) + sum(r["d"][split:])
        group[rid] = key
    checks = {}
    for f in data.get("graph") or []:
        rid = (f.get("id") or "").lower()
        if f.get("check") in IDLE and cost.get(rid, 0.0) >= 0.005:
            checks.setdefault(f["check"], []).append({"id": rid, "name": f.get("name") or rid.rsplit("/", 1)[-1],
                                                      "group": group[rid], "current": round(cost[rid], 2)})
    hints = []
    for check, found in checks.items():
        total = round(sum(x["current"] for x in found), 2)
        if total < IDLE_FLOOR * rate:
            continue
        one, many, why = IDLE[check]
        found.sort(key=lambda x: -x["current"])
        single = len(found) == 1
        hints.append({"kind": "idle", "check": check, "label": found[0]["name"] if single else f"{len(found)} {many}",
                      "reason": f"{one}: {why}" if single else f"{why}; biggest: {found[0]['name']}",
                      "resources": found, "current": total, "amount": total})
    return hints


def summarize(data):
    """Turn the raw daily data into a compact JSON an AI agent can reason about."""
    days, split = data["days"], data["split"]
    n = len(days) - split
    rate = data.get("usd_rate") or 1.0  # the bill's currency per US dollar: the rules' floors and prices are in dollars

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

    growing = sorted((x for x in line_items
                      if x["change"] >= max(rate, grand * 0.005) and (x["change_pct"] is None or x["change_pct"] > 20)),
                     key=lambda x: -x["change"])
    growers = growing[:10]
    drops = [x for x in line_items  # a refund (negative now) is a credit, not a saving: it goes in credits_and_refunds
             if x["current"] > -0.01 and x["change"] <= -max(rate, grand * 0.005) and (x["change_pct"] or 0) < -20]
    drops = sorted(drops, key=lambda x: x["change"])[:10]
    # worth a look: news (what changed) takes turns with to-dos (what to fix), each sorted by its own dollars
    def hint(kind, x, amount, **extra):
        return {"kind": kind, "service": x["service"], "meter": x["meter"], "current": x["current"],
                "previous": x["previous"], "change": x["change"], "amount": money(amount), **extra}

    flags, todos = [], []
    for x in line_items:  # biggest first
        why = pit(x["service"], x["meter"], x["current"] / n * 30.4 / rate)  # the rules are in US dollars
        if why and x["current"] >= grand * 0.002:
            flags.append({"service": x["service"], "meter": x["meter"], "current": x["current"], "reason": why})
            todos.append(hint("pit", x, x["current"], reason=why))
    items = {(x["service"], x["meter"]): x for x in line_items}
    spiked = {}
    for r in data["views"]["service"]["rows"]:
        s = spike(r["d"], split, floor=max(10 * rate, grand * 0.0025))
        if s and tuple(r["k"]) in items and items[tuple(r["k"])]["current"] > 0:  # a charge refunded in full is no news
            spiked[tuple(r["k"])] = hint("spike", items[tuple(r["k"])], s["excess"], date=days[s["day"]],
                                         day_cost=money(r["d"][s["day"]]), usual=money(s["usual"]))
    advisor = data.get("advisor")
    if advisor is not None:
        advisor = [link_tip(rec, line_items, n) for rec in advisor]
    # steady spend is Advisor's job when it's there: it prices the saving and knows what's already reserved
    advisor_has_commitments = bool(advisor) and not data.get("advisor_error") and any(commitment_kinds(r) for r in advisor)
    if not advisor_has_commitments and data.get("metric") != "AmortizedCost":  # amortized, reserved usage looks flat too
        todos += steady(data, {(x["service"], x["meter"]) for x in line_items}, n, rate)
    todos = sorted(todos + always_on(data, max(10 * rate, grand * 0.002)) + idle(data, rate), key=lambda h: -h["amount"])
    flagged = {(f["service"], f["meter"]) for f in flags}
    news = sorted([*spiked.values(), *(hint("grower", x, x["change"]) for x in growing
                                       if (x["service"], x["meter"]) not in flagged | set(spiked))],
                  key=lambda h: -h["amount"])
    hints = [h for pair in zip_longest(news, todos) for h in pair if h]

    daily = [0.0] * len(days)
    for r in data["views"]["service"]["rows"]:
        for i, v in enumerate(r["d"]):
            daily[i] += v

    tag_view = data["views"].get("tag")
    by_tag = breakdown("tag", "value", "service") if tag_view else None
    for row in by_tag or []:
        if row["value"] == "":  # flagged, so a real tag value "(untagged)" stays a different row
            row.update(value="(untagged)", untagged=True)
            if row["share_pct"] >= 10:
                row["how_to_fix"] = UNTAGGED_FIX

    return {
        "tool": "aztree",
        "schema": SCHEMA,
        "instructions_for_ai": AI_INSTRUCTIONS,
        "generated": data.get("generated"),
        "metric": data.get("metric", "ActualCost"),
        "currency": "mixed" if data.get("mixed_currencies") else data.get("currency", "USD"),
        "currencies": data.get("mixed_currencies") or [data.get("currency", "USD")],
        "usd_rate": data.get("usd_rate"),
        "subscriptions": [{k: v for k, v in s.items() if k != "tenant"} for s in data.get("subscriptions", [])],  # not needed there
        "demo_data": bool(data.get("demo")),
        "period": {
            "current": {"start": days[split], "end": days[-1], "days": n},
            "previous": {"start": days[0], "end": days[split - 1], "days": split},
        },
        "totals": {**entry(grand, grand_prev), "daily_avg": money(grand / n), "monthly_pace": money(grand / n * 30.4),
                   "credits_and_refunds": money(sum(min(x["current"], 0) for x in line_items)),
                   "commitment_purchases": data.get("commitment_purchases")},
        "by_service": [{"service": k, **entry(g["cur"], g["prev"])} for k, g in services if keep(g["cur"], g["prev"])],
        "by_subscription": breakdown("subscription", "subscription_id", "service"),
        "by_region": breakdown("region", "region", "service"),
        "by_resource_group": breakdown("resource", "resource_group", "resource_id", "resources",
                                       label=lambda k, names: names.get(k, k), limit=TOP_RESOURCES),
        "tag": tag_view["tag"] if tag_view else None,
        "by_tag": by_tag,
        "top_growers": growers,
        "top_drops": drops,
        "flags": flags,
        "hints": hints,
        "advisor": advisor,
        "advisor_error": data.get("advisor_error"),
        "resource_fallback": data.get("resource_fallback") or [],
        "graph_error": data.get("graph_error"),
        "forecast": data.get("forecast"),
        "source": data.get("source"),
        "line_items": line_items,
        "daily_totals": [{"date": d, "cost": money(v)} for d, v in zip(days, daily)],
    }


def export(data, path):
    path.write_text(json.dumps(summarize(data), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------- output

def render(data, out):
    html = template()
    page = {**data, "export": summarize(data), "portal": PORTAL}  # the page reads its hints from the export: one source of truth
    # < keeps names like "</script>" or "<!--" from ending the script block early
    blob = json.dumps(page, separators=(",", ":"), ensure_ascii=False).replace("<", "\\u003c")
    out.write_text(html.replace("__AZTREE_DATA__", blob), encoding="utf-8")


def keep_snapshot(data, folder, stamp, kept=HISTORY_KEPT):
    """A dated copy of a run's data, and only the newest `kept` of them. Costs can be read again later; what Resource
    Graph and Advisor found that day can't, so a later version can say what changed since."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"aztree-{stamp:%Y-%m-%d_%H%M%S}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    for old in sorted(folder.glob("aztree-????-??-??_??????.json"))[:-kept]:
        old.unlink()


def resolve_targets(args, az, cli_list=None):
    """`cli_list` lists subscriptions through the Azure CLI (every tenant); without it, ARM lists the token's tenant."""
    if args.scope:
        return [scope_target(args.scope)]
    current = None if (args.all or args.subscription) else current_subscription()
    available = cli_list() if cli_list else list_subscriptions(az)
    return [subscription_target(s) for s in pick_subscriptions(available, args.subscription, args.all, current)]


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # subscription names can hold characters a Windows pipe can't encode
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    saved = home() / "aztree-data.json"
    ap = argparse.ArgumentParser(prog="aztree", description="See where your Azure money goes, as a treemap.")
    ap.add_argument("--version", action="version", version=f"aztree {__version__}")
    ap.add_argument("--demo", action="store_true", help="use fake data (no Azure access needed)")
    who = ap.add_mutually_exclusive_group()
    who.add_argument("--subscription", action="append", default=[], metavar="ID_OR_NAME",
                     help="subscription to read, repeat for more (default: the Azure CLI's current one)")
    who.add_argument("--all", action="store_true", help="read every enabled subscription you can see")
    who.add_argument("--scope", help="any Cost Management scope, e.g. a billing account (not tested yet)")
    who.add_argument("--focus", action="append", metavar="PATH",
                     help="read Cost Management FOCUS export files (CSV) instead of calling Azure: a file or a folder, "
                          "repeat for more")
    ap.add_argument("--days", type=int, default=30, help="period to show, compared with the period before it (default 30)")
    ap.add_argument("--metric", default="ActualCost", choices=["ActualCost", "AmortizedCost"],
                    help="AmortizedCost spreads reservation and savings plan purchases over their term")
    ap.add_argument("--no-advisor", action="store_true", help="skip Azure Advisor's cost recommendations")
    ap.add_argument("--tag", metavar="KEY", help="tag for the tag view (default: the tag on the most resources)")
    ap.add_argument("--no-graph", action="store_true", help="skip the Resource Graph checks for idle resources")
    ap.add_argument("--from", dest="source", nargs="?", const=str(saved), metavar="JSON",
                    help="reopen saved data without calling Azure (default: the last run)")
    ap.add_argument("--out", default=str(home() / "aztree.html"), help="where to write the page (default ~/.aztree/aztree.html)")
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    ap.add_argument("--export", nargs="?", const=str(home() / "aztree-export.json"), metavar="FILE",
                    help="write a summary JSON for an AI agent (default ~/.aztree/aztree-export.json) instead of the page")
    ap.add_argument("--verbose", action="store_true", help="print pages, rows and query units per request")
    args = ap.parse_args(argv)

    if not 1 <= args.days <= 180:
        die("--days must be between 1 and 180 (aztree reads two periods, and a query spans a year at most).")

    if args.focus and (args.source or args.demo):
        die("--focus reads export files, so it can't be combined with --from or --demo.")

    if args.source:
        source = Path(args.source)
        if not source.exists():
            die(f"no saved data at {source}. Read your costs once with `aztree` first (--demo runs aren't saved).")
        data = json.loads(source.read_text(encoding="utf-8"))
    elif args.demo:
        data = demo(args.days)
        data.update(generated=dt.datetime.now().strftime("%Y-%m-%d %H:%M"), metric=args.metric)
    else:
        if args.focus:
            from . import focus  # only --focus needs it
            try:
                data = focus.read(focus.find_runs(args.focus), args.days, args.metric, tag=args.tag)
            except focus.FocusError as e:
                die(str(e))
        else:
            try:
                az = Azure(lambda tenant: get_token(tenant=tenant), verbose=args.verbose)
                cli = shutil.which("az") and not os.environ.get("AZURE_ACCESS_TOKEN")
                targets = resolve_targets(args, az, cli_list=cli_subscriptions if cli else None)
                who_ = targets[0]["name"] if len(targets) == 1 else f"{len(targets)} subscriptions"
                print(f"aztree: reading {who_}, last {args.days} days (+{args.days} before, for comparison)")
                data = fetch(az, targets, args.days, args.metric, advisor=not args.no_advisor, tag=args.tag,
                             graph=not args.no_graph)
            except AzureError as e:
                die(explain(e))
        now = dt.datetime.now()
        data.update(generated=now.strftime("%Y-%m-%d %H:%M"), metric=args.metric, schema=SCHEMA)
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        keep_snapshot(data, saved.parent / "history", now)
        print(f"aztree: saved the data to {saved} (reopen it with: aztree --from)")

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

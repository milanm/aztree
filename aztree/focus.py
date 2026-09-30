"""Cost Management's FOCUS export files, read into the data aztree's page draws, without calling Azure.

    aztree --focus ./focus     # a folder of export runs (their manifests say which run owns each day), or files

Only CSV (and CSV.gz) exports: Parquet needs a library aztree doesn't ship.
"""
import csv
import gzip
import json
import re
from collections import Counter
from pathlib import Path

from . import VIEWS, group_key, region_name

MANIFESTS = {"manifest.json", "_manifest.json"}  # Microsoft's docs show both names
# the columns every row needs; the cost column comes from the metric, and FOCUS 1.2 renamed the meter
REQUIRED = ["ChargePeriodStart", "BillingCurrency", "ChargeCategory", "CommitmentDiscountType", "ServiceName",
            "x_SkuMeterCategory", "SubAccountId", "SubAccountName", "RegionName", "RegionId", "x_ResourceGroupName",
            "ResourceId", "Tags"]
COST = {"ActualCost": ("BilledCost", "x_BilledCostInUsd"), "AmortizedCost": ("EffectiveCost", "x_EffectiveCostInUsd")}
METER = ["x_SkuMeterName", "SkuMeter"]
OPTIONAL = ["x_SkuMeterSubcategory", "CommitmentDiscountName", "ChargeDescription", "x_BillingExchangeRate",
            "x_PricingCurrency", "PricingCurrency"]
NOT_FOCUS = "not a FOCUS cost export: export 'Cost and usage details (FOCUS)', version 1.0 or later"
DAY = re.compile(r"\d{4}-\d{2}-\d{2}$")


class FocusError(Exception):
    """Why the files can't be read. main() prints it and stops."""


def is_csv(path):
    name = path.name.lower()
    return name.endswith(".csv") or name.endswith(".csv.gz")


def is_parquet(path):
    return ".parquet" in path.name.lower()


def day_of(value):
    """'2025-03-21T21:04:06.5234447Z' -> '2025-03-21'. Only the date: Python 3.9 can't parse the rest."""
    value = str(value or "")
    return value[:10] if re.match(r"\d{4}-\d{2}-\d{2}", value) else None


def file_run(path, named):
    return {"name": str(path), "files": [path], "start": None, "end": None, "submitted": None, "submitted_at": "",
            "named": named}


def manifest_run(path, log):
    """The export run a manifest describes: the CSV files beside it (only those it lists, when it lists them) and the
    days it covers. None for an export that isn't FOCUS. Also returns the Parquet files found there."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise FocusError(f"can't read the manifest {path}: {e}") from None
    kind = str((manifest.get("exportConfig") or {}).get("type") or "")
    if kind and "focus" not in kind.lower():
        log(f"  skipped {path.parent}: a {kind} export, not FOCUS")
        return None, []
    here = sorted(f for f in path.parent.iterdir() if f.is_file() and f.name.lower() not in MANIFESTS)
    listed = {str(b.get("blobName") or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
              for b in manifest.get("blobs") or []} - {""}
    missing = sorted(listed - {f.name for f in here})
    if missing:
        log(f"  {path.parent}: {len(missing)} of the manifest's files aren't here, so this run is incomplete: "
            f"{', '.join(missing[:3])}")
    files = [f for f in here if is_csv(f) and (not listed or f.name in listed)]
    info = manifest.get("runInfo") or {}
    run = {"name": str(path.parent), "files": files, "start": day_of(info.get("startDate")),
           "end": day_of(info.get("endDate")), "submitted": day_of(info.get("submittedTime")),
           "submitted_at": str(info.get("submittedTime") or ""), "named": False}
    if not (run["start"] and run["end"] and run["submitted"]):  # a manifest aztree doesn't understand: read it like a file
        run.update(start=None, end=None, submitted=None, submitted_at="")
    return (run if files else None), [f for f in here if is_parquet(f)]


def find_runs(paths, log=print):
    """The export runs under `paths`. A folder with a manifest is one run; any other CSV file is a run of its own.
    Dates are 'yyyy-mm-dd' from the manifest (None without one); `named` marks a file given on the command line."""
    runs, parquet = [], []
    for p in map(Path, paths):
        if not p.exists():
            raise FocusError(f"no file or folder at {p}")
        if p.is_file():
            if is_csv(p):
                runs.append(file_run(p, named=True))
            elif is_parquet(p):
                parquet.append(p)
            else:
                raise FocusError(f"{p} isn't a CSV export (.csv or .csv.gz)")
            continue
        run_dirs = set()
        for m in sorted(f for f in p.rglob("*") if f.is_file() and f.name.lower() in MANIFESTS):
            run_dirs.add(m.parent)
            run, skipped = manifest_run(m, log)
            parquet += skipped
            if run:
                runs.append(run)
        for f in sorted(p.rglob("*")):
            if f.is_file() and f.parent not in run_dirs:
                if is_csv(f):
                    runs.append(file_run(f, named=False))
                elif is_parquet(f):
                    parquet.append(f)
    if not runs:
        if parquet:
            raise FocusError("aztree reads CSV exports: set the export's format to CSV (Gzip is fine) and export again.")
        raise FocusError(f"no CSV export files in {', '.join(map(str, paths))}")
    if parquet:
        log(f"  skipped {len(parquet)} Parquet file{'s' if len(parquet) > 1 else ''}: aztree reads CSV exports")
    return runs


def open_text(path):
    if path.name.lower().endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8-sig", newline="")
    return open(path, encoding="utf-8-sig", newline="")


def number(text):
    return float(text) if text else 0.0  # "abc" raises ValueError, which read_run() turns into a FocusError


def columns(header, metric):
    """Where the columns this reader uses sit in `header`, and the names of the required ones it lacks."""
    pos = {name.strip(): i for i, name in enumerate(header)}
    cost, usd = COST[metric]
    meter = next((m for m in METER if m in pos), None)
    missing = [c for c in [*REQUIRED, cost] if c not in pos] + ([] if meter else [METER[0]])
    if missing:
        return None, missing
    col = {c: pos.get(c) for c in [*REQUIRED, *OPTIONAL]}
    col.update(cost=pos[cost], usd=pos.get(usd), meter=pos[meter],
               pricing=pos.get("x_PricingCurrency", pos.get("PricingCurrency")))
    return col, []


def optional(row, col, name):
    return row[col[name]] if col.get(name) is not None else ""


def subscription_of(sub_account):
    """'/subscriptions/<guid>' (or a bare guid) -> the lower-case guid; '' for charges outside any subscription."""
    m = re.match(r"(?:/subscriptions/)?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
                 sub_account.strip(), re.I)
    return m.group(1).lower() if m else ""


def service_and_meter(row, col):
    """Usage keeps Cost Management's names (meter category and meter), so the money-pit rules match. Purchases,
    unused commitments and credits have no usage meter: they go under their FOCUS service and what they are."""
    category = row[col["x_SkuMeterCategory"]]
    if row[col["ChargeCategory"]] == "Usage" and category:
        return category, row[col["meter"]] or "(no meter)"
    service = row[col["ServiceName"]] or optional(row, col, "x_SkuMeterSubcategory") or "(no service)"
    what = optional(row, col, "ChargeDescription") or optional(row, col, "CommitmentDiscountName") or "(no meter)"
    return service, what


def in_usd(row, col, cost, currency):
    """The row's cost in US dollars: Azure's own column when it's filled, else the cost converted back at the row's
    exchange rate when it was priced in dollars. None when neither says."""
    if col["usd"] is not None and row[col["usd"]]:
        return number(row[col["usd"]])
    if currency == "USD":
        return cost
    rate = number(optional(row, col, "x_BillingExchangeRate"))
    if col["pricing"] is not None and row[col["pricing"]] == "USD" and rate > 0:
        return cost / rate
    return None


class Rows:
    """What the files hold, summed per view, key and day for every day read. The window is cut out once the last
    complete day is known. Also the names, currencies, tags and purchases read() needs."""

    def __init__(self, metric):
        self.metric = metric
        self.sums = {v: {} for v in [*VIEWS, "tags"]}  # view -> {(key, day): [cost, cost in USD or None]}
        self.currencies = {}  # day -> the billing currencies seen that day
        self.sub_names, self.sub_currencies = {}, {}  # subscription guid -> its name; -> Counter of its currencies
        self.groups = {}  # resource group key -> (label, subscription guid)
        self.tagged = set()  # (resource id, raw Tags): the tag view's key is chosen from these
        self.purchases = {}  # day -> net cost of reservation and savings plan purchases (ActualCost only)
        self.last = {}  # run name -> the last day read from it
        self.first = None  # the first day read
        self.files = self.rows = 0

    def add(self, run, col, row):
        day = row[col["ChargePeriodStart"]][:10]
        if not DAY.match(day):
            raise ValueError(f"ChargePeriodStart isn't a date: {row[col['ChargePeriodStart']]!r}")
        allowed = run.get("days")
        if allowed is not None and day not in allowed:
            return  # a newer run owns this day
        self.rows += 1
        self.first = min(self.first or day, day)
        self.last[run["name"]] = max(self.last.get(run["name"], day), day)
        cost, currency = number(row[col["cost"]]), row[col["BillingCurrency"]]
        usd = in_usd(row, col, cost, currency)
        self.currencies.setdefault(day, set()).add(currency)
        sub = subscription_of(row[col["SubAccountId"]])
        if sub:
            self.sub_names.setdefault(sub, row[col["SubAccountName"]] or sub)
            self.sub_currencies.setdefault(sub, Counter())[currency] += 1
        service, meter = service_and_meter(row, col)
        rid, rg, tags = row[col["ResourceId"]].lower(), row[col["x_ResourceGroupName"]], row[col["Tags"]]
        group = group_key({"scope": f"/subscriptions/{sub}" if sub else ""}, rg, rid)
        self.groups.setdefault(group, (rg or "(no resource group)", sub))
        if rid and tags:
            self.tagged.add((rid, tags))
        region = region_name(row[col["RegionName"]] or row[col["RegionId"]])
        for view, key in (("service", (service, meter)), ("subscription", (sub, service)), ("region", (region, service)),
                          ("resource", (group, rid or "(no resource)")), ("tags", (tags, service))):
            s = self.sums[view].setdefault((key, day), [0.0, 0.0])
            s[0] += cost
            s[1] = None if s[1] is None or usd is None else s[1] + usd
        if self.metric == "ActualCost" and row[col["ChargeCategory"]] == "Purchase" and row[col["CommitmentDiscountType"]]:
            self.purchases[day] = self.purchases.get(day, 0.0) + cost


def read_run(run, rows, log=print):
    """Add a run's files to `rows`. A file without the FOCUS columns stops a file named on the command line, and is
    skipped with a line when a folder held it."""
    for path in run["files"]:
        with open_text(path) as f:
            reader = csv.reader(f)
            header = next(reader, [])
            col, missing = columns(header, rows.metric)
            if missing:
                why = f"{path}: {NOT_FOCUS} (no {', '.join(missing)})"
                if run["named"]:
                    raise FocusError(why)
                log(f"  skipped {why}")
                continue
            rows.files += 1
            for n, row in enumerate(reader, 2):  # the header is row 1
                if not row:
                    continue
                if len(row) < len(header):
                    raise FocusError(f"{path}: row {n} has {len(row)} of {len(header)} columns")
                try:
                    rows.add(run, col, row)
                except ValueError as e:
                    raise FocusError(f"{path}: row {n}: {e}") from None

"""Cost Management's FOCUS export files, read into the data aztree's page draws, without calling Azure.

    aztree --focus ./focus     # a folder of export runs (their manifests say which run owns each day), or files

Only CSV (and CSV.gz) exports: Parquet needs a library aztree doesn't ship.
"""
import csv
import datetime as dt
import gzip
import json
import re
import zlib
from collections import Counter
from pathlib import Path

from . import VIEWS, fold, group_key, pick_currency, region_name

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


def obj(value):
    return value if isinstance(value, dict) else {}


def manifest_run(path, log):
    """The export run a manifest describes: the CSV files beside it (only those it lists, when it lists them) and the
    days it covers. None for an export that isn't FOCUS. Also returns the Parquet files found there."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise FocusError(f"can't read the manifest {path}: {e}") from None
    if not isinstance(manifest, dict):
        raise FocusError(f"can't read the manifest {path}: it isn't a JSON object")
    kind = str(obj(manifest.get("exportConfig")).get("type") or "")
    if kind and "focus" not in kind.lower():
        log(f"  skipped {path.parent}: a {kind} export, not FOCUS")
        return None, []
    here = sorted(f for f in path.parent.iterdir() if f.is_file() and f.name.lower() not in MANIFESTS)
    blobs = manifest.get("blobs") if isinstance(manifest.get("blobs"), list) else []
    listed = {str(b.get("blobName") or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
              for b in blobs if isinstance(b, dict)} - {""}
    missing = sorted(listed - {f.name for f in here})
    if missing:
        log(f"  {path.parent}: {len(missing)} of the manifest's files aren't here, so this run is incomplete: "
            f"{', '.join(missing[:3])}")
    files = [f for f in here if is_csv(f) and (not listed or f.name in listed)]
    info = obj(manifest.get("runInfo"))
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
    unique = {}
    for run in runs:  # the same file or folder given twice is one run
        unique.setdefault(str(Path(run["name"]).resolve()), run)
    runs = list(unique.values())
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
        self.seen = {}  # run name -> the days read from it
        self.first = None  # the first day read
        self.files = self.rows = 0

    def add(self, run, col, row):
        day = row[col["ChargePeriodStart"]][:10]
        try:
            dt.date.fromisoformat(day if DAY.match(day) else "")  # the pattern keeps keys yyyy-mm-dd; this rejects 02-30
        except ValueError:
            raise ValueError(f"ChargePeriodStart isn't a date: {row[col['ChargePeriodStart']]!r}") from None
        allowed = run.get("days")
        if allowed is not None and day not in allowed:
            return  # a newer run owns this day
        self.rows += 1
        self.first = min(self.first or day, day)
        self.last[run["name"]] = max(self.last.get(run["name"], day), day)
        self.seen.setdefault(run["name"], set()).add(day)
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
        try:
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
        except UnicodeDecodeError:
            raise FocusError(f"{path} isn't UTF-8 text: save it as \"CSV UTF-8\" or download the export again") from None
        except (OSError, EOFError, zlib.error) as e:  # a locked file, or a .gz that isn't gzip or is cut short
            raise FocusError(f"can't read {path}: {e}") from None


def shift(day, n):
    return (dt.date.fromisoformat(day) + dt.timedelta(n)).isoformat()


def days_between(first, last):
    a, b = dt.date.fromisoformat(first), dt.date.fromisoformat(last)
    return [(a + dt.timedelta(i)).isoformat() for i in range((b - a).days + 1)]


def own_days(runs, log=print):
    """Give each day to the newest export run whose manifest covers it: with overwrite off a month has a run a day,
    a month's first days also rewrite the month before, and two exports can cover the same days. Sets run["days"]:
    the days a run may add, or None (all of them) for a file without a manifest."""
    taken, lost = set(), 0
    for run in sorted((r for r in runs if r["start"]), key=lambda r: (r["submitted_at"], r["end"]), reverse=True):
        covered = set(days_between(run["start"], run["end"]))
        lost += len(covered & taken)
        run["days"] = covered - taken
        taken |= covered
    for run in runs:
        if not run["start"]:
            run["days"] = None
    if lost:
        log(f"  skipped {lost} day{'s' if lost != 1 else ''} of older export runs that a newer run also covers")


def usable_last(run, last_read=None):
    """The last complete day a run holds. An export run can still be filling in its last days, so it stops two days
    before it was submitted, like last_full_day(). A file without a manifest stops on its last day when that ends a
    month, else on the day before."""
    if run["start"]:
        return min(run["end"], shift(run["submitted"], -2))
    if not last_read:
        return None
    return last_read if shift(last_read, 1).endswith("-01") else shift(last_read, -1)


def tag_dict(raw, parsed):
    """A row's Tags as a dict, parsed once per distinct string. What isn't a JSON object counts as no tags; `parsed`
    remembers it as None so read() can say how many there were."""
    if raw not in parsed:
        try:
            value = json.loads(raw) if raw else {}
        except ValueError:
            value = None
        parsed[raw] = value if isinstance(value, dict) else None
    return parsed[raw] or {}


def tag_value(raw, key, parsed):
    low = key.lower()
    return next((str(v) for k, v in tag_dict(raw, parsed).items() if k.lower() == low), "")


def pick_tag(tagged, wanted, parsed):
    """--tag, or the key on the most distinct resources, like choose_tag(): keys match case-insensitively, hidden-*
    keys don't count, ties go to the name that sorts first. None when nothing is tagged."""
    if wanted:
        return wanted
    resources, spelling = {}, {}
    for rid, raw in sorted(tagged):
        for key in tag_dict(raw, parsed):
            low = key.lower()
            if not low.startswith("hidden-"):
                spelling.setdefault(low, key)
                resources.setdefault(low, set()).add(rid)
    if not resources:
        return None
    return spelling[min(resources, key=lambda k: (-len(resources[k]), k))]


def read(runs, days, metric, tag=None, log=print):
    """The runs' rows as the data dict fetch() returns: the same views, names and currency rules, with no Advisor,
    Resource Graph or forecast. `days` shrinks when the files cover less than two periods."""
    own_days(runs, log)
    exported = [r for r in runs if r["start"]]
    loose = [r for r in runs if not r["start"]]
    end = max((usable_last(r) for r in exported), default=None)
    if end:  # skip export runs outside the window before opening them: a 13-month download reads what it needs
        first_needed = shift(end, 1 - 2 * days)
        exported_open = [r for r in exported if r["days"] and any(first_needed <= d <= end for d in r["days"])]
    else:
        exported_open = []
    opened = loose + exported_open
    files = [f for r in opened for f in r["files"]]
    log(f"aztree: reading {len(files)} file{'s' if len(files) != 1 else ''} from {len(opened)} export "
        f"run{'s' if len(opened) != 1 else ''} ({sum(f.stat().st_size for f in files) / 1e6:.0f} MB)")
    rows = Rows(metric)
    for run in opened:
        read_run(run, rows, log)
    for run in loose:
        last = rows.last.get(run["name"])
        if last and usable_last(run, last) != last:
            log(f"  {Path(run['name']).name} ends on {last}, which may still be filling in; the days before it count")
    lasts = [usable_last(r) for r in exported] + [usable_last(r, rows.last.get(r["name"])) for r in loose]
    lasts = [d for d in lasts if d]
    if not rows.first or not lasts:
        raise FocusError("the export files hold no cost rows")
    end = max(lasts)
    covered = (dt.date.fromisoformat(end) - dt.date.fromisoformat(rows.first)).days + 1
    if covered < 2:
        raise FocusError(f"the files hold {max(covered, 0)} complete day{'s' if covered != 1 else ''}; "
                         "aztree needs at least 2 to compare two periods")
    if covered < 2 * days:
        days = covered // 2
        log(f"  the files cover {covered} days, so the period is {days} days, compared with the {days} before")
    dates = [shift(end, i + 1 - 2 * days) for i in range(2 * days)]
    index = {d: i for i, d in enumerate(dates)}

    def entries(view):
        return [(key, index[day], cost, usd) for (key, day), (cost, usd) in rows.sums[view].items() if day in index]

    raw = {v: entries(v) for v in VIEWS}
    parsed = {}
    chosen = pick_tag(rows.tagged, tag, parsed)
    if chosen:
        raw["tag"] = [((tag_value(t, chosen, parsed), service), i, cost, usd)
                      for (t, service), i, cost, usd in entries("tags")]
    bad = sum(v is None for v in parsed.values())
    if bad:
        log(f"  Tags that aren't JSON objects: {bad} distinct value{'s' if bad != 1 else ''}; their rows count as untagged")
    repeated = sum(1 for n in Counter(d for r in opened for d in rows.seen.get(r["name"], ())).values() if n > 1)
    if repeated:  # export runs never share a day, so a file without a manifest is in each of these
        log(f"  {repeated} day{'s appear' if repeated != 1 else ' appears'} in more than one file without a manifest; "
            "if the files are repeated runs of one export, keep their manifests or pass one file")
    empty = [d for d in dates if d not in rows.currencies]
    if empty:
        log(f"  {len(empty)} of the {len(dates)} days have no rows in the files (the first is {empty[0]}); "
            "download the export runs that cover them")
    found = set().union(*(rows.currencies.get(d, set()) for d in dates)) - {""}
    currency, usd, mixed, usd_rate = pick_currency(found, raw, log)
    many = len(rows.sub_names) > 1  # like fetch(): a group's label names its subscription when there are several
    names = {"service": {}, "region": {}, "subscription": {**rows.sub_names, "": "(no subscription)"},
             "resource": {g: f"{label} · {rows.sub_names[sub]}" if many and sub in rows.sub_names else label
                          for g, (label, sub) in rows.groups.items()}}
    views = {v: {"dims": VIEWS[v], "names": names[v], "rows": fold(raw[v], len(dates), usd)} for v in VIEWS}
    if chosen:
        views["tag"] = {"dims": ["TagValue", "ServiceName"], "names": {}, "tag": chosen,
                        "rows": fold(raw["tag"], len(dates), usd)}
    purchases = round(sum(rows.purchases.get(d, 0.0) for d in dates[days:]), 2) if metric == "ActualCost" else None
    return {
        "days": dates, "split": days, "views": views, "currency": currency,
        "subscriptions": [{"id": s, "name": rows.sub_names[s], "currency": rows.sub_currencies[s].most_common(1)[0][0]}
                          for s in sorted(rows.sub_names)],
        "resource_fallback": [], "advisor": None, "advisor_error": None,
        "mixed_currencies": mixed, "usd_rate": usd_rate,
        "forecast": None, "forecast_note": None, "graph": None, "graph_error": None, "demo": False,
        "source": {"kind": "focus", "files": rows.files, "rows": rows.rows, "first": rows.first,
                   "last": max(rows.last.values())},
        "commitment_purchases": purchases,
    }

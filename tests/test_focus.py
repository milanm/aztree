import csv
import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import aztree  # noqa: E402
from aztree import focus  # noqa: E402


def scratch(test):
    """A temp folder removed when the test ends."""
    d = Path(tempfile.mkdtemp(prefix="aztree-focus-"))
    test.addCleanup(shutil.rmtree, d, True)
    return d


# the columns aztree reads, named as in Microsoft's FOCUS 1.0 sample export (EA-Cost-FOCUS_1.0.csv)
HEADER = ["BilledCost", "EffectiveCost", "x_BilledCostInUsd", "x_EffectiveCostInUsd", "BillingCurrency",
          "ChargePeriodStart", "ChargeCategory", "CommitmentDiscountType", "CommitmentDiscountName", "ChargeDescription",
          "ServiceName", "x_SkuMeterCategory", "x_SkuMeterSubcategory", "x_SkuMeterName", "SubAccountId",
          "SubAccountName", "RegionName", "RegionId", "x_ResourceGroupName", "ResourceId", "Tags",
          "x_BillingExchangeRate", "x_PricingCurrency"]
GUID = "aaaaaaaa-1111-2222-3333-444444444444"
SUB = f"/subscriptions/{GUID}"
VM = f"{SUB}/resourcegroups/rg-app/providers/microsoft.compute/virtualmachines/vm1"


def row(day, cost=1.0, month="2026-09", **columns):
    """One usage row in the shape of the sample: vm1 in rg-app on a day of `month`, costing `cost` dollars."""
    r = {"BilledCost": cost, "EffectiveCost": cost, "x_BilledCostInUsd": "", "x_EffectiveCostInUsd": "",
         "BillingCurrency": "USD", "ChargePeriodStart": f"{month}-{day:02d}T00:00Z", "ChargeCategory": "Usage",
         "CommitmentDiscountType": "", "CommitmentDiscountName": "",
         "ChargeDescription": "Virtual Machines Dsv5 Series - D2s v5", "ServiceName": "Virtual Machines",
         "x_SkuMeterCategory": "Virtual Machines", "x_SkuMeterSubcategory": "Dsv5 Series", "x_SkuMeterName": "D2s v5",
         "SubAccountId": SUB, "SubAccountName": "acme-prod", "RegionName": "East US", "RegionId": "eastus",
         "x_ResourceGroupName": "rg-app", "ResourceId": VM, "Tags": '{"env": "prod"}',
         "x_BillingExchangeRate": "1", "x_PricingCurrency": "USD"}
    r.update(columns)
    return r


def days(first, last, cost=1.0, month="2026-09", **columns):
    return [row(d, cost, month, **columns) for d in range(first, last + 1)]


def write_csv(path, rows, header=HEADER, encoding="utf-8"):
    """An export file; a name ending in .gz is compressed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt", encoding=encoding, newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def write_run(folder, rows, start, end, submitted, kind="FocusCost", listed=("part_0_0001.csv",)):
    """An export run as Cost Management writes one: a partition and a manifest in Microsoft's documented shape."""
    write_csv(folder / "part_0_0001.csv", rows)
    manifest = {"manifestVersion": "2024-04-01", "exportConfig": {"exportName": "focus", "type": kind},
                "runInfo": {"submittedTime": submitted, "startDate": f"{start}T00:00:00", "endDate": f"{end}T00:00:00Z"},
                "blobs": [{"blobName": f"exports/focus/20260901-20260930/run/{name}"} for name in listed]}
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


class FindRunsTest(unittest.TestCase):
    def setUp(self):
        self.dir, self.lines = scratch(self), []

    def find(self, *paths):
        return focus.find_runs([str(p) for p in paths], log=self.lines.append)

    def test_a_folder_with_a_manifest_is_one_run(self):
        write_run(self.dir / "focus" / "20260901-20260930" / "run1", days(1, 20), "2026-09-01", "2026-09-20",
                  "2026-09-20T06:12:44.1234567Z")
        (run,) = self.find(self.dir)
        self.assertEqual([f.name for f in run["files"]], ["part_0_0001.csv"])
        self.assertEqual((run["start"], run["end"], run["submitted"], run["named"]),
                         ("2026-09-01", "2026-09-20", "2026-09-20", False))

    def test_a_file_without_a_manifest_is_a_run_of_its_own(self):
        f = write_csv(self.dir / "costs.csv.gz", days(1, 3))
        (run,) = self.find(f)
        self.assertEqual((run["files"], run["start"], run["named"]), ([f], None, True))
        (run,) = self.find(self.dir)
        self.assertFalse(run["named"])  # found in a folder: a bad file there is skipped, not fatal

    def test_only_the_files_the_manifest_lists(self):
        folder = write_run(self.dir / "run1", days(1, 2), "2026-09-01", "2026-09-02", "2026-09-03T00:00:00Z")
        write_csv(folder / "stray.csv", days(1, 2))  # a copy someone saved there
        (run,) = self.find(self.dir)
        self.assertEqual([f.name for f in run["files"]], ["part_0_0001.csv"])

    def test_a_missing_partition_is_reported(self):
        write_run(self.dir / "run1", days(1, 2), "2026-09-01", "2026-09-02", "2026-09-03T00:00:00Z",
                  listed=("part_0_0001.csv", "part_1_0001.csv"))
        self.find(self.dir)
        self.assertTrue(any("incomplete" in line and "part_1_0001.csv" in line for line in self.lines), self.lines)

    def test_exports_that_are_not_focus_are_skipped(self):
        write_run(self.dir / "focus", days(1, 2), "2026-09-01", "2026-09-02", "2026-09-03T00:00:00Z")
        write_run(self.dir / "actual", days(1, 2), "2026-09-01", "2026-09-02", "2026-09-04T00:00:00Z", kind="ActualCost")
        (run,) = self.find(self.dir)
        self.assertTrue(run["name"].endswith("focus"))
        self.assertTrue(any("ActualCost" in line for line in self.lines), self.lines)

    def test_parquet_alone_says_to_export_csv(self):
        (self.dir / "part_0_0001.snappy.parquet").write_bytes(b"PAR1")
        with self.assertRaisesRegex(focus.FocusError, "reads CSV exports"):
            self.find(self.dir)

    def test_parquet_beside_csv_is_skipped_with_a_line(self):
        (self.dir / "old.parquet").write_bytes(b"PAR1")
        write_csv(self.dir / "costs.csv", days(1, 2))
        self.assertEqual(len(self.find(self.dir)), 1)
        self.assertTrue(any("Parquet" in line for line in self.lines), self.lines)

    def test_nothing_to_read_names_the_path(self):
        with self.assertRaisesRegex(focus.FocusError, "no CSV export files"):
            self.find(self.dir)
        with self.assertRaisesRegex(focus.FocusError, "no file or folder"):
            self.find(self.dir / "nope")


PURCHASE = dict(ChargeCategory="Purchase", CommitmentDiscountType="Reservation", CommitmentDiscountName="VM_RI_03-10-2023_07-59",
                ServiceName="Azure Reservations", ChargeDescription="Virtual Machines BS Series - B1s - US South Central",
                x_SkuMeterCategory="Virtual Machines", x_SkuMeterSubcategory="BS Series", x_SkuMeterName="B1s",
                ResourceId="/providers/microsoft.capacity/reservationorders/1/reservations/", x_ResourceGroupName="",
                RegionName="South Central US", RegionId="southcentralus", Tags="", EffectiveCost=0)
UNUSED_PLAN = dict(CommitmentDiscountType="Savings Plan", CommitmentDiscountName="Compute_SavingsPlan_11-07-2022_09-55",
                   ServiceName="Azure Savings Plan for Compute", ChargeDescription="", x_SkuMeterCategory="",
                   x_SkuMeterSubcategory="", x_SkuMeterName="", SubAccountId="", SubAccountName="", x_ResourceGroupName="",
                   ResourceId="/providers/microsoft.billingbenefits/savingsplanorders/1/savingsplans/2",
                   RegionName="Global", RegionId="global", Tags="")


class ReadRunTest(unittest.TestCase):
    def setUp(self):
        self.dir, self.lines = scratch(self), []

    def read(self, rows, metric="ActualCost", header=HEADER, name="costs.csv", encoding="utf-8"):
        path = write_csv(self.dir / name, rows, header, encoding)
        (run,) = focus.find_runs([str(path)], log=self.lines.append)
        got = focus.Rows(metric)
        focus.read_run(run, got, log=self.lines.append)
        return got

    @staticmethod
    def totals(got, view):
        out = {}
        for (key, _), (cost, _) in got.sums[view].items():
            out[key] = round(out.get(key, 0.0) + cost, 4)
        return out

    def test_usage_keeps_cost_managements_names(self):
        got = self.read(days(1, 2))
        self.assertEqual(self.totals(got, "service"), {("Virtual Machines", "D2s v5"): 2.0})
        self.assertEqual(self.totals(got, "subscription"), {(GUID, "Virtual Machines"): 2.0})
        self.assertEqual(self.totals(got, "region"), {("East US", "Virtual Machines"): 2.0})
        self.assertEqual(self.totals(got, "resource"), {(f"{SUB}/resourcegroups/rg-app", VM): 2.0})
        self.assertEqual((got.sub_names, got.first, got.rows, got.files), ({GUID: "acme-prod"}, "2026-09-01", 2, 1))

    def test_purchases_and_unused_commitments_keep_their_own_names(self):
        got = self.read([row(1, 20.64, **PURCHASE), row(1, 0.5, **UNUSED_PLAN)])
        self.assertEqual(self.totals(got, "service"), {
            ("Azure Reservations", "Virtual Machines BS Series - B1s - US South Central"): 20.64,
            ("Azure Savings Plan for Compute", "Compute_SavingsPlan_11-07-2022_09-55"): 0.5})
        self.assertIn(("", "Azure Savings Plan for Compute"), self.totals(got, "subscription"))
        self.assertEqual(set(self.totals(got, "resource")), {(SUB, PURCHASE["ResourceId"]), ("", UNUSED_PLAN["ResourceId"])})

    def test_the_metric_picks_the_column_and_only_actual_cost_counts_purchases(self):
        actual = self.read([row(1, 20.64, **PURCHASE)])
        amortized = self.read([row(1, 20.64, **PURCHASE)], metric="AmortizedCost")
        self.assertEqual(sum(self.totals(actual, "service").values()), 20.64)
        self.assertEqual(sum(self.totals(amortized, "service").values()), 0.0)
        self.assertEqual((actual.purchases, amortized.purchases), ({"2026-09-01": 20.64}, {}))

    def test_gzip_a_bom_and_dates_with_seconds(self):  # FOCUS 1.0r2 adds seconds; Excel and Storage Explorer add a BOM
        got = self.read(days(1, 1, ChargePeriodStart="2026-09-01T00:00:00Z"), name="part.csv.gz", encoding="utf-8-sig")
        self.assertEqual(list(got.sums["service"]), [(("Virtual Machines", "D2s v5"), "2026-09-01")])

    def test_focus_1_2_calls_the_meter_sku_meter(self):
        header = [("SkuMeter" if c == "x_SkuMeterName" else c) for c in HEADER]
        got = self.read(days(1, 1, SkuMeter="D2s v5"), header=header)
        self.assertEqual(self.totals(got, "service"), {("Virtual Machines", "D2s v5"): 1.0})

    def test_one_resource_group_in_two_casings_is_one_group(self):
        got = self.read([row(1, x_ResourceGroupName="RG-App"), row(2)])
        self.assertEqual(got.groups, {f"{SUB}/resourcegroups/rg-app": ("RG-App", GUID)})

    def test_allocation_copies_net_out(self):
        got = self.read([row(1, 10.0), row(1, -10.0)])
        self.assertEqual(self.totals(got, "service"), {("Virtual Machines", "D2s v5"): 0.0})

    def test_dollars_come_from_azure_or_from_the_exchange_rate(self):
        got = self.read([row(1, 9.0, BillingCurrency="EUR", x_BillingExchangeRate="0.9"),
                         row(2, 9.0, BillingCurrency="EUR", x_BilledCostInUsd="11"),
                         row(3, 9.0, BillingCurrency="EUR", x_PricingCurrency="EUR")])
        usd = {day: round(s[1], 4) if s[1] is not None else None for (_, day), s in got.sums["service"].items()}
        self.assertEqual(usd, {"2026-09-01": 10.0, "2026-09-02": 11.0, "2026-09-03": None})

    def test_a_named_file_without_focus_columns_stops(self):
        with self.assertRaisesRegex(focus.FocusError, "not a FOCUS cost export.*Tags"):
            self.read(days(1, 1), header=[c for c in HEADER if c != "Tags"])

    def test_a_file_found_in_a_folder_without_focus_columns_is_skipped(self):
        write_csv(self.dir / "prices" / "sheet.csv", [{"MeterId": "1"}], header=["MeterId"])
        (run,) = focus.find_runs([str(self.dir / "prices")], log=self.lines.append)
        got = focus.Rows("ActualCost")
        focus.read_run(run, got, log=self.lines.append)
        self.assertEqual(got.files, 0)
        self.assertTrue(any("skipped" in line and "not a FOCUS cost export" in line for line in self.lines), self.lines)

    def test_a_bad_date_or_number_names_the_file_and_row(self):
        with self.assertRaisesRegex(focus.FocusError, r"costs\.csv: row 3: ChargePeriodStart"):
            self.read([row(1), row(2, ChargePeriodStart="")])
        with self.assertRaisesRegex(focus.FocusError, r"costs\.csv: row 2: .*abc"):
            self.read([row(1, "abc")])


FETCH_KEYS = {"days", "split", "views", "currency", "subscriptions", "resource_fallback", "advisor", "advisor_error",
              "mixed_currencies", "usd_rate", "forecast", "forecast_note", "graph", "graph_error", "demo"}


class ReadTest(unittest.TestCase):
    def setUp(self):
        self.dir, self.lines = scratch(self), []

    def read(self, *paths, days=30, metric="ActualCost", tag=None):
        runs = focus.find_runs([str(p) for p in paths or [self.dir]], log=self.lines.append)
        return focus.read(runs, days, metric, tag, log=self.lines.append)

    @staticmethod
    def total(data, view="service"):
        return round(sum(sum(r["d"]) for r in data["views"][view]["rows"]), 4)

    def test_a_file_ending_mid_month_drops_its_last_day(self):  # the sample ends on a half-filled day
        write_csv(self.dir / "a.csv", days(1, 10))
        data = self.read()
        self.assertEqual((data["days"][0], data["days"][-1], data["split"]), ("2026-09-02", "2026-09-09", 4))
        self.assertTrue(any("filling in" in line for line in self.lines), self.lines)
        self.assertTrue(any("the files cover 9 days, so the period is 4 days" in line for line in self.lines), self.lines)

    def test_a_file_ending_on_a_months_last_day_keeps_it(self):
        write_csv(self.dir / "a.csv", days(1, 30))
        self.assertEqual(self.read()["days"][-1], "2026-09-30")

    def test_the_newest_run_owns_each_day(self):  # overwrite off: yesterday's month-to-date copy is still there
        write_run(self.dir / "run-19", days(1, 19, 5.0), "2026-09-01", "2026-09-19", "2026-09-19T05:00:00Z")
        write_run(self.dir / "run-20", days(1, 20, 1.0), "2026-09-01", "2026-09-20", "2026-09-20T05:00:00Z")
        data = self.read()
        self.assertEqual(data["days"][-1], "2026-09-18")  # submitted on the 20th: the 19th may still be filling in
        self.assertEqual(self.total(data), 18.0)
        self.assertEqual(data["source"]["files"], 1)  # the older run owns no day, so it isn't opened
        self.assertTrue(any("skipped 19 days" in line for line in self.lines), self.lines)

    def test_a_months_first_days_rewrite_the_month_before(self):
        write_run(self.dir / "aug", days(1, 31, month="2026-08"), "2026-08-01", "2026-08-31", "2026-09-01T03:00:00Z")
        write_run(self.dir / "sep", days(1, 1), "2026-09-01", "2026-09-01", "2026-09-01T05:00:00Z")
        self.assertEqual(self.read()["days"][-1], "2026-08-30")
        shutil.rmtree(self.dir / "aug")
        shutil.rmtree(self.dir / "sep")
        write_run(self.dir / "aug", days(1, 31, month="2026-08"), "2026-08-01", "2026-08-31", "2026-09-05T03:00:00Z")
        write_run(self.dir / "sep", days(1, 4), "2026-09-01", "2026-09-05", "2026-09-05T05:00:00Z")
        self.assertEqual(self.read()["days"][-1], "2026-09-03")

    def test_a_run_outside_the_window_is_not_opened(self):
        write_run(self.dir / "jul", days(1, 31, month="2026-07"), "2026-07-01", "2026-07-31", "2026-08-02T05:00:00Z")
        write_run(self.dir / "sep", days(1, 27), "2026-09-01", "2026-09-28", "2026-09-28T05:00:00Z")
        data = self.read(days=7)
        self.assertEqual((data["days"][0], data["days"][-1]), ("2026-09-13", "2026-09-26"))
        self.assertEqual(data["source"]["files"], 1)

    def test_the_tag_on_the_most_resources(self):
        vm2, vm3 = VM.replace("vm1", "vm2"), VM.replace("vm1", "vm3")
        write_csv(self.dir / "a.csv", days(1, 30, Tags='{"env": "prod", " org": "a"}')
                  + days(1, 30, ResourceId=vm2, Tags='{"Env": "dev"}') + days(1, 30, ResourceId=vm3, Tags='{"org": "b"}'))
        view = self.read()["views"]["tag"]
        self.assertEqual(view["tag"], "env")  # env and Env are one key on two resources; " org" and org one each
        self.assertEqual({r["k"][0] for r in view["rows"]}, {"prod", "dev", ""})

    def test_tags_that_are_not_text_or_not_json(self):
        write_csv(self.dir / "a.csv", days(1, 30, Tags='{"env": 3}') + days(1, 30, ResourceId=VM + "x", Tags="oops"))
        data = self.read(tag="env")
        self.assertEqual({r["k"][0] for r in data["views"]["tag"]["rows"]}, {"3", ""})
        self.assertTrue(any("aren't JSON objects" in line for line in self.lines), self.lines)

    def test_a_tag_nobody_has_is_all_untagged(self):
        write_csv(self.dir / "a.csv", days(1, 30))
        view = self.read(tag="team")["views"]["tag"]
        self.assertEqual((view["tag"], {r["k"][0] for r in view["rows"]}), ("team", {""}))

    def test_one_currency_priced_in_dollars(self):
        write_csv(self.dir / "a.csv", days(1, 30, 9.0, BillingCurrency="EUR", x_BillingExchangeRate="0.9"))
        data = self.read()
        self.assertEqual((data["currency"], data["usd_rate"], data["mixed_currencies"]), ("EUR", 0.9, None))

    def test_two_currencies_without_dollar_figures_are_mixed(self):
        other = "/subscriptions/bbbbbbbb-1111-2222-3333-444444444444"
        write_csv(self.dir / "a.csv", days(1, 30, BillingCurrency="EUR", x_PricingCurrency="EUR")
                  + days(1, 30, BillingCurrency="GBP", x_PricingCurrency="GBP", SubAccountId=other, SubAccountName="uk"))
        self.assertEqual(self.read()["mixed_currencies"], ["EUR", "GBP"])

    def test_purchases_in_the_current_period(self):
        write_csv(self.dir / "a.csv", days(1, 30) + [row(30, 20.64, **PURCHASE)])
        self.assertEqual(self.read()["commitment_purchases"], 20.64)
        self.assertIsNone(self.read(metric="AmortizedCost")["commitment_purchases"])
        write_csv(self.dir / "a.csv", days(1, 30) + [row(29, 20.64, **PURCHASE), row(30, -20.64, **PURCHASE)])
        self.assertEqual(self.read()["commitment_purchases"], 0.0)  # bought and refunded: net

    def test_the_shape_fetch_returns(self):
        other = "/subscriptions/bbbbbbbb-1111-2222-3333-444444444444"
        write_csv(self.dir / "a.csv", days(1, 30) + days(1, 30, SubAccountId=other, SubAccountName="acme-dev",
                                                          ResourceId=VM.replace(GUID, other.rsplit("/", 1)[1]))
                  + [row(30, 0.5, **UNUSED_PLAN)])
        data = self.read()
        self.assertEqual(set(data), FETCH_KEYS | {"source", "commitment_purchases"})
        self.assertEqual(data["subscriptions"][0], {"id": GUID, "name": "acme-prod", "currency": "USD"})
        self.assertEqual(data["views"]["subscription"]["names"][""], "(no subscription)")
        self.assertEqual(data["views"]["resource"]["names"][f"{SUB}/resourcegroups/rg-app"], "rg-app · acme-prod")
        self.assertEqual((data["source"]["kind"], data["source"]["first"], data["source"]["last"]),
                         ("focus", "2026-09-01", "2026-09-30"))
        aztree.summarize(data)
        aztree.render(data, self.dir / "page.html")

    def test_fewer_than_two_complete_days_stops(self):
        write_csv(self.dir / "a.csv", days(1, 2))  # the 2nd may be filling in: one complete day
        with self.assertRaisesRegex(focus.FocusError, "at least 2"):
            self.read()


class FocusCliTest(unittest.TestCase):
    def setUp(self):
        self.dir = scratch(self)
        write_csv(self.dir / "exports" / "a.csv", days(1, 30))

    def main(self, *argv):
        def no_token(*a, **k):
            raise AssertionError("--focus must not ask Azure for a token")

        with mock.patch.object(aztree, "get_token", no_token), \
                mock.patch.dict(os.environ, {"AZTREE_HOME": str(self.dir / "home")}), \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            aztree.main(list(argv))
        return out.getvalue()

    def test_focus_reads_files_without_azure_and_saves_the_run(self):
        page = self.dir / "page.html"
        out = self.main("--focus", str(self.dir / "exports"), "--out", str(page), "--no-open")
        self.assertIn("reading 1 file from 1 export run", out)
        self.assertIn('"kind":"focus"', page.read_text(encoding="utf-8"))
        saved = json.loads((self.dir / "home" / "aztree-data.json").read_text(encoding="utf-8"))
        self.assertEqual((saved["source"]["kind"], saved["metric"]), ("focus", "ActualCost"))
        again = self.dir / "again.html"
        self.main("--from", "--out", str(again), "--no-open")
        self.assertIn('"kind":"focus"', again.read_text(encoding="utf-8"))

    def test_focus_errors_are_one_line(self):
        with self.assertRaises(SystemExit):
            self.main("--focus", str(self.dir / "nope"), "--no-open")

    def test_focus_does_not_mix_with_other_sources(self):
        for extra in (["--demo"], ["--from"], ["--subscription", "acme-prod"]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                self.main("--focus", str(self.dir / "exports"), "--no-open", *extra)


if __name__ == "__main__":
    unittest.main()

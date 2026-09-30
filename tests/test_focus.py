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


if __name__ == "__main__":
    unittest.main()

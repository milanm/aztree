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


if __name__ == "__main__":
    unittest.main()

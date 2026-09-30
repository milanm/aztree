"""Cost Management's FOCUS export files, read into the data aztree's page draws, without calling Azure.

    aztree --focus ./focus     # a folder of export runs (their manifests say which run owns each day), or files

Only CSV (and CSV.gz) exports: Parquet needs a library aztree doesn't ship.
"""
import json
import re
from pathlib import Path

MANIFESTS = {"manifest.json", "_manifest.json"}  # Microsoft's docs show both names


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

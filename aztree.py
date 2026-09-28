#!/usr/bin/env python3
"""aztree: see where your Azure money goes, as a disktree-style treemap.

    python3 aztree.py            # read your Azure costs (last 30 days) and open the map
    python3 aztree.py --demo     # fake data, no Azure needed

Needs a logged-in Azure CLI (`az login`), or a token in AZURE_ACCESS_TOKEN. No other dependencies.
"""
import os
import shutil
import subprocess
import sys

ARM = "https://management.azure.com"


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

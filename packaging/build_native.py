"""Freeze aztree into one executable for this platform with PyInstaller, then check it works.

    python packaging/build_native.py [--rid win-x64]

Writes build-native/aztree-<rid>[.exe]. The executable needs no Python: it's what the GitHub release attaches and
what the dotnet tool (dotnet/) runs. Needs `pip install pyinstaller`.
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "build-native"


def default_rid():
    system = {"Windows": "win", "Darwin": "osx"}.get(platform.system(), "linux")
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    return f"{system}-{arch}"


def version():
    sys.path.insert(0, str(REPO))
    import aztree
    return aztree.__version__


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rid", default=default_rid(), help="runtime id to name the file after (default: this machine)")
    rid = ap.parse_args().rid
    exe = ".exe" if rid.startswith("win") else ""

    subprocess.run([sys.executable, "-m", "PyInstaller", "--onefile", "--name", "aztree", "--noconfirm", "--clean",
                    "--paths", str(REPO), "--add-data", f"{REPO / 'aztree' / 'viewer.html'}{os.pathsep}aztree",
                    "--distpath", str(OUT / "dist"), "--workpath", str(OUT / "work"), "--specpath", str(OUT),
                    str(REPO / "aztree" / "__main__.py")], check=True)
    built = OUT / "dist" / f"aztree{exe}"

    # the frozen program must say its version and find its page: the page ships inside it as data
    said = subprocess.run([str(built), "--version"], capture_output=True, text=True, check=True).stdout.strip()
    if said != f"aztree {version()}":
        sys.exit(f"build_native: --version said {said!r}, expected 'aztree {version()}'")
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "demo.html"
        subprocess.run([str(built), "--demo", "--no-open", "--out", str(page)], check=True,
                       env={**os.environ, "AZTREE_HOME": tmp})
        if '"demo":true' not in page.read_text(encoding="utf-8"):
            sys.exit("build_native: the demo page has no demo data")

    target = OUT / f"aztree-{rid}{exe}"
    shutil.copy2(built, target)
    print(target)


if __name__ == "__main__":
    main()

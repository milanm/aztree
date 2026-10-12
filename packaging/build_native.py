"""Freeze aztree into one executable for this platform with PyInstaller, then check it works (one check calls Azure).

    python packaging/build_native.py [--rid win-x64]

Writes build-native/aztree-<rid>[.exe]. The executable needs no Python: it's what the GitHub release attaches and
what the dotnet tool (dotnet/) runs. Needs `pip install pyinstaller`.
"""
import argparse
import os
import platform
import re
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


def write_version_resource(path):
    """The Windows executable's version details, in PyInstaller's --version-file format. SignPath signs only an
    executable that names its product and version (.signpath/artifact-configuration.xml)."""
    v = version()
    nums = [int(n) for n in re.match(r"\d+(?:\.\d+)*", v).group(0).split(".")][:4]
    nums = tuple(nums + [0] * (4 - len(nums)))
    strings = {"ProductName": "aztree", "ProductVersion": v, "FileDescription": "aztree", "FileVersion": v,
               "InternalName": "aztree", "OriginalFilename": "aztree.exe",
               "LegalCopyright": "Copyright (c) 2026 Branko Petric, Milan Milanović. MIT License."}
    table = ", ".join(f"StringStruct({k!r}, {s!r})" for k, s in strings.items())
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
                    date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [{table}])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
""", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rid", default=default_rid(), help="runtime id to name the file after (default: this machine)")
    rid = ap.parse_args().rid
    exe = ".exe" if rid.startswith("win") else ""

    windows = []
    if exe:
        OUT.mkdir(exist_ok=True)
        write_version_resource(OUT / "version-info.txt")
        windows = ["--version-file", str(OUT / "version-info.txt")]
    subprocess.run([sys.executable, "-m", "PyInstaller", "--onefile", "--name", "aztree", "--noconfirm", "--clean",
                    "--paths", str(REPO), "--hidden-import", "aztree.focus", "--add-data", f"{REPO / 'aztree' / 'viewer.html'}{os.pathsep}aztree",
                    "--distpath", str(OUT / "dist"), "--workpath", str(OUT / "work"), "--specpath", str(OUT), *windows,
                    str(REPO / "aztree" / "__main__.py")], check=True)
    built = OUT / "dist" / f"aztree{exe}"
    if exe:
        # what Windows reads from the file, which SignPath checks before it signs
        said = subprocess.run(["powershell", "-NoProfile", "-Command",
                               f"$v = (Get-Item -LiteralPath '{built}').VersionInfo; $v.ProductName + ' ' + $v.ProductVersion"],
                              capture_output=True, text=True, check=True).stdout.strip()
        if said != f"aztree {version()}":
            sys.exit(f"build_native: the executable's version details say {said!r}, expected 'aztree {version()}'")

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
        # and must trust Azure's certificate: an OpenSSL that trusts nothing fails every call (issue #2). A made-up
        # token and subscription get an HTTP error from Azure, which only comes back over verified TLS.
        said = subprocess.run([str(built), "--scope", "/subscriptions/00000000-0000-0000-0000-000000000000",
                               "--days", "1", "--no-advisor", "--no-graph", "--no-open"], capture_output=True, text=True,
                              env={**os.environ, "AZTREE_HOME": tmp, "AZURE_ACCESS_TOKEN": "not-a-token"})
        if "Azure error: HTTP 4" not in said.stderr:
            sys.exit(f"build_native: Azure didn't answer over HTTPS:\n{said.stdout}{said.stderr}")

    target = OUT / f"aztree-{rid}{exe}"
    shutil.copy2(built, target)
    print(target)


if __name__ == "__main__":
    main()

# PyInstaller spec — single dispatcher binary for the aiventbus daemon + CLI.
#
# Build from the repo root:
#     pyinstaller packaging/aiventbus.spec --noconfirm
#
# Output: dist/aiventbus-bundle/  (one-dir bundle containing the launcher + _internal/)
#
# The produced binary is installed once at /opt/aiventbus/aiventbus-launcher.
# /usr/bin/aiventbus and /usr/bin/aibus are symlinks pointing at it — the
# launcher reads argv[0] to decide whether to run the daemon or the CLI.

from PyInstaller.utils.hooks import collect_all, collect_submodules

_hidden = []
_datas = []
_binaries = []


def _gather(pkg: str) -> None:
    datas, binaries, hidden = collect_all(pkg)
    _datas.extend(datas)
    _binaries.extend(binaries)
    _hidden.extend(hidden)


# Heavy runtime deps with dynamic imports that PyInstaller's static analysis
# routinely misses. `collect_all` pulls modules + package data files.
for pkg in (
    "aiventbus",
    "uvicorn",
    "uvloop",
    "httptools",
    "websockets",
    "watchfiles",
    "aiosqlite",
    "apscheduler",
    "pydantic",
    "pydantic_core",
    "fastapi",
    "starlette",
    "click",
    "httpx",
    "prometheus_client",
):
    try:
        _gather(pkg)
    except Exception:
        # uvloop is POSIX-only; missing packages shouldn't break the build.
        pass


_hidden += collect_submodules("email")


a = Analysis(
    ["../aiventbus/_launcher.py"],
    pathex=[".."],
    binaries=_binaries,
    datas=_datas,
    hiddenimports=_hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="aiventbus-launcher",
    console=True,
    strip=False,
    upx=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="aiventbus-bundle",
)

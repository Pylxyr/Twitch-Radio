# PyInstaller spec for the bot core ("TwitchRadioCore").
# Build from the project root:
#   pyinstaller packaging/core.spec --noconfirm --distpath dist/core --workpath build/pyinstaller
# Result: dist/core/TwitchRadioCore/TwitchRadioCore.exe (one-folder build; a
# one-file build would unpack ~150 MB to %TEMP% on every start and again for
# every yt-dlp worker process).
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(ROOT, ".env.example"), "."),
    (os.path.join(ROOT, "assets"), "assets"),
    (os.path.join(ROOT, "twitch_radio", "admin", "static"), os.path.join("twitch_radio", "admin", "static")),
]
binaries = []
hiddenimports = collect_submodules("twitch_radio") + ["dotenv"]

# yt-dlp loads extractors/plugins dynamically; curl_cffi ships native
# browser-impersonation libraries. yt_dlp_ejs is deliberately NOT collected:
# yt-dlp downloads the matching JS solver scripts at runtime.
for package in ("yt_dlp", "curl_cffi", "twitchio", "aiohttp", "certifi"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception as exc:  # a missing optional package must not stop the build
        print(f"WARNING: could not collect {package}: {exc}")
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# The version of the yt-dlp that is frozen in, so ytdlp_loader can tell whether
# a user-installed copy is newer. Written next to the spec, outside git.
import yt_dlp.version

_cache = os.path.join(SPECPATH, ".cache")
os.makedirs(_cache, exist_ok=True)
_version_file = os.path.join(_cache, "ytdlp_bundled_version.txt")
with open(_version_file, "w", encoding="utf-8") as _handle:
    _handle.write(yt_dlp.version.__version__)
datas.append((_version_file, "."))

a = Analysis(
    [os.path.join(ROOT, "bot.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "unittest", "pydoc", "test", "yt_dlp_ejs"],
    noarchive=False,
)


def _is_ejs(name):
    top = name.replace("\\", "/").split("/", 1)[0].split(".", 1)[0]
    return top == "yt_dlp_ejs" or top.startswith("yt_dlp_ejs-")


# yt-dlp's own PyInstaller hook adds the package's JS files when it is
# installed, which would also make `import yt_dlp_ejs` succeed in the frozen core.
a.datas = [entry for entry in a.datas if not _is_ejs(entry[0])]
a.pure = [entry for entry in a.pure if not _is_ejs(entry[0])]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TwitchRadioCore",
    console=True,  # stdin/stdout carry the control protocol; the app starts it hidden
    upx=False,
    icon=os.path.join(ROOT, "gui", "build", "icon.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, name="TwitchRadioCore", upx=False)

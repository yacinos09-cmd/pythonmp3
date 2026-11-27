#!/usr/bin/env python3
"""Compile MP3 Compiler en application autonome avec PyInstaller.

    python build.py                 # Windows → dist/MP3 Compiler.exe (fichier unique)
                                    # macOS   → dist/MP3 Compiler.app
    python build.py --onedir        # Windows : dossier (démarrage plus rapide)
    python build.py --ffmpeg PATH   # FFmpeg à intégrer (sinon détection auto)
    python build.py --no-ffmpeg     # ne pas intégrer FFmpeg (détecté sur le système)

FFmpeg intégré, par ordre de priorité :
    1. --ffmpeg PATH
    2. vendor/ffmpeg/ffmpeg(.exe)   (déposez-y un binaire statique)
    3. le binaire fourni par le paquet pip « imageio-ffmpeg »

PyInstaller ne fait pas de compilation croisée : construisez le .exe sous
Windows et le .app sous macOS (ou utilisez le workflow GitHub Actions fourni).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from app import APP_NAME, __version__  # noqa: E402

BUILD = ROOT / "build"
DIST = ROOT / "dist"
IS_WIN, IS_MAC = os.name == "nt", sys.platform == "darwin"
EXE = "ffmpeg.exe" if IS_WIN else "ffmpeg"

EXCLUDES = ["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
            "PySide6.QtWebEngineQuick", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
            "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf",
            "PySide6.QtQuick3D", "PySide6.QtDesigner"]


def locate_ffmpeg(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            sys.exit(f"✘ FFmpeg introuvable : {p}")
        return p
    vendored = ROOT / "vendor" / "ffmpeg" / EXE
    if vendored.is_file():
        return vendored
    try:
        import imageio_ffmpeg
        return Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return None


def check_binary(ffmpeg: Path) -> None:
    try:
        enc = subprocess.run([str(ffmpeg), "-hide_banner", "-encoders"], capture_output=True,
                             text=True, timeout=30).stdout
    except OSError as exc:
        sys.exit(f"✘ Le binaire FFmpeg ne s'exécute pas sur cette machine : {exc}")
    if "libmp3lame" not in enc:
        print("⚠ Ce FFmpeg ne contient pas libmp3lame : le réencodage MP3 sera impossible.")
    else:
        print("✔ FFmpeg contient libmp3lame")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--onedir", action="store_true", help="dossier au lieu d'un .exe unique (Windows/Linux)")
    ap.add_argument("--ffmpeg", help="chemin du binaire FFmpeg à intégrer")
    ap.add_argument("--no-ffmpeg", action="store_true", help="ne pas intégrer FFmpeg")
    args = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("✘ PyInstaller manquant : pip install -r requirements-build.txt")

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
           "--name", APP_NAME, "--distpath", str(DIST), "--workpath", str(BUILD / "work"),
           "--specpath", str(BUILD), "--paths", str(ROOT),
           "--hidden-import", "PySide6.QtMultimedia"]
    for m in EXCLUDES:
        cmd += ["--exclude-module", m]

    # .app macOS : toujours en mode dossier (le mode onefile est déconseillé pour les .app)
    cmd.append("--onedir" if (args.onedir or IS_MAC) else "--onefile")

    if IS_MAC:
        cmd += ["--osx-bundle-identifier", "com.mp3compiler.app"]
    icon = ROOT / "assets" / ("icon.ico" if IS_WIN else "icon.icns")
    if icon.is_file() and (IS_WIN or IS_MAC):
        cmd += ["--icon", str(icon)]
    png = ROOT / "assets" / "icon.png"
    if png.is_file():
        cmd += ["--add-data", f"{png}{os.pathsep}assets"]

    if not args.no_ffmpeg:
        ff = locate_ffmpeg(args.ffmpeg)
        if ff is None:
            print("⚠ Aucun FFmpeg à intégrer : l'application cherchera FFmpeg sur le système.\n"
                  "  (pip install imageio-ffmpeg, ou déposez un binaire dans vendor/ffmpeg/)")
        else:
            print(f"→ FFmpeg intégré : {ff}")
            check_binary(ff)
            stage = BUILD / "ffmpeg_stage"
            shutil.rmtree(stage, ignore_errors=True)
            stage.mkdir(parents=True)
            staged = stage / EXE              # renommé en « ffmpeg(.exe) »
            shutil.copy2(ff, staged)
            staged.chmod(0o755)
            cmd += ["--add-binary", f"{staged}{os.pathsep}ffmpeg"]

    cmd.append(str(ROOT / "main.py"))
    print("→", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    subprocess.run(cmd, check=True)

    if IS_MAC:
        out = DIST / f"{APP_NAME}.app"
    elif args.onedir:
        out = DIST / APP_NAME
    else:
        out = DIST / (f"{APP_NAME}.exe" if IS_WIN else APP_NAME)
    print(f"\n✔ {APP_NAME} {__version__} compilé : {out}")
    if IS_MAC:
        print("  Distribution : ditto -c -k --keepParent "
              f"\"dist/{APP_NAME}.app\" \"dist/{APP_NAME}-macOS.zip\"")


if __name__ == "__main__":
    main()

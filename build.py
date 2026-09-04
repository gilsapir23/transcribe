"""Build a standalone desktop app with PyInstaller for the current OS.

Usage:
    python build.py

Output:
    Windows -> dist/TranscribeApp/TranscribeApp.exe  (copy the whole
               dist/TranscribeApp folder anywhere, or zip it, to "install"
               on another Windows machine — no installer required)
    macOS   -> dist/TranscribeApp.app                (zip it or drop it in
               /Applications on another Mac)

Run this ON the target OS (PyInstaller does not cross-compile). The
GitHub Actions workflow in .github/workflows/build.yml runs this on both
a Windows and a macOS runner automatically for every tagged release.
"""
import platform
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg

ROOT = Path(__file__).parent
SEP = ";" if sys.platform == "win32" else ":"


def main():
    ffmpeg_bin = Path(imageio_ffmpeg.get_ffmpeg_exe())

    args = [
        sys.executable, "-m", "PyInstaller",
        "--name", "TranscribeApp",
        "--windowed",
        "--noconfirm",
        "--clean",
        "--add-data", f"{ROOT / 'resources'}{SEP}resources",
        "--add-binary", f"{ffmpeg_bin}{SEP}ffmpeg_bin",
        str(ROOT / "main.py"),
    ]

    icon_ico = ROOT / "resources" / "icon.ico"
    icon_icns = ROOT / "resources" / "icon.icns"
    if sys.platform == "win32" and icon_ico.exists():
        args[-1:-1] = ["--icon", str(icon_ico)]
    elif sys.platform == "darwin" and icon_icns.exists():
        args[-1:-1] = ["--icon", str(icon_icns)]

    print("Running:", " ".join(args))
    subprocess.run(args, check=True, cwd=ROOT)

    _dedupe_ffmpeg_copy()

    print()
    print(f"Build complete for {platform.system()}. See the 'dist' folder.")


def _dedupe_ffmpeg_copy():
    """PyInstaller's own imageio_ffmpeg hook auto-collects the ffmpeg binary
    into '<bundle>/imageio_ffmpeg/binaries/' in addition to the copy we place
    at '<bundle>/ffmpeg_bin/' (which app/transcriber.py prefers when frozen).
    That's ~85-90MB duplicated for nothing, so drop the auto-collected copy.
    """
    dist = ROOT / "dist"
    for binaries_dir in dist.rglob("imageio_ffmpeg/binaries"):
        for f in binaries_dir.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    main()

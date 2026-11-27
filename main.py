#!/usr/bin/env python3
"""Point d'entrée de MP3 Compiler.

    python main.py                  lance l'application
    python main.py --check-ffmpeg   diagnostic : affiche le FFmpeg détecté et quitte
"""
import sys


def _check_ffmpeg() -> int:
    from app.audio import check_ffmpeg
    st = check_ffmpeg()
    print(st.describe())
    print(st.path or "-")
    return 0 if st.ok and st.has_mp3_encoder else 1


if __name__ == "__main__":
    if "--check-ffmpeg" in sys.argv:
        sys.exit(_check_ffmpeg())
    from app.ui.main_window import run
    sys.exit(run())

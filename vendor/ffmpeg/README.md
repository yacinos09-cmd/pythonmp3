# FFmpeg à intégrer (optionnel)

Déposez ici un binaire **statique** de FFmpeg pour la plateforme sur laquelle vous compilez :

- Windows : `vendor/ffmpeg/ffmpeg.exe` — build « essentials » de https://www.gyan.dev/ffmpeg/builds/ ou https://github.com/BtbN/FFmpeg-Builds/releases
- macOS : `vendor/ffmpeg/ffmpeg` — build statique de https://evermeet.cx/ffmpeg/ (Intel) ou https://www.osxexperts.net/ (Apple Silicon)

N'utilisez pas le `ffmpeg` installé par Homebrew pour l'intégration : il dépend de bibliothèques
dynamiques présentes uniquement sur votre Mac.

Si ce dossier est vide, `build.py` utilise automatiquement le binaire fourni par `pip install imageio-ffmpeg`.
Ce même dossier sert aussi en mode développement (`python main.py`).

# MP3 Compiler

Application de bureau (Windows / macOS / Linux) pour **organiser des fichiers MP3 par catégories**
et **les compiler en un seul fichier MP3** par catégorie, ou tout regroupé.

- Python 3.10+ · interface **PySide6 (Qt 6)** · traitement audio **FFmpeg** · analyse **mutagen**
- Exécutable autonome via **PyInstaller** (`.exe` Windows, `.app` macOS), FFmpeg intégré

---

## Fonctionnalités

**Catégories**
- Créer, renommer (double-clic ou F2), réordonner (glisser-déposer ou ▲▼), supprimer.
- Case à cocher par catégorie : catégories incluses dans l'export.
- Colonnes nombre de pistes et durée (des pistes cochées).

**Pistes**
- Ajout par sélection de fichiers, de dossier (récursif) ou **glisser-déposer** depuis l'explorateur/Finder
  — sur la liste des pistes (à la position du dépôt) ou directement sur une catégorie.
- Réordonner (glisser-déposer ou ▲▼, multi-sélection), retirer (Suppr), renommer le titre,
  **déplacer vers une autre catégorie** (glisser une piste sur une catégorie ou clic droit).
- Durée, format (débit, fréquence, canaux) de chaque piste ; durée totale de la catégorie.
- Fichiers corrompus refusés à l'import ; fichiers devenus introuvables affichés en rouge.
- **Pré-écoute** : double-clic ou Entrée, lecture/pause, stop, barre de position.

**Export**
- Portée : catégories cochées, ou catégorie courante seule. Seules les pistes cochées sont exportées.
- Un MP3 **par catégorie** ou **tout regroupé** en un seul MP3 (dans l'ordre des catégories).
- **Arborescence** : préréglages (à plat, sous-dossier par catégorie, numérotés…) ou modèle libre
  avec `{project}`, `{category}`, `{n}`, `{count}`, `{date}` et `/` pour les sous-dossiers.
  **Aperçu en direct** des fichiers qui seront créés.
- **Encodage** :
  - *Automatique* : concaténation **sans réencodage** (instantanée, sans perte) quand toutes les pistes
    sont des MP3 de même fréquence et nombre de canaux ; sinon réencodage automatique.
  - *Toujours réencoder* : 128 → 320 kb/s, 44,1 / 48 kHz, stéréo / mono.
  - Le réencodage décode tout en PCM puis encode **en une seule passe** : aucun blanc entre les pistes.
  - Si la copie directe échoue sur un fichier atypique → bascule automatique sur le réencodage.
- Fichier existant : remplacer / créer « nom (2).mp3 » / ne pas exporter.
- Pistes manquantes ou corrompues : ignorées (signalées) ou bloquantes, au choix.
- Export dans un thread séparé : **barre de progression**, **journal coloré**, **annulation**,
  écriture atomique (`.part` renommé à la fin : jamais de MP3 à moitié écrit).
- Tags ID3 : titre = catégorie, album = projet.

**Projet et sauvegarde**
- Chaque projet est un dossier contenant `project.json` (+ `project.json.bak`).
- **Sauvegarde automatique** à chaque modification (écriture atomique) ; dernier projet rouvert au lancement.
- Fichiers **référencés** (par défaut) ou **copiés** dans `media/` du projet (projet portable) —
  menu *Fichier › Copier les fichiers importés*, et *Rassembler tous les fichiers dans le projet*.

---

## Arborescence

```
mp3-compiler/
├── main.py                  Point d'entrée (+ diagnostic --check-ffmpeg)
├── app/
│   ├── __init__.py          Nom et version
│   ├── models.py            Projet / Catégorie / Piste, persistance JSON atomique
│   ├── audio.py             Analyse (mutagen), détection FFmpeg, export (copie / réencodage)
│   ├── export_plan.py       Sélection, modèles de chemins, noms de fichiers sûrs
│   └── ui/
│       ├── main_window.py   Fenêtre principale, lecteur, journal
│       ├── widgets.py       Listes réordonnables avec glisser-déposer
│       ├── dialogs.py       Dialogue d'export, paramètres FFmpeg
│       └── export_worker.py Export en arrière-plan (QThread)
├── tests/test_core.py       Tests (modèle, chemins, export réel avec FFmpeg)
├── build.py                 Compilation PyInstaller (intègre FFmpeg)
├── vendor/ffmpeg/           (optionnel) binaire FFmpeg statique à intégrer
├── assets/                  (optionnel) icon.ico / icon.icns / icon.png
├── .github/workflows/build.yml  Build automatique Windows + macOS
├── requirements.txt         Dépendances d'exécution
└── requirements-build.txt   + PyInstaller, imageio-ffmpeg, pytest
```

---

## 1. Installer

```bash
cd mp3-compiler
python -m venv .venv
# Windows :  .venv\Scripts\activate
# macOS   :  source .venv/bin/activate
pip install -r requirements-build.txt     # ou requirements.txt pour simplement lancer l'app
```

## 2. Vérifier FFmpeg

```bash
python main.py --check-ffmpeg
# FFmpeg 7.1 — libmp3lame OK
# /chemin/vers/ffmpeg
```

FFmpeg est cherché dans cet ordre : chemin défini dans *Paramètres* → variable `MP3COMPILER_FFMPEG` →
FFmpeg intégré à l'exécutable → `vendor/ffmpeg/` → `PATH` → emplacements courants
(`/opt/homebrew/bin`, `/usr/local/bin`, `C:\ffmpeg\bin`…) → paquet `imageio-ffmpeg`.

Avec `requirements-build.txt`, `imageio-ffmpeg` fournit déjà un FFmpeg : rien d'autre à installer.
Sinon : `winget install Gyan.FFmpeg` (Windows) ou `brew install ffmpeg` (macOS).
L'encodeur **libmp3lame** est nécessaire pour le réencodage (la copie directe s'en passe).

## 3. Lancer en développement

```bash
python main.py
python -m pytest -q        # 8 tests, dont des exports réels
```

## 4. Compiler l'exécutable

PyInstaller ne fait **pas de compilation croisée** : compilez sur la plateforme cible.

**Windows** (PowerShell) :
```powershell
python build.py            # → dist\MP3 Compiler.exe  (fichier unique, FFmpeg inclus)
python build.py --onedir   # → dist\MP3 Compiler\     (démarrage plus rapide)
```

**macOS** :
```bash
python build.py            # → dist/MP3 Compiler.app
ditto -c -k --keepParent "dist/MP3 Compiler.app" "dist/MP3-Compiler-macOS.zip"
```

Options : `--ffmpeg CHEMIN` (binaire à intégrer), `--no-ffmpeg` (utiliser celui du système).
Priorité du FFmpeg intégré : `--ffmpeg` → `vendor/ffmpeg/` → `imageio-ffmpeg`. Le script vérifie
que le binaire s'exécute et contient libmp3lame. Une icône dans `assets/` est utilisée si présente.

**Sans Windows ou sans Mac** : poussez le projet sur GitHub, puis *Actions › build › Run workflow*
(ou poussez un tag `v1.0.0`). Le `.exe` et le `.app` zippé sont téléchargeables dans les artefacts.

### Première ouverture sur macOS
L'app n'est pas signée par un développeur Apple : clic droit › **Ouvrir** la première fois,
ou `xattr -dr com.apple.quarantine "/Applications/MP3 Compiler.app"`.
Pour une diffusion large : signature `codesign` + notarisation avec un compte Apple Developer.

### Windows SmartScreen
Un `.exe` non signé déclenche « Windows a protégé votre ordinateur » → *Informations
complémentaires › Exécuter quand même*. Une signature Authenticode supprime l'avertissement.

---

## Format du projet (`project.json`)

```json
{
  "format_version": 1,
  "name": "Mon projet",
  "copy_files": false,
  "categories": [
    {
      "id": "3f2a…", "name": "Intro", "enabled": true,
      "tracks": [
        { "id": "a91c…", "path": "media/jingle.mp3", "title": "jingle", "enabled": true,
          "duration": 12.4, "codec": "mp3", "sample_rate": 44100, "channels": 2, "bitrate": 192 }
      ]
    }
  ]
}
```
Chemins relatifs = fichiers dans le dossier du projet ; chemins absolus = fichiers référencés.

## Dépannage

| Symptôme | Solution |
|---|---|
| 🔴 « FFmpeg introuvable » en bas de la fenêtre | *Fichier › Paramètres / FFmpeg…* → indiquer `ffmpeg(.exe)` puis *Tester* |
| « sans libmp3lame » | Utiliser un build FFmpeg complet (gyan.dev, evermeet.cx, imageio-ffmpeg) |
| Piste en rouge | Fichier déplacé/supprimé : la retirer et la réimporter |
| Pas de son en pré-écoute | Vérifier la sortie audio du système ; `MP3COMPILER_NO_PLAYER=1` désactive le lecteur |
| Démarrage lent du `.exe` | Normal en mode fichier unique (décompression) ; utiliser `--onedir` |

## Licences
Le code de l'application est libre d'usage. **FFmpeg** est sous LGPL/GPL selon le build (les builds
incluant libmp3lame sont généralement GPL) : si vous redistribuez l'exécutable avec FFmpeg intégré,
joignez la licence de FFmpeg et indiquez où obtenir ses sources. PySide6 est sous LGPLv3.

"""
prepare.py — everything for one episode, in one command.

    python prepare.py episodes/episode-02 --title "Mon épisode" --level A2

It runs, in order, and tells you what it skipped and why:
  1. import_episode.py   listen, align the transcript, let Claude choose the words   (skipped if episode.json exists)
  2. generate_images.py  one picture per word, on this computer                      (skipped if the image tools are not installed)
  3. generate_voice.py   voice for what the original audio cannot give               (skipped if there is no voice program)
  4. db.py               load the episode into PostgreSQL                            (skipped if ATELIER_DB is not set)

Options
  --again        run step 1 even if episode.json exists (the listening itself is reused from heard.json)
  --no-images    skip step 2          --no-voice    skip step 3
  anything else (--title, --level, --max-words, --whisper…) is passed to import_episode.py
"""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(script, *args):
    print(f"\n── {script} " + "─" * max(4, 60 - len(script)))
    return subprocess.call([sys.executable, "-u", str(HERE / script), *args], cwd=str(HERE)) == 0


def main():
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        raise SystemExit(__doc__)
    source = args.pop(0)
    flags = {name: name in args for name in ("--again", "--no-images", "--no-voice")}
    rest = [a for a in args if a not in flags]
    folder = Path(source) if os.path.isdir(source) else Path(source).resolve().parent
    done, skipped = [], []

    if (folder / "episode.json").is_file() and not flags["--again"]:
        skipped.append("préparation : episode.json existe déjà (--again pour recommencer)")
    elif run("import_episode.py", source, *rest):
        done.append("préparation")
    else:
        raise SystemExit("\nLa préparation a échoué : je m'arrête là.")

    if flags["--no-images"]:
        skipped.append("images : --no-images")
    elif not all(importlib.util.find_spec(m) for m in ("torch", "diffusers")):
        skipped.append("images : outils absents (pip install -r requirements-images.txt)")
    else:
        done.append("images") if run("generate_images.py", str(folder)) else skipped.append("images : la génération a échoué (voir plus haut)")

    if flags["--no-voice"]:
        skipped.append("voix : --no-voice")
    elif not (shutil.which("edge-tts") or shutil.which("say")):
        skipped.append("voix : ni edge-tts ni `say` ici (pip install edge-tts)")
    else:
        done.append("voix") if run("generate_voice.py", str(folder)) else skipped.append("voix : la synthèse a échoué (voir plus haut)")

    try:
        from dotenv import load_dotenv
        load_dotenv(HERE / ".env")
    except ImportError:
        pass
    if not os.environ.get("ATELIER_DB"):
        skipped.append("base de données : ATELIER_DB n'est pas défini (l'app lit alors les fichiers)")
    else:
        done.append("base de données") if run("db.py", "sync") else skipped.append("base de données : le chargement a échoué (voir plus haut)")

    print("\nFait : " + (", ".join(done) or "rien"))
    for line in skipped:
        print("Passé : " + line)
    print("Ouvre l'app (python server.py) : l'épisode est dans la liste en haut.")


if __name__ == "__main__":
    main()

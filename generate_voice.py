"""
generate_voice.py — the voice that reads the few words the original audio cannot give.

The rule of the app: everything a learner hears is the podcast itself, with one exception.
  - "Phrase" and "Extrait" (a few words around the word) always play the ORIGINAL audio.
  - "Mot" plays the original audio when the episode says the word in its dictionary form ("atteindre").
  - When the episode says another form ("souhaite" for "souhaiter"), or the word could not be cut for sure,
    a voice reads the dictionary form                                  -> voice/<n>-w.mp3
Sentences are never read by a voice: a voice has no real intonation. There are no generated example sentences.
The voice is a ready-made one. It never imitates the podcast's host.

Use
  python generate_voice.py episodes/episode-01                 Denise, a natural French female voice (needs: pip install edge-tts)
  python generate_voice.py episodes/episode-01 --voice fr-FR-EloiseNeural      another voice of the same service
  python generate_voice.py episodes/episode-01 --engine say --voice Amélie     the Mac's own voices, offline
  python generate_voice.py episodes/episode-01 --command "piper -m voix.onnx -f {out}" --ext wav
                                                               any other program: it receives the text on its
                                                               standard input ({text} also works) and must write {out}
Options
  --redo     make again the files that already exist
  --limit N  only the first N files (to try)
  --list     only show what would be made

The default engine, edge-tts, sends each word to Microsoft's online read-aloud service: it needs the internet.
voice/sources.json says, for every word, where its sound comes from and which text each file reads.
The server only plays a voice file whose text there matches the word of today: a file left over from an
older word list is never played for the wrong word.
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time

from import_episode import norm, word_clip

DENISE = "fr-FR-DeniseNeural"


def plan(episode):
    """What each word needs. Returns (jobs, sources): jobs are (file stem, text) to generate."""
    jobs, sources = [], []
    sentences = episode.get("sentences", [])
    for i, v in enumerate(episode.get("vocab", [])):
        if not 0 <= v.get("sid", -1) < len(sentences):
            continue
        clip = word_clip(sentences[v["sid"]], v["form"])
        same_form = norm(v["form"]) == norm(v["word"])
        entry = {"n": i, "word": v["word"]}
        if clip and same_form:
            entry["word_audio"] = {"source": "original", "start": clip[0], "end": clip[1]}
        else:
            entry["word_audio"] = {"source": "voice", "file": f"{i}-w", "text": v["word"],
                                   "why": "not found in the audio" if not clip else f"the episode says « {v['form']} »"}
            jobs.append((f"{i}-w", v["word"]))
        sources.append(entry)
    return jobs, sources


def speak_with_edge(text, out, voice):
    """Microsoft's online voices through the edge-tts program. Writes an MP3."""
    subprocess.run(["edge-tts", "--voice", voice, "--text", text, "--write-media", out], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def speak_with_say(text, out, voice):
    """macOS only, offline. Writes a WAV file that every phone browser can play."""
    subprocess.run(["say", "-v", voice, "-o", out, "--data-format=LEI16@22050", text], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def speak_with_command(text, out, template):
    args = [a.replace("{out}", out).replace("{text}", text) for a in shlex.split(template)]
    subprocess.run(args, input=text.encode("utf-8"), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def main():
    ap = argparse.ArgumentParser(description="Voix pour les mots et les phrases que l'audio original ne donne pas.")
    ap.add_argument("folder")
    ap.add_argument("--engine", default=None, choices=["edge", "say"],
                    help="edge (par défaut si edge-tts est installé) ou say (voix du Mac, hors ligne)")
    ap.add_argument("--voice", default=None, help=f"edge : {DENISE} par défaut ; say : Thomas, Amélie…")
    ap.add_argument("--command", default=None, help="autre programme de synthèse ; {out} = fichier à écrire")
    ap.add_argument("--ext", default="wav", choices=["wav", "m4a", "mp3"], help="type de fichier écrit par --command")
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    path = os.path.join(args.folder, "episode.json")
    if not os.path.isfile(path):
        raise SystemExit(f"Pas d'épisode préparé ici : {path}")
    with open(path, encoding="utf-8") as f:
        episode = json.load(f)
    jobs, sources = plan(episode)
    original = sum(1 for s in sources if s["word_audio"]["source"] == "original")
    print(f"{len(sources)} mots : {original} dits tels quels dans l'audio original, {len(sources) - original} à lire par la voix")
    if args.list:
        for stem, text in jobs:
            print(f"  {stem}  {text}")
        return

    if args.command:
        kind, voice, ext, label = "command", None, args.ext, args.command.split()[0]
    else:
        kind = args.engine or ("edge" if shutil.which("edge-tts") else "say")
        if kind == "edge" and not shutil.which("edge-tts"):
            raise SystemExit("edge-tts n'est pas installé : pip install edge-tts")
        if kind == "say" and not shutil.which("say"):
            raise SystemExit("Ni edge-tts ni `say` ici. Installe edge-tts (pip install edge-tts) ou donne un programme avec --command.")
        voice = args.voice or (DENISE if kind == "edge" else "Thomas")
        ext, label = ("mp3" if kind == "edge" else "wav"), f"{kind}:{voice}"
    print(f"Voix : {label}")

    folder = os.path.join(args.folder, "voice")
    os.makedirs(folder, exist_ok=True)
    before = {}                                                   # what the files already there read: {file name: text}
    try:
        with open(os.path.join(folder, "sources.json"), encoding="utf-8") as f:
            for e in json.load(f):
                for key in ("word_audio", "example_audio"):
                    if isinstance(e.get(key), dict) and e[key].get("file"):
                        before[e[key]["file"]] = e[key].get("text")
    except (OSError, ValueError, AttributeError):
        pass
    made = skipped = 0
    failed = []
    try:
        for stem, text in jobs:
            out = os.path.join(folder, f"{stem}.{ext}")
            same_text = before.get(f"{stem}.{ext}") == text      # a file of an older word list is made again
            if os.path.isfile(out) and os.path.getsize(out) > 0 and same_text and not args.redo:
                skipped += 1
                continue
            if args.limit and made >= args.limit:
                break
            error = ""
            for attempt in range(2):                                  # an online voice can fail once: try again
                try:
                    if kind == "command":
                        speak_with_command(text, out, args.command)
                    elif kind == "edge":
                        speak_with_edge(text, out, voice)
                    else:
                        speak_with_say(text, out, voice)
                    error = "" if os.path.isfile(out) and os.path.getsize(out) > 0 else "aucun fichier écrit"
                except FileNotFoundError as exc:
                    raise SystemExit(f"Programme introuvable : {exc.filename}")
                except subprocess.CalledProcessError as exc:
                    error = (exc.stderr.decode(errors="replace").strip().splitlines() or ["erreur"])[-1]
                if not error:
                    break
                time.sleep(1)
            if error:
                failed.append(stem)
                print(f"  {stem}  ÉCHEC : {error}")
                if len(failed) >= 5 and made == 0:                    # nothing works: stop instead of failing 200 times
                    raise SystemExit("Rien ne passe. Vérifie la connexion internet (edge) ou le nom de la voix.")
                continue
            made += 1
            print(f"  {stem}.{ext}  {text}")
    finally:                                                      # even after Ctrl+C, say what each file is
        for s in sources:
            for key in ("word_audio", "example_audio"):
                if key in s and s[key]["source"] == "voice":
                    s[key]["file"] += f".{ext}"
                    s[key]["engine"] = label
        with open(os.path.join(folder, "sources.json"), "w", encoding="utf-8") as f:
            json.dump(sources, f, ensure_ascii=False, indent=1)
    print(f"{made} fichiers créés, {skipped} déjà là" + (f", {len(failed)} en échec (relance la commande pour les refaire)" if failed else "")
          + f". Dossier : {folder}")


if __name__ == "__main__":
    sys.exit(main())

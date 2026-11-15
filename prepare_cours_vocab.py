#!/usr/bin/env python3
"""
prepare_cours_vocab.py — the words of each course lesson, written beside its video as <video>.vocab.json.

    python prepare_cours_vocab.py                       every lesson in cours/ that has no .vocab.json yet
    python prepare_cours_vocab.py "cours/Road to A1 _ French Course/Unite 10"    one folder (or one video file)
    --force          redo lessons that already have a file (and listen to them again)
    --max 40         words per lesson (default 40)
    --whisper small  Whisper size, as in prepare_sons.py
    --langue en      language of the definitions: en or fr

For each word, the teacher's own voice is cut from the video: <stem>.clips/<n>.mp3 (needs ffmpeg:
`brew install ffmpeg`). The timings Whisper heard are kept in <stem>.heard.json, so a lesson that
already has its .vocab.json but no sound yet is given its clips without listening again.

Then check the files by hand, and load them with:  python db.py cours-vocab
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

import prepare_sons                                  # transcribe(): the same Whisper listening as the pronunciation lessons
from import_episode import tokens                    # the same word splitting as the episodes

VIDEO_EXT = (".webm", ".mkv", ".mp4")
MODEL = os.environ.get("ATELIER_CLAUDE_MODEL") or "claude-sonnet-5-5"
LANGS = {"en": "English", "fr": "French"}
PROMPT = """You are preparing vocabulary for a French course for beginners (level A1).
The lesson is "{title}". Below is its speech, as a speech recogniser heard it with French set as the language.
The teacher often explains in another language: ignore those parts. Keep only French the teacher really says,
and correct a recogniser mistake only when you are sure of it.

Choose up to {max_words} words or short expressions that a learner at A1 should learn from this lesson:
useful everyday words, verbs in their dictionary form. No names of people, no grammar labels.

Answer with only a JSON list and no other text. Each item is an object with these keys:
"word": the form people use most in everyday spoken French, with its article for a noun ("une chambre");
  for a noun that is almost always said in the plural (les frites, les maths), give the plural ("les frites");
  for a masculine/feminine noun, give both forms with the feminine first ("une voisine / un voisin");
  examples: "les frites" (not "une frite"), "les maths" (not "une mathématique");
"form": the form used in the lesson ("chambre", "sur");
"pluriel": the everyday plural with its article ("des voisins / des voisines", "les yeux"), or the same word
  when the noun has no plural or is only used in one form ("le pain"); an empty string for a non-noun;
"pos": one of verbe, nom, adjectif, adverbe, expression, autre;
"definition": a short definition in {lang};
"example": one short French sentence from the lesson;
"img": for a concrete noun or action, one short English sentence describing a simple picture of it (a person, a place, an object), or an empty string for an abstract word.

Speech:
{speech}
"""


def ask(title, heard, lang, max_words, client, model):
    speech = " ".join(word for word, _, _ in heard)
    prompt = PROMPT.format(title=title, max_words=max_words, lang=LANGS[lang], speech=speech)
    reply = client.messages.create(model=model, max_tokens=6000, messages=[{"role": "user", "content": prompt}])
    text = "".join(block.text for block in reply.content if getattr(block, "type", "") == "text")
    items = json.loads(text[text.index("["): text.rindex("]") + 1])
    return [item for item in items if isinstance(item, dict) and str(item.get("word") or "").strip()][:max_words]


def word_timings(heard):
    """[(token, start, end)] for every heard word, split the same way as the form of a vocabulary item."""
    timed = []
    for word, start, end in heard:
        parts = tokens(word)
        if len(parts) == 1:                          # a word Whisper split in two is not used for clips
            timed.append((parts[0], start, end))
    return timed


def clip_of(timed, form):
    """(start, end) in seconds where the form is spoken, with a little room either side; None if not safe to cut."""
    want = tokens(form)
    if not want:
        return None
    for i in range(len(timed) - len(want) + 1):
        if [t for t, _, _ in timed[i:i + len(want)]] == want:
            start = max(0.0, timed[i][1] - 0.08)
            end = timed[i + len(want) - 1][2] + 0.12
            if 0.15 <= end - start <= 6:
                return round(start, 2), round(end, 2)
            return None
    return None


def cut(video, start, end, out):
    """Write the teacher's voice between start and end (seconds) as an mp3 file."""
    out.parent.mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-ss", f"{start:.2f}",
                    "-t", f"{end - start:.2f}", "-vn", "-c:a", "libmp3lame", "-q:a", "4", str(out)], check=True)


def add_clips(video, items, heard, root):
    """Give each item its clip: the file's path relative to cours/, or no clip when the word is not clear."""
    timed = word_timings(heard)
    clips_dir = video.with_name("." + video.stem + ".clips")
    for i, item in enumerate(items):
        item.pop("clip", None)
        span = clip_of(timed, str(item.get("form") or item["word"]))
        if not span:
            continue
        out = clips_dir / f"{i}.mp3"
        cut(video, span[0], span[1], out)
        item["clip"] = out.resolve().relative_to((root / "cours").resolve()).as_posix()
    return sum(1 for item in items if item.get("clip"))


def videos_in(target):
    if target.is_file():
        return [target]
    return sorted(p for p in target.rglob("*") if p.suffix.lower() in VIDEO_EXT)


def main(argv=None):
    ap = argparse.ArgumentParser(description="The words of each course lesson.")
    ap.add_argument("target", nargs="?", help="a folder or a video file (default: all of cours/)")
    ap.add_argument("--force", action="store_true", help="redo lessons that already have a .vocab.json")
    ap.add_argument("--max", type=int, default=40, help="words per lesson")
    ap.add_argument("--whisper", default="small", help="Whisper size: tiny, base, small, medium")
    ap.add_argument("--langue", default="en", choices=sorted(LANGS), help="language of the definitions")
    args = ap.parse_args(argv)

    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg est nécessaire pour découper la voix du professeur : brew install ffmpeg")

    root = Path(os.environ.get("ATELIER_ROOT") or Path(__file__).resolve().parent)
    target = Path(args.target) if args.target else root / "cours"
    if not target.exists():
        target = root / args.target
    if not target.exists():
        raise SystemExit(f"Introuvable : {args.target}")

    client = None
    done = 0
    for video in videos_in(target):
        out = video.with_name(video.stem + ".vocab.json")
        heard_file = video.with_name(video.stem + ".heard.json")
        if out.exists() and not args.force:
            # a lesson made before the sounds existed: cut its clips from the saved timings, if there are any
            if heard_file.exists() and '"clip"' not in out.read_text(encoding="utf-8"):
                items = json.loads(out.read_text(encoding="utf-8"))
                heard = [tuple(h) for h in json.loads(heard_file.read_text(encoding="utf-8"))]
                kept = add_clips(video, items, heard, root)
                out.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(f"{kept} voix découpées → {out.name}")
            else:
                print(f"déjà fait : {out.name}")
            continue
        try:
            heard = prepare_sons.transcribe(video, args.whisper, "fr")
            if not heard:
                print(f"rien entendu : {video.name}")
                continue
            if client is None:
                import anthropic
                client = anthropic.Anthropic()       # uses ANTHROPIC_API_KEY, or your `ant auth login`
            items = ask(prepare_sons.title_of(video.name), heard, args.langue, args.max, client, MODEL)
            heard_file.write_text(json.dumps(heard, ensure_ascii=False) + "\n", encoding="utf-8")
            kept = add_clips(video, items, heard, root)
        except Exception as exc:                     # one lesson failing does not stop the others
            print(f"erreur sur {video.name} : {exc}", file=sys.stderr)
            traceback.print_exc()
            continue
        out.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        done += 1
        print(f"{len(items)} mots, {kept} voix → {out.name}")
    print(f"{done} leçon(s) préparée(s).")


if __name__ == "__main__":
    main()

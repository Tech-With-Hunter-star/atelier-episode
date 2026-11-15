"""Find, in the videos of a pronunciation course, the moments where the teacher says each example word.

    python prepare_sons.py "cours/French pronunciation course" 12 13 14 15        (the lessons to prepare; none = all)

For each video: Whisper listens and notes the time of every word, then Claude reads those words and writes a short summary
of the lesson and the example words of each sound, with the moment each one is said. The result goes to sons.json in the
course folder; the app then shows, under the video, one button per word that jumps to that moment and repeats it.
A lesson already in sons.json is skipped (add --encore to redo it). Each lesson is one paid call to Claude.

  --whisper small     Whisper size: tiny, base, small, medium (bigger = slower, more accurate)
  --langue fr         the language Whisper expects. fr hears the French example words best, even when the teacher explains in English
  --explications en   language of the summary: en, fr, zh, it
"""
import argparse
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

VIDEO = (".mp4", ".m4v", ".mov", ".mkv", ".webm")
LANGS = {"en": "English", "fr": "French", "zh": "Chinese (simplified)", "it": "Italian"}
MODEL = os.environ.get("ATELIER_CLAUDE_MODEL") or "claude-sonnet-5-5"
MAX_WORDS, MAX_EXAMPLES = 2500, 8
PROMPT = """You are preparing a French pronunciation lesson for a beginner (level A1).
The lesson is a video called "{title}". Below is every word a speech recogniser heard in it, as "index:word".
The recogniser was set to French while the teacher often explains in another language, so the explanations are garbled:
ignore them. What matters are the French example words the teacher says to show a sound. A word may be misheard; keep an
example only when you are confident which French word was really said, and write it correctly.

Return only JSON, with this shape:
{{"resume": "one or two sentences, in {lang}, in your own words: which sound(s) this lesson teaches and the one thing to remember",
  "sons": [{{"son": "the sound as the lesson names it, e.g. ON", "api": "its IPA symbol, e.g. ɔ̃", "graphies": ["on", "om"],
            "seuls": [{{"i": 45}}, {{"i": 210}}],
            "exemples": [{{"mot": "bonjour", "i": 123}}]}}]}}

Rules:
- "i" is the index of the word in the list (for an example of several words, the index of its first word, and add "n": the number of words).
- At most {max_examples} examples per sound, each a different word, spread over the lesson; prefer clear, common words.
- "seuls" lists up to 4 moments where the teacher says the sound by itself, not inside a word, the most certain first. The
  recogniser usually writes it as a very short word, often in capitals ("ON,", "OM", "an", "un", "hein"), typically when the
  teacher names the sound ("we have ON, which is…", "the sound ON") or shows how to make it. Each is one word: give its index.
  Do not take the ordinary English or French word that merely looks the same ("starting on", "on peut", "un bonbon").
  Return "seuls": [] when you cannot find such a moment.
- Only words that are in the list. If you cannot find any example, return "sons": [].
- The summary rests on the title and the example words. Do not repeat garbled explanations, and do not describe the mouth or
  the tongue unless you are certain.

Words:
{words}"""


class SoundsError(Exception):
    """Something the user can fix or retry."""


def nfc(text):
    return unicodedata.normalize("NFC", str(text))


def lesson_number(name):
    found = re.match(r"\s*(\d+)\s*[-–._]", name)
    return int(found.group(1)) if found else None


def title_of(name):
    return re.sub(r"\s+", " ", re.sub(r"^\s*\d+\s*[-–._]\s*", "", nfc(Path(name).stem)).replace("_", " ")).strip()


def transcribe(path, size, language):
    """[(word, start, end), …] for every word Whisper hears in the video."""
    from faster_whisper import WhisperModel
    print(f"Whisper ({size}) écoute {os.path.basename(path)}…")
    model = WhisperModel(size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(path), language=language or None, word_timestamps=True, vad_filter=True)
    heard = []
    for seg in segments:
        for w in seg.words or []:
            if w.word.strip():
                heard.append((w.word.strip(), float(w.start), float(w.end)))
        print(f"  écoute : {min(1.0, seg.end / info.duration):4.0%}", end="\r", flush=True)
    print(f"  {len(heard)} mots entendus, durée {info.duration / 60:.1f} min")
    return heard


def parse(text, heard):
    """Claude's answer, checked against what was heard: {resume, sons: [{son, api, graphies, exemples: [{mot, debut, fin}]}]}."""
    try:
        data = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except ValueError:
        raise SoundsError("La réponse de Claude n'a pas pu être lue.")
    sounds = []
    for s in data.get("sons") or [] if isinstance(data, dict) else []:
        if not isinstance(s, dict) or not str(s.get("son") or "").strip():
            continue
        examples, seen = [], set()
        for e in s.get("exemples") or []:
            try:
                i, n = int(e["i"]), max(1, min(6, int(e.get("n") or 1)))
                word = str(e["mot"]).strip()
            except (KeyError, TypeError, ValueError):
                continue
            if not word or word.lower() in seen or not 0 <= i < len(heard):
                continue                                           # an index Claude invented is dropped
            last = min(len(heard) - 1, i + n - 1)
            start, end = max(0.0, heard[i][1] - 0.25), heard[last][2] + 0.35       # a little air around the word
            if end - start > 8:
                continue
            seen.add(word.lower())
            examples.append({"mot": word, "debut": round(start, 2), "fin": round(max(end, start + 0.9), 2)})
            if len(examples) == MAX_EXAMPLES:
                break
        if examples:
            sound = {"son": str(s["son"]).strip()[:24], "api": str(s.get("api") or "").strip()[:12],
                     "graphies": [str(g).strip()[:12] for g in (s.get("graphies") or []) if str(g).strip()][:10],
                     "exemples": sorted(examples, key=lambda x: x["debut"])}
            alone = []                                             # the sound said alone: the moments Claude found, best first
            for one in (s.get("seuls") or ([s["seul"]] if isinstance(s.get("seul"), dict) else [])):
                try:
                    i = int(one["i"])
                except (KeyError, TypeError, ValueError):
                    continue
                if 0 <= i < len(heard):
                    start = max(0.0, heard[i][1] - 0.2)
                    moment = {"debut": round(start, 2), "fin": round(max(heard[i][2] + 0.35, start + 0.8), 2)}
                    if moment not in alone and moment["fin"] - moment["debut"] <= 4:
                        alone.append(moment)
            if alone:
                sound["seuls"] = alone[:4]
            sounds.append(sound)
    return {"resume": str(data.get("resume") or "").strip()[:400] if isinstance(data, dict) else "", "sons": sounds}


def sounds_of(title, heard, lang="en", client=None, model=None):
    """Ask Claude for the summary and the example words of one lesson. client is only passed by the tests."""
    if not heard:
        return {"resume": "", "sons": []}
    if client is None:
        try:
            import anthropic
            client = anthropic.Anthropic()                        # uses ANTHROPIC_API_KEY, or your `ant auth login`
        except Exception as e:
            raise SoundsError(f"Claude n'est pas disponible sur cet ordinateur : {e}")
    words = "\n".join(f"{i}:{w}" for i, (w, _, _) in enumerate(heard[:MAX_WORDS]))
    prompt = PROMPT.format(title=title, lang=LANGS.get(lang, LANGS["en"]), max_examples=MAX_EXAMPLES, words=words)
    try:
        reply = client.messages.create(model=model or MODEL, max_tokens=4000, messages=[{"role": "user", "content": prompt}])
    except Exception as e:
        raise SoundsError(f"Claude n'a pas répondu : {e}")
    return parse("".join(b.text for b in reply.content if getattr(b, "type", "") == "text"), heard)


def listen(path, size, language):
    """What Whisper heard in a video; kept in .sons/ so that preparing a lesson again does not mean listening again."""
    cache = path.parent / ".sons" / (path.name + f".{size}.{language or 'auto'}.json")
    try:
        return [tuple(w) for w in json.loads(cache.read_text(encoding="utf-8"))]
    except (OSError, ValueError):
        pass
    heard = transcribe(path, size, language)
    cache.parent.mkdir(exist_ok=True)
    cache.write_text(json.dumps(heard, ensure_ascii=False), encoding="utf-8")
    return heard


def load(folder):
    try:
        data = json.loads((folder / "sons.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("videos"), dict) else {"videos": {}}
    except (OSError, ValueError):
        return {"videos": {}}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dossier")
    ap.add_argument("lecons", nargs="*", type=int)
    ap.add_argument("--whisper", default="small")
    ap.add_argument("--langue", default="fr")
    ap.add_argument("--explications", default="en", choices=sorted(LANGS))
    ap.add_argument("--encore", action="store_true")
    args = ap.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass
    folder = Path(args.dossier)
    if not folder.is_dir():
        raise SystemExit(f"Le dossier {folder} n'existe pas.")
    videos = sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO and not p.name.startswith(".")),
                    key=lambda p: (lesson_number(p.name) is None, lesson_number(p.name) or 0, p.name))
    if args.lecons:
        videos = [p for p in videos if lesson_number(p.name) in args.lecons]
    if not videos:
        raise SystemExit("Aucune vidéo ne correspond.")
    data, done = load(folder), 0
    for path in videos:
        name = nfc(path.name)
        if name in data["videos"] and not args.encore:
            print(f"Déjà prête : {name}")
            continue
        try:
            result = sounds_of(title_of(name), listen(path, args.whisper, args.langue), args.explications)
        except SoundsError as e:
            print(f"  ✗ {name} : {e}")
            continue
        data["videos"][name] = result
        (folder / "sons.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")   # saved after each lesson
        done += 1
        print(f"  ✓ {title_of(name)} : " + (", ".join(f"{s['son']} ({len(s['exemples'])} mots{', son seul : ' + str(len(s['seuls'])) + ' moment(s)' if s.get('seuls') else ''})" for s in result["sons"])
                                              or "aucun mot trouvé"))
    print(f"{done} leçon(s) préparée(s). Écrit : {folder / 'sons.json'}. Recharge la page Prononciation de l'app.")


if __name__ == "__main__":
    main()

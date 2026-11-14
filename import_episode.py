"""
import_episode.py — prepare any audio episode for the Atelier d'épisode app.

What it does
  1. Listens to the audio with Whisper and gets the time of every word.
  2. If there is a transcript, lines it up with the audio so each sentence gets an exact start and end.
     If there is none (or it does not match the audio), it uses what Whisper heard as the text.
  3. Asks Claude to pick the words worth learning at your level, with a simple French definition.
  4. Writes everything to one file (episode.json) that the app loads from its "Ajouter" tab.

The simple way: one folder per episode, any names
  episodes/mon-episode/   <- put one audio file here (mp3, m4a, wav…) and, if you have it, one .txt transcript
  python import_episode.py episodes/mon-episode

Or point at the files directly
  python import_episode.py chemin/audio.mp3
  python import_episode.py chemin/audio.mp3 chemin/transcription.txt

Useful options
  --title "…"         name shown in the app (default: the folder or file name)
  --level A1          your level (A1, A2, B1…), used when choosing words
  --max-words 0       how many words Claude may pick; 0 = every word worth learning at your level
  --repeat-known      also pick words you already marked as known in earlier episodes (by default they are skipped)
  --whisper small     Whisper size: tiny, base, small, medium (bigger = slower, more accurate)
  --max-chars 220     cut sentences longer than this at a comma or linking word (0 = never)
  --no-vocab          skip Claude, only do the text and timings (handy for a first test)
  --tag-only          keep the words; only ask Claude for the kind of each word (verb, noun…), one short request
  --words-only        skip the listening: keep the sentences already in episode.json and only redo the words
  --out fichier.json  where to write (default: episode.json next to the audio)
"""
import argparse
import difflib
import json
import os
import re
import sys

# ----------------------------------------------------------------------------- transcript

MUSIC_LINE = re.compile(r"^\[[^\d\]]+\]$")                       # [Musique]
STAMP_LINE = re.compile(r"^\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]\s*(.*)$")  # [03:23] text…
SENTENCE = re.compile(r"[^.!?…]+(?:[.!?…]+[\"»”)]*|$)")


def split_transcript(raw):
    """Return a list of {"text", "para"}; "para" marks the first sentence of a [mm:ss] block."""
    sentences = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or MUSIC_LINE.match(line):
            continue
        m = STAMP_LINE.match(line)
        new_block = bool(m)
        text = m.group(4) if m else line
        if not m and not re.search(r"[.!?…]", line):
            continue                                              # a title line, not speech
        first = True
        for part in SENTENCE.findall(text):
            part = part.strip()
            if not part:
                continue
            glue = sentences and not (first and new_block) and (
                re.match(r"[a-zà-ÿ]", part) or len(sentences[-1]["text"]) < 12)
            if glue:
                sentences[-1]["text"] += " " + part               # "et... et de l'autre" stays one sentence
            else:
                sentences.append({"text": part, "para": first and (new_block or not sentences)})
            first = False
    return sentences


BREAKS = re.compile(r"(?<=[,;:]) | (?=(?:et|mais|donc|parce que|puisque|alors que|ou bien|car)\b)")


def split_long(text, limit):
    """Cut a very long sentence at a comma or a linking word, as near the middle as possible."""
    if limit <= 0 or len(text) <= limit:
        return [text]
    middle, best = len(text) / 2, None
    for m in BREAKS.finditer(text):
        left, right = text[:m.start()].strip(), text[m.end():].strip()
        if len(left) >= 40 and len(right) >= 40 and (best is None or abs(m.start() - middle) < abs(best[0] - middle)):
            best = (m.start(), left, right)
    if best is None:
        return [text]
    return split_long(best[1], limit) + split_long(best[2], limit)


def shorten(sentences, limit):
    """Apply split_long to a whole transcript; only the first piece keeps the paragraph mark."""
    out = []
    for s in sentences:
        for k, piece in enumerate(split_long(s["text"], limit)):
            out.append({"text": piece, "para": s["para"] and k == 0})
    return out


def tokens(text):
    """Lower-case words without punctuation, so the transcript and Whisper can be compared."""
    text = text.lower().replace("’", "'").replace("œ", "oe")
    return [t for t in re.split(r"[^0-9a-zà-ÿ']+", text) if t.strip("'")]


# ----------------------------------------------------------------------------- audio

AUDIO_TYPES = (".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus", ".flac", ".mp4", ".webm")


def transcribe(audio_path, size):
    """Return [(word as heard, start, end), …] for every word Whisper hears, plus the audio length."""
    from faster_whisper import WhisperModel
    print(f"Whisper ({size}) écoute {os.path.basename(audio_path)}… quelques minutes.")
    model = WhisperModel(size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(audio_path, language="fr", word_timestamps=True, vad_filter=True)
    raw = []
    for seg in segments:
        for w in seg.words or []:
            if w.word.strip():
                raw.append((w.word.strip(), float(w.start), float(w.end)))
        print(f"  écoute : {min(1.0, seg.end / info.duration):4.0%}", end="\r", flush=True)
    print(f"  {len(raw)} mots entendus, durée {info.duration / 60:.1f} min")
    return raw, float(info.duration)


def sentences_from_audio(raw):
    """No transcript: build the sentences from what Whisper heard, with their times."""
    sentences, cur = [], []
    for i, (word, a, b) in enumerate(raw):
        cur.append((word, a, b))
        gap = raw[i + 1][1] - b if i + 1 < len(raw) else 99
        full_stop = re.search(r"[.!?…][\"»”)]*$", word)
        too_long = (len(cur) >= 28 and gap > 0.5) or len(cur) >= 40   # Whisper sometimes forgets the full stop
        if full_stop or too_long or i + 1 == len(raw):
            prev_end = sentences[-1]["end"] if sentences else 0.0
            start = max(prev_end - 0.05, cur[0][1] - 0.15, 0.0)
            text = " ".join(w for w, _, _ in cur)
            sentences.append({"text": text[0].upper() + text[1:], "para": not sentences or start - prev_end > 2.0,
                              "start": round(start, 2), "end": round(cur[-1][2] + 0.10, 2),
                              "words": [[t, round(x, 2), round(y, 2)] for w, x, y in cur for t in tokens(w)]})
            cur = []
    return sentences


def align(sentences, heard, duration):
    """Give every transcript sentence a start and an end, in seconds. Returns the share of words found."""
    heard = [(t, a, b) for word, a, b in heard for t in tokens(word)]
    script = [(t, i) for i, s in enumerate(sentences) for t in tokens(s["text"])]
    n = len(script)
    start, end = [None] * n, [None] * n
    sure = [False] * n                                            # True when Whisper heard exactly this word
    matcher = difflib.SequenceMatcher(None, [t for t, _ in script], [h[0] for h in heard], autojunk=False)
    matched = 0
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            start[a + k], end[a + k] = heard[b + k][1], heard[b + k][2]
            sure[a + k] = True
            matched += 1

    # words Whisper heard differently: place them between their matched neighbours
    if matched == 0:
        return 0.0
    PACE = 0.3                                                    # rough seconds per spoken word
    i = 0
    while i < n:
        if start[i] is not None:
            i += 1
            continue
        j = i
        while j + 1 < n and start[j + 1] is None:
            j += 1                                                # i..j is one run of unmatched words
        lo, hi = i - 1, j + 1
        t0 = end[lo] if lo >= 0 else 0.0
        t1 = start[hi] if hi < n else duration
        step = (t1 - t0) / (j - i + 2)
        for k in range(i, j + 1):
            if step <= 2 * PACE:                                  # no pause in between: spread evenly
                start[k] = t0 + step * (k - lo)
            elif hi < n and script[k][1] == script[hi][1]:        # a pause, and the word belongs to what follows
                start[k] = max(t0, t1 - PACE * (hi - k))
            elif lo >= 0 and script[k][1] == script[lo][1]:       # a pause, and the word belongs to what came before
                start[k] = min(t1, t0 + PACE * (k - lo - 1))
            else:
                start[k] = t0 + step * (k - lo)
            end[k] = start[k] + PACE * 0.8
        i = j + 1

    first, last = {}, {}
    for idx, (_, si) in enumerate(script):
        first.setdefault(si, idx)
        last[si] = idx
    prev_end = 0.0
    for i, s in enumerate(sentences):
        if i not in first:                                        # a sentence with no words (rare)
            s["start"], s["end"] = round(prev_end, 2), round(prev_end + 0.5, 2)
            continue
        a = max(prev_end - 0.05, start[first[i]] - 0.15, 0.0)
        b = max(a + 0.3, end[last[i]] + 0.10)
        s["start"], s["end"] = round(a, 2), round(min(b, duration), 2)
        # the time of every word, kept so that single words can be clipped from the audio later
        # a word whose time was only estimated carries a trailing 0, so it is never used as a clip
        s["words"] = [[script[k][0], round(start[k], 2), round(end[k], 2)] + ([] if sure[k] else [0])
                      for k in range(first[i], last[i] + 1)]
        prev_end = s["end"]
    return matched / max(1, n)


def word_clip(sentence, form):
    """Where a word (or expression) is spoken inside its sentence: (start, end) in seconds, or None.

    None means: no word timings, the form is not found, or one of its words was not heard clearly."""
    words, want = sentence.get("words") or [], tokens(form)
    if not words or not want:
        return None
    heard = [w[0] for w in words]
    for i in range(len(heard) - len(want) + 1):
        if heard[i:i + len(want)] == want:
            part = words[i:i + len(want)]
            if any(len(w) > 3 for w in part):                     # an estimated time: not safe to cut
                return None
            start, end = max(0.0, part[0][1] - 0.08), part[-1][2] + 0.12
            if not 0.15 <= end - start <= 6:
                return None
            return round(start, 2), round(end, 2)
    return None


WORD_IN_TEXT = re.compile(r"[0-9a-zà-ÿœ'’]+")
CLAUSE_END = re.compile(r"[,;:()«»—–]")


def word_excerpt(sentence, form, around=5, short=12):
    """A few words of the ORIGINAL sentence around a word: {"start", "end", "text"}, or None.

    Podcast sentences are long; this gives the learner the two or three seconds where the word lives,
    in the speaker's own voice. None when the sentence is short enough to be heard whole, when the word
    is not found, or when there are no word timings."""
    words, text, want = sentence.get("words") or [], sentence.get("text", ""), tokens(form)
    spans = [m.span() for m in WORD_IN_TEXT.finditer(text.lower()) if m.group().strip("'’")]
    n = len(words)
    if not want or n <= short or len(spans) != n:
        return None
    heard = [w[0] for w in words]
    bare = [h.split("'")[-1] for h in heard]                      # "d'entreprendre" also answers to "entreprendre"
    i = next((k for k in range(n - len(want) + 1)
              if heard[k:k + len(want)] == want or [bare[k]] + heard[k + 1:k + len(want)] == want), None)
    if i is None:
        return None
    j = i + len(want) - 1
    lo, hi = max(0, i - around), min(n - 1, j + around)
    breaks = [k for k in range(1, n) if CLAUSE_END.search(text[spans[k - 1][1]:spans[k][0]])]   # a clause starts at k
    left = [k for k in breaks if lo <= k <= i]
    right = [k - 1 for k in breaks if j <= k - 1 <= hi]
    if left:
        lo = left[-1]
    if right:
        hi = right[0]
    if hi - lo + 1 < 4:                                           # a clause of two words says too little: take the plain window
        lo, hi = max(0, i - 3), min(n - 1, j + 3)
    if lo == 0 and hi == n - 1:
        return None
    while lo > 0 and len(words[lo]) > 3:                          # never cut on a word whose time was only estimated
        lo -= 1
    while hi < n - 1 and len(words[hi]) > 3:
        hi += 1
    start = sentence.get("start", words[0][1]) if lo == 0 else max(0.0, words[lo][1] - 0.10)
    end = sentence.get("end", words[-1][2]) if hi == n - 1 else words[hi][2] + 0.15
    if not 0.4 <= end - start <= 12:
        return None
    return {"start": round(start, 2), "end": round(end, 2),
            "text": ("… " if lo else "") + text[spans[lo][0]:spans[hi][1]] + (" …" if hi < n - 1 else "")}


def listen(audio, size, folder, again=False):
    """transcribe(), but the result is kept in heard.json so the audio is only listened to once."""
    cache = os.path.join(folder, "heard.json")
    stamp = [os.path.basename(audio), os.path.getsize(audio), size]
    if not again and os.path.isfile(cache):
        try:
            with open(cache, encoding="utf-8") as f:
                kept = json.load(f)
            if kept.get("stamp") == stamp:
                print("Écoute déjà faite pour cet audio : je reprends heard.json (--listen-again pour recommencer)")
                return [tuple(w) for w in kept["words"]], kept["duration"]
        except (ValueError, KeyError, OSError):
            pass
    raw, duration = transcribe(audio, size)
    try:
        with open(cache, "w", encoding="utf-8") as f:
            json.dump({"stamp": stamp, "duration": duration, "words": raw}, f, ensure_ascii=False)
    except OSError:
        pass
    return raw, duration


# ----------------------------------------------------------------------------- vocabulary

PROMPT = """You are preparing vocabulary for a learner of French at level {level} who studies one podcast episode at a time.
Below are sentences from the episode, each with its number.

{goal}
Skip proper nouns, numbers, and the most basic words (être, avoir, je, et…).
{known}
Answer with a JSON array only, no text before or after. Each item:
  "word": the dictionary form, with its article for nouns (e.g. "une habitude", "deviner", "avoir la flemme")
  "form": the exact letters as they appear in the sentence (e.g. "habitudes"), copied character for character
  "sid":  the number of the sentence it comes from
  "pos":  the kind of word, exactly one of: "verbe", "nom", "adjectif", "adverbe", "expression", "autre"
  "def":  a definition in very simple French, 12 words at most, without using the word itself
  "img":  in English, 25 words at most: one concrete scene that shows what the sentence means around this word.
          Describe people, objects and action only. No text, letters or signs in the scene.
          Any person in the scene is a woman with very short hair, in comfortable casual clothes (never a man,
          never a dress or a skirt, no makeup).

Sentences:
{sentences}
"""
GOAL_BEST = ("Pick the {n} words or short expressions most worth learning at this level: "
             "frequent, reusable, and likely unknown to the learner.")
GOAL_ALL = ("Pick every word or short expression that a learner at this level probably does not know yet "
            "and that is worth learning ({n} at most for this part), in the order they appear.")
KINDS = ("verbe", "nom", "adjectif", "adverbe", "expression", "autre")
PART = 50                                                         # sentences sent to Claude in one request
PER_PART = 45                                                     # words asked for one part when there is no limit


def norm(word):
    """A word as it is compared with the learner's history: lowercase, without its article."""
    w = " ".join(str(word).lower().replace("\u2019", "'").split())
    return re.sub(r"^(?:(?:un|une|des|le|la|les|du|de la|se)\s+|de l'|l'|d'|s')", "", w).strip() or w


def ask_claude(client, model, prompt):
    reply = client.messages.create(model=model, max_tokens=12000, messages=[{"role": "user", "content": prompt}])
    text = "".join(block.text for block in reply.content if getattr(block, "type", "") == "text")
    return json.loads(text[text.index("["): text.rindex("]") + 1])


def pick_words(sentences, level, max_words, model, known=()):
    """Ask Claude for the words to learn. max_words 0 means: every word worth learning.

    known: words the learner already marked as known in earlier episodes; they are never picked again."""
    import anthropic
    client = anthropic.Anthropic()                                # uses ANTHROPIC_API_KEY, or your `ant auth login`
    history = {norm(w) for w in known}
    parts = [range(i, min(i + PART, len(sentences))) for i in range(0, len(sentences), PART)]
    wanted = f"{max_words} mots" if max_words else "tous les mots utiles"
    print(f"Claude choisit {wanted} pour le niveau {level}"
          + (f" ({len(history)} mots déjà connus écartés)" if history else "") + "…")

    vocab, seen, failed = [], set(), 0
    for k, part in enumerate(parts):
        if max_words and len(vocab) >= max_words:
            break
        if max_words:
            n = min(max_words - len(vocab), -(-max_words * len(part) // len(sentences)) + 2)
            goal = GOAL_BEST.format(n=n)
        else:
            goal = GOAL_ALL.format(n=PER_PART)
        skip = sorted(history | seen)[:600]
        known_line = ("Never pick any of these, the learner already has them: " + ", ".join(skip) + "\n") if skip else ""
        prompt = PROMPT.format(level=level, goal=goal, known=known_line,
                               sentences="\n".join(f"{i}. {sentences[i]['text']}" for i in part))
        items = None
        for attempt in range(2):                                  # an answer that is not JSON is asked for once more
            try:
                items = ask_claude(client, model, prompt)
                break
            except ValueError:
                continue
        if items is None:
            failed += 1
            print(f"  partie {k + 1}/{len(parts)} : réponse illisible, partie ignorée")
            continue
        before = len(vocab)
        for it in items:
            if not isinstance(it, dict):
                continue
            word, form, definition = str(it.get("word", "")).strip(), str(it.get("form", "")).strip(), str(it.get("def", "")).strip()
            scene = str(it.get("img", "")).strip()
            if not word or not form or norm(word) in seen or norm(word) in history:
                continue
            sid = it.get("sid")
            ok = isinstance(sid, int) and 0 <= sid < len(sentences) and form.lower() in sentences[sid]["text"].lower()
            if not ok:                                            # wrong sentence number: look for the form ourselves
                sid = next((i for i in list(part) + list(range(len(sentences)))
                            if form.lower() in sentences[i]["text"].lower()), None)
            if sid is None:
                continue
            seen.add(norm(word))
            entry = {"word": word, "form": form, "def": definition, "sid": sid, "img": scene}
            kind = str(it.get("pos", "")).strip().lower()
            if kind in KINDS:
                entry["pos"] = kind
            vocab.append(entry)
            if max_words and len(vocab) >= max_words:             # Claude sometimes gives one or two extra
                break
        if len(parts) > 1:
            print(f"  partie {k + 1}/{len(parts)} : {len(vocab) - before} mots")
    if failed == len(parts):
        raise SystemExit("Claude n'a pas répondu avec une liste JSON. Relance le script.")
    vocab.sort(key=lambda v: v["sid"])
    print(f"  {len(vocab)} mots gardés")
    return vocab


TAG_PROMPT = """For each French word or expression below, give its kind.
Answer with a JSON array only, one item per line of the list, in the same order.
Each item is exactly one of: "verbe", "nom", "adjectif", "adverbe", "expression", "autre".

{words}
"""


def tag_words(vocab, model):
    """Ask Claude for the kind (verb, noun…) of the words that do not have one yet. One short request."""
    todo = [v for v in vocab if v.get("pos") not in KINDS]
    if not todo:
        print("Tous les mots ont déjà leur catégorie.")
        return 0
    import anthropic
    client = anthropic.Anthropic()
    print(f"Claude classe {len(todo)} mots (verbe, nom, adjectif…)…")
    done = 0
    for i in range(0, len(todo), 150):
        group = todo[i:i + 150]
        try:
            kinds = ask_claude(client, model, TAG_PROMPT.format(words="\n".join(f"{k + 1}. {v['word']}" for k, v in enumerate(group))))
        except ValueError:
            kinds = []
        if len(kinds) != len(group):                              # an answer of the wrong length cannot be trusted
            print("  réponse inutilisable pour une partie des mots : ils restent sans catégorie")
            continue
        for v, kind in zip(group, kinds):
            if str(kind).strip().lower() in KINDS:
                v["pos"] = str(kind).strip().lower()
                done += 1
    print(f"  {done} mots classés")
    return done


def known_words(root, episode_id):
    """Words marked as known in OTHER episodes: from PostgreSQL when ATELIER_DB is set, else from data/store.json."""
    if os.environ.get("ATELIER_DB"):
        try:
            import db
            with db.connect() as conn:
                return db.known_words(conn, episode_id)
        except Exception as exc:                                  # a stopped database must not stop the import
            print(f"  (base de données inaccessible pour l'historique des mots : {str(exc).strip().splitlines()[0]})")
    try:
        with open(os.path.join(root, "data", "store.json"), encoding="utf-8") as f:
            store = json.load(f)
    except (OSError, ValueError):
        return []
    words = []
    for key, progress in store.items():
        eid = key[len("atelier:ep:"):] if key.startswith("atelier:ep:") else None
        if not eid or eid == episode_id or not isinstance(progress, dict) or not progress.get("kn"):
            continue
        try:
            with open(os.path.join(root, "episodes", eid, "episode.json"), encoding="utf-8") as f:
                vocab = json.load(f).get("vocab", [])
        except (OSError, ValueError):
            continue
        marked = set(map(str, progress["kn"]))
        words += [v["word"] for v in vocab if f"{v.get('form')}|{v.get('sid')}" in marked]
    return sorted(set(words))


# ----------------------------------------------------------------------------- main

def find_inputs(source, transcript):
    """Accept a folder (one audio + maybe one .txt inside) or an audio file. Returns (audio, transcript or None, folder)."""
    if os.path.isdir(source):
        names = sorted(os.listdir(source))
        audios = [n for n in names if n.lower().endswith(AUDIO_TYPES)]
        if not audios:
            raise SystemExit(f"Aucun fichier audio dans {source} (formats acceptés : {', '.join(AUDIO_TYPES)}).")
        if len(audios) > 1:
            print(f"Plusieurs fichiers audio dans ce dossier, je prends : {audios[0]}")
        texts = [n for n in names if n.lower().endswith(".txt")]
        if transcript is None and texts:
            transcript = os.path.join(source, "transcript.txt" if "transcript.txt" in texts else texts[0])
        return os.path.join(source, audios[0]), transcript, source
    if not os.path.isfile(source):
        raise SystemExit(f"Introuvable : {source}")
    return source, transcript, os.path.dirname(os.path.abspath(source))


def main():
    ap = argparse.ArgumentParser(description="Prépare un épisode pour l'app Atelier d'épisode.")
    ap.add_argument("source", help="un dossier d'épisode, ou un fichier audio")
    ap.add_argument("transcript", nargs="?", default=None, help="facultatif : le fichier .txt de la transcription")
    ap.add_argument("--title", default=None)
    ap.add_argument("--level", default="A1")
    ap.add_argument("--max-words", type=int, default=0,
                    help="how many words at most; 0 (default) = every word worth learning at your level")
    ap.add_argument("--repeat-known", action="store_true",
                    help="also pick words you already marked as known in earlier episodes")
    ap.add_argument("--whisper", default="small")
    ap.add_argument("--max-chars", type=int, default=220,
                    help="cut sentences longer than this at a comma or linking word (0 = never cut)")
    ap.add_argument("--claude-model", default="claude-sonnet-5-5")
    ap.add_argument("--no-vocab", action="store_true")
    ap.add_argument("--words-only", action="store_true",
                    help="keep the sentences and timings already in episode.json; only ask Claude for the words again")
    ap.add_argument("--tag-only", action="store_true",
                    help="keep the words as they are; only ask Claude for the kind of each word (verb, noun…)")
    ap.add_argument("--listen-again", action="store_true", help="ignore heard.json and listen to the audio again")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    audio, transcript, folder = find_inputs(args.source, args.transcript)
    here = os.path.dirname(os.path.abspath(__file__))
    known = [] if args.repeat_known or args.no_vocab else known_words(here, os.path.basename(os.path.abspath(folder)))
    out = args.out or os.path.join(folder, "episode.json")
    name = os.path.basename(os.path.abspath(args.source)) if os.path.isdir(args.source) else os.path.splitext(os.path.basename(audio))[0]
    title = args.title or re.sub(r"[-_]+", " ", name).strip().capitalize()
    print(f"Épisode : {title}")
    print(f"Audio : {audio}")
    print(f"Transcription : {transcript or 'aucune, le texte viendra de Whisper'}")

    if args.tag_only:                                             # nothing changes but the "pos" of each word
        if not os.path.isfile(out):
            raise SystemExit(f"--tag-only a besoin d'un fichier existant : {out}")
        with open(out, encoding="utf-8") as f:
            episode = json.load(f)
        if tag_words(episode.get("vocab", []), args.claude_model):
            with open(out, "w", encoding="utf-8") as f:
                json.dump(episode, f, ensure_ascii=False, indent=1)
            print(f"Écrit : {out}")
        return

    if args.words_only:                                           # no listening: reuse what the last run produced
        if not os.path.isfile(out):
            raise SystemExit(f"--words-only a besoin d'un fichier existant : {out}")
        with open(out, encoding="utf-8") as f:
            episode = json.load(f)
        episode["title"] = args.title or episode.get("title") or title
        episode["level"] = args.level
        episode["vocab"] = pick_words(episode["sentences"], args.level, args.max_words, args.claude_model, known)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(episode, f, ensure_ascii=False, indent=1)
        print(f"Écrit : {out}  ({len(episode['sentences'])} phrases, {len(episode['vocab'])} mots)")
        return

    raw, duration = listen(audio, args.whisper, folder, args.listen_again)
    if not raw:
        raise SystemExit("Whisper n'a entendu aucune parole dans ce fichier.")

    sentences, source_text = None, "whisper"
    if transcript:
        with open(transcript, encoding="utf-8") as f:
            sentences = shorten(split_transcript(f.read()), args.max_chars)
        share = align(sentences, raw, duration) if sentences else 0.0
        n_text, n_audio = sum(len(tokens(x["text"])) for x in sentences), sum(len(tokens(w)) for w, _, _ in raw)
        print(f"  {len(sentences)} phrases dans la transcription, {share:.0%} de ses mots retrouvés dans l'audio")
        same_length = 0.6 <= n_text / max(1, n_audio) <= 1.6      # a transcript covers about as many words as the audio
        if share < 0.55 or not same_length:
            print("  Cette transcription ne correspond pas à cet audio : je prends le texte entendu par Whisper.")
            sentences = None
        else:
            source_text = "transcript"
            if share < 0.75:
                print("  Attention : moins de 75 %. Certains temps seront approximatifs. Essaie --whisper medium.")
    if sentences is None:
        sentences = sentences_from_audio(raw)
        print(f"  {len(sentences)} phrases construites à partir de l'audio (texte de Whisper, il peut contenir des erreurs)")

    vocab = [] if args.no_vocab else pick_words(sentences, args.level, args.max_words, args.claude_model, known)
    if not vocab and os.path.isfile(out):                         # listening again must not throw the chosen words away
        try:
            with open(out, encoding="utf-8") as f:
                old = json.load(f)
            texts = [x["text"] for x in sentences]
            for v in old.get("vocab", []):
                where = old["sentences"][v["sid"]]["text"]
                home = texts.index(where) if where in texts else next(
                    (i for i, t in enumerate(texts) if t in where and v["form"].lower() in t.lower()), None)
                if home is not None:
                    vocab.append({**v, "sid": home})
            if vocab:
                print(f"  {len(vocab)} mots repris de l'ancien episode.json")
        except (ValueError, KeyError, IndexError, OSError):
            vocab = []

    episode = {
        "version": 1,
        "title": title,
        "level": args.level,
        "audio": os.path.basename(audio),
        "duration": round(duration, 2),
        "text_source": source_text,
        "sentences": sentences,
        "vocab": vocab,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(episode, f, ensure_ascii=False, indent=1)
    print(f"Écrit : {out}  ({len(sentences)} phrases, {len(vocab)} mots)")


if __name__ == "__main__":
    main()

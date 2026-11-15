"""
server.py — the Atelier d'épisode server. It runs on your own computer; nothing goes to the cloud.

What it does
  - serves the app (app/index.html) so a phone on the same Wi-Fi can open it
  - lists the episodes found in episodes/ and sends their sentences, words, audio and images
  - keeps your progress (stars, review boxes, settings) in data/store.json
  - accepts a new episode sent from the app's "Ajouter" tab and runs import_episode.py on it in the background

Start it
  python server.py
then open the address it prints, on this computer or on your phone.

Where things are kept
  - without a database (the default): episodes are read from the folders in episodes/, progress is data/store.json
  - with ATELIER_DB set in .env: episodes and progress are read from and saved to PostgreSQL (see db.py).
    Audio, images and voice files always stay files on disk.
"""
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(os.environ.get("ATELIER_ROOT") or Path(__file__).resolve().parent)
EPISODES = ROOT / "episodes"
APP = ROOT / "app"
DATA = ROOT / "data"
IMPORT_SCRIPT = os.environ.get("ATELIER_IMPORT_SCRIPT") or str(ROOT / "import_episode.py")

AUDIO_TYPES = (".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus", ".flac", ".mp4", ".webm")
IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".webp")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
SAFE_IMAGE = re.compile(r"^\d{1,4}\.(png|jpg|jpeg|webp)$")
SAFE_VOICE = re.compile(r"^\d{1,4}-[wg]\.(m4a|wav|mp3)$")
VOICE_TYPES = (".m4a", ".wav", ".mp3")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from import_liens import title_key                                      # noqa: E402
from import_episode import norm, word_clip, word_excerpt                # noqa: E402  (light: Whisper and Claude are only loaded when used)

DB = None                                            # the db module when PostgreSQL is in use, otherwise None


def use_database():
    """Called once at start. Turns the database on if ATELIER_DB is set and PostgreSQL answers."""
    global DB
    DB = None
    if not os.environ.get("ATELIER_DB"):
        return "fichiers (pas de base de données configurée)"
    try:
        import db
        with db.connect() as conn:
            db.init(conn)
            loaded = db.sync_all(conn, ROOT)
            db.import_store_file(conn, ROOT)
        DB = db
        return f"PostgreSQL ({len(loaded)} épisodes chargés)"
    except Exception as exc:                         # a stopped database must not stop the app
        return f"fichiers (PostgreSQL ne répond pas : {str(exc).strip().splitlines()[0] if str(exc).strip() else exc})"

app = FastAPI(title="Atelier d'épisode")
store_lock = threading.Lock()
JOBS = {}                                    # episode id -> {"state", "started", "log", "title"}


# ----------------------------------------------------------------------------- helpers

def episode_folder(eid):
    """The folder of one episode. Refuses anything that is not a plain folder name inside episodes/."""
    if not SAFE_ID.match(eid) or ".." in eid:
        raise HTTPException(404, "Épisode inconnu")
    folder = EPISODES / eid
    if not folder.is_dir():
        raise HTTPException(404, "Épisode inconnu")
    return folder


def find_audio(folder):
    for name in sorted(os.listdir(folder)):
        if name.lower().endswith(AUDIO_TYPES):
            return folder / name
    return None


def image_sources(folder):
    """What generate_images.py wrote about its pictures: {word number: entry}. Empty if it never ran."""
    try:
        with open(folder / "images" / "sources.json", encoding="utf-8") as f:
            return {e.get("n"): e for e in json.load(f) if isinstance(e, dict)}
    except (OSError, ValueError):
        return {}


def find_image(folder, index, word, sources):
    """The picture of a word, or None. Only a picture registered for exactly this word is shown: after the
    word list changes, the pictures of the old list still lie in images/ under the same numbers."""
    entry = sources.get(index) or {}
    name = str(entry.get("file") or "")
    if entry.get("word") != word or not SAFE_IMAGE.match(name) or not name.startswith(f"{index}."):
        return None
    path = folder / "images" / name
    return path if path.is_file() else None


def voice_sources(folder):
    """What generate_voice.py wrote about its files: {word number: entry}. Empty if it never ran."""
    try:
        with open(folder / "voice" / "sources.json", encoding="utf-8") as f:
            return {e.get("n"): e for e in json.load(f) if isinstance(e, dict)}
    except (OSError, ValueError):
        return {}


def find_voice(folder, index, kind, text, sources):
    """The voice file of a word (kind "w") or of its generated example ("g"), or None.

    A file is only trusted if sources.json says it reads exactly this text: after the word list changes,
    the files of the old list still lie in voice/ under the same numbers and must not be played."""
    entry = (sources.get(index) or {}).get("word_audio" if kind == "w" else "example_audio") or {}
    name = str(entry.get("file") or "")
    if entry.get("text") != text or not SAFE_VOICE.match(name) or not name.startswith(f"{index}-{kind}."):
        return None
    path = folder / "voice" / name
    return path if path.is_file() else None


def read_episode(folder):
    path = folder / "episode.json"
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


def job_status(eid):
    """Where a background import stands: state, percentage of listening done, and its last lines of output."""
    job = JOBS.get(eid)
    if not job:
        return None
    text = ""
    try:
        with open(job["log"], encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        pass
    percents = re.findall(r"écoute :\s*(\d+)%", text)
    lines = [l.strip() for l in re.split(r"[\r\n]+", text) if l.strip() and "écoute :" not in l]
    step = "Préparation"
    if percents:
        step = "Écoute de l'audio"
    if "mots entendus" in text:
        step = "Alignement du texte"
    if "Claude choisit" in text:
        step = "Choix des mots"
    if job["state"] == "done":
        step = "Terminé"
    if job["state"] == "error":
        step = "Erreur"
    return {"id": eid, "title": job["title"], "state": job["state"], "step": step,
            "percent": int(percents[-1]) if percents else 0, "seconds": int(time.time() - job["started"]),
            "last_lines": lines[-4:]}


def slugify(title):
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50] or "episode"
    candidate, n = slug, 2
    while (EPISODES / candidate).exists():
        candidate, n = f"{slug}-{n}", n + 1
    return candidate


def load_store():
    try:
        with open(DATA / "store.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_store(store):
    DATA.mkdir(exist_ok=True)
    tmp = DATA / "store.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False)
    os.replace(tmp, DATA / "store.json")              # never leaves a half-written file behind


# ----------------------------------------------------------------------------- episodes

@app.get("/api/episodes")
def list_episodes():
    """Every folder in episodes/: ready ones with their numbers, and ones still being prepared."""
    out = []
    in_db = {}
    if DB:
        with DB.connect() as conn:
            in_db = {e["id"]: e for e in DB.list_episodes(conn)}
    if EPISODES.is_dir():
        for name in sorted(os.listdir(EPISODES)):
            folder = EPISODES / name
            if not folder.is_dir() or not SAFE_ID.match(name):
                continue
            job = job_status(name)
            if DB:
                row = in_db.get(name)
                data = row and {"title": row["title"], "sentences": [0] * row["sentences"],
                                "vocab": [0] * row["words"], "duration": row["duration"]}
            else:
                data = read_episode(folder)
            if job and job["state"] == "running":
                data = None                           # being prepared again: not ready until it is finished
            if data is None and not job:
                continue                              # a folder with nothing imported yet
            out.append({
                "id": name,
                "title": (data or {}).get("title") or (job or {}).get("title") or name,
                "ready": data is not None,
                "sentences": len((data or {}).get("sentences", [])),
                "words": len((data or {}).get("vocab", [])),
                "duration": (data or {}).get("duration", 0),
                "has_audio": find_audio(folder) is not None,
                "job": job if job and job["state"] != "done" else None,
            })
    return out


@app.get("/api/episodes/{eid}")
def get_episode(eid: str):
    """One episode as the app needs it: sentences, words, and the addresses of its audio and images."""
    folder = episode_folder(eid)
    if DB:
        with DB.connect() as conn:
            data = DB.get_episode(conn, eid)
        if data is None:
            raise HTTPException(404, "Cet épisode n'est pas encore prêt")
        sentences, words = data["sentences"], data["vocab"]
        for item in words:
            item.pop("lex", None)                     # the dictionary id stays in the database
    else:
        data = read_episode(folder)
        if data is None:
            raise HTTPException(404, "Cet épisode n'est pas encore prêt")
        sentences = [{"text": s["text"], "start": s["start"], "end": s["end"], "para": bool(s.get("para"))}
                     for s in data.get("sentences", [])]
        words = []
        for i, v in enumerate(data.get("vocab", [])):
            if not 0 <= v.get("sid", -1) < len(sentences):
                continue
            item = {k: v[k] for k in ("word", "form", "def", "sid", "pos") if v.get(k) not in (None, "")}
            item["idx"] = i
            clip = word_clip(data["sentences"][v["sid"]], v["form"])
            if clip:
                item["clip"] = list(clip)
            ctx = word_excerpt(data["sentences"][v["sid"]], v["form"])
            if ctx:
                item["ctx"] = [ctx["start"], ctx["end"], ctx["text"]]
            words.append(item)
    vocab, spoken, drawn = [], voice_sources(folder), image_sources(folder)
    for item in words:
        i = item.pop("idx")
        image = find_image(folder, i, item["word"], drawn)
        if image:
            item["image"] = f"/media/{eid}/images/{image.name}?v={int(image.stat().st_mtime)}"
        # the rule: "Mot" is the original audio when the episode says the dictionary form itself; when it says
        # another form ("souhaite" for "souhaiter") and a voice file exists, the voice reads the dictionary form
        voice = find_voice(folder, i, "w", item["word"], spoken)
        if voice and "clip" in item and norm(item["form"]) != norm(item["word"]):
            del item["clip"]
        if voice and "clip" not in item:
            item["voice_word"] = f"/media/{eid}/voice/{voice.name}?v={int(voice.stat().st_mtime)}"
        vocab.append(item)
    audio = find_audio(folder)
    return {"id": eid, "title": data.get("title") or eid, "level": data.get("level"),
            "duration": data.get("duration", 0), "sentences": sentences, "vocab": vocab,
            "audio_url": f"/media/{eid}/audio" if audio else None}


@app.get("/media/{eid}/audio")
def get_audio(eid: str):
    audio = find_audio(episode_folder(eid))
    if not audio:
        raise HTTPException(404, "Pas d'audio pour cet épisode")
    return FileResponse(audio, media_type=mimetypes.guess_type(audio.name)[0] or "audio/mpeg")


@app.get("/media/{eid}/images/{name}")
def get_image(eid: str, name: str):
    if not SAFE_IMAGE.match(name):
        raise HTTPException(404, "Image inconnue")
    path = episode_folder(eid) / "images" / name
    if not path.is_file():
        raise HTTPException(404, "Image inconnue")
    return FileResponse(path)


@app.get("/media/{eid}/voice/{name}")
def get_voice(eid: str, name: str):
    if not SAFE_VOICE.match(name):
        raise HTTPException(404, "Voix inconnue")
    path = episode_folder(eid) / "voice" / name
    if not path.is_file():
        raise HTTPException(404, "Voix inconnue")
    return FileResponse(path)


# ----------------------------------------------------------------------------- adding an episode from the app

def run_import(eid, folder, title, level):
    """Runs import_episode.py on the folder, writing everything it prints to a log the app can follow."""
    job = JOBS[eid]
    try:
        with open(job["log"], "w", encoding="utf-8") as log:
            code = subprocess.call([sys.executable, "-u", IMPORT_SCRIPT, str(folder), "--title", title, "--level", level],
                                   stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT))
        ok = code == 0 and (folder / "episode.json").is_file()
        if ok and DB:
            with DB.connect() as conn:
                DB.sync_folder(conn, folder)
                conn.commit()
        job["state"] = "done" if ok else "error"
    except Exception as exc:                          # the import must never take the server down with it
        job["state"] = "error"
        with open(job["log"], "a", encoding="utf-8") as log:
            log.write(f"\n{exc}\n")


@app.post("/api/episodes")
async def add_episode(title: str = Form(...), level: str = Form("A1"), audio: UploadFile = File(...),
                      transcript: UploadFile = File(None), transcript_text: str = Form("")):
    """Receives an audio file and, if there is one, a transcript; then prepares the episode in the background."""
    if any(j["state"] == "running" for j in JOBS.values()):
        raise HTTPException(409, "Un épisode est déjà en préparation. Attends qu'il soit prêt.")
    ext = os.path.splitext(audio.filename or "")[1].lower()
    if ext not in AUDIO_TYPES:
        raise HTTPException(400, "Ce fichier n'est pas un audio reconnu (mp3, m4a, wav…).")
    title = title.strip()[:120] or "Mon épisode"
    level = level if re.fullmatch(r"[ABC][12]", level) else "A1"
    EPISODES.mkdir(exist_ok=True)
    eid = slugify(title)
    folder = EPISODES / eid
    folder.mkdir()
    with open(folder / f"audio{ext}", "wb") as f:
        while chunk := await audio.read(1 << 20):
            f.write(chunk)
    text = transcript_text.strip()
    if transcript is not None and transcript.filename:
        text = (await transcript.read()).decode("utf-8", errors="replace").strip() or text
    if text:
        with open(folder / "transcript.txt", "w", encoding="utf-8") as f:
            f.write(text + "\n")
    JOBS[eid] = {"state": "running", "started": time.time(), "log": str(folder / "import.log"), "title": title}
    threading.Thread(target=run_import, args=(eid, folder, title, level), daemon=True).start()
    return job_status(eid)


@app.get("/api/jobs/{eid}")
def get_job(eid: str):
    status = job_status(eid)
    if not status:
        raise HTTPException(404, "Aucune préparation en cours pour cet épisode")
    return status


# ----------------------------------------------------------------------------- progress and settings

@app.get("/api/store")
def get_store():
    """Everything the app remembers: progress per episode, activity days, settings."""
    if DB:
        with DB.connect() as conn:
            return DB.store_all(conn)
    with store_lock:
        return load_store()


@app.put("/api/store/{key}")
def put_store(key: str, value=Body(...)):
    if not key or len(key) > 200:
        raise HTTPException(400, "Clé invalide")
    if DB:
        with store_lock, DB.connect() as conn:
            DB.store_put(conn, key, value)
        return {"ok": True}
    with store_lock:
        store = load_store()
        store[key] = value
        save_store(store)
    return {"ok": True}


# ----------------------------------------------------------------------------- the course followed
# Everything in the folder cours/ beside this file: one folder per course, and inside it either one folder per
# lesson, or the files of all lessons side by side (then files that start with the same number belong together).
COURS = ROOT / "cours"
KINDS = {"video": {".mp4", ".m4v", ".mov", ".mkv", ".webm"}, "pdf": {".pdf"}, "audio": {".mp3", ".m4a", ".wav", ".ogg"},
         "image": {".png", ".jpg", ".jpeg", ".webp"}, "doc": {".docx", ".pptx", ".txt", ".md", ".xlsx"}}
CORRECTION = re.compile(r"corr(ig|ect)", re.IGNORECASE)
DEVOIR = re.compile(r"devoir|exerc|homework|corr(ig|ect)|production\s*[ée]crite", re.IGNORECASE)
RESSOURCE = re.compile(r"ress?ource|bonus|annexe", re.IGNORECASE)


def natural(name):
    """Sort key that puts "2" before "10"."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", str(name))]


def course_file(path):
    """What the app needs to know about one file of a lesson, or None for a file it cannot use."""
    kind = next((k for k, exts in KINDS.items() if path.suffix.lower() in exts), None)
    if not kind or path.name.startswith("."):
        return None
    rel = path.relative_to(COURS).as_posix()
    where = nfc(path.parent.name + "/" + path.stem)               # a file's name, or the folder it sits in, says what it is
    return {"nom": nfc(path.name), "type": kind, "correction": bool(CORRECTION.search(nfc(path.stem))),
            "partie": "devoir" if DEVOIR.search(where) else "ressource" if RESSOURCE.search(where) else "lecon",
            "taille": path.stat().st_size, "url": "/cours-media/" + "/".join(quote(part) for part in rel.split("/"))}


def clean_title(name):
    name = nfc(name)
    return re.sub(r"\s+", " ", re.sub(r"[_]+", " ", Path(name).stem if "." in name else name)).strip()


def folders_of(folder):
    """Sub-folders in natural order; extras (Sources, Ressources, Bonus) come after the units."""
    return sorted((d for d in folder.iterdir() if d.is_dir() and not d.name.startswith(".")),
                  key=lambda d: (bool(re.search(r"^sources?$|ress?ource|bonus|annexe", nfc(d.name), re.IGNORECASE)), natural(d.name)))


def place(trail):
    """Where a lesson sits: its unit, then the chapter(s) under it."""
    return {"groupe": trail[0] if trail else "", "chapitre": " · ".join(trail[1:])}


CHAPTER = re.compile(r"(?:chap(?:itre|\.)?|lesson|le[çc]on)\s*(?:\d+\.)?(\d+)|unit[ée]\s*\d+\.(\d+)", re.IGNORECASE)
NUMBER = re.compile(r"\s*(\d+)\s*[-–._]")                          # "02 - ..." or "02_...": the place of a file in its folder


def number_shared(folder, number):
    """True when another file of the folder starts with the same number (then the number, not the order, says where it goes)."""
    if not number:
        return False
    return sum(1 for y in folder.iterdir() if y.is_file() and (NUMBER.match(y.name) or [None, None])[1] == number.group(1)) > 1


def nfc(text):
    """macOS writes é as e + accent; put it back in one piece so that names can be read."""
    return unicodedata.normalize("NFC", str(text))


def chapter_of(name):
    found = CHAPTER.search(nfc(name))
    return (found.group(1) or found.group(2)) if found else None


def lesson_title(name):
    """"02 - French for Beginners _ Unité 10, Chap. 1 _ Les Études.webm" -> "Chapitre 1 · Les Études"."""
    text = NUMBER.sub("", nfc(Path(name).stem), count=1) if NUMBER.match(nfc(name)) else nfc(Path(name).stem)
    unit = re.search(r"unit[ée]\s*\d+(?:\.\d+)?[\s,_\-–]*", text, re.IGNORECASE)
    if unit and text[unit.end():].strip():
        text = text[unit.end():]
    chapter = re.search(r"(chap(?:itre|\.)?|lesson|le[çc]on)\s*(?:\d+\.)?(\d+)", text, re.IGNORECASE)
    if chapter:
        word = "Chapitre" if chapter.group(1).lower().startswith("chap") else "Leçon"
        text = f"{word} {chapter.group(2)} · " + re.sub(r"^[\s_,\-–:]+", "", text[chapter.end():])
    return re.sub(r"\s+", " ", text.replace("_", " ")).strip(" ·")


def loose_lessons(folder, trail):
    """Files lying side by side in a folder. A chapter's video and its homework ("Chap. 1 _ Devoirs") make one lesson;
    homework that names no chapter goes with the lesson just before it; otherwise the leading number says which lesson
    a file belongs to. Lessons keep the order of the numbers."""
    groups, last = {}, None
    for x in sorted(folder.iterdir(), key=lambda x: natural(x.name)):
        if x.is_file() and course_file(x):
            chapter, number = chapter_of(x.stem), NUMBER.match(x.name)
            if chapter:
                key = "c" + chapter
            elif DEVOIR.search(nfc(x.stem)) and last and x.suffix.lower() in KINDS["video"] and not number_shared(folder, number):
                key = last
            else:
                key = number.group(1).lstrip("0") or "0" if number else x.stem
            groups.setdefault(key, []).append(x)
            last = key
    found = []
    for key, items in groups.items():
        main = next((x for x in items if x.suffix.lower() in KINDS["video"] and not DEVOIR.search(nfc(x.stem))), items[0])
        found.append({"id": f"{folder.relative_to(COURS).as_posix()}/#{key}", "titre": lesson_title(main.name), **place(trail),
                      "fichiers": [course_file(x) for x in items]})
    return found


def lessons_in(folder, trail):
    """Walk level > unit > chapter > lesson (any depth): the last folder, the one that holds the files, is the lesson.
    Folders named devoirs, exercices, corrections or ressources are parts of a lesson, not lessons."""
    subs = [d for d in folders_of(folder) if not (DEVOIR.search(d.name) or RESSOURCE.search(d.name))]
    numbered = {m.group(1) for m in (NUMBER.match(x.name) for x in folder.iterdir()
                                     if x.is_file() and x.suffix.lower() in KINDS["video"]) if m}
    videos = [x for x in folder.iterdir() if x.is_file() and x.suffix.lower() in KINDS["video"] and not DEVOIR.search(nfc(x.stem))]
    if not subs and trail and len(numbered) < 2 and len(videos) < 3:   # several videos side by side are several lessons
        files = [f for f in (course_file(x) for x in sorted(folder.rglob("*"), key=lambda x: natural(x.name)) if x.is_file()) if f]
        return [{"id": folder.relative_to(COURS).as_posix(), "titre": clean_title(folder.name), **place(trail[:-1]),
                 "fichiers": files}] if files else []
    found = []
    for sub in subs:
        found += lessons_in(sub, trail + [clean_title(sub.name)])
    return found + loose_lessons(folder, trail)


def add_links(folder, lessons):
    """Put the teacher's links (liens.json, written by import_liens.py) into the lesson whose video has the same title."""
    try:
        videos = json.loads((folder / "liens.json").read_text(encoding="utf-8")).get("videos", [])
    except (OSError, ValueError, AttributeError):
        videos = []
    by_title = {v.get("cle"): v for v in videos if isinstance(v, dict) and v.get("cle")}
    for lesson in lessons:
        links = []
        for f in lesson["fichiers"]:
            video = by_title.get(title_key(f["nom"])) if f["type"] == "video" else None
            if video:
                f["youtube"] = "https://www.youtube.com/watch?v=" + str(video.get("id"))      # the same video, when the computer is closed
                links += [x for x in video.get("liens", []) if isinstance(x, dict) and str(x.get("lien", "")).startswith("http")
                          and x["lien"] not in [y["lien"] for y in links]]
        lesson["liens"] = [{"titre": str(x.get("titre") or ""), "lien": x["lien"]} for x in links]
    try:                                                          # sujets.json: {name of a writing sheet: what it asks}
        prompts = json.loads((folder / "sujets.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prompts = {}
    try:                                                          # sons.json (prepare_sons.py): the moments of each example word
        sounds = json.loads((folder / "sons.json").read_text(encoding="utf-8")).get("videos", {})
    except (OSError, ValueError, AttributeError):
        sounds = {}
    if isinstance(sounds, dict):
        sounds = {nfc(k): v for k, v in sounds.items() if isinstance(v, dict)}
        for lesson in lessons:
            for f in lesson["fichiers"]:
                if f["nom"] in sounds and sounds[f["nom"]].get("sons"):
                    f["resume"], f["sons"] = str(sounds[f["nom"]].get("resume") or ""), sounds[f["nom"]]["sons"]
    if isinstance(prompts, dict):
        prompts = {nfc(k): str(v) for k, v in prompts.items()}
        for lesson in lessons:
            for f in lesson["fichiers"]:
                if f["nom"] in prompts:
                    f["consigne"] = prompts[f["nom"]]


@app.get("/api/cours")
def list_courses():
    """The levels found in cours/, each with its lessons (and the unit each belongs to) in natural order."""
    courses = []
    if not COURS.is_dir():
        return courses
    for folder in folders_of(COURS):
        lessons = lessons_in(folder, [])
        add_links(folder, lessons)
        if lessons:
            courses.append({"id": folder.name, "titre": clean_title(folder.name), "lecons": lessons})
    return courses


@app.get("/api/cours-mots")
def course_words():
    """The words of every course lesson, by unit and lesson (from the table cours_vocab, see db.py cours-vocab)."""
    if not DB:
        return {"unites": []}
    with DB.connect() as conn:
        rows = conn.execute("select unit, lesson, lesson_no, word, form, pos, definition, example, image, clip, lexicon_id, plural from cours_vocab"
                            " order by unit, lesson, idx").fetchall()
        known = {r[0] for r in conn.execute("select lexicon_id from word_progress where user_id = %s and known", (DB.USER,)).fetchall()}
    unites = {}
    for unit, lesson, numero, word, form, pos, definition, example, image, clip, lex, plural in rows:
        lecons = unites.setdefault(unit, {})
        lecon = lecons.setdefault(lesson, {"lesson": lesson, "numero": numero, "titre": clean_title(Path(lesson).name), "mots": []})
        lecon["mots"].append({"word": word, "form": form, "pos": pos, "definition": definition, "example": example,
                              "image": "cours-media/" + quote(image) if image else None,
                              "clip": "cours-media/" + quote(clip) if clip else None, "lex": lex, "known_db": lex in known,
                              "plural": plural})
    def order(unit):
        found = re.search(r"\d+", unit)
        return (int(found.group()) if found else 999, unit)
    return {"unites": [{"unit": unit, "lecons": list(unites[unit].values())} for unit in sorted(unites, key=order)]}


@app.get("/cours-media/{path:path}")
def course_media(path: str):
    target = (COURS / path).resolve()
    if COURS.resolve() not in target.parents or not target.is_file() or not course_file(target):
        raise HTTPException(404, "Fichier introuvable")
    return FileResponse(target, media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream")


@app.post("/api/corriger")
def corriger_texte(body: dict = Body(...)):
    """Claude corrects a text the learner wrote. The app keeps the text and the correction itself (in the store),
    so they follow the same road as the rest of the progress, offline included."""
    import corriger
    try:
        return corriger.correct(str(body.get("consigne") or ""), str(body.get("texte") or ""),
                                level=str(body.get("niveau") or "A1")[:4], lang=str(body.get("langue") or "en")[:2])
    except corriger.CorrectionError as e:
        raise HTTPException(502, str(e))


# ----------------------------------------------------------------------------- the app itself

def app_file(name, media_type=None):
    path = APP / name
    if not path.is_file():
        raise HTTPException(404, "Fichier absent")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-cache"})


@app.get("/")
def index():
    return app_file("index.html", "text/html")


@app.get("/logo.png")
def logo():
    return app_file("logo.png", "image/png")


@app.get("/sw.js")
def service_worker():
    """The script that lets the app answer from the phone when this computer is asleep (see app/sw.js)."""
    return app_file("sw.js", "text/javascript")


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse({
        "name": "Atelier d'épisode", "short_name": "Atelier", "start_url": "/", "display": "standalone",
        "background_color": "#f5f6f8", "theme_color": "#f5f6f8",
        "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}],
    }, media_type="application/manifest+json")


@app.get("/icon-192.png")
def icon_192():
    return app_file("icon-192.png", "image/png")


@app.get("/icon-512.png")
def icon_512():
    return app_file("icon-512.png", "image/png")


@app.get("/apple-touch-icon.png")
def icon_apple():
    return app_file("icon-192.png", "image/png")


def local_address():
    """This computer's address on the home network, for opening the app from a phone."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))          # no data is sent; it only reveals which address is in use
            return s.getsockname()[0]
    except OSError:
        return None


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("ATELIER_PORT", "8000"))
    print("Atelier d'épisode")
    print(f"  Données : {use_database()}")
    print(f"  Sur cet ordinateur : http://localhost:{port}")
    if local_address():
        print(f"  Sur le téléphone (même Wi-Fi) : http://{local_address()}:{port}")
    print("  Pour arrêter : Ctrl+C")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

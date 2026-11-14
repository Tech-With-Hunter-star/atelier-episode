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

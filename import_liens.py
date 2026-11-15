"""Fetch the links a teacher puts under each video of a YouTube playlist, for the lessons of a course.

    python import_liens.py "https://www.youtube.com/playlist?list=PL..." "cours/Road to A1 _ French Course"

It asks YouTube's official Data API (free; needs YOUTUBE_API_KEY in .env) for the title and the description of every
video of the playlist, keeps the links of the "RESSOURCES" part of each description, and writes them to liens.json in the
course folder. The server then shows them in the lesson whose video has the same title. Nothing is downloaded.
Run it again whenever the playlist changes: liens.json is rewritten.
"""
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://www.googleapis.com/youtube/v3/"
URL = re.compile(r"https?://[^\s<>\"')\]]+")
START = re.compile(r"ress?ources?\b.*(utilis|used|vid[ée]o|:)", re.IGNORECASE)     # "RESSOURCES UTILISÉES DANS CETTE VIDÉO:"
END = re.compile(r"^\s*(tags?|hashtags?|chapitres?|chapters?|timestamps?)\s*:?\s*$", re.IGNORECASE)
YOUTUBE = re.compile(r"(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/|youtube\.com/shorts/)([\w-]{11})")


class LinksError(Exception):
    """Something the user can fix: no key, wrong playlist, no network."""


def title_key(text):
    """The same key for a video title and for the file named after it ("Unité 7, Chap. 5 : X" and "07 - Unité 7, Chap.5 _ X.webm")."""
    text = unicodedata.normalize("NFC", str(text)).lower()
    text = re.sub(r"\.(mkv|webm|mp4|m4v|mov)$", "", text)
    text = re.sub(r"^\s*\d+\s*[-–._]\s*", "", text)
    return re.sub(r"[\W_]+", " ", text).strip()


def playlist_id(text):
    found = re.search(r"[?&]list=([\w-]+)", text)
    if found:
        return found.group(1)
    if re.fullmatch(r"[\w-]{10,}", text.strip()):
        return text.strip()
    raise LinksError("Je ne reconnais pas cette playlist. Colle l'adresse complète, celle qui contient list=...")


def call(path, key, fetch=None, **params):
    """One question to the YouTube Data API."""
    address = API + path + "?" + urllib.parse.urlencode({**params, "key": key})
    if fetch:
        return fetch(address)
    try:
        with urllib.request.urlopen(address, timeout=20) as answer:
            return json.loads(answer.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8"))["error"]["message"]
        except Exception:
            pass
        raise LinksError(f"YouTube a refusé ({e.code}). {detail}".strip()) from None
    except OSError as e:
        raise LinksError(f"Pas de réponse de YouTube : {e}") from None


def playlist_videos(playlist, key, fetch=None):
    """Every video of the playlist, in order: [{id, titre, description}]."""
    videos, page = [], None
    while True:
        params = {"part": "snippet", "playlistId": playlist, "maxResults": 50}
        if page:
            params["pageToken"] = page
        data = call("playlistItems", key, fetch, **params)
        for item in data.get("items", []):
            s = item.get("snippet", {})
            video = (s.get("resourceId") or {}).get("videoId")
            if video and s.get("title") not in ("Private video", "Deleted video"):
                videos.append({"id": video, "titre": s.get("title", ""), "description": s.get("description", "")})
        page = data.get("nextPageToken")
        if not page:
            return videos


def links_of(description):
    """The links of the resources part of a description: [{titre, lien}]. Without such a part, every link of the description."""
    lines = description.splitlines()
    start = next((i for i, line in enumerate(lines) if START.search(line) and not URL.search(line)), None)
    part = lines[start + 1:] if start is not None else lines
    found = []
    for line in part:
        if start is not None and END.match(line):
            break
        for link in URL.findall(line):
            link = link.rstrip(".,;")
            label = re.sub(r"\s+", " ", re.sub(r"[\s:•▶►\-–|]+$", "", line[:line.index(link)])).strip()
            if link not in [f["lien"] for f in found]:
                found.append({"titre": label, "lien": link})
    return found


def name_videos(found, key, fetch=None):
    """Give its title to a link that points to another YouTube video and has no words of its own."""
    ids = sorted({m.group(1) for f in found for m in [YOUTUBE.search(f["lien"])] if m})
    titles = {}
    for i in range(0, len(ids), 50):
        data = call("videos", key, fetch, part="snippet", id=",".join(ids[i:i + 50]))
        for item in data.get("items", []):
            titles[item["id"]] = item.get("snippet", {}).get("title", "")
    for f in found:
        m = YOUTUBE.search(f["lien"])
        if m and titles.get(m.group(1)):
            f["titre"] = f"{f['titre']} : {titles[m.group(1)]}" if f["titre"] else titles[m.group(1)]


def collect(playlist, key, fetch=None):
    """What goes into liens.json."""
    videos = playlist_videos(playlist, key, fetch)
    if not videos:
        raise LinksError("Cette playlist est vide, ou elle n'est pas publique.")
    seen = {}
    for v in videos:                                               # a link under most videos is the channel's, not the lesson's
        for link in {x.rstrip(".,;") for x in URL.findall(v["description"])}:
            seen[link] = seen.get(link, 0) + 1
    everywhere = {link for link, n in seen.items() if len(videos) >= 4 and n > len(videos) / 2}
    for v in videos:
        v["liens"] = [f for f in links_of(v.pop("description")) if f["lien"] not in everywhere]
    name_videos([f for v in videos for f in v["liens"]], key, fetch)
    return {"playlist": playlist, "videos": [{"cle": title_key(v["titre"]), **v} for v in videos]}


def main(argv):
    if len(argv) != 3:
        raise SystemExit(__doc__)
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass
    key = os.environ.get("YOUTUBE_API_KEY")
    folder = Path(argv[2])
    try:
        if not key:
            raise LinksError("YOUTUBE_API_KEY n'est pas défini. Ajoute-le dans .env (voir .env.example).")
        if not folder.is_dir():
            raise LinksError(f"Le dossier {folder} n'existe pas.")
        data = collect(playlist_id(argv[1]), key)
    except LinksError as e:
        raise SystemExit(str(e))
    (folder / "liens.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    with_links = [v for v in data["videos"] if v["liens"]]
    print(f"{len(data['videos'])} vidéos, dont {len(with_links)} avec des liens ({sum(len(v['liens']) for v in with_links)} liens).")
    print(f"Écrit : {folder / 'liens.json'}. Recharge la page Leçons de l'app.")


if __name__ == "__main__":
    main(sys.argv)

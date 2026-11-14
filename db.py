"""
db.py — the PostgreSQL side of Atelier d'épisode.

The database is optional. Without it the server reads the episode folders and keeps progress in data/store.json.
With it (ATELIER_DB set in .env), episodes and progress live in PostgreSQL; audio and images stay files on disk.

    python db.py init      create the tables (schema.sql); safe to run again
    python db.py sync      load every episodes/<folder>/episode.json into the tables, and bring in data/store.json once
    python db.py status    how many rows there are

ATELIER_DB is an ordinary connection address, for example
    postgresql://atelier:atelier@localhost:5432/atelier
"""
import datetime
import json
import os
import re
import sys
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from import_episode import norm, word_clip, word_excerpt

HERE = Path(__file__).resolve().parent
USER = 1                                            # one learner for now; every learner table already has user_id
AUDIO_TYPES = (".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus", ".flac", ".mp4", ".webm")
EPISODE_KEY = re.compile(r"^atelier:ep:(.+)$")
WORDS_KEY = re.compile(r"^atelier:words:(.+)$")


def address():
    return os.environ.get("ATELIER_DB") or ""


def connect(url=None):
    return psycopg.connect(url or address(), connect_timeout=5)


def init(conn):
    """Create what is missing. A database made before the dictionary is brought up to date on the way."""
    old = conn.execute("select 1 from information_schema.columns"
                       " where table_name = 'word_progress' and column_name = 'word_key'").fetchone()
    if old:                                         # word_progress used to be per episode; it is rebuilt from user_store
        conn.execute("drop view if exists episode_progress")
        conn.execute("drop table word_progress")
    conn.execute((HERE / "schema.sql").read_text(encoding="utf-8"))
    for eid, idx, word in conn.execute("select episode_id, idx, word from vocab where lexicon_id is null").fetchall():
        conn.execute("update vocab set lexicon_id = %s where episode_id = %s and idx = %s",
                     (lexicon_id(conn, word), eid, idx))          # words loaded before the dictionary existed
    if old:
        rebuild_words(conn)
    rebuild_added(conn)                             # words added before this table existed
    conn.commit()


def lexicon_id(conn, word, pos=None):
    """The number of a word in the dictionary; the word is added if it is new. A known kind of word is kept up to date."""
    lemma = norm(word)
    found = conn.execute("select id from lexicon where lemma = %s", (lemma,)).fetchone()
    if found:
        if pos:
            conn.execute("update lexicon set pos = %s where id = %s and pos is distinct from %s", (pos, found[0], pos))
        return found[0]
    return conn.execute("insert into lexicon (lemma, display, pos) values (%s, %s, %s) on conflict (lemma) do update"
                        " set lemma = excluded.lemma returning id", (lemma, str(word).strip(), pos)).fetchone()[0]


# ----------------------------------------------------------------------------- content: files -> tables

def sync_episode(conn, eid, data, audio_file=None):
    """Replace one episode's content with what episode.json says. Progress tables are not touched."""
    with conn.transaction():
        conn.execute("delete from episodes where id = %s", (eid,))            # cascades to sentences, words, vocab
        conn.execute(
            "insert into episodes (id, title, level, audio_file, duration_s, text_source) values (%s,%s,%s,%s,%s,%s)",
            (eid, data.get("title") or eid, data.get("level"), audio_file or data.get("audio"),
             data.get("duration") or 0, data.get("text_source")))
        sentences = data.get("sentences", [])
        with conn.cursor() as cur:
            cur.executemany(
                "insert into sentences (episode_id, idx, text, new_para, start_s, end_s) values (%s,%s,%s,%s,%s,%s)",
                [(eid, i, s["text"], bool(s.get("para")), s["start"], s["end"]) for i, s in enumerate(sentences)])
            cur.executemany(
                "insert into sentence_words (episode_id, sentence_idx, idx, word, start_s, end_s, heard)"
                " values (%s,%s,%s,%s,%s,%s,%s)",
                [(eid, i, k, w[0], w[1], w[2], len(w) == 3)
                 for i, s in enumerate(sentences) for k, w in enumerate(s.get("words") or [])])
            rows = []
            for i, v in enumerate(data.get("vocab", [])):
                if not 0 <= v.get("sid", -1) < len(sentences):
                    continue
                clip = word_clip(sentences[v["sid"]], v["form"]) or (None, None)
                ctx = word_excerpt(sentences[v["sid"]], v["form"]) or {}
                rows.append((eid, i, lexicon_id(conn, v["word"], v.get("pos")), v["word"], v["form"], v.get("def"), v["sid"], v.get("img"),
                             v.get("gen"), v.get("genForm"), clip[0], clip[1],
                             ctx.get("start"), ctx.get("end"), ctx.get("text")))
            cur.executemany(
                "insert into vocab (episode_id, idx, lexicon_id, word, form, definition, sentence_idx, image_prompt,"
                " gen_example, gen_form, clip_start_s, clip_end_s, ctx_start_s, ctx_end_s, ctx_text)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
    return len(sentences), len(rows)


def find_audio(folder):
    for name in sorted(os.listdir(folder)):
        if name.lower().endswith(AUDIO_TYPES):
            return name
    return None


def sync_folder(conn, folder):
    """Load one episode folder. Returns (sentences, words), or None if it has no readable episode.json."""
    folder = Path(folder)
    try:
        data = json.loads((folder / "episode.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    counts = sync_episode(conn, folder.name, data, find_audio(folder))
    rebuild_words(conn)                             # the word list may have changed: progress follows the dictionary ids
    return counts


def sync_all(conn, root):
    """Load every episode folder, and remove from the tables the episodes whose folder is gone."""
    episodes, seen = Path(root) / "episodes", {}
    if episodes.is_dir():
        for name in sorted(os.listdir(episodes)):
            if (episodes / name).is_dir():
                counts = sync_folder(conn, episodes / name)
                if counts:
                    seen[name] = counts
    known = [r[0] for r in conn.execute("select id from episodes").fetchall()]
    for eid in known:
        if eid not in seen:
            conn.execute("delete from episodes where id = %s", (eid,))
    rebuild_words(conn)
    conn.commit()
    return seen


# ----------------------------------------------------------------------------- content: tables -> app

def list_episodes(conn):
    rows = conn.execute(
        "select e.id, e.title, e.duration_s, e.audio_file,"
        " (select count(*) from sentences s where s.episode_id = e.id),"
        " (select count(*) from vocab v where v.episode_id = e.id)"
        " from episodes e order by e.id").fetchall()
    return [{"id": r[0], "title": r[1], "duration": float(r[2]), "audio_file": r[3], "sentences": r[4], "words": r[5]}
            for r in rows]


def get_episode(conn, eid):
    ep = conn.execute("select id, title, level, duration_s, audio_file from episodes where id = %s", (eid,)).fetchone()
    if not ep:
        return None
    sentences = [{"text": r[0], "para": r[1], "start": float(r[2]), "end": float(r[3])} for r in conn.execute(
        "select text, new_para, start_s, end_s from sentences where episode_id = %s order by idx", (eid,))]
    vocab = []
    for r in conn.execute(
            "select v.idx, v.word, v.form, v.definition, v.sentence_idx, v.gen_example, v.gen_form, v.clip_start_s,"
            " v.clip_end_s, v.lexicon_id, l.pos, v.ctx_start_s, v.ctx_end_s, v.ctx_text from vocab v left join lexicon l on l.id = v.lexicon_id"
            " where v.episode_id = %s order by v.idx", (eid,)):
        item = {"idx": r[0], "word": r[1], "form": r[2], "def": r[3], "sid": r[4]}
        if r[7] is not None:
            item["clip"] = [float(r[7]), float(r[8])]
        if r[9] is not None:
            item["lex"] = r[9]
        if r[10]:
            item["pos"] = r[10]
        if r[11] is not None:
            item["ctx"] = [float(r[11]), float(r[12]), r[13]]
        vocab.append(item)
    return {"id": ep[0], "title": ep[1], "level": ep[2], "duration": float(ep[3]), "audio_file": ep[4],
            "sentences": sentences, "vocab": vocab}


# ----------------------------------------------------------------------------- learner: what the app saves

def store_all(conn, user=USER):
    return {k: v for k, v in conn.execute(
        "select key, value from user_store where user_id = %s and value is not null and value <> 'null'::jsonb", (user,))}


def _day(text):
    try:
        return datetime.date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def store_put(conn, key, value, user=USER):
    """Save one key exactly as the app sent it, and refresh the tables that describe the same thing in rows."""
    with conn.transaction():
        conn.execute(
            "insert into user_store (user_id, key, value) values (%s,%s,%s)"
            " on conflict (user_id, key) do update set value = excluded.value, updated_at = now()",
            (user, key, Jsonb(value)))
        episode = EPISODE_KEY.match(key)
        if episode:
            _sentence_rows(conn, user, episode.group(1), value if isinstance(value, dict) else {})
            rebuild_words(conn, user)
        elif WORDS_KEY.match(key):
            _added_rows(conn, user, WORDS_KEY.match(key).group(1), value if isinstance(value, list) else [])
        elif key == "atelier:days":
            days = sorted({d for d in map(_day, value if isinstance(value, list) else []) if d})
            with conn.cursor() as cur:
                cur.executemany("insert into activity_days (user_id, day) values (%s,%s) on conflict do nothing",
                                [(user, d) for d in days])                    # days are only ever added


def _sentence_rows(conn, user, eid, p):
    """Turn the app's progress for one episode into sentence_progress rows."""
    recited = {i for i in map(_int, p.get("ok") or []) if i is not None}
    passed = {i for i in map(_int, p.get("dict") or []) if i is not None}
    steps = {_int(k): _int(v) for k, v in (p.get("step") or {}).items()}
    marks = {_int(k): v for k, v in (p.get("dn") or {}).items() if isinstance(v, (int, float))}
    rows = [(user, eid, i, i in recited, steps.get(i), marks.get(i), i in passed)
            for i in sorted(x for x in recited | passed | set(steps) | set(marks) if x is not None)]
    conn.execute("delete from sentence_progress where user_id = %s and episode_id = %s", (user, eid))
    with conn.cursor() as cur:
        cur.executemany(
            "insert into sentence_progress (user_id, episode_id, sentence_idx, recited, recite_step, dictation_best,"
            " dictation_passed) values (%s,%s,%s,%s,%s,%s,%s)", rows)


def _added_rows(conn, user, eid, words):
    """The words the learner added herself in one episode, as user_words rows. A word already there keeps its date."""
    keep = {}
    for w in words:
        if isinstance(w, dict) and str(w.get("form") or "").strip() and _int(w.get("sid")) is not None:
            keep[(_int(w["sid"]), str(w["form"]).strip())] = str(w.get("word") or w["form"]).strip()
    for sid, form in conn.execute("select sentence_idx, form from user_words where user_id = %s and episode_id = %s",
                                  (user, eid)).fetchall():
        if (sid, form) not in keep:
            conn.execute("delete from user_words where user_id = %s and episode_id = %s and sentence_idx = %s and form = %s",
                         (user, eid, sid, form))
    for (sid, form), word in keep.items():
        conn.execute("insert into user_words (user_id, episode_id, sentence_idx, form, lexicon_id) values (%s,%s,%s,%s,%s)"
                     " on conflict do nothing", (user, eid, sid, form, lexicon_id(conn, word)))


def rebuild_added(conn, user=USER):
    """Fill user_words from what the app saved, for every episode (used once, when the table is new)."""
    for key, value in conn.execute("select key, value from user_store where user_id = %s and key like 'atelier:words:%%'",
                                   (user,)).fetchall():
        _added_rows(conn, user, key[len("atelier:words:"):], value if isinstance(value, list) else [])


def rebuild_words(conn, user=USER):
    """Recompute word_progress from everything the app saved, across all episodes.

    The app remembers a word as "form|sentence number" inside one episode. Here each of those is translated
    to the word's dictionary id, so that a word known in one episode is known everywhere."""
    in_episode = {(r[0], r[1], r[2]): r[3] for r in conn.execute(
        "select episode_id, form, sentence_idx, lexicon_id from vocab where lexicon_id is not null"
        " union all select episode_id, form, sentence_idx, lexicon_id from user_words where user_id = %s", (user,))}
    words = {}
    for key, p in conn.execute("select key, value from user_store where user_id = %s and key like 'atelier:ep:%%'", (user,)):
        if not isinstance(p, dict):
            continue
        eid = key[len("atelier:ep:"):]
        known, boxes = set(map(str, p.get("kn") or [])), p.get("box") if isinstance(p.get("box"), dict) else {}
        for k in known | set(boxes):
            form, _, sid = k.rpartition("|")
            if not form:
                continue
            lex = in_episode.get((eid, form, _int(sid))) or lexicon_id(conn, form)   # a word you added yourself in the app
            w = words.setdefault(lex, {"known": False, "known_in": set(), "box": None, "due": None})
            if k in known:
                w["known"] = True
                w["known_in"].add(eid)
            box = boxes.get(k) if isinstance(boxes.get(k), dict) else {}
            level = _int(box.get("b"))
            if level is not None and (w["box"] is None or level > w["box"]):
                w["box"], w["due"] = level, _day(box.get("due")) if box.get("due") else None
    conn.execute("delete from word_progress where user_id = %s", (user,))
    with conn.cursor() as cur:
        cur.executemany(
            "insert into word_progress (user_id, lexicon_id, known, known_in, box, due) values (%s,%s,%s,%s,%s,%s)",
            [(user, lex, w["known"], sorted(w["known_in"]), w["box"], w["due"]) for lex, w in sorted(words.items())])
    return len(words)


def known_words(conn, except_episode=None, user=USER):
    """The words the learner knows, as shown in the dictionary.

    except_episode: a word known ONLY from that episode is left out, so that preparing an episode again keeps its words."""
    rows = conn.execute(
        "select l.display from word_progress w join lexicon l on l.id = w.lexicon_id"
        " where w.user_id = %s and w.known and not (w.known_in <@ %s::text[] and cardinality(w.known_in) > 0)"
        " order by l.lemma",
        (user, [except_episode] if except_episode else [])).fetchall()
    return [r[0] for r in rows]


def import_store_file(conn, root, user=USER):
    """Bring data/store.json into the database, once: only if the database holds no progress yet."""
    path = Path(root) / "data" / "store.json"
    if conn.execute("select 1 from user_store where user_id = %s limit 1", (user,)).fetchone():
        return 0
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    for key, value in saved.items():
        store_put(conn, key, value, user)
    conn.commit()
    return len(saved)


# ----------------------------------------------------------------------------- command line

def main():
    try:
        from dotenv import load_dotenv
        load_dotenv(HERE / ".env")
    except ImportError:
        pass
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command not in ("init", "sync", "status"):
        raise SystemExit(__doc__)
    if not address():
        raise SystemExit("ATELIER_DB n'est pas défini. Ajoute-le dans .env (voir .env.example).")
    root = Path(os.environ.get("ATELIER_ROOT") or HERE)
    try:
        conn = connect()
    except psycopg.OperationalError as exc:
        raise SystemExit(f"Connexion impossible à PostgreSQL : {exc}")
    with conn:
        init(conn)
        if command == "init":
            print("Tables prêtes.")
        if command == "sync":
            for eid, (n_sentences, n_words) in sync_all(conn, root).items():
                print(f"  {eid} : {n_sentences} phrases, {n_words} mots")
            moved = import_store_file(conn, root)
            if moved:
                print(f"  progression reprise de data/store.json : {moved} clés")
        if command in ("sync", "status"):
            for table in ("episodes", "sentences", "sentence_words", "vocab", "lexicon", "user_store", "user_words",
                          "sentence_progress", "word_progress", "activity_days"):
                print(f"  {table:18} {conn.execute(f'select count(*) from {table}').fetchone()[0]}")


if __name__ == "__main__":
    main()

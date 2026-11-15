"""These tests need a real PostgreSQL. Give its address in ATELIER_TEST_DB (a database that may be emptied)."""
import json

from fastapi.testclient import TestClient

from conftest import load_server, make_episode


def count(conn, table, where="true"):
    return conn.execute(f"select count(*) from {table} where {where}").fetchone()[0]


def test_sync_loads_an_episode_and_can_be_repeated(root, test_db):
    import db
    with db.connect(test_db) as conn:
        db.init(conn)
        db.init(conn)                                             # creating the tables twice is harmless
        assert db.sync_all(conn, root) == {"essai": (3, 2)}
        assert db.sync_all(conn, root) == {"essai": (3, 2)}       # loading twice gives the same rows, not double
        assert (count(conn, "episodes"), count(conn, "sentences"), count(conn, "sentence_words"), count(conn, "vocab")) == (1, 3, 13, 2)
        assert count(conn, "sentence_words", "not heard") == 1
        ep = db.get_episode(conn, "essai")
        assert ep["vocab"][0]["clip"] == [1.52, 2.52] and "clip" not in ep["vocab"][1]
        assert ep["sentences"][1] == {"text": "Aujourd'hui, on parle de petits défis.", "para": False, "start": 3.0, "end": 7.0}
        (root / "episodes" / "essai" / "episode.json").unlink()   # the folder no longer holds an episode
        assert db.sync_all(conn, root) == {} and count(conn, "episodes") == 0 and count(conn, "vocab") == 0


def test_progress_becomes_rows_and_survives_a_reload(root, test_db):
    import db
    progress = {"ok": [0, 2], "dict": [0], "dn": {"0": 18, "1": 11.5}, "step": {"0": 3, "1": 2},
                "kn": ["bienvenue|0"], "box": {"bienvenue|0": {"b": 3, "due": "2026-11-18"}, "défis|1": {"b": 1, "due": "2026-11-14"}}}
    with db.connect(test_db) as conn:
        db.init(conn)
        db.sync_all(conn, root)
        db.store_put(conn, "atelier:ep:essai", progress)
        db.store_put(conn, "atelier:days", ["2026-11-13", "2026-11-14", "pas une date"])
        db.store_put(conn, "atelier:set", {"rate": 0.8})
        conn.commit()
        assert db.store_all(conn)["atelier:ep:essai"] == progress
        rows = conn.execute("select sentence_idx, recited, recite_step, dictation_best, dictation_passed"
                            " from sentence_progress order by 1").fetchall()
        assert [(r[0], r[1], r[2], float(r[3]) if r[3] is not None else None, r[4]) for r in rows] == [
            (0, True, 3, 18.0, True), (1, False, 2, 11.5, False), (2, True, None, None, False)]
        assert count(conn, "word_progress", "known") == 1 and count(conn, "word_progress", "box = 1") == 1
        assert count(conn, "activity_days") == 2
        view = conn.execute("select sentences, recited, dictations_passed, dictation_average, words, words_known"
                            " from episode_progress where episode_id = 'essai'").fetchone()
        assert (view[0], view[1], view[2], float(view[3]), view[4], view[5]) == (3, 2, 1, 14.8, 2, 1)

        assert db.known_words(conn) == ["bienvenue"]              # the history used when the next episode is prepared
        assert db.known_words(conn, "essai") == []                # ... but never against the episode itself

        db.sync_all(conn, root)                                   # preparing the episode again keeps the progress
        assert count(conn, "sentence_progress") == 3 and count(conn, "word_progress") == 2
        assert count(conn, "lexicon") == 2                        # ... and the dictionary keeps the same two words

        progress["ok"] = [0]                                      # the app saves again: rows follow
        db.store_put(conn, "atelier:ep:essai", progress)
        assert count(conn, "sentence_progress", "recited") == 1
        db.store_put(conn, "atelier:ep:essai", None)              # "remettre l'épisode à zéro"
        conn.commit()
        assert count(conn, "sentence_progress") == 0 and "atelier:ep:essai" not in db.store_all(conn)


def test_old_progress_file_is_brought_in_once(root, test_db):
    import db
    (root / "data").mkdir()
    (root / "data" / "store.json").write_text(json.dumps({"atelier:set": {"rate": 0.7}, "atelier:days": ["2026-11-14"]}), encoding="utf-8")
    with db.connect(test_db) as conn:
        db.init(conn)
        assert db.import_store_file(conn, root) == 2
        assert db.import_store_file(conn, root) == 0
        assert db.store_all(conn)["atelier:set"] == {"rate": 0.7} and count(conn, "activity_days") == 1


def test_server_uses_the_database(root, monkeypatch, test_db):
    server = load_server(root, monkeypatch, test_db)
    assert server.use_database().startswith("PostgreSQL (1 ")
    client = TestClient(server.app)
    assert [(e["id"], e["ready"], e["sentences"], e["words"]) for e in client.get("/api/episodes").json()] == [("essai", True, 3, 2)]
    ep = client.get("/api/episodes/essai").json()
    assert ep["vocab"][0]["clip"] == [1.52, 2.52] and ep["vocab"][0]["image"].startswith("/media/essai/images/0.png")
    assert ep["vocab"][1]["voice_word"].startswith("/media/essai/voice/1-w.wav") and "idx" not in ep["vocab"][1]
    assert ep["vocab"][0]["pos"] == "nom" and "lex" not in ep["vocab"][0]
    assert client.put("/api/store/atelier:ep:essai", json={"ok": [1]}).status_code == 200
    assert client.get("/api/store").json() == {"atelier:ep:essai": {"ok": [1]}}
    assert not (root / "data").exists()                           # nothing written to the file in database mode
    import db
    with db.connect(test_db) as conn:
        assert count(conn, "sentence_progress", "recited") == 1


def test_server_falls_back_to_files_when_the_database_is_down(root, monkeypatch):
    server = load_server(root, monkeypatch, "postgresql://nobody@127.0.0.1:1/none")
    assert server.use_database().startswith("fichiers (PostgreSQL ne répond pas")
    assert TestClient(server.app).get("/api/episodes").json()[0]["id"] == "essai"


def second_episode(root):
    """Another episode that uses "bienvenue" again, plus a new word."""
    folder = root / "episodes" / "suite"
    folder.mkdir()
    (folder / "episode.json").write_text(json.dumps({
        "title": "La suite", "duration": 5, "sentences": [{"text": "Bienvenue dans ce nouveau voyage.", "start": 0, "end": 4}],
        "vocab": [{"word": "Bienvenue", "form": "Bienvenue", "def": "d", "sid": 0},
                  {"word": "un voyage", "form": "voyage", "def": "d", "sid": 0}]}, ensure_ascii=False), encoding="utf-8")


def test_a_word_has_one_number_in_every_episode(root, test_db):
    import db
    second_episode(root)
    with db.connect(test_db) as conn:
        db.init(conn)
        db.sync_all(conn, root)
        assert [r[0] for r in conn.execute("select lemma from lexicon order by id")] == ["bienvenue", "défi", "voyage"]
        ids = conn.execute("select array_agg(distinct lexicon_id), count(*) from vocab where lower(word) = 'bienvenue'").fetchone()
        assert len(ids[0]) == 1 and ids[1] == 2                   # two appearances, one dictionary id
        before = dict(conn.execute("select lemma, id from lexicon").fetchall())
        db.sync_all(conn, root)
        assert dict(conn.execute("select lemma, id from lexicon").fetchall()) == before   # ids never change


def test_known_in_one_episode_is_known_everywhere(root, test_db):
    import db
    second_episode(root)
    with db.connect(test_db) as conn:
        db.init(conn)
        db.sync_all(conn, root)
        db.store_put(conn, "atelier:ep:essai", {"kn": ["bienvenue|0"]})
        conn.commit()
        assert count(conn, "word_progress") == 1
        assert db.known_words(conn, "suite") == ["bienvenue"]     # preparing "suite": do not pick it again
        assert db.known_words(conn, "essai") == []                # preparing "essai" again: its own word stays
        both = conn.execute("select episode_id, words_known from episode_progress order by 1").fetchall()
        assert both == [("essai", 1), ("suite", 1)]               # the word counts as known in both episodes
        db.store_put(conn, "atelier:ep:suite", {"kn": ["Bienvenue|0", "voyage|0"], "box": {"mot ajouté|0": {"b": 2, "due": "2026-11-20"}}})
        conn.commit()
        assert db.known_words(conn, "essai") == ["bienvenue", "un voyage"]    # now also known from another episode
        assert count(conn, "word_progress") == 3 and count(conn, "lexicon", "lemma = 'mot ajouté'") == 1
        row = conn.execute("select known_in from word_progress w join lexicon l on l.id = w.lexicon_id where lemma = 'bienvenue'").fetchone()
        assert row[0] == ["essai", "suite"]


def test_an_older_database_is_brought_up_to_date(root, test_db):
    import db
    with db.connect(test_db) as conn:
        db.init(conn)
        db.sync_all(conn, root)
        db.store_put(conn, "atelier:ep:essai", {"kn": ["bienvenue|0"]})
        conn.execute("drop view episode_progress; drop table word_progress;"
                     " create table word_progress (user_id integer, episode_id text, word_key text, known boolean)")
        conn.commit()
        db.init(conn)                                             # what `python db.py sync` does first
        assert count(conn, "word_progress", "known") == 1 and db.known_words(conn) == ["bienvenue"]
        assert conn.execute("select words_known from episode_progress").fetchone()[0] == 1


def test_words_she_added_herself_live_in_their_own_table(root, test_db):
    import db
    with db.connect(test_db) as conn:
        db.init(conn)
        db.sync_all(conn, root)
        added = [{"word": "tous", "form": "tous", "def": None, "sid": 0}, {"word": "parle", "form": "parle", "def": None, "sid": 1}]
        db.store_put(conn, "atelier:words:essai", added)
        db.store_put(conn, "atelier:ep:essai", {"kn": ["tous|0"]})
        conn.commit()
        rows = conn.execute("select sentence_idx, form, l.lemma from user_words a join lexicon l on l.id = a.lexicon_id order by 1").fetchall()
        assert rows == [(0, "tous", "tous"), (1, "parle", "parle")]
        assert count(conn, "vocab") == 2                          # the words Claude chose are a different table
        assert db.known_words(conn) == ["tous"]                   # a word she added and knows is history too
        assert conn.execute("select words, words_added from episode_progress").fetchone() == (2, 2)
        first = conn.execute("select added_at from user_words where form = 'tous'").fetchone()[0]
        db.sync_all(conn, root)                                   # loading the episode again does not touch them
        db.store_put(conn, "atelier:words:essai", added[:1])      # she removed one
        conn.commit()
        assert conn.execute("select form, added_at from user_words").fetchall() == [("tous", first)]
        db.store_put(conn, "atelier:words:essai", [])             # "remettre l'épisode à zéro"
        conn.commit()
        assert count(conn, "user_words") == 0


def test_words_added_before_the_table_existed_are_brought_in(root, test_db):
    import db
    with db.connect(test_db) as conn:
        db.init(conn)
        db.sync_all(conn, root)
        db.store_put(conn, "atelier:words:essai", [{"word": "tous", "form": "tous", "sid": 0}])
        conn.execute("drop view episode_progress; drop table user_words")
        conn.commit()
        db.init(conn)
        assert count(conn, "user_words") == 1


def test_written_texts_and_their_mistakes_have_their_own_tables(root, test_db):
    import db
    one = {"id": 1794724235000, "at": 1794724235000, "consigne": "Présente-toi", "texte": "salut, je suis 20 ans", "lang": "en",
           "fixedAt": 1794724240000,
           "res": {"corrige": "Salut, j'ai 20 ans.", "note": 9, "niveau": "A1", "commentaire": "ok", "modele": "m",
                   "erreurs": [{"extrait": "salut", "correction": "Salut", "type": "ponctuation", "explication": "capital"},
                               {"extrait": "je suis", "correction": "j'ai", "type": "conjugaison", "explication": "avoir"},
                               {"extrait": "", "correction": "x", "type": "accent"}]}}
    two = {"id": 1794724300000, "at": 1794724300000, "consigne": "", "texte": "je mange un pomme", "res": None,
           "lecon": "Road to A1/Unite 7/#c5", "leconTitre": "Unite 7 · Chapitre 5"}
    with db.connect(test_db) as conn:
        db.init(conn)
        db.store_put(conn, "atelier:ecrits", [one, two, {"id": "x"}, "bruit"])
        assert conn.execute("select body, corrected, score, corrected_at is not null from writings order by id").fetchall() == [
            ("salut, je suis 20 ans", "Salut, j'ai 20 ans.", 9, True), ("je mange un pomme", None, None, False)]
        assert conn.execute("select lesson from writings order by id").fetchall() == [(None,), ("Road to A1/Unite 7/#c5",)]
        assert conn.execute("select kind, count(*) from writing_errors group by kind order by kind").fetchall() == [
            ("conjugaison", 1), ("ponctuation", 1)]
        db.store_put(conn, "atelier:ecrits", [one, two])                 # saving again changes nothing
        assert count(conn, "writings") == 2 and count(conn, "writing_errors") == 2
        db.store_put(conn, "atelier:ecrits", [two])                      # a text she deleted takes its mistakes with it
        assert count(conn, "writings") == 1 and count(conn, "writing_errors") == 0
        conn.execute("drop table writing_errors; drop table writings")   # a database from before these tables
        conn.execute("update user_store set value = %s where key = 'atelier:ecrits'", (db.Jsonb([one]),))
        conn.commit()
        db.init(conn)
        assert count(conn, "writings") == 1 and count(conn, "writing_errors") == 2

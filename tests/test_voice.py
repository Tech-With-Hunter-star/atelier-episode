import json
import os
import subprocess
import sys

from fastapi.testclient import TestClient

import generate_voice
from conftest import load_server, make_episode


def episode_with_a_conjugated_word():
    e = make_episode()
    e["vocab"].append({"word": "parler", "form": "parle", "def": "Dire des mots.", "sid": 1})   # heard clearly, but conjugated
    return e


def test_plan_reads_the_dictionary_form_when_the_episode_says_another_form():
    jobs, sources = generate_voice.plan(episode_with_a_conjugated_word())
    assert jobs == [("1-w", "un défi"), ("2-w", "parler")]                    # words only: no sentence is read by a voice
    assert sources[0]["word_audio"]["source"] == "original"                  # "bienvenue" is said as it is
    assert sources[2]["word_audio"] == {"source": "voice", "file": "2-w", "text": "parler", "why": "the episode says « parle »"}


def test_server_prefers_the_voice_only_for_another_form_and_only_once_the_file_exists(root, monkeypatch):
    folder = root / "episodes" / "essai"
    (folder / "episode.json").write_text(json.dumps(episode_with_a_conjugated_word(), ensure_ascii=False), encoding="utf-8")
    client = TestClient(load_server(root, monkeypatch).app)
    word = client.get("/api/episodes/essai").json()["vocab"][2]
    assert word["clip"] == [4.02, 4.62] and "voice_word" not in word         # no voice file yet: the original stays
    (folder / "voice" / "2-w.wav").write_bytes(b"RIFF old")                   # left over from an older word list
    assert "voice_word" not in client.get("/api/episodes/essai").json()["vocab"][2]   # nobody vouches for it: not played
    (folder / "voice" / "2-w.mp3").write_bytes(b"ID3 new")
    listed = json.loads((folder / "voice" / "sources.json").read_text(encoding="utf-8"))
    listed.append({"n": 2, "word": "parler", "word_audio": {"source": "voice", "file": "2-w.mp3", "text": "parler"}})
    (folder / "voice" / "sources.json").write_text(json.dumps(listed, ensure_ascii=False), encoding="utf-8")
    vocab = client.get("/api/episodes/essai").json()["vocab"]
    assert "clip" not in vocab[2] and vocab[2]["voice_word"].startswith("/media/essai/voice/2-w.mp3")
    assert vocab[0]["clip"] == [1.52, 2.52] and "voice_word" not in vocab[0]  # same form: still the original
    assert client.get("/media/essai/voice/2-w.mp3").status_code == 200


def test_voice_files_are_made_once_and_failures_do_not_stop_the_run(root, tmp_path):
    folder = root / "episodes" / "essai"
    (folder / "episode.json").write_text(json.dumps(episode_with_a_conjugated_word(), ensure_ascii=False), encoding="utf-8")
    for old in (folder / "voice").iterdir():
        old.unlink()
    (folder / "voice" / "9-w.wav").write_bytes(b"RIFF stale")
    fake = tmp_path / "edge-tts"                                              # stands in for the real program
    fake.write_text("#!/bin/sh\nwhile [ $# -gt 0 ]; do case $1 in --text) t=$2;; --write-media) o=$2;; --voice) v=$2;; esac; shift; done\n"
                    "[ \"$t\" = parler ] && { echo boom >&2; exit 1; }\nprintf '%s|%s' \"$v\" \"$t\" > \"$o\"\n")
    fake.chmod(0o755)
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}")
    script = os.path.join(os.path.dirname(generate_voice.__file__), "generate_voice.py")
    run = lambda: subprocess.run([sys.executable, script, str(folder)], env=env, capture_output=True, text=True)
    first = run()
    assert first.returncode == 0 and "1 fichiers créés" in first.stdout and "1 en échec" in first.stdout
    assert (folder / "voice" / "1-w.mp3").read_text() == "fr-FR-DeniseNeural|un défi"
    assert not (folder / "voice" / "2-w.mp3").exists()
    assert "0 fichiers créés, 1 déjà là" in run().stdout
    (folder / "voice" / "1-w.mp3").write_text("an older word")               # same name, but sources.json will say another text
    listed = json.loads((folder / "voice" / "sources.json").read_text(encoding="utf-8"))
    listed[1]["word_audio"]["text"] = "un vieux mot"
    (folder / "voice" / "sources.json").write_text(json.dumps(listed, ensure_ascii=False), encoding="utf-8")
    assert "1 fichiers créés, 0 déjà là" in run().stdout
    assert (folder / "voice" / "1-w.mp3").read_text() == "fr-FR-DeniseNeural|un défi"
    sources = json.loads((folder / "voice" / "sources.json").read_text(encoding="utf-8"))
    assert sources[1]["word_audio"]["engine"] == "edge:fr-FR-DeniseNeural" and sources[1]["word_audio"]["file"] == "1-w.mp3"


def test_files_of_an_older_word_list_are_never_played(root, monkeypatch):
    folder = root / "episodes" / "essai"
    changed = make_episode()
    changed["vocab"][1] = {"word": "aujourd'hui", "form": "zzz", "def": "Ce jour.", "sid": 1, "gen": "Une autre phrase."}
    (folder / "episode.json").write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    word = TestClient(load_server(root, monkeypatch).app).get("/api/episodes/essai").json()["vocab"][1]
    assert "voice_word" not in word and "voice_gen" not in word              # 1-w.wav and 1-g.wav say "un défi": ignored


def test_a_long_sentence_gives_an_excerpt_from_files_and_from_the_database(root, monkeypatch):
    from test_import import long_sentence
    folder = root / "episodes" / "essai"
    e = make_episode()
    e["sentences"].append(long_sentence())
    e["vocab"].append({"word": "le bonheur", "form": "bonheur", "def": "Une grande joie.", "sid": 3, "gen": "Un vieil exemple."})
    (folder / "episode.json").write_text(json.dumps(e, ensure_ascii=False), encoding="utf-8")
    word = TestClient(load_server(root, monkeypatch).app).get("/api/episodes/essai").json()["vocab"][2]
    assert word["ctx"][2] == "… aussi beaucoup de moments de bonheur …" and word["ctx"][0] < word["clip"][0] < word["ctx"][1]
    assert "gen" not in word
    url = os.environ.get("ATELIER_TEST_DB")
    if url:
        import psycopg
        with psycopg.connect(url) as conn:
            conn.execute("drop schema public cascade; create schema public;")
        server = load_server(root, monkeypatch, url)
        assert server.use_database().startswith("PostgreSQL")
        assert TestClient(server.app).get("/api/episodes/essai").json()["vocab"][2]["ctx"] == word["ctx"]

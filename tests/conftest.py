import shutil
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def make_episode():
    """A tiny episode: three sentences with word times, two words (one with a generated example)."""
    return {
        "version": 1, "title": "Épisode d'essai", "level": "A2", "audio": "audio.mp3", "duration": 12.0,
        "text_source": "transcript",
        "sentences": [
            {"text": "Bonjour à tous et bienvenue.", "para": True, "start": 0.0, "end": 3.0,
             "words": [["bonjour", 0.2, 0.7], ["à", 0.8, 0.9], ["tous", 0.9, 1.3], ["et", 1.4, 1.5], ["bienvenue", 1.6, 2.4]]},
            {"text": "Aujourd'hui, on parle de petits défis.", "para": False, "start": 3.0, "end": 7.0,
             "words": [["aujourd'hui", 3.1, 3.8], ["on", 3.9, 4.0], ["parle", 4.1, 4.5], ["de", 4.5, 4.6],
                       ["petits", 4.7, 5.1], ["défis", 5.2, 5.9, 0]]},
            {"text": "À bientôt !", "para": True, "start": 7.0, "end": 9.0, "words": [["à", 7.2, 7.4], ["bientôt", 7.4, 8.1]]},
        ],
        "vocab": [
            {"word": "bienvenue", "form": "bienvenue", "def": "Ce qu'on dit pour accueillir.", "sid": 0, "img": "a door", "pos": "nom"},
            {"word": "un défi", "form": "défis", "def": "Quelque chose de difficile à réussir.", "sid": 1, "img": "a hill",
             "gen": "Ce défi est difficile.", "genForm": "défi"},
        ],
    }


@pytest.fixture
def root(tmp_path):
    """A project folder with one prepared episode, like the real one on the Mac."""
    folder = tmp_path / "episodes" / "essai"
    (folder / "images").mkdir(parents=True)
    (folder / "voice").mkdir()
    (folder / "episode.json").write_text(json.dumps(make_episode(), ensure_ascii=False), encoding="utf-8")
    (folder / "audio.mp3").write_bytes(b"ID3" + bytes(2000))
    (folder / "images" / "0.png").write_bytes(b"\x89PNG fake")
    (folder / "images" / "sources.json").write_text(json.dumps([     # what generate_images.py records about its pictures
        {"n": 0, "word": "bienvenue", "file": "0.png", "prompt": "a door", "model": "test"}]), encoding="utf-8")
    (folder / "voice" / "0-w.wav").write_bytes(b"RIFF fake")      # must be ignored: word 0 has an original clip
    (folder / "voice" / "1-w.wav").write_bytes(b"RIFF fake")
    (folder / "voice" / "1-g.wav").write_bytes(b"RIFF fake")
    (folder / "voice" / "sources.json").write_text(json.dumps([          # what generate_voice.py records about its files
        {"n": 0, "word": "bienvenue", "word_audio": {"source": "voice", "file": "0-w.wav", "text": "bienvenue"}},
        {"n": 1, "word": "un défi", "word_audio": {"source": "voice", "file": "1-w.wav", "text": "un défi"},
         "example_audio": {"source": "voice", "file": "1-g.wav", "text": "Ce défi est difficile."}}], ensure_ascii=False), encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "index.html").write_text("<html>app</html>", encoding="utf-8")
    shutil.copy(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "sw.js"), tmp_path / "app" / "sw.js")
    return tmp_path


def load_server(root, monkeypatch, database=None):
    """Import server.py pointed at a test folder, with or without a database."""
    monkeypatch.setenv("ATELIER_ROOT", str(root))
    if database:
        monkeypatch.setenv("ATELIER_DB", database)
    else:
        monkeypatch.delenv("ATELIER_DB", raising=False)
    sys.modules.pop("server", None)
    import server
    return server


@pytest.fixture
def test_db():
    """The address of a throwaway PostgreSQL database; tests that need it are skipped when it is not given."""
    url = os.environ.get("ATELIER_TEST_DB")
    if not url:
        pytest.skip("ATELIER_TEST_DB n'est pas défini")
    import psycopg
    with psycopg.connect(url) as conn:
        conn.execute("drop schema public cascade; create schema public;")
    return url

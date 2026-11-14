import json
import sys
import time

from fastapi.testclient import TestClient

from conftest import load_server


def test_episode_list_and_content(root, monkeypatch):
    client = TestClient(load_server(root, monkeypatch).app)
    listing = client.get("/api/episodes").json()
    assert [(e["id"], e["ready"], e["sentences"], e["words"], e["has_audio"]) for e in listing] == [("essai", True, 3, 2, True)]
    ep = client.get("/api/episodes/essai").json()
    assert ep["audio_url"] == "/media/essai/audio" and "words" not in ep["sentences"][0]
    first, second = ep["vocab"]
    assert first["clip"] == [1.52, 2.52] and first["image"].startswith("/media/essai/images/0.png")
    assert "voice_word" not in first                              # original audio wins over a voice file
    assert first["pos"] == "nom" and "pos" not in second
    assert "clip" not in second and second["voice_word"].startswith("/media/essai/voice/1-w.wav")
    assert "gen" not in second and "voice_gen" not in second     # sentences are never read by a voice
    assert "ctx" not in first                                     # short sentences have no excerpt: they are heard whole


def test_media_and_refusals(root, monkeypatch):
    client = TestClient(load_server(root, monkeypatch).app)
    part = client.get("/media/essai/audio", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert client.get("/media/essai/voice/1-g.wav").status_code == 200
    assert client.get("/media/essai/images/0.png").status_code == 200
    for bad in ("/api/episodes/absent", "/api/episodes/..", "/media/essai/images/episode.json",
                "/media/essai/voice/..%2Fepisode.json", "/media/essai/voice/9-g.wav"):
        assert client.get(bad).status_code == 404, bad


def test_progress_is_kept_in_a_file(root, monkeypatch):
    client = TestClient(load_server(root, monkeypatch).app)
    assert client.get("/api/store").json() == {}
    assert client.put("/api/store/atelier:set", json={"rate": 0.8}).status_code == 200
    assert client.get("/api/store").json() == {"atelier:set": {"rate": 0.8}}
    assert json.loads((root / "data" / "store.json").read_text(encoding="utf-8")) == {"atelier:set": {"rate": 0.8}}


def test_adding_an_episode_runs_the_import(root, monkeypatch, tmp_path):
    fake = tmp_path / "fake_import.py"                            # stands in for import_episode.py: no Whisper, no Claude
    fake.write_text(
        "import json, sys, os\n"
        "folder = sys.argv[1]\n"
        "print('écoute : 100%')\n"
        "json.dump({'title': sys.argv[3], 'level': sys.argv[5], 'duration': 5, 'sentences': ["
        "{'text': open(os.path.join(folder, 'transcript.txt'), encoding='utf-8').read().strip(), 'start': 0, 'end': 5}],"
        " 'vocab': []}, open(os.path.join(folder, 'episode.json'), 'w', encoding='utf-8'))\n", encoding="utf-8")
    monkeypatch.setenv("ATELIER_IMPORT_SCRIPT", str(fake))
    client = TestClient(load_server(root, monkeypatch).app)
    bad = client.post("/api/episodes", data={"title": "x"}, files={"audio": ("notes.txt", b"abc", "text/plain")})
    assert bad.status_code == 400
    sent = client.post("/api/episodes", data={"title": "Mon Épisode n° 2", "level": "B1", "transcript_text": "Une phrase."},
                       files={"audio": ("podcast.mp3", b"ID3" + bytes(500), "audio/mpeg")})
    assert sent.status_code == 200 and sent.json()["id"] == "mon-episode-n-2"
    for _ in range(100):
        job = client.get("/api/jobs/mon-episode-n-2").json()
        if job["state"] != "running":
            break
        time.sleep(0.1)
    assert job["state"] == "done" and job["percent"] == 100
    ep = client.get("/api/episodes/mon-episode-n-2").json()
    assert ep["title"] == "Mon Épisode n° 2" and ep["level"] == "B1" and ep["sentences"][0]["text"] == "Une phrase."
    assert (root / "episodes" / "mon-episode-n-2" / "audio.mp3").is_file()

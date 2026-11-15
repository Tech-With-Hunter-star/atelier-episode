import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

import prepare_sons
from conftest import load_server

HEARD = [("alors", 0.0, 0.4), ("bonjour", 1.0, 1.6), ("le", 2.0, 2.1), ("pont", 2.1, 2.6), ("mon", 5.0, 5.4), ("oncle", 5.4, 6.0),
         ("on", 8.0, 8.3), ("on", 8.9, 9.2)]


class FakeClaude:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.prompts.append(kw["messages"][0]["content"])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.answer)])


def test_the_example_words_get_their_moments():
    answer = json.dumps({"resume": "The nasal sound ON.", "sons": [
        {"son": "ON", "api": "ɔ̃", "graphies": ["on", "om"], "seuls": [{"i": 7}, {"i": 6}, {"i": 7}, {"i": 300}, {"x": 1}], "exemples": [
            {"mot": "mon oncle", "i": 4, "n": 2}, {"mot": "bonjour", "i": 1}, {"mot": "Bonjour", "i": 1}, {"mot": "inventé", "i": 99}, {"mot": "x"}]},
        {"son": "AN", "seuls": [{"i": 400}], "exemples": [{"mot": "le pont", "i": 2, "n": 2}]},
        {"son": "vide", "exemples": []}]})
    claude = FakeClaude("Voici :\n" + answer)
    result = prepare_sons.sounds_of("How to pronounce ON in French", HEARD, "en", claude)
    assert "1:bonjour" in claude.prompts[0] and "How to pronounce ON" in claude.prompts[0]
    assert result == {"resume": "The nasal sound ON.", "sons": [
        {"son": "ON", "api": "ɔ̃", "graphies": ["on", "om"], "seuls": [{"debut": 8.7, "fin": 9.55}, {"debut": 7.8, "fin": 8.65}],
         "exemples": [
            {"mot": "bonjour", "debut": 0.75, "fin": 1.95}, {"mot": "mon oncle", "debut": 4.75, "fin": 6.35}]},     # in the order they are said
        {"son": "AN", "api": "", "graphies": [], "exemples": [{"mot": "le pont", "debut": 1.75, "fin": 2.95}]}]}   # an invented moment is dropped
    assert prepare_sons.sounds_of("t", [], "en", claude) == {"resume": "", "sons": []}
    assert prepare_sons.lesson_number("12 - How to pronounce ON in French.mkv") == 12
    assert prepare_sons.title_of("12 - How to pronounce ON in French.mkv") == "How to pronounce ON in French"


def test_the_sounds_of_a_video_reach_the_app(root, monkeypatch):
    folder = root / "cours" / "French pronunciation course"
    folder.mkdir(parents=True)
    for name in ["12 - How to pronounce ON in French.mkv", "13 - How to pronounce AN in French.mkv", "14 - IN.mkv"]:
        (folder / name).write_bytes(b"v")
    sound = {"son": "ON", "api": "ɔ̃", "graphies": ["on"], "exemples": [{"mot": "bonjour", "debut": 0.75, "fin": 1.95}]}
    (folder / "sons.json").write_text(json.dumps({"videos": {"12 - How to pronounce ON in French.mkv": {"resume": "ON.", "sons": [sound]},
                                                             "13 - How to pronounce AN in French.mkv": {"resume": "rien", "sons": []}}}), encoding="utf-8")
    on, an, other = [l["fichiers"][0] for l in TestClient(load_server(root, monkeypatch).app).get("/api/cours").json()[0]["lecons"]]
    assert on["sons"] == [sound] and on["resume"] == "ON." and "sons" not in an and "sons" not in other


def test_whisper_listens_to_a_video_only_once(tmp_path, monkeypatch):
    video, calls = tmp_path / "12 - ON.mkv", []
    video.write_bytes(b"v")
    monkeypatch.setattr(prepare_sons, "transcribe", lambda path, size, language: calls.append(path) or HEARD[:2])
    assert prepare_sons.listen(video, "small", "fr") == HEARD[:2] and prepare_sons.listen(video, "small", "fr") == HEARD[:2]
    assert len(calls) == 1 and (tmp_path / ".sons").is_dir()
    prepare_sons.listen(video, "medium", "fr")                       # another size is another listening
    assert len(calls) == 2

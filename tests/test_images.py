import json
import sys
import types

from fastapi.testclient import TestClient

import generate_images as gi
from conftest import load_server, make_episode


def test_scenes_are_rewritten_for_the_chosen_people():
    assert gi.feminise("A man smiling at his son, the boy waves at him.") == "A woman smiling at her daughter, the girl waves at her."
    assert gi.feminise("A woman with long curly hair in a red dress and lipstick.") == "A woman in a red casual clothes and bare face."
    assert gi.feminise("A human hand holding a pen, some women talking.") == "A human hand holding a pen, some women talking."   # "human", "women" untouched
    assert gi.feminise("He meets a uncle.") == "She meets an aunt."


def test_prompt_is_scene_then_people_then_style():
    prompt = gi.build_prompt("A man reading.", gi.PEOPLE, "flat illustration")
    assert prompt.startswith("A woman reading. all people are women with very short") and prompt.endswith("flat illustration")
    assert gi.build_prompt("A man reading.", "", "flat illustration") == "A man reading. flat illustration"   # no people imposed


def test_a_picture_left_from_an_older_word_list_is_never_shown(root, monkeypatch):
    folder = root / "episodes" / "essai"
    client = TestClient(load_server(root, monkeypatch).app)
    assert client.get("/api/episodes/essai").json()["vocab"][0]["image"].startswith("/media/essai/images/0.png")
    changed = make_episode()
    changed["vocab"][0]["word"] = "un autre mot"                              # the list changed; 0.png still shows "bienvenue"
    (folder / "episode.json").write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    assert "image" not in client.get("/api/episodes/essai").json()["vocab"][0]
    (folder / "images" / "sources.json").unlink()                              # nobody vouches for the picture at all
    (folder / "episode.json").write_text(json.dumps(make_episode(), ensure_ascii=False), encoding="utf-8")
    assert "image" not in client.get("/api/episodes/essai").json()["vocab"][0]


def test_only_pictures_that_changed_are_drawn_again(root, monkeypatch, capsys):
    folder = root / "episodes" / "essai"
    drawn = []

    class Picture:
        def save(self, path):
            open(path, "wb").write(b"png")

    monkeypatch.setattr(gi, "load_model", lambda name: ("pipe", "test"))
    monkeypatch.setattr(gi, "draw", lambda pipe, prompt, steps, size, turbo: (drawn.append(prompt), Picture())[1])

    def run(*extra):
        monkeypatch.setattr(sys, "argv", ["generate_images.py", str(folder), *extra])
        gi.main()

    run()                                                                      # 0.png is registered with another prompt: both are drawn
    assert len(drawn) == 2 and drawn[0].startswith("a door. all people are women")
    run()
    assert len(drawn) == 2 and "à jour" in capsys.readouterr().out             # nothing changed: nothing drawn
    e = make_episode()
    e["vocab"][1]["img"] = "a man climbing a mountain"
    (folder / "episode.json").write_text(json.dumps(e, ensure_ascii=False), encoding="utf-8")
    run()
    assert len(drawn) == 3 and drawn[2].startswith("a woman climbing a mountain")   # only the picture whose scene changed
    run("--people", "")
    assert len(drawn) == 5                                                     # another choice of people: all again
    listed = json.loads((folder / "images" / "sources.json").read_text(encoding="utf-8"))
    assert [x["word"] for x in listed] == ["bienvenue", "un défi"] and listed[1]["prompt"].startswith("a man climbing")

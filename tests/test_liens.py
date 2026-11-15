import json
import unicodedata

import pytest
from fastapi.testclient import TestClient

import import_liens
from conftest import load_server

COMMON = "Study with us: https://www.skool.com/x/about\n\nInstagram ▶ https://instagram.com/x\n\n"
DESCRIPTIONS = {
    "French for Beginners : Unité 7, Chap. 5 : Le Déménagement #1": COMMON + "RESSOURCES UTILISÉES DANS CETTE VIDÉO:\n(Normalement je mentionne tout)\n\n"
        "Vidéo bonus visite de l'appartement : https://www.youtube.com/watch?v=abcdefghijk\n\nExercice TV5Monde : https://apprendre.tv5monde.com/fr/exe.\n\n"
        "TAGS\n#french https://example.com/tag",
    "French for Beginners : Unité 7, Chap. 5 : Devoirs": COMMON + "RESSOURCES UTILISÉES DANS CETTE VIDÉO:\nCorrigé : https://example.com/corrige",
    "French for Beginners : Unité 7, Introduction": COMMON,
    "French for Beginners : Unité 7, Chap. 6 : Le Déménagement #2": COMMON + "https://youtu.be/abcdefghijk",
}


def fake(address):
    if "playlistItems" in address:
        first = "pageToken" not in address
        items = list(DESCRIPTIONS.items())[:2] if first else list(DESCRIPTIONS.items())[2:]
        answer = {"items": [{"snippet": {"title": t, "description": d, "resourceId": {"videoId": f"vid{i:08d}"}}}
                            for i, (t, d) in enumerate(items, 0 if first else 2)]}
        if first:
            answer["nextPageToken"] = "p2"
            answer["items"].append({"snippet": {"title": "Private video", "description": "", "resourceId": {"videoId": "x" * 11}}})
        return answer
    assert "id=abcdefghijk" in address
    return {"items": [{"id": "abcdefghijk", "snippet": {"title": "[A1] Visiter un appartement parisien"}}]}


def test_the_links_of_a_playlist():
    assert import_liens.playlist_id("https://www.youtube.com/playlist?list=PLabc-123_x&si=9") == "PLabc-123_x"
    with pytest.raises(import_liens.LinksError):
        import_liens.playlist_id("https://www.youtube.com/watch?v=abc")
    data = import_liens.collect("PLabc", "key", fake)
    videos = {v["titre"]: v["liens"] for v in data["videos"]}
    assert len(videos) == 4                                              # two pages; the private video is left out
    assert videos["French for Beginners : Unité 7, Chap. 5 : Le Déménagement #1"] == [   # only the resources part, tags and channel links out
        {"titre": "Vidéo bonus visite de l'appartement : [A1] Visiter un appartement parisien", "lien": "https://www.youtube.com/watch?v=abcdefghijk"},
        {"titre": "Exercice TV5Monde", "lien": "https://apprendre.tv5monde.com/fr/exe"}]
    assert videos["French for Beginners : Unité 7, Introduction"] == []
    assert videos["French for Beginners : Unité 7, Chap. 6 : Le Déménagement #2"] == [  # no resources heading: its own links, named
        {"titre": "[A1] Visiter un appartement parisien", "lien": "https://youtu.be/abcdefghijk"}]
    assert import_liens.title_key("French for Beginners : Unité 7, Chap. 5 : Le Déménagement #1") == import_liens.title_key(
        unicodedata.normalize("NFD", "10 - French for Beginners _ Unité 7, Chap. 5 _ Le Déménagement #1.webm"))


def test_links_show_in_their_lesson(root, monkeypatch):
    unit = root / "cours" / "Road to A1" / "Unite 7"
    unit.mkdir(parents=True)
    for name in ["01 - French for Beginners _ Unité 7, Introduction.mkv", "10 - French for Beginners _ Unité 7, Chap. 5 _ Le Déménagement #1.webm",
                 "11 - French for Beginners _ Unité 7, Chap. 5 _ Devoirs.webm"]:
        (unit / unicodedata.normalize("NFD", name)).write_bytes(b"v")
    client = TestClient(load_server(root, monkeypatch).app)
    assert [l["liens"] for l in client.get("/api/cours").json()[0]["lecons"]] == [[], []]          # no liens.json yet
    (unit.parent / "liens.json").write_text(json.dumps(import_liens.collect("PLabc", "key", fake)), encoding="utf-8")
    intro, five = client.get("/api/cours").json()[0]["lecons"]
    assert intro["liens"] == [] and intro["fichiers"][0]["youtube"] == "https://www.youtube.com/watch?v=vid00000002"
    assert [x["lien"] for x in five["liens"]] == ["https://www.youtube.com/watch?v=abcdefghijk", "https://apprendre.tv5monde.com/fr/exe",
                                                  "https://example.com/corrige"]            # the lesson's links, then its homework's
    (unit.parent / "liens.json").write_text("pas du json", encoding="utf-8")
    assert client.get("/api/cours").json()[0]["lecons"][1]["liens"] == []                          # a broken file breaks nothing
    assert client.get("/cours-media/Road%20to%20A1/liens.json").status_code == 404

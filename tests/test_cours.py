import unicodedata

from fastapi.testclient import TestClient

from conftest import load_server


def test_courses_are_read_from_the_folder(root, monkeypatch):
    cours = root / "cours"
    a = cours / "Français A1"                                        # one folder per lesson
    for name, files in {"02 - Les articles": ["lecon.mp4", "devoir.pdf", "correction devoir.mp4", "notes.xyz"],
                        "10 - Le passé composé": ["video.mp4"], "1 - Se présenter": ["cours.mp4", ".caché.mp4"]}.items():
        (a / name).mkdir(parents=True)
        for f in files:
            (a / name / f).write_bytes(b"x" * 10)
    b = cours / "phonetique"                                         # all files side by side
    b.mkdir()
    for f in ["01_les_voyelles.mp4", "01_exercices.pdf", "02_les_nasales.mp4", "02_corrigé.mp4", "bonus.pdf"]:
        (b / f).write_bytes(b"y" * 5)
    (cours / "vide").mkdir()

    client = TestClient(load_server(root, monkeypatch).app)
    courses = client.get("/api/cours").json()
    assert [c["titre"] for c in courses] == ["Français A1", "phonetique"]              # the empty folder is left out
    a1 = courses[0]["lecons"]
    assert [l["titre"] for l in a1] == ["1 - Se présenter", "02 - Les articles", "10 - Le passé composé"]   # 2 before 10
    articles = {f["nom"]: f for f in a1[1]["fichiers"]}
    assert set(articles) == {"lecon.mp4", "devoir.pdf", "correction devoir.mp4"}        # the unknown kind is ignored
    assert articles["correction devoir.mp4"]["correction"] and not articles["lecon.mp4"]["correction"]
    assert articles["devoir.pdf"]["type"] == "pdf" and [f["nom"] for f in a1[0]["fichiers"]] == ["cours.mp4"]   # hidden file ignored
    ph = courses[1]["lecons"]
    assert [l["titre"] for l in ph] == ["les voyelles", "les nasales", "bonus"]
    assert [f["nom"] for f in ph[1]["fichiers"]] == ["02_corrigé.mp4", "02_les_nasales.mp4"] and ph[1]["fichiers"][0]["correction"]

    video = client.get(articles["lecon.mp4"]["url"])
    assert video.status_code == 200 and video.content == b"x" * 10 and video.headers["content-type"] == "video/mp4"
    piece = client.get(articles["lecon.mp4"]["url"], headers={"Range": "bytes=2-5"})    # the player asks for pieces of a video
    assert piece.status_code == 206 and piece.content == b"xxxx"
    assert client.get("/cours-media/../episodes/essai/episode.json").status_code == 404   # nothing outside cours/
    assert client.get("/cours-media/Fran%C3%A7ais%20A1/02%20-%20Les%20articles/notes.xyz").status_code == 404


def test_level_unit_chapter_lesson(root, monkeypatch):
    cours = root / "cours"
    for path in ["A1/Unité 2/Chapitre 1/Leçon 1/video.mp4", "A1/Unité 1/Chapitre 2/Leçon 1/video.mp4",
                 "A1/Unité 1/Chapitre 10/Leçon 2/video.mp4", "A1/Unité 1/Chapitre 10/Leçon 2/devoirs/fiche.pdf",
                 "A1/Unité 1/Chapitre 10/Leçon 2/devoirs/video prof.mp4", "A1/Unité 1/Chapitre 10/Leçon 2/Ressources/liste.pdf",
                 "A1/Unité 1/Chapitre 10/Leçon 2/exercices 3.pdf", "A1/Unité 1/Chapitre 10/Leçon 1/video.mp4",
                 "A1/Unité 1/intro.mp4", "A2/Unité 1/Chapitre 1/video.mp4", "A1/Unité 3/vide/notes.xyz"]:
        (cours / path).parent.mkdir(parents=True, exist_ok=True)
        (cours / path).write_bytes(b"z")
    courses = TestClient(load_server(root, monkeypatch).app).get("/api/cours").json()
    assert [c["titre"] for c in courses] == ["A1", "A2"]
    assert [(l["groupe"], l["chapitre"], l["titre"]) for l in courses[0]["lecons"]] == [
        ("Unité 1", "Chapitre 2", "Leçon 1"), ("Unité 1", "Chapitre 10", "Leçon 1"), ("Unité 1", "Chapitre 10", "Leçon 2"),
        ("Unité 1", "", "intro"), ("Unité 2", "Chapitre 1", "Leçon 1")]
    two = courses[0]["lecons"][2]                                       # homework and resources belong to their lesson
    assert two["id"] == "A1/Unité 1/Chapitre 10/Leçon 2"
    assert {f["nom"]: f["partie"] for f in two["fichiers"]} == {"video.mp4": "lecon", "fiche.pdf": "devoir", "video prof.mp4": "devoir",
                                                                 "liste.pdf": "ressource", "exercices 3.pdf": "devoir"}
    assert (courses[1]["lecons"][0]["groupe"], courses[1]["lecons"][0]["chapitre"]) == ("Unité 1", "")   # three levels still work


def test_a_chapter_and_its_homework_make_one_lesson(root, monkeypatch):
    unit = root / "cours" / "Road to A1 _ French Course" / "Unite 10"
    unit.mkdir(parents=True)
    for name in ["01 - French for Beginners _ Unité 10, Introduction.mkv", "02 - French for Beginners _ Unité 10, Chap. 1 _ Les Études.webm",
                 "03 - French for Beginners _ Unité 10, Chap. 1 _ Devoirs.mkv", "04 - French Course for Beginners _ Unité 10, Chapitre 2 _ Vinted.mkv",
                 "05 - French Course for Beginners, Chapitre 10.2 _ Devoirs.mkv", "06 - French for Beginners _ Unité 10, Chap.3 _ Le Stage.webm",
                 "07 - French for Beginners _ Unité 10, Chap.Devoirs.webm", "12 - French for Beginners _ Unité 10, Examen Final !.webm",
                 "Unité 10, Chap. 2 _ Production écrite.pdf"]:
        (unit / unicodedata.normalize("NFD", name)).write_bytes(b"v")                    # a Mac writes é as e + accent
    old = unit.parent / "Unite 0"
    old.mkdir()
    for name in ["03 - Learning French - Unité 0, Lesson 1 _ Alphabet.webm", "04 - Learning French - Unité 0, Lesson 1 _ Homeworks.webm",
                 "05 - NLF French Course, Unité 0.2 _ Devoirs.mkv", "06 - Learning French - Unité 0, Lesson 2 _ Numbers.webm"]:
        (old / name).write_bytes(b"v")
    extra = unit.parent / "Sources"
    extra.mkdir()
    for name in ["120 Easy French Sentences (1).mkv", "120 Easy French Sentences (2).webm", "150 French Sentences _ La Ville.mkv"]:
        (extra / name).write_bytes(b"v")
    every = TestClient(load_server(root, monkeypatch).app).get("/api/cours").json()[0]["lecons"]
    assert [l["groupe"] for l in every if l["groupe"] != "Unite 10"] == ["Unite 0"] * 2 + ["Sources"] * 3      # extras after the units
    assert [(l["titre"], len(l["fichiers"])) for l in every[:2]] == [("Leçon 1 · Alphabet", 2), ("Leçon 2 · Numbers", 2)]
    assert [l["titre"] for l in every[-3:]] == ["120 Easy French Sentences (1)", "120 Easy French Sentences (2)", "150 French Sentences La Ville"]
    lessons = [l for l in every if l["groupe"] == "Unite 10"]
    assert [l["titre"] for l in lessons] == ["Introduction", "Chapitre 1 · Les Études", "Chapitre 2 · Vinted", "Chapitre 3 · Le Stage",
                                             "Examen Final !"]
    assert [[f["partie"] for f in l["fichiers"]] for l in lessons] == [["lecon"], ["lecon", "devoir"], ["lecon", "devoir", "devoir"],
                                                                       ["lecon", "devoir"], ["lecon"]]   # homework with no number: the lesson before
    assert lessons[2]["fichiers"][2]["nom"] == "Unité 10, Chap. 2 _ Production écrite.pdf"               # the writing sheet is homework
    assert "consigne" not in lessons[2]["fichiers"][2]
    (unit.parent / "sujets.json").write_text('{"Unité 10, Chap. 2 _ Production écrite.pdf": "Describe a room."}', encoding="utf-8")
    again = TestClient(load_server(root, monkeypatch).app).get("/api/cours").json()[0]["lecons"]
    assert [f.get("consigne") for l in again if l["titre"] == "Chapitre 2 · Vinted" for f in l["fichiers"]] == [None, None, "Describe a room."]
    assert lessons[1]["id"].endswith("Unite 10/#c1")


def test_no_course_folder_means_no_course(root, monkeypatch):
    assert TestClient(load_server(root, monkeypatch).app).get("/api/cours").json() == []

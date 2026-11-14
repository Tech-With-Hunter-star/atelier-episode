import json
import sys
import types

import import_episode as imp


def test_split_transcript_drops_music_and_time_stamps():
    sentences = imp.split_transcript("[Musique]\n[00:05] Bonjour à tous. Comment ça va ?\n\n[01:10] Très bien, merci beaucoup !")
    assert [s["text"] for s in sentences] == ["Bonjour à tous.", "Comment ça va ?", "Très bien, merci beaucoup !"]
    assert sentences[0]["para"]


def test_long_sentence_is_cut_without_losing_a_word():
    text = ("Je voulais vous parler aujourd'hui d'une habitude très simple, que j'ai prise il y a deux ans, "
            "et qui a complètement changé ma façon de travailler le matin, parce que je me lève plus tôt, "
            "mais aussi parce que je prends enfin le temps de lire quelques pages avant de commencer.")
    pieces = imp.split_long(text, 120)
    assert len(pieces) >= 2 and max(map(len, pieces)) <= 160
    assert " ".join(pieces) == text
    assert imp.split_long("Une phrase courte.", 120) == ["Une phrase courte."]
    assert imp.split_long(text, 0) == [text]                      # 0 means: never cut


def test_shorten_keeps_paragraph_mark_on_first_piece_only():
    long = "Un deux trois quatre cinq six sept huit neuf dix onze, et douze treize quatorze quinze seize dix-sept dix-huit."
    out = imp.shorten([{"text": long, "para": True}], 60)
    assert [s["para"] for s in out] == [True] + [False] * (len(out) - 1) and len(out) > 1


def heard_words(text, pace=0.4, start=1.0):
    return [(w, start + i * pace, start + i * pace + 0.3) for i, w in enumerate(text.split())]


def test_align_gives_times_in_order():
    sentences = [{"text": "Bonjour à tous.", "para": True}, {"text": "On parle de défis aujourd'hui.", "para": False}]
    heard = heard_words("Bonjour à tous on parle de défi aujourd'hui")        # "défi": Whisper heard it differently
    share = imp.align(sentences, heard, 10.0)
    assert 0.8 < share < 1
    assert sentences[0]["start"] < sentences[0]["end"] <= sentences[1]["start"] + 0.06 < sentences[1]["end"]
    assert abs(sentences[1]["start"] - (1.0 + 3 * 0.4 - 0.15)) < 0.2
    flagged = [w for w in sentences[1]["words"] if len(w) == 4]
    assert [w[0] for w in flagged] == ["défis"]                   # only the word that was not heard exactly


def test_word_clip_only_when_the_word_was_really_heard():
    s = {"text": "On parle de petits défis.", "words": [["on", 3.9, 4.0], ["parle", 4.1, 4.5], ["de", 4.5, 4.6],
                                                        ["petits", 4.7, 5.1], ["défis", 5.2, 5.9, 0]]}
    assert imp.word_clip(s, "parle") == (4.02, 4.62)
    assert imp.word_clip(s, "Parle de") == (4.02, 4.72)           # an expression of two words
    assert imp.word_clip(s, "défis") is None                      # estimated time
    assert imp.word_clip(s, "absent") is None
    assert imp.word_clip({"text": "Sans temps."}, "temps") is None


def test_pick_words_checks_what_claude_returns(monkeypatch):
    sentences = [{"text": "Voici de petits défis pour vous."}, {"text": "À bientôt."}]
    answer = [
        {"word": "un défi", "form": "défis", "sid": 0, "def": "d", "img": "x", "gen": "Ce défi est dur.", "genForm": "défi"},
        {"word": "inventé", "form": "absent", "sid": 0, "def": "d", "img": "x"},          # not in the sentence
        {"word": "bientôt", "form": "bientôt", "sid": 1, "def": "d", "img": "x", "gen": "Sans le mot.", "genForm": "zzz"},
        {"word": "loin", "form": "pour", "sid": 9, "def": "d", "img": "x"},               # wrong sentence number: repaired
    ]

    class Client:
        class messages:
            @staticmethod
            def create(**_):
                return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=json.dumps(answer))])

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=lambda: Client()))
    words = imp.pick_words(sentences, "A2", 40, "x")
    assert [(w["word"], w["sid"]) for w in words] == [("un défi", 0), ("loin", 0), ("bientôt", 1)]   # in episode order
    assert all("gen" not in w for w in words)                    # generated example sentences are no longer kept


def test_listening_is_kept_and_reused(tmp_path, monkeypatch):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x" * 10)
    calls = []
    monkeypatch.setattr(imp, "transcribe", lambda path, size: (calls.append(1), ([("bonjour", 0.0, 0.5)], 3.0))[1])
    first = imp.listen(str(audio), "small", str(tmp_path))
    second = imp.listen(str(audio), "small", str(tmp_path))
    assert first == second and len(calls) == 1
    audio.write_bytes(b"x" * 20)                                  # another audio: listen again
    imp.listen(str(audio), "small", str(tmp_path))
    assert len(calls) == 2


def fake_claude(monkeypatch, answer_for):
    """Replace Claude by a function that receives the prompt and returns the list to answer with."""
    prompts = []

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                prompts.append(kw["messages"][0]["content"])
                return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=json.dumps(answer_for(prompts[-1])))])

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=lambda: Client()))
    return prompts


def test_norm_drops_the_article_but_not_the_start_of_a_word():
    assert imp.norm("Un Défi") == "défi" and imp.norm("l\u2019habitude") == "habitude" and imp.norm("se lever") == "lever"
    assert imp.norm("semaine") == "semaine" and imp.norm("la") == "la" and imp.norm("avoir la flemme") == "avoir la flemme"


def test_every_word_mode_asks_part_by_part_and_never_repeats(monkeypatch):
    sentences = [{"text": f"Phrase {i} avec le mot numéro{i} dedans."} for i in range(120)]

    def answer(prompt):                                            # one word per sentence shown, plus a repeat
        shown = [int(l.split(".")[0]) for l in prompt.split("Sentences:")[1].strip().splitlines()]
        return [{"word": f"un numéro{i}", "form": f"numéro{i}", "sid": i, "def": "d", "img": "x"} for i in shown] + \
               [{"word": "un numéro0", "form": "numéro0", "sid": 0, "def": "d", "img": "x"}]

    prompts = fake_claude(monkeypatch, answer)
    words = imp.pick_words(sentences, "A1", 0, "x")
    assert len(prompts) == 3 and len(words) == 120                # 120 sentences = 3 parts of 50, 50, 20
    assert [w["sid"] for w in words] == list(range(120))
    assert "numéro0" in prompts[1].split("Sentences:")[0]         # later parts are told what is already picked
    assert "level A1" in prompts[0] and "every word" in prompts[0]


def test_a_limit_is_respected_across_parts(monkeypatch):
    sentences = [{"text": f"Phrase {i} avec le mot numéro{i} dedans."} for i in range(120)]
    fake_claude(monkeypatch, lambda prompt: [
        {"word": f"un numéro{i}", "form": f"numéro{i}", "sid": i, "def": "d", "img": "x"}
        for i in [int(l.split(".")[0]) for l in prompt.split("Sentences:")[1].strip().splitlines()]])
    assert len(imp.pick_words(sentences, "A1", 30, "x")) == 30


def test_words_already_known_are_never_picked_again(monkeypatch):
    sentences = [{"text": "Voici de petits défis et une habitude."}]
    prompts = fake_claude(monkeypatch, lambda prompt: [
        {"word": "un défi", "form": "défis", "sid": 0, "def": "d", "img": "x"},
        {"word": "une habitude", "form": "habitude", "sid": 0, "def": "d", "img": "x"}])
    words = imp.pick_words(sentences, "A1", 0, "x", known=["Le défi"])
    assert [w["word"] for w in words] == ["une habitude"]
    assert "Never pick any of these" in prompts[0] and "défi" in prompts[0].split("Sentences:")[0]


def test_history_comes_from_other_episodes_only(tmp_path, monkeypatch):
    monkeypatch.delenv("ATELIER_DB", raising=False)
    for eid, word in (("ep1", "un défi"), ("ep2", "une habitude")):
        folder = tmp_path / "episodes" / eid
        folder.mkdir(parents=True)
        (folder / "episode.json").write_text(json.dumps({"vocab": [
            {"word": word, "form": "forme", "sid": 3}, {"word": "pas connu", "form": "autre", "sid": 4}]}), encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "store.json").write_text(json.dumps({
        "atelier:ep:ep1": {"kn": ["forme|3"]}, "atelier:ep:ep2": {"kn": ["forme|3"]}, "atelier:set": {"rate": 1}}), encoding="utf-8")
    assert imp.known_words(str(tmp_path), "ep2") == ["un défi"]   # preparing ep2 again: its own words stay
    assert imp.known_words(str(tmp_path), "ep3") == ["un défi", "une habitude"]
    assert imp.known_words(str(tmp_path / "nothing"), "ep3") == []


def test_kinds_of_words_are_added_without_touching_the_words(monkeypatch, capsys):
    vocab = [{"word": "un défi"}, {"word": "viser", "pos": "verbe"}, {"word": "lent"}]
    fake_claude(monkeypatch, lambda prompt: ["nom", "Adjectif"])
    assert imp.tag_words(vocab, "x") == 2
    assert [v.get("pos") for v in vocab] == ["nom", "verbe", "adjectif"]
    fake_claude(monkeypatch, lambda prompt: ["nom"])              # wrong length: nothing is guessed
    again = [{"word": "a"}, {"word": "b"}]
    assert imp.tag_words(again, "x") == 0 and "pos" not in again[0]


def long_sentence():
    text = "Et ensuite évidemment, je te souhaite aussi beaucoup de moments de bonheur, de moments de tranquillité aussi et beaucoup de succès dans tout ce que tu décides d'entreprendre cette année."
    words = [[t, 10 + k * 0.4, 10 + k * 0.4 + 0.3] for k, t in enumerate(imp.tokens(text))]
    return {"text": text, "start": 9.9, "end": 10 + len(words) * 0.4, "words": words}


def test_excerpt_is_a_few_original_words_around_the_word():
    s = long_sentence()
    x = imp.word_excerpt(s, "bonheur")
    assert x["text"] == "… aussi beaucoup de moments de bonheur …"                      # five words before, then up to the comma
    assert x["start"] == round(10 + 6 * 0.4 - 0.10, 2) and x["end"] == round(10 + 11 * 0.4 + 0.3 + 0.15, 2)
    last = imp.word_excerpt(s, "entreprendre")                                          # found inside "d'entreprendre"
    assert last["text"] == "… tout ce que tu décides d'entreprendre cette année" and last["end"] == s["end"]
    assert imp.word_excerpt(s, "absent") is None
    assert imp.word_excerpt({"text": "À bientôt !", "start": 0, "end": 2, "words": [["à", 0, 1], ["bientôt", 1, 2]]}, "bientôt") is None
    assert imp.word_excerpt({"text": s["text"]}, "bonheur") is None                     # no word times


def test_excerpt_never_cuts_on_a_word_that_was_not_heard():
    s = long_sentence()
    s["words"][6].append(0)                                                            # "aussi": its time is an estimate
    x = imp.word_excerpt(s, "bonheur")
    assert x["text"].startswith("… souhaite aussi beaucoup") and x["start"] == round(10 + 5 * 0.4 - 0.10, 2)

import json
import types

import pytest
from fastapi.testclient import TestClient

import corriger
from conftest import load_server

ANSWER = {"corrige": "Je suis Leilei, j'ai une sœur et elle est infirmière.",
          "erreurs": [{"extrait": "soeur", "correction": "sœur", "type": "Orthographe", "explication": "The word is written with œ."},
                      {"extrait": "infirmier", "correction": "infirmière", "type": "accord", "explication": "The noun agrees with 'elle'."},
                      {"extrait": "", "correction": "rien", "type": "accord"}],
          "note": "13.6", "niveau": "A1+", "commentaire": "Clear. Work on the feminine forms."}


class FakeClaude:
    """Stands in for the Anthropic client: remembers what it was asked and gives a prepared answer."""
    def __init__(self, text):
        self.text, self.asked = text, []
        self.messages = types.SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.asked.append(kw)
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=self.text)])


def test_a_correction_is_read_checked_and_tidied():
    claude = FakeClaude("Voici :\n" + json.dumps(ANSWER, ensure_ascii=False) + "\nBon courage.")
    r = corriger.correct("Présente ta famille.", "Je suis Leilei, j'ai une soeur et elle est infirmier.", lang="en", client=claude)
    assert r["corrige"].endswith("infirmière.") and r["note"] == 14 and r["niveau"] == "A1+"
    assert [e["type"] for e in r["erreurs"]] == ["orthographe", "accord"]            # the empty entry is dropped, the type is put in lower case
    prompt = claude.asked[0]["messages"][0]["content"]
    assert "Présente ta famille." in prompt and "elle est infirmier" in prompt and "English" in prompt


def test_what_cannot_be_corrected_says_why():
    with pytest.raises(corriger.CorrectionError, match="Écris"):
        corriger.correct("", "  ", client=FakeClaude("{}"))
    with pytest.raises(corriger.CorrectionError, match="trop long"):
        corriger.correct("", "a" * 5000, client=FakeClaude("{}"))
    with pytest.raises(corriger.CorrectionError, match="pas pu être lue"):
        corriger.correct("", "Bonjour à tous.", client=FakeClaude("désolé"))
    with pytest.raises(corriger.CorrectionError, match="incomplète"):
        corriger.correct("", "Bonjour à tous.", client=FakeClaude('{"note": 12}'))


def test_the_server_passes_the_text_on_and_reports_failures(root, monkeypatch):
    client = TestClient(load_server(root, monkeypatch).app)
    seen = {}

    def fake(consigne, texte, level="A1", lang="en", **kw):
        seen.update(consigne=consigne, texte=texte, level=level, lang=lang)
        if "panne" in texte:
            raise corriger.CorrectionError("Claude n'a pas répondu : hors ligne")
        return {"corrige": texte, "erreurs": [], "note": 20, "niveau": "A1", "commentaire": ""}

    monkeypatch.setattr(corriger, "correct", fake)
    r = client.post("/api/corriger", json={"consigne": "Décris ta ville.", "texte": "J'habite à Cesena.", "langue": "zh"})
    assert r.status_code == 200 and r.json()["note"] == 20 and seen == {"consigne": "Décris ta ville.", "texte": "J'habite à Cesena.", "level": "A1", "lang": "zh"}
    bad = client.post("/api/corriger", json={"texte": "panne"})
    assert bad.status_code == 502 and "hors ligne" in bad.json()["detail"]

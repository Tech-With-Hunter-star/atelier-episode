"""
corriger.py — Claude corrects a short text written in French by the learner (a "production écrite").

The server calls correct() when the app sends a text; nothing here touches files or the database.
The answer is a dictionary the app can show and keep:

  {"corrige": the whole text, corrected,
   "erreurs": [{"extrait": what was written, "correction": what it should be,
                "type": one of TYPES, "explication": why, in the language asked for}],
   "note": a mark out of 20, "niveau": the level this text shows (A1, A1+, A2…),
   "commentaire": two or three sentences on what is good and what to work on}

Try it alone:
  python corriger.py "Présente ta famille." "Je suis Leilei, j'ai une soeur et elle est infirmier."
"""
import json
import os
import sys

MODEL = os.environ.get("ATELIER_CLAUDE_MODEL") or "claude-sonnet-5-5"
TYPES = ["orthographe", "accent", "accord", "conjugaison", "article ou préposition", "vocabulaire", "ordre des mots", "ponctuation"]
LANGS = {"en": "English", "fr": "very simple French (level A1)", "zh": "Chinese", "it": "Italian"}
MAX_CHARS = 8000

PROMPT = """You are a careful French teacher correcting a short text written by a learner of level {level}.

The task the learner was given (may be empty):
<consigne>
{consigne}
</consigne>

The learner's text:
<texte>
{texte}
</texte>

Correct it the way a teacher corrects a "production écrite":
- Keep the learner's own sentences and ideas. Change only what is wrong or really unnatural; do not rewrite in a more advanced style.
- List every error, in the order of the text. "extrait" is the exact words the learner wrote (copied character for character),
  "correction" is what they should be. One error per entry; a word with two problems gets two entries.
- "type" is exactly one of: {types}.
- "explication": one short sentence in {lang}, saying the rule, not just repeating the correction.
- "note": a whole number from 0 to 20, as a French teacher would mark this text for level {level}:
  does it answer the task, is it understandable, how many errors for its length.
- "niveau": the level the text shows by itself: A1, A1+, A2, A2+ or B1.
- "commentaire": two or three sentences in {lang}: what is already good, then the one or two things to work on first.
Do not flatter: if the text is weak, say so plainly and kindly.

Answer with one JSON object and nothing else:
{{"corrige": "...", "erreurs": [{{"extrait": "...", "correction": "...", "type": "...", "explication": "..."}}], "note": 0, "niveau": "A1", "commentaire": "..."}}
"""


class CorrectionError(Exception):
    """The correction could not be made; the message can be shown to the learner."""


def parse(text):
    """The dictionary inside Claude's answer, checked and tidied. Raises CorrectionError if it is not usable."""
    try:
        data = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except ValueError:
        raise CorrectionError("La réponse de Claude n'a pas pu être lue. Réessaie.")
    if not isinstance(data, dict) or not isinstance(data.get("corrige"), str) or not data["corrige"].strip():
        raise CorrectionError("La réponse de Claude est incomplète. Réessaie.")
    errors = []
    for e in data.get("erreurs") or []:
        if isinstance(e, dict) and str(e.get("extrait") or "").strip() and str(e.get("correction") or "").strip():
            kind = str(e.get("type") or "").strip().lower()
            errors.append({"extrait": str(e["extrait"]).strip(), "correction": str(e["correction"]).strip(),
                           "type": kind if kind in TYPES else "orthographe" if not kind else kind,
                           "explication": str(e.get("explication") or "").strip()})
    try:
        mark = max(0, min(20, int(round(float(data.get("note"))))))
    except (TypeError, ValueError):
        mark = None
    return {"corrige": data["corrige"].strip(), "erreurs": errors, "note": mark,
            "niveau": str(data.get("niveau") or "").strip()[:4], "commentaire": str(data.get("commentaire") or "").strip()}


def keep_raw(answer, stop_reason):
    """Keeps Claude's last unreadable answer in _backup/, so it can be looked at."""
    try:
        folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_backup")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "corriger-derniere-reponse.txt"), "w", encoding="utf-8") as f:
            f.write(f"stop_reason: {stop_reason}\n\n{answer}\n")
    except OSError:
        pass


def correct(consigne, texte, level="A1", lang="en", client=None, model=None):
    """Ask Claude to correct one text. client is only passed by the tests."""
    texte = (texte or "").strip()
    if len(texte) < 3:
        raise CorrectionError("Écris d'abord ton texte.")
    if len(texte) > MAX_CHARS:
        raise CorrectionError(f"Le texte est trop long ({len(texte)} caractères, {MAX_CHARS} au plus).")
    if client is None:
        try:
            import anthropic
            client = anthropic.Anthropic()                        # uses ANTHROPIC_API_KEY, or your `ant auth login`
        except Exception as e:                                    # the library is missing, or no key was found
            raise CorrectionError(f"Claude n'est pas disponible sur cet ordinateur : {e}")
    prompt = PROMPT.format(level=level or "A1", consigne=(consigne or "").strip()[:1500], texte=texte,
                           types=", ".join(TYPES), lang=LANGS.get(lang, LANGS["en"]))
    result = None
    for attempt in (1, 2):                                        # a reply that cannot be read is asked for once more
        try:
            reply = client.messages.create(model=model or MODEL, max_tokens=8000, messages=[{"role": "user", "content": prompt}])
        except Exception as e:
            raise CorrectionError(f"Claude n'a pas répondu : {e}")
        answer = "".join(block.text for block in reply.content if getattr(block, "type", "") == "text")
        try:
            result = parse(answer)
            break
        except CorrectionError:
            if attempt == 2:
                keep_raw(answer, getattr(reply, "stop_reason", None))
                raise
    result["modele"] = model or MODEL
    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    try:
        print(json.dumps(correct(sys.argv[1], sys.argv[2]), ensure_ascii=False, indent=1))
    except CorrectionError as e:
        sys.exit(str(e))

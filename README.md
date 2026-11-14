# Atelier d'épisode

Learn French from one podcast episode at a time. Give it an audio file (and a transcript if you have one);
it returns the episode cut into sentences with exact times, a word list at your level, pictures, and
exercises: listening, vocabulary, dictation, recitation and spaced review.

Everything runs on your own computer and is used from a phone on the same Wi-Fi.

中文说明：[README.zh.md](README.zh.md)

| Listen sentence by sentence | Words of the episode | Fill the gap | Review cards |
| --- | --- | --- | --- |
| ![Episode](docs/screen-episode.png) | ![Words](docs/screen-words.png) | ![Game](docs/screen-game.png) | ![Cards](docs/screen-cards.png) |

The screenshots show the sample episode in `sample/`, written for this repository.

## Why it exists

Most vocabulary apps read words with a synthetic voice and invent example sentences. A beginner then
learns a pronunciation and an intonation nobody uses. Here, what you hear is the podcast itself:

- **Phrase**: the whole original sentence.
- **Extrait**: a few original words around the word, about four seconds.
- **Mot**: the original clip when the speaker says the dictionary form. Only when the episode says another
  form ("souhaite" for "souhaiter") does a preset voice read the dictionary form.

No sentence is ever read by a synthetic voice, and no example sentence is generated.

## How it works

```
audio (+ transcript) → import_episode.py → episode.json ─┐
                       generate_images.py → images/ ─────┤
                       generate_voice.py  → voice/  ─────┼→ server.py → app in the browser
             (optional) db.py → PostgreSQL ──────────────┘
```

| Step | Tool | What it does |
| --- | --- | --- |
| Hear the audio | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | A start and end time for every spoken word |
| Match the transcript | Python `difflib` | Lines the written sentences up with the heard words, so each sentence gets its real times |
| Choose the words | Claude (Anthropic API) | Picks the words above the learner's level, with a simple French definition and the kind of word. Words already marked as known in earlier episodes are left out |
| Draw the words | [diffusers](https://github.com/huggingface/diffusers) with `sd-turbo` | One picture per word, on the local machine |
| Read dictionary forms | [edge-tts](https://github.com/rany2/edge-tts) | One preset voice, only for words the episode says in another form |
| Store | PostgreSQL 16 in Docker | Episodes, sentences, a dictionary shared by all episodes, and the learner's progress |
| Serve | FastAPI | Sends the app, the audio and the pictures; saves progress |
| App | One HTML file, plain JavaScript | Works on a phone, can be added to the home screen |

## Try it

```
pip install -r requirements.txt
mkdir -p episodes
cp -r sample/demo episodes/demo
python server.py
```

Open `http://localhost:8000`. The sample has no audio; [sample/README.md](sample/README.md) explains how to
add your own recording and run the real pipeline on it.

## Add an episode of your own

Put an audio file, and a `transcript.txt` if you have one, in `episodes/my-episode/`, then:

```
python prepare.py episodes/my-episode
```

This runs the import, the pictures, the voice and the database in turn, and skips a step whose tools are
missing. Each step can also be run alone:

```
python import_episode.py episodes/my-episode --title "My episode" --level A1
python generate_images.py episodes/my-episode --test
python generate_voice.py episodes/my-episode
python db.py sync
```

Choosing the words needs access to Claude: copy `.env.example` to `.env` and put your Anthropic API key in it.
The pictures need `pip install -r requirements-images.txt` (large).

## Database (optional)

Without a database, progress is kept in `data/store.json`. With one:

```
docker compose up -d
python db.py sync
```

and `ATELIER_DB=postgresql://atelier:atelier@localhost:5433/atelier` in `.env`.

| Table | Holds |
| --- | --- |
| `episodes`, `sentences`, `sentence_words` | The content of each episode, down to the time of every word |
| `lexicon` | One row and one id per French word, across all episodes |
| `vocab` | The words chosen for an episode, each pointing to `lexicon` |
| `word_progress` | What the learner knows, per word and not per episode: a known word never comes back |
| `user_words` | Words the learner added by hand |
| `sentence_progress`, `activity_days` | Sentences mastered, days of practice |

The view `episode_progress` gives one line per episode. The full definition is in `schema.sql`.

## Tests

```
pip install -r requirements-dev.txt
pytest -q
```

The database tests run when `ATELIER_TEST_DB` points to a throwaway PostgreSQL database (it gets emptied), and are skipped otherwise.

## Limits

- French audio only.
- One learner; the tables are ready for several, the app has no login.
- No login on the server: use it on your home network only.
- One episode is prepared at a time.
- The small picture model does not always follow the description.

## Content and licences

- **Podcasts are not in this repository.** Audio, transcripts, `episode.json`, pictures and voice files stay
  on your computer; `.gitignore` excludes them. They belong to their authors and are for personal study.
- The code is under the MIT licence (see `LICENSE`).
- `sd-turbo` is distributed by Stability AI under its Community License; read it before any commercial use.
- `edge-tts` is an unofficial client for a Microsoft online voice service. It can be replaced by the macOS
  voice with `--engine say`, or by any command with `--command`.
- The generated voice is a preset voice. It is never a copy of a podcast host's voice.

## How it was built

Product design, technical decisions and testing by Leilei Zhang. Built with Claude (Anthropic) as an AI
coding assistant working under my direction: I defined what each part had to do, reviewed the behaviour
on a MacBook Pro M1 Pro, and decided every change.

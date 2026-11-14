# Sample episode

`demo/` holds a ten-sentence episode written for this repository, so it can be shared freely.
It has no audio: the times in `episode.json` are placeholders.

See it in the app:

```
mkdir -p episodes
cp -r sample/demo episodes/demo
python server.py
```

Try the real pipeline: record yourself reading `demo/transcript.txt`, save the recording as
`episodes/demo/audio.m4a`, then run

```
python import_episode.py episodes/demo --title "My first episode"
```

The script listens to your recording, gives every sentence its real start and end time,
and asks Claude for the words to learn.

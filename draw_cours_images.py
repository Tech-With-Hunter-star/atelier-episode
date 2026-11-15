#!/usr/bin/env python3
"""
draw_cours_images.py — one picture per course word, drawn on your computer with the same model and the same
people and style rules as the episode pictures (generate_images.py).

    python draw_cours_images.py                       every lesson in cours/ whose words have a picture description
    python draw_cours_images.py "<folder or video>"   one folder, or one lesson
    --model stabilityai/sd-turbo   the model (the default is the one already used for the episodes)
    --steps 2  --size 512  --limit N

The pictures go in a hidden folder next to the video, ".<video name>.images/0.png", the number being the word's
position. Each word that has one gets an "image" entry in its .vocab.json. Words that already have a picture are kept.
Then load the words again with:  python db.py cours-vocab
"""
import argparse
import json
import os
import sys
from pathlib import Path

import generate_images as gi                       # the same drawing code, rules and style as the episode pictures

ROOT = Path(os.environ.get("ATELIER_ROOT") or Path(__file__).resolve().parent)
COURS = (ROOT / "cours").resolve()


def main(argv=None):
    ap = argparse.ArgumentParser(description="One picture per course word.")
    ap.add_argument("target", nargs="?", help="a folder or a lesson (default: all of cours/)")
    ap.add_argument("--model", default="stabilityai/sd-turbo")
    ap.add_argument("--steps", type=int, default=2)
    ap.add_argument("--size", type=int, default=512)
    ap.add_argument("--limit", type=int, default=None, help="at most this many pictures (for a test)")
    args = ap.parse_args(argv)

    target = Path(args.target) if args.target else COURS
    if not target.exists():
        target = ROOT / args.target
    if not target.exists():
        raise SystemExit(f"Introuvable : {args.target}")
    target = target.resolve()                      # absolute, so the picture paths can be made relative to cours/
    files = [target] if target.is_file() else sorted(target.rglob("*.vocab.json"))
    turbo = any(key in args.model for key in ("turbo", "lightning"))

    pipe, drawn = None, 0
    for vocab in files:
        if not vocab.name.endswith(".vocab.json"):
            continue
        stem = vocab.name[: -len(".vocab.json")]
        items = json.loads(vocab.read_text(encoding="utf-8"))
        folder = vocab.parent / ("." + stem + ".images")
        changed = False
        for i, item in enumerate(items):
            scene = str(item.get("img") or "").strip()
            if not scene:
                continue
            path = folder / f"{i}.png"
            rel = path.relative_to(COURS).as_posix()
            if path.exists():
                if item.get("image") != rel:
                    item["image"], changed = rel, True
                continue
            if args.limit is not None and drawn >= args.limit:
                break
            if pipe is None:
                pipe, _ = gi.load_model(args.model)
            folder.mkdir(exist_ok=True)
            prompt = gi.build_prompt(scene, gi.PEOPLE, gi.STYLE)
            gi.draw(pipe, prompt, args.steps, args.size, turbo).save(path)
            item["image"], changed = rel, True
            drawn += 1
            print(f"  {item.get('word')} → {path.name}")
        if changed:
            vocab.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"{vocab.name} : {sum(1 for x in items if x.get('image'))} images")
    print(f"{drawn} image(s) dessinée(s).")


if __name__ == "__main__":
    main()

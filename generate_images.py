"""
generate_images.py — one picture per word, made on your own computer.

It reads episode.json, takes the scene Claude described for each word ("img"), and asks a local image model
to draw it. Pictures are saved as images/0.png, images/1.png… inside the episode folder; the number is the
word's position in the list. The server sends them to the app automatically.

Every picture gets the same three parts:  the scene  +  who the people are  +  the look.
  people (PEOPLE below): all people are women with very short hair, no makeup, in comfortable casual clothes.
  The scene is adjusted to match: "a man" becomes "a woman", "a dress" becomes "casual clothes"…
  A small model follows this most of the time, not every time.

First time
  pip install -r requirements-images.txt        (large: PyTorch and the image library)

Step 1: one test picture, timed
  python generate_images.py episodes/episode-01 --test

Step 2: all the words (pictures that are already right are skipped)
  python generate_images.py episodes/episode-01

Options
  --model stabilityai/sd-turbo   the model to use; the first run downloads it (a few GB)
  --steps 2                      drawing steps: turbo models need 1 to 4, ordinary ones 20 to 30
  --size 512                     width and height in pixels
  --limit 10                     only the first N pictures still to draw
  --redo                         draw again even if the picture exists
  --people "..."                 who the people in the pictures are
  --style "..."                  the look shared by all pictures

images/sources.json says, for every picture, which word and which description it was drawn from.
A picture is drawn again by itself when its word, its description, the people or the style changed,
and the server only shows a picture whose word there matches the word of today: after the word list
changes, a picture left under the same number is never shown on the wrong word.

Run for the first time on 14 Nov 2026 on a MacBook Pro M1 Pro, macOS 14, torch 2.14: about 11 s a picture.
"""
import argparse
import json
import os
import re
import sys
import time

STYLE = "flat vector illustration, simple shapes, soft colours, friendly children's book style, plain background, no text"
PEOPLE = "all people are women with very short cropped hair or a buzz cut, no makeup, wearing a comfortable t-shirt and trousers"

# the scene was written before the people were decided: make it agree with them
SWAPS = {"man": "woman", "men": "women", "boy": "girl", "boys": "girls", "guy": "woman", "guys": "women",
         "gentleman": "woman", "gentlemen": "women", "he": "she", "him": "her", "his": "her", "himself": "herself",
         "father": "mother", "dad": "mother", "grandfather": "grandmother", "grandpa": "grandmother",
         "brother": "sister", "son": "daughter", "uncle": "aunt", "husband": "partner", "boyfriend": "friend",
         "businessman": "businesswoman", "salesman": "saleswoman", "waiter": "waitress", "king": "queen",
         "dress": "casual clothes", "dresses": "casual clothes", "skirt": "trousers", "skirts": "trousers",
         "gown": "casual clothes", "makeup": "bare face", "lipstick": "bare face", "ponytail": "short hair",
         "braid": "short hair", "braids": "short hair"}
SWAP = re.compile(r"\b(" + "|".join(sorted(SWAPS, key=len, reverse=True)) + r")\b", re.IGNORECASE)
LONG_HAIR = re.compile(r"\b(?:with |and )?(?:long|curly|wavy|flowing|shoulder-length)(?:[ ,]+(?:long|curly|wavy|dark|blond|blonde|brown|black|red))* hair\b", re.IGNORECASE)


def feminise(scene):
    """The scene with its people turned into the women of PEOPLE: no men, no dresses, no long hair."""
    def swap(m):
        new = SWAPS[m.group(1).lower()]
        return new.capitalize() if m.group(1)[0].isupper() else new
    scene = LONG_HAIR.sub("", scene)
    scene = SWAP.sub(swap, scene)
    scene = re.sub(r"\b([Aa]) (?=[aeiouAEIOU])", lambda m: m.group(1) + "n ", scene)   # "a aunt" -> "an aunt"
    return re.sub(r"\s{2,}", " ", scene).replace(" ,", ",").replace(" .", ".").strip()


def build_prompt(scene, people, style):
    """What the model receives. The scene comes first: small models listen most to the beginning."""
    scene = feminise(scene) if people else scene
    return ". ".join(part.strip().rstrip(".") for part in (scene, people, style) if part and part.strip())


def load_model(name):
    import torch
    from diffusers import AutoPipelineForText2Image
    if torch.backends.mps.is_available():
        device, dtype = "mps", torch.float32          # Apple Silicon: half precision can give black pictures
    elif torch.cuda.is_available():
        device, dtype = "cuda", torch.float16
    else:
        device, dtype = "cpu", torch.float32
    print(f"Chargement du modèle {name} sur {device}… (le premier lancement le télécharge)")
    pipe = AutoPipelineForText2Image.from_pretrained(name, torch_dtype=dtype)
    pipe = pipe.to(device)
    pipe.enable_attention_slicing()                   # uses less memory, a little slower
    pipe.set_progress_bar_config(disable=True)
    return pipe, device


def draw(pipe, prompt, steps, size, turbo):
    # turbo and lightning models are trained to work without guidance; ordinary models need it
    result = pipe(prompt=prompt, num_inference_steps=steps, guidance_scale=0.0 if turbo else 7.0,
                  width=size, height=size)
    return result.images[0]


def main():
    ap = argparse.ArgumentParser(description="Dessine une image par mot, en local.")
    ap.add_argument("folder", help="le dossier de l'épisode, par exemple episodes/episode-01")
    ap.add_argument("--model", default="stabilityai/sd-turbo")
    ap.add_argument("--steps", type=int, default=2)
    ap.add_argument("--size", type=int, default=512)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--test", action="store_true", help="une seule image, chronométrée")
    ap.add_argument("--people", default=PEOPLE, help='qui sont les personnes dessinées ; "" pour ne rien imposer')
    ap.add_argument("--style", default=STYLE)
    args = ap.parse_args()

    path = os.path.join(args.folder, "episode.json")
    if not os.path.isfile(path):
        raise SystemExit(f"Pas de episode.json dans {args.folder}. Lance d'abord import_episode.py.")
    with open(path, encoding="utf-8") as f:
        vocab = json.load(f).get("vocab", [])
    todo = [(i, v) for i, v in enumerate(vocab) if v.get("img")]
    if not todo:
        raise SystemExit("Aucune description d'image dans episode.json.\n"
                         f"Lance : python import_episode.py {args.folder} --words-only")
    out = os.path.join(args.folder, "images")
    os.makedirs(out, exist_ok=True)
    register = os.path.join(out, "sources.json")
    try:                                                          # what the pictures already there were drawn from
        with open(register, encoding="utf-8") as f:
            known = {e["n"]: e for e in json.load(f) if isinstance(e, dict) and "n" in e}
    except (OSError, ValueError):
        known = {}
    wanted = {i: {"n": i, "word": v["word"], "file": f"{i}.png", "model": args.model,
                  "prompt": build_prompt(v["img"], args.people, args.style)} for i, v in todo}
    for i in [n for n in known if n not in wanted or not os.path.isfile(os.path.join(out, f"{n}.png"))]:
        del known[i]                                              # a word that is gone, or a picture that was deleted

    def up_to_date(i):
        return i in known and all(known[i].get(k) == wanted[i][k] for k in ("word", "prompt", "model"))

    if not args.redo:
        todo = [(i, v) for i, v in todo if not up_to_date(i)]
    if args.test:
        todo = todo[:1] or [(i, v) for i, v in enumerate(vocab) if v.get("img")][:1]
    elif args.limit:
        todo = todo[:args.limit]
    if not todo:
        with open(register, "w", encoding="utf-8") as f:
            json.dump([known[i] for i in sorted(known)], f, ensure_ascii=False, indent=1)
        print("Toutes les images sont à jour. Ajoute --redo pour les refaire.")
        return

    turbo = any(t in args.model.lower() for t in ("turbo", "lightning", "lcm"))
    start = time.time()
    pipe, device = load_model(args.model)
    print(f"  modèle prêt en {time.time() - start:.0f} s")

    times = []
    try:
        for n, (i, v) in enumerate(todo, 1):
            t = time.time()
            image = draw(pipe, wanted[i]["prompt"], args.steps, args.size, turbo)
            image.save(os.path.join(out, f"{i}.png"))
            known[i] = wanted[i]
            times.append(time.time() - t)
            print(f"  {n}/{len(todo)}  {v['word']}  ({times[-1]:.0f} s)", flush=True)
    finally:                                                      # even after Ctrl+C, the pictures made are registered
        with open(register, "w", encoding="utf-8") as f:
            json.dump([known[i] for i in sorted(known)], f, ensure_ascii=False, indent=1)
    if not times:
        return

    mean = sum(times) / len(times)
    print(f"Images dans {out}")
    if args.test:
        left = sum(1 for i in wanted if not up_to_date(i))
        print(f"Une image : {mean:.0f} s sur {device}. Pour les {left} autres images à faire : environ {mean * left / 60:.0f} min.")
        print("Regarde l'image. Si elle te convient, relance sans --test.")
    else:
        print(f"{len(todo)} images, {mean:.0f} s en moyenne.")


if __name__ == "__main__":
    sys.exit(main())

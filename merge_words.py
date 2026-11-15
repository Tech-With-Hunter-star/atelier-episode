"""Merge one dictionary word into another, keeping every use and every "known" mark.
Usage: python merge_words.py OLD_ID NEW_ID   (ids from the lexicon table)"""
import sys

from pathlib import Path

import db


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass
    old, new = int(sys.argv[1]), int(sys.argv[2])
    with db.connect() as conn:
        for row in conn.execute("select id, lemma, display from lexicon where id in (%s, %s)", (old, new)).fetchall():
            print(f"  {row[0]}: {row[2]} ({row[1]})")
        moved = db.apply_merges(conn, {old: new})
        conn.commit()
    print(f"{moved} mot(s) fusionné(s) : {old} -> {new}")


if __name__ == "__main__":
    main()

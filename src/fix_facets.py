#!/usr/bin/env python3
"""
Recompute navigation facets on an existing library.db.

Two fixes over the first pass:

1. Word-boundary bug. `\\b(2AC)\\b` does not match "2AC_Fish_NCX", because "_"
   is a word character, so there is no boundary between "C" and "_". Filenames
   here are underscore-separated throughout, so position matching silently hit
   only 8% of files. Using explicit (?<![A-Za-z0-9]) lookarounds fixes it.

2. The manifest. `not word/_original_locations.txt` records the folder each
   file came from before flattening. That hierarchy encodes topic and season
   far more reliably than filenames do -- Policy_Debate/Topics/Arctic_2025-26
   gives division, topic and year in one path. Recovering it restores the
   organisation the flattening removed, as metadata rather than folders.

Usage: python3 fix_facets.py [--db library.db] [--manifest ...]
"""

import argparse
import collections
import os
import re
import sqlite3

SEP = r"(?<![A-Za-z0-9])"
END = r"(?![A-Za-z0-9])"

POSITIONS = ["1AC", "2AC", "1NC", "2NC", "1AR", "2AR", "1NR", "2NR"]
POSITION_PAT = [(re.compile(SEP + p + END, re.I), p) for p in POSITIONS]

ARGTYPE_PAT = [
    (re.compile(SEP + r"(?:DA|Disad)" + END, re.I),                "Disadvantage"),
    (re.compile(SEP + r"CP" + END + r"|\bcounterplan\b", re.I),    "Counterplan"),
    (re.compile(SEP + r"K" + END + r"|\bkritik\b", re.I),          "Kritik"),
    (re.compile(SEP + r"T" + END + r"|\btopicality\b", re.I),      "Topicality"),
    (re.compile(SEP + r"(?:AT|A2)" + END, re.I),                   "Answers To"),
    (re.compile(SEP + r"AFF" + END, re.I),                         "Aff"),
    (re.compile(SEP + r"(?:NEG|CaseNeg|1NR)" + END, re.I),         "Neg"),
    (re.compile(r"\bdrill", re.I),                                 "Drill"),
    (re.compile(r"\bimpacts?\b", re.I),                            "Impacts"),
    (re.compile(r"\blesson|\blecture|\bclass\b|\bteach", re.I),    "Teaching"),
    (re.compile(r"\bflow(?:ing)?\b", re.I),                        "Flowing"),
    (re.compile(r"\bfeedback\b|\bsurvey\b|\brubric\b", re.I),      "Feedback"),
    (re.compile(r"\btemplate\b|\bblank\b", re.I),                  "Template"),
    (re.compile(r"\bround\b|\bflows?\b|\bvs?\.?\b", re.I),         "Round"),
]

FORMAT_PAT = [
    (re.compile(SEP + r"PF" + END + r"|\bpublic\s*forum\b", re.I), "Public Forum"),
    (re.compile(SEP + r"SD" + END + r"|\bsmart\s*debate\b", re.I), "Smart Debate"),
    (re.compile(SEP + r"LD" + END + r"|\blincoln", re.I),          "Lincoln-Douglas"),
    (re.compile(SEP + r"(?:CX|NCX|VCX|JVCX|Policy)" + END, re.I),  "Policy"),
]

# Division names as they appeared at the top of the original tree.
DIVISION = {
    "Policy_Debate": "Policy",
    "Public_Forum": "Public Forum",
    "Smart_Debate": "Smart Debate",
    "Classroom_Materials": "Policy",
    "ADL Gabe": "Policy",
}

YEAR_IN_PATH = re.compile(r"(20\d{2})[-–_](\d{2})|(20\d{2})|['’](\d{2})[-–]['’](\d{2})")

# Only 122 of 1,296 files sat under a Topics/ folder. The rest of the tree
# encoded speech position and argument type instead, so those folders are
# mapped to the facet they actually describe rather than being forced into a
# "topic" label that would read as noise ("Dump", "DAs", "2AC").
FOLDER_POSITION = {
    "1AC": "1AC", "2AC": "2AC", "1NC": "1NC", "2NC": "2NC",
    "1AR": "1AR", "2AR": "2AR", "1NR": "1NR", "2NR": "2NR",
}
FOLDER_ARGTYPE = {
    "DAs": "Disadvantage",
    "T_Blocks": "Topicality",
    "AFF_Blocks": "Aff",
    "Drill_Docs": "Drill",
    "Drills": "Drill",
    "K Answer Backfiles": "Kritik",
    "Impacts": "Impacts",
    "Cards_Backfiles": "Cards",
    "Round_Flows": "Round",
    "Speeches": "Round",
    "Lessons": "Teaching",
    "Lectures": "Teaching",
    "Scripts": "Teaching",
    "Templates": "Template",
    "Gamification": "Teaching",
    "Novice Files": "Teaching",
}
# Folder names that describe filing, not subject matter.
STOP_TOPIC = {
    "Topics", "Misc", "Generic", "Undated", "Unknown", "Condo", "WIP", "Dump",
    "Lit", "CX", "PF", "Media", "Resources", "Gabe's Files", "Debate Toolbox",
    "ADL Tools", "ADL Class DT", "user_approval", "duplicates", "scripts",
} | set(FOLDER_POSITION) | set(FOLDER_ARGTYPE)


def norm_year(m):
    if not m:
        return None
    if m.group(1):
        return f"{m.group(1)}-{m.group(2)}"
    if m.group(3):
        return m.group(3)
    if m.group(4):
        return f"20{m.group(4)}-{m.group(5)}"
    return None


def load_manifest(path):
    """Map final filename stem -> original relative folder path."""
    out = {}
    if not os.path.exists(path):
        print(f"  (manifest not found at {path} -- skipping path facets)")
        return out
    cur_old = None
    for line in open(path, encoding="utf-8", errors="replace"):
        s = line.rstrip("\n")
        if s.startswith("    -> "):
            if cur_old:
                new = s[7:].split("   [")[0].strip()
                stem = os.path.splitext(os.path.basename(new))[0]
                folder = os.path.dirname(cur_old)
                if folder and folder != ".":
                    out[stem] = folder
            cur_old = None
        elif s and not s.startswith(("=", "Original", "Format")):
            cur_old = s.strip()
    return out


def topic_from_path(folder):
    """
    Topic, year, and any position/argtype the folder implies.

    A real topic is the segment directly under a "Topics" folder
    (Policy_Debate/Topics/Arctic_2025-26 -> "Arctic", "2025-26"). Anywhere
    else, the folder is describing how the file was filed, not what it argues,
    so no topic is claimed rather than inventing a misleading one.
    """
    if not folder:
        return None, None, None, None
    parts = [p for p in folder.split(os.sep) if p and not p.startswith(".")]

    year = None
    for p in reversed(parts):
        y = norm_year(YEAR_IN_PATH.search(p))
        if y:
            year = y
            break

    pos = arg = None
    for p in reversed(parts):
        if pos is None and p in FOLDER_POSITION:
            pos = FOLDER_POSITION[p]
        if arg is None and p in FOLDER_ARGTYPE:
            arg = FOLDER_ARGTYPE[p]

    topic = None
    for i, p in enumerate(parts):
        if p == "Topics" and i + 1 < len(parts):
            base = YEAR_IN_PATH.sub("", parts[i + 1]).strip("_- ")
            base = base.replace("_", " ").strip()
            if base and base not in STOP_TOPIC and len(base) >= 3:
                topic = base[:40]
            break

    return topic, year, pos, arg


def collection_from_path(folder):
    """Honest browse axis: the original folder, lightly cleaned."""
    if not folder:
        return None
    parts = [p for p in folder.split(os.sep) if p and not p.startswith(".")]
    if not parts:
        return None
    return " / ".join(p.replace("_", " ") for p in parts[:2])[:48]


def division_from_path(folder):
    if not folder:
        return None
    top = folder.split(os.sep)[0]
    return DIVISION.get(top)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="library.db")
    ap.add_argument("--manifest", default="../not word/_original_locations.txt")
    args = ap.parse_args()

    man = load_manifest(args.manifest)
    print(f"manifest entries: {len(man):,}")

    db = sqlite3.connect(args.db)
    cols = {r[1] for r in db.execute("PRAGMA table_info(files)")}
    for c, t in (("topic", "TEXT"), ("division", "TEXT"),
                 ("orig_folder", "TEXT"), ("collection", "TEXT")):
        if c not in cols:
            db.execute(f"ALTER TABLE files ADD COLUMN {c} {t}")

    rows = db.execute("SELECT file_id, stem FROM files").fetchall()
    stats = collections.Counter()
    for fid, stem in rows:
        folder = man.get(stem)
        topic, pyear, fpos, farg = topic_from_path(folder)
        div = division_from_path(folder) if folder else None
        coll = collection_from_path(folder)

        # Filename wins when it says something; the folder fills the gaps.
        pos = next((v for p, v in POSITION_PAT if p.search(stem)), None) or fpos
        arg = next((v for p, v in ARGTYPE_PAT if p.search(stem)), None) or farg
        fmt = next((v for p, v in FORMAT_PAT if p.search(stem)), None) or div or "Policy"
        yr = norm_year(YEAR_IN_PATH.search(stem)) or pyear

        db.execute(
            "UPDATE files SET position=?, argtype=?, format=?, year=?, topic=?,"
            " division=?, orig_folder=?, collection=? WHERE file_id=?",
            (pos, arg, fmt, yr, topic, div, folder, coll, fid),
        )
        for k, v in (("position", pos), ("argtype", arg), ("year", yr),
                     ("topic", topic), ("division", div), ("collection", coll)):
            if v:
                stats[k] += 1

    db.commit()
    n = len(rows)
    print(f"\nCOVERAGE ({n:,} files)")
    for k in ("division", "collection", "argtype", "position", "year", "topic"):
        print(f"  {k:<11} {stats[k]:>5,}  ({stats[k]/n*100:.0f}%)")

    print("\nARGTYPE")
    for t, c in db.execute("SELECT COALESCE(argtype,'(unlabelled)'), COUNT(*) c FROM files"
                           " GROUP BY 1 ORDER BY c DESC LIMIT 12"):
        print(f"  {t:<22} {c:>5}")
    print("\nTOP COLLECTIONS")
    for t, c in db.execute("SELECT collection, COUNT(*) c FROM files WHERE collection"
                           " IS NOT NULL GROUP BY 1 ORDER BY c DESC LIMIT 10"):
        print(f"  {t:<38} {c:>5}")
    print("\nREAL TOPICS (from Topics/ folders only)")
    for t, c in db.execute("SELECT topic, COUNT(*) c FROM files WHERE topic IS NOT NULL"
                           " GROUP BY 1 ORDER BY c DESC LIMIT 10"):
        print(f"  {t:<32} {c:>4}")
    db.close()


if __name__ == "__main__":
    main()

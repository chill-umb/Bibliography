#!/usr/bin/env python3
"""
Single-script replacement for the old DBLP -> Mendeley -> library.bib workflow.

Workflow:
  1. Paste DBLP bibtex entries into new-workflow/new.bib (as-is, with their DBLP keys).
  2. Run:  python3 new-workflow/prepare_bib.py
  3. The script:
       - cleans/normalizes each entry in new.bib (strips DBLP-only fields,
         expands venue names, normalizes dashes, escapes LaTeX-special
         characters in url/doi/title/author/etc., normalizes whitespace)
       - generates a unique "Lastname+Year[+letter]" citation key for each entry
       - warns about likely duplicates (fuzzy title match against library.bib)
         and asks for confirmation before skipping/keeping them
       - prepends the cleaned entries to the top of new-workflow/library.bib
         WITHOUT touching/reformatting any existing entry in that file
       - empties new-workflow/new.bib (a temporary backup is kept under
         new-workflow/processed/ only while the script is running, then
         removed -- it does not persist between runs)
       - regenerates the derived libraries: library-short.bib, library-no-url.bib,
         library-no-doi.bib, library-no-url-short.bib

Everything this script reads and writes lives under new-workflow/. It never
touches the top-level library.bib or any other file outside this folder.

Setup (one-time): pip3 install -r requirements.txt
"""

import os
import re
import sys
import shutil
import datetime

try:
    import bibtexparser
    from bibtexparser.bparser import BibTexParser
    from fuzzywuzzy import fuzz
except ImportError as exc:
    sys.exit(
        f"Missing dependency ({exc.name}). Install requirements first:\n"
        f"    pip3 install -r requirements.txt\n"
        f"(run from inside the new-workflow/ directory)"
    )

# ---------------------------------------------------------------------------
# Paths (everything confined to new-workflow/)
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
NEW_BIB_PATH = os.path.join(HERE, "new.bib")
LIBRARY_PATH = os.path.join(HERE, "library.bib")
RECIPES_PATH = os.path.join(HERE, "data", "dblp2daslab.recipes")
ADDITIONAL_RECIPES_PATH = os.path.join(HERE, "data", "additional_recipies")
PROCESSED_DIR = os.path.join(HERE, "processed")

LIBRARY_SHORT_PATH = os.path.join(HERE, "library-short.bib")
LIBRARY_NO_URL_PATH = os.path.join(HERE, "library-no-url.bib")
LIBRARY_NO_DOI_PATH = os.path.join(HERE, "library-no-doi.bib")
LIBRARY_NO_URL_SHORT_PATH = os.path.join(HERE, "library-no-url-short.bib")

DUPLICATE_SCORE_THRESHOLD = 80  # same threshold the old prepare_bibtex.py used

# Fields that DBLP exports but that we don't want to keep in the library.
FIELDS_TO_STRIP = ["crossref", "timestamp", "biburl", "bibsource"]

# Human-readable fields that get whitespace/dash normalization AND
# LaTeX-special-character escaping, since they're typeset as running text.
TEXT_FIELDS = ["title", "booktitle", "journal", "author", "publisher", "series", "note"]

# Fields holding a URL/DOI identifier. These also need LaTeX-special-character
# escaping (see escape_latex_specials below) since the bibliography style may
# not wrap them in a verbatim \url{...} macro -- but they don't get dash
# normalization or brace-stripping like the human-readable text fields above.
URL_LIKE_FIELDS = ["url", "doi"]


# ---------------------------------------------------------------------------
# Step 0: load venue-name recipes
# ---------------------------------------------------------------------------

def load_recipes(path):
    """Load a '|' separated long<->short mapping file into a dict."""
    mapping = {}
    if not os.path.exists(path):
        return mapping
    with open(path, encoding="utf-8") as f:
        for line in f:
            # .rstrip() (not just "\n") so a stray trailing "\r" -- e.g. if
            # this file is ever edited/saved on Windows -- can't end up
            # silently embedded in a venue name.
            line = line.rstrip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) >= 2:
                mapping[parts[0].strip()] = parts[1].strip()
    return mapping


# ---------------------------------------------------------------------------
# Step 1: text normalization helpers
# ---------------------------------------------------------------------------

# Unicode dash-like characters we want to normalize to plain ASCII, so the
# .bib file behaves consistently regardless of the LaTeX engine/encoding
# Overleaf (or any other LaTeX toolchain) ends up using.
EN_DASH = "–"          # –
EM_DASH = "—"          # —
NB_HYPHEN = "‑"        # ‑  (non-breaking hyphen)
FIGURE_DASH = "‒"      # ‒
HORIZONTAL_BAR = "―"   # ―
HYPHEN_UNI = "‐"       # ‐

OTHER_DASHES = [HYPHEN_UNI, NB_HYPHEN, FIGURE_DASH, HORIZONTAL_BAR]


def normalize_dashes(text):
    """
    Normalize unicode dash characters to plain ASCII, using BibTeX's own
    convention so the result still typesets correctly in LaTeX/Overleaf:
      - "--" for ranges (e.g. page/year ranges: 2016--2021)
      - " --- " for em-dash style phrase separators
      - "-" for plain hyphenation
    """
    if not text:
        return text

    # en-dash between two digits (e.g. "2016–2021") -> "--" (BibTeX range dash)
    text = re.sub(r"(?<=\d)" + EN_DASH + r"(?=\d)", "--", text)
    # en-dash used as a spaced phrase separator -> " -- "
    text = re.sub(r"\s*" + EN_DASH + r"\s*", " -- ", text)

    # em-dash used as a phrase separator -> " --- "
    text = re.sub(r"\s*" + EM_DASH + r"\s*", " --- ", text)

    # any remaining "exotic" dash characters are just plain hyphens
    for ch in OTHER_DASHES:
        text = text.replace(ch, "-")

    # Collapse any run of 3+ plain ASCII hyphens (e.g. a malformed
    # "140:1----140:27" page range) down to BibTeX's double-hyphen range
    # dash. This is the exact typo found (and manually fixed 85 times) in
    # the trusted library.bib, so new entries must not be able to reproduce
    # it.
    text = re.sub(r"-{3,}", "--", text)

    return text


def clean_pages_field(text):
    """
    Pages get the usual whitespace/dash normalization, plus one more rule
    specific to this field: BibTeX's convention is always a double-hyphen
    for a page range (e.g. "143--154"), never a single hyphen. DBLP itself
    always exports double-hyphen, but a manually-typed or pasted-from-
    elsewhere page range could use a single one, so upgrade it here too.
    """
    text = normalize_whitespace(text)
    text = normalize_dashes(text)
    # A lone single hyphen between digits -> "--". The negative lookbehind/
    # lookahead make sure this doesn't touch a hyphen that's already part of
    # a "--" pair (normalize_dashes runs first, so by this point any
    # malformed 3+ run is already collapsed to exactly "--").
    text = re.sub(r"(?<=\d)(?<!-)-(?!-)(?=\d)", "--", text)
    return text


def normalize_whitespace(text):
    """Collapse DBLP's hard-wrapped multi-line field values into one line."""
    if not text:
        return text
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def strip_braces(text):
    if not text:
        return text
    return text.replace("{", "").replace("}", "")


def escape_latex_specials(text):
    """
    "_", "#", "%", "&", and "~" are special/active characters in LaTeX text
    mode (e.g. unescaped "_" triggers math mode, unescaped "%" comments out
    the rest of the line). Any field that gets typeset as text -- titles,
    venue names, author names, AND url/doi (since the bibliography style may
    not wrap them in a verbatim \\url{...} macro) -- needs these escaped.
    This is exactly the convention already used throughout library.bib, e.g.
    `title = {{HyPer: A Hybrid OLTP {\\&} OLAP ...}}` and
    `url = {...CIDR13{\\_}Paper118.pdf}`.

    This first undoes any escaping already present, then re-applies it
    uniformly, so it's idempotent regardless of whether the input was
    already escaped, partially escaped, or raw.
    """
    if not text:
        return text

    # Undo any existing escaping so re-escaping below can't double-escape.
    text = text.replace("{\\_}", "_")
    text = text.replace("\\_", "_")
    text = text.replace("{\\&}", "&")
    text = text.replace("\\&", "&")
    text = text.replace("{\\%}", "%")
    text = text.replace("\\%", "%")
    text = text.replace("{\\#}", "#")
    text = text.replace("\\#", "#")
    text = text.replace("{~}", "~")

    # Re-apply the escaping, matching the convention already used throughout
    # library.bib (and DBLP's own bibtex export).
    text = text.replace("_", "{\\_}")
    text = text.replace("#", "{\\#}")
    text = text.replace("%", "{\\%}")
    text = text.replace("&", "{\\&}")
    text = text.replace("~", "{~}")
    return text


def normalize_text_field(text):
    """Whitespace/dash normalization only -- no LaTeX-special escaping yet.

    Used for booktitle/journal so venue-name matching (apply_venue_replacement)
    runs against unescaped text, matching the convention of
    data/dblp2daslab.recipes (whose keys use bare "\\&", not "{\\&}"). If
    escaping were applied first, a DBLP-escaped "{\\&}" would no longer match
    the recipe's "\\&" key and the venue replacement would silently fail.
    """
    text = normalize_whitespace(text)
    text = normalize_dashes(text)
    return text


def clean_text_field(text):
    text = normalize_text_field(text)
    text = escape_latex_specials(text)
    return text


def clean_url_like_field(text):
    text = normalize_whitespace(text)
    text = escape_latex_specials(text)
    return text


# ---------------------------------------------------------------------------
# Step 2: entry cleaning
# ---------------------------------------------------------------------------

def apply_venue_replacement(value, recipes):
    """
    Some recipe keys are substrings of other recipe keys (e.g. "Conference
    on Innovative Data Systems" is a substring of "Biennial Conference on
    Innovative Data Systems") -- matching on insertion order would mean the
    result silently depends on which one happens to come first in the
    recipes file. Instead, always prefer the longest matching key, since
    that's the most specific match.
    """
    best_short_name = None
    best_long_name_len = -1
    for long_name, short_name in recipes.items():
        if long_name in value and len(long_name) > best_long_name_len:
            best_short_name = short_name
            best_long_name_len = len(long_name)
    return best_short_name if best_short_name is not None else value


def clean_entry(entry, recipes):
    """
    Clean a single bibtexparser entry dict in place. Mirrors (and extends)
    the cleanup performed by the old scripts/prepare_bibtex.py.
    """
    # Drop DBLP-only bookkeeping fields.
    for field in FIELDS_TO_STRIP:
        entry.pop(field, None)

    # Title: strip protective braces DBLP adds, normalize whitespace/dashes,
    # escape LaTeX specials. NOTE: the extra protective double-brace wrap
    # (`title = {{...}}`, matching 2364/2374 existing entries) is applied
    # later, in render_entry, at serialization time only -- not here. If it
    # were added here, the braces would leak into entry["title"] and corrupt
    # the fuzzy duplicate-title comparison and the key-generation step that
    # both run on this same entry dict afterwards.
    if "title" in entry:
        entry["title"] = strip_braces(entry["title"])
        entry["title"] = clean_text_field(entry["title"])

    if "author" in entry:
        entry["author"] = clean_text_field(entry["author"])

    if entry.get("ENTRYTYPE") == "inproceedings" and "booktitle" in entry:
        entry["booktitle"] = strip_braces(entry["booktitle"])
        entry["booktitle"] = normalize_text_field(entry["booktitle"])
        entry["booktitle"] = apply_venue_replacement(entry["booktitle"], recipes)
        entry["booktitle"] = escape_latex_specials(entry["booktitle"])
        entry.pop("editor", None)
        entry.pop("publisher", None)

    if entry.get("ENTRYTYPE") == "article" and "journal" in entry:
        entry["journal"] = strip_braces(entry["journal"])
        entry["journal"] = normalize_text_field(entry["journal"])
        entry["journal"] = apply_venue_replacement(entry["journal"], recipes)
        entry["journal"] = escape_latex_specials(entry["journal"])

    if "publisher" in entry:
        entry["publisher"] = strip_braces(entry["publisher"])
        entry["publisher"] = clean_text_field(entry["publisher"])

    if "series" in entry:
        entry["series"] = clean_text_field(entry["series"])

    if "note" in entry:
        entry["note"] = clean_text_field(entry["note"])

    for field in URL_LIKE_FIELDS:
        if field in entry:
            entry[field] = clean_url_like_field(entry[field])

    if "pages" in entry:
        entry["pages"] = clean_pages_field(entry["pages"])

    return entry


# ---------------------------------------------------------------------------
# Step 3: unique key generation (Lastname+Year[+letter], the convention
# already used throughout library.bib)
# ---------------------------------------------------------------------------

def first_author_lastname(author_field):
    first_author = author_field.split(" and ")[0].strip()
    if "," in first_author:
        lastname = first_author.split(",")[0].strip()
    else:
        parts = first_author.split(" ")
        lastname = parts[-1].strip()
    # Keys must not contain braces/accents markup; keep it simple/ASCII-safe.
    lastname = strip_braces(lastname)
    lastname = re.sub(r"[^A-Za-z0-9]", "", lastname)
    return lastname


def make_unique_key(entry, existing_keys):
    lastname = first_author_lastname(entry.get("author", "Unknown"))
    year = entry.get("year", "").strip()
    base_key = f"{lastname}{year}"

    if base_key not in existing_keys:
        return base_key

    for letter_code in range(ord("a"), ord("z") + 1):
        candidate = f"{base_key}{chr(letter_code)}"
        if candidate not in existing_keys:
            return candidate

    raise RuntimeError(f"Could not generate a unique key for base '{base_key}'")


# ---------------------------------------------------------------------------
# Step 4: rendering a cleaned entry back to bibtex text (used only for the
# NEW entries being prepended -- existing entries in library.bib are never
# re-rendered, so the rest of the file is byte-for-byte untouched).
# ---------------------------------------------------------------------------

FIELD_ORDER = [
    "author", "title", "booktitle", "journal", "year", "volume", "number",
    "pages", "publisher", "series", "note", "doi", "url",
]


def render_entry(entry):
    entry_type = entry["ENTRYTYPE"]
    key = entry["ID"]
    lines = [f"@{entry_type}{{{key},"]

    seen = set()
    ordered_fields = [f for f in FIELD_ORDER if f in entry] + \
                      [f for f in entry if f not in FIELD_ORDER and f not in ("ENTRYTYPE", "ID")]
    for field in ordered_fields:
        if field in seen or field in ("ENTRYTYPE", "ID"):
            continue
        seen.add(field)
        value = entry[field]
        if field == "title":
            # Extra protective brace pair around the whole title, matching
            # the convention used by 2364/2374 existing entries
            # (`title = {{...}}`). Applied here (serialization time) only,
            # so it never leaks into duplicate-matching or key generation.
            value = "{" + value + "}"
        lines.append(f"  {field} = {{{value}}},")

    # remove trailing comma on the last field line
    if len(lines) > 1:
        lines[-1] = lines[-1].rstrip(",")
    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 5: duplicate detection (same fuzzy-title heuristic as before)
# ---------------------------------------------------------------------------

def yes_or_no(question):
    while True:
        reply = input(question + " (y/n): ").strip().lower()
        if reply.startswith("y"):
            return True
        if reply.startswith("n"):
            return False
        print("Please answer y or n.")


def check_for_duplicate(entry, existing_entries):
    """Returns True if the user wants to SKIP adding this (likely-duplicate) entry."""
    if not existing_entries:
        return False
    # Compare on brace-stripped text only. Existing double-braced entries
    # parse with one residual brace pair still in the in-memory title
    # (e.g. "{AgentTune: ...}"), while the new entry's title at this point
    # has none (its protective brace pair isn't added until render_entry).
    # Without stripping both sides, that's 2 characters of pure markup noise
    # added to the Levenshtein distance on one side only, which can shift
    # the similarity score for borderline near-duplicates.
    new_title = strip_braces(entry["title"])
    scores = [fuzz.ratio(strip_braces(existing.get("title", "")), new_title) for existing in existing_entries]
    max_score = max(scores) if scores else 0
    print(f'For entry "{entry["title"]}" max similarity score vs. library.bib: {max_score}')
    if max_score < DUPLICATE_SCORE_THRESHOLD:
        return False

    positions = [i for i, s in enumerate(scores) if s == max_score]
    print("***************************************")
    print(f"WARNING!! POSSIBLE DUPLICATE FOUND ({max_score}% title similarity).")
    print(f'New entry: "{entry["title"]}"')
    print("Matches existing entry(ies) in library.bib:")
    for p in positions:
        ex = existing_entries[p]
        print(f"  - [{ex.get('ID', '?')}] {ex.get('title', '?')}")
    print("***************************************")
    return not yes_or_no("Keep this NEW entry anyway (add it to library.bib)?")


# ---------------------------------------------------------------------------
# Step 6: main clean + merge pipeline
# ---------------------------------------------------------------------------

def parse_bib_file(path):
    parser = BibTexParser(common_strings=True)
    parser.ignore_nonstandard_types = False
    with open(path, encoding="utf-8") as f:
        return bibtexparser.load(f, parser=parser)


def get_existing_keys(library_raw_text):
    return set(re.findall(r"^@\w+\{\s*([^,\s]+)\s*,", library_raw_text, flags=re.MULTILINE))


def run_clean_and_merge():
    if not os.path.exists(NEW_BIB_PATH) or not os.path.getsize(NEW_BIB_PATH):
        print(f"'{NEW_BIB_PATH}' is empty -- nothing to do.")
        return False

    if not os.path.exists(LIBRARY_PATH):
        print(f"ERROR: '{LIBRARY_PATH}' does not exist. Aborting.")
        sys.exit(1)

    recipes = load_recipes(RECIPES_PATH)

    with open(LIBRARY_PATH, encoding="utf-8") as f:
        library_raw_text = f.read()
    existing_keys = get_existing_keys(library_raw_text)

    existing_db = parse_bib_file(LIBRARY_PATH)
    existing_entries = existing_db.entries

    new_db = parse_bib_file(NEW_BIB_PATH)

    # bibtexparser silently routes anything it can't parse as a proper entry
    # (e.g. a missing closing brace) into .comments instead of raising an
    # error. Without this check, a malformed paste would be dropped with NO
    # warning at all -- the run would report success for the entries that
    # did parse and the student would have no idea a paper went missing.
    if new_db.comments:
        print("\n!!! WARNING: found content in new.bib that could NOT be parsed as a valid bibtex entry !!!")
        print("It will NOT be added. Check for a missing brace/comma and re-paste it. Unparsed content:")
        for c in new_db.comments:
            preview = c.strip().replace("\n", " ")
            if len(preview) > 200:
                preview = preview[:200] + "..."
            print(f"  - {preview}")
        print()

    if not new_db.entries:
        print(f"No entries found in '{NEW_BIB_PATH}'. Nothing to do.")
        return False

    rendered_blocks = []
    skipped = []
    added = []

    for entry in new_db.entries:
        # title is required for cleaning/dup-check; skip (with a warning) if missing.
        if "title" not in entry:
            print(f"WARNING: entry '{entry.get('ID', '?')}' has no title field -- skipping.")
            skipped.append(entry.get("ID", "?"))
            continue

        entry = clean_entry(entry, recipes)

        if check_for_duplicate(entry, existing_entries):
            print(f'Skipping "{entry["title"]}" (flagged as duplicate).')
            skipped.append(entry.get("ID", "?"))
            continue

        key = make_unique_key(entry, existing_keys)
        existing_keys.add(key)
        entry["ID"] = key

        rendered_blocks.append(render_entry(entry))
        added.append(key)
        # Also treat newly-added entries as "existing" for duplicate-checking
        # the rest of this same batch.
        existing_entries.append(entry)

    if not rendered_blocks:
        print("No entries were added to library.bib.")
    else:
        new_text = "\n\n".join(rendered_blocks) + "\n\n" + library_raw_text
        with open(LIBRARY_PATH, "w", encoding="utf-8") as f:
            f.write(new_text)
        print(f"\nAdded {len(rendered_blocks)} entrie(s) to the top of '{LIBRARY_PATH}':")
        for k in added:
            print(f"  - {k}")

    # Archive the processed new.bib, then clear it for the next batch.
    os.makedirs(PROCESSED_DIR, exist_ok=True)
    if rendered_blocks or skipped:
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = os.path.join(PROCESSED_DIR, f"new-{timestamp}.bib")
        shutil.copyfile(NEW_BIB_PATH, backup_path)
        print(f"Backed up processed new.bib to '{backup_path}'.")

    with open(NEW_BIB_PATH, "w", encoding="utf-8") as f:
        f.write("")
    print(f"Cleared '{NEW_BIB_PATH}' (ready for the next batch).")

    if skipped:
        print(f"Skipped {len(skipped)} entrie(s): {', '.join(skipped)}")

    return bool(rendered_blocks)


# ---------------------------------------------------------------------------
# Step 7: derived/additional libraries
# ---------------------------------------------------------------------------

def comment_out_fields(raw_text, field_names):
    pattern = re.compile(
        r"^(\s*)(" + "|".join(re.escape(f) for f in field_names) + r")(\s*=)",
        flags=re.MULTILINE,
    )
    return pattern.sub(lambda m: f"{m.group(1)}//{m.group(2)}{m.group(3)}", raw_text)


def build_no_url_library(library_raw_text):
    return comment_out_fields(library_raw_text, ["url", "doi"])


def build_no_doi_library(library_raw_text):
    return comment_out_fields(library_raw_text, ["doi"])


def build_short_venue_map():
    """
    Long venue name -> short form, derived from:
      1. the curated additional_recipies file (most reliable), then
      2. journal/booktitle values in the CURRENT library.bib that already
         contain a parenthetical abbreviation, e.g. "... (SIGMOD)".
    additional_recipies entries take priority on conflicts.
    """
    mapping = {}

    db = parse_bib_file(LIBRARY_PATH)
    paren_re = re.compile(r"\(([^()]+)\)\s*$")
    for entry in db.entries:
        for field in ("journal", "booktitle"):
            if field not in entry:
                continue
            value = strip_braces(entry[field]).strip()
            m = paren_re.search(value)
            if not m:
                continue
            candidate = m.group(1).strip()
            # Sanity check: a real venue abbreviation is short (e.g. "SIGMOD",
            # "IEEE DEBULL"). Some trailing parentheticals are explanatory
            # text, not an abbreviation -- e.g. one real entry's series field
            # is "Lecture Notes in Computer Science (including subseries
            # Lecture Notes in Artificial Intelligence and Lecture Notes in
            # Bioinformatics)", where the "abbreviation" would actually be
            # longer than the original. Reject anything that isn't short.
            if len(candidate) > 30 or len(candidate.split()) > 5:
                continue
            mapping.setdefault(value, candidate)

    additional = load_recipes(ADDITIONAL_RECIPES_PATH)
    mapping.update(additional)  # curated recipes win over the auto-detected ones
    return mapping


def build_short_library():
    """
    Recreates the intent of the (previously broken, Python-2-only)
    scripts/shorten_papers*.py: abbreviate journal/booktitle venue names and
    drop the `pages` field from inproceedings entries. Unlike the old script,
    this only rewrites the journal/booktitle field values (not a blind
    string-replace over the whole file), so it can't accidentally corrupt
    titles/authors that happen to contain a venue's long name as a substring.
    """
    venue_map = build_short_venue_map()
    db = parse_bib_file(LIBRARY_PATH)

    for entry in db.entries:
        for field in ("journal", "booktitle"):
            if field in entry:
                value = strip_braces(entry[field]).strip()
                if value in venue_map:
                    entry[field] = venue_map[value]
        if entry.get("ENTRYTYPE") == "inproceedings":
            entry.pop("pages", None)

    # Don't let bibtexparser re-serialize stray top-of-file comments (e.g. a
    # leftover "Automatically generated by Mendeley..." note) as a literal
    # @comment{...} entry -- it isn't a real bibtex entry and the other
    # derived files (built from raw text) don't fabricate one either.
    db.comments = []

    return bibtexparser.dumps(db)


def run_populate_additional_libraries():
    if not os.path.exists(LIBRARY_PATH):
        print(f"ERROR: '{LIBRARY_PATH}' does not exist. Aborting.")
        sys.exit(1)

    with open(LIBRARY_PATH, encoding="utf-8") as f:
        library_raw_text = f.read()

    print("Building library-no-url.bib ...")
    with open(LIBRARY_NO_URL_PATH, "w", encoding="utf-8") as f:
        f.write(build_no_url_library(library_raw_text))

    print("Building library-no-doi.bib ...")
    with open(LIBRARY_NO_DOI_PATH, "w", encoding="utf-8") as f:
        f.write(build_no_doi_library(library_raw_text))

    print("Building library-short.bib ...")
    short_text = build_short_library()
    with open(LIBRARY_SHORT_PATH, "w", encoding="utf-8") as f:
        f.write(short_text)

    print("Building library-no-url-short.bib ...")
    with open(LIBRARY_NO_URL_SHORT_PATH, "w", encoding="utf-8") as f:
        f.write(build_no_url_library(short_text))

    print("Done populating derived libraries.")


# ---------------------------------------------------------------------------

def cleanup_processed_dir():
    """
    The processed/ backup is only useful transiently (e.g. to recover from a
    mistake within the same run). It should never persist between runs, so
    remove it -- and everything in it -- once the run is complete.
    """
    if os.path.exists(PROCESSED_DIR):
        shutil.rmtree(PROCESSED_DIR)
        print(f"Removed '{PROCESSED_DIR}' (backups don't persist between runs).")


def main():
    print(f"Working directory for this workflow: {HERE}\n")
    print("=== Step 1/2: cleaning new.bib and merging into library.bib ===")
    run_clean_and_merge()

    print("\n=== Step 2/2: populating additional libraries ===")
    run_populate_additional_libraries()

    cleanup_processed_dir()


if __name__ == "__main__":
    main()

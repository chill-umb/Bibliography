# Bibliography workflow 

## One-time setup

```
pip3 install -r requirements.txt
```

## Adding a new reference

1. Go to [DBLP](https://dblp.org), find the paper, copy its BibTeX entry.
2. Paste it into `new.bib` (in this folder). You can paste multiple entries
   at once. Keep the DBLP key as-is -- the script replaces it.
3. Run:
   ```
   python3 prepare_bib.py
   ```

That's it. The script will:

- Clean each entry: strip DBLP-only bookkeeping fields (`timestamp`,
  `biburl`, `bibsource`), expand venue names to their full form (e.g.
  `SIGMOD Conference` -> `Proceedings of the ACM SIGMOD International
  Conference on Management of Data`), normalize dashes (so page/year ranges
  always use BibTeX's `--` convention, never a stray Unicode dash or a
  malformed `----`), and escape LaTeX-special characters (`_`, `#`, `%`,
  `&`, `~`) wherever they appear -- titles, authors, venue names, URLs, and
  DOIs -- so the file compiles cleanly in Overleaf/LaTeX.
- Generate a unique citation key in `Lastname+Year` form (e.g. `Smith2024`),
  adding a letter suffix (`Smith2024a`, `Smith2024b`, ...) if that key is
  already taken.
- Check for likely duplicates (fuzzy title match against the existing
  library) and ask you to confirm before adding anything that looks like it
  might already be in there.
- Add the cleaned entries to the **top** of `library.bib`, without touching
  or reformatting any existing entry.
- Clear `new.bib` so it's ready for the next batch.
- Regenerate the derived libraries (see below) from the updated `library.bib`.

## Files in this folder

| File | What it is |
|---|---|
| `prepare_bib.py` | The script. Run this. |
| `new.bib` | Scratch file -- paste new DBLP entries here before running the script. Empty between runs. |
| `library.bib` | The real bibliography. New entries get added to the top of this file. |
| `library-no-url.bib` | `library.bib` with `url`/`doi` fields commented out. |
| `library-no-doi.bib` | `library.bib` with just the `doi` field commented out. |
| `library-short.bib` | `library.bib` with venue names abbreviated (e.g. `SIGMOD` instead of the full conference name) and `pages` dropped from conference papers. |
| `library-no-url-short.bib` | The short version with `url`/`doi` also commented out. |
| `data/dblp2daslab.recipes` | Long-form venue name mappings (DBLP's short name -> full name), used during cleaning. |
| `data/additional_recipies` | Long name -> abbreviation mappings, used to build `library-short.bib`. |
| `requirements.txt` | Python packages the script needs. |

## Notes

- All four `library-*.bib` files are fully regenerated from `library.bib`
  every time you run the script -- don't hand-edit them, edit `library.bib`
  instead (or just add new entries the normal way, via `new.bib`).
- If the duplicate-check prompt appears and you're not sure, it's safe to
  say "no" (skip adding it) and double-check manually -- you can always
  paste it into `new.bib` again later.
- If you see a `WARNING: found content in new.bib that could NOT be parsed`
  message, it means part of what you pasted isn't valid bibtex (usually a
  missing `{`/`}` or `,`). That part is shown in the warning but not added
  anywhere -- fix it and paste it into `new.bib` again on its own.
- This script only ever touches files inside this `new-workflow/` folder.

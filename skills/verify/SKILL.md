---
name: verify
description: Check a paper's references for hallucinated or wrong citations before submission, or check the reference list of a paper you are reviewing. Use when the user asks to verify, check or audit references, citations, a bibliography or a .bib file, worries about desk rejection over hallucinated references, or after you add or edit BibTeX entries or reference lists yourself.
argument-hint: "[paper directory | refs.bib | overleaf.zip | paper.pdf]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/citecheck.py *)
---

# Verify references

The bundled checker looks up every reference in a local DBLP index (if built), Crossref, arXiv, Semantic Scholar and OpenAlex (DOIs through doi.org, arXiv IDs through the arXiv API) and compares title, authors (family names and initials), year and venue with the indexed record. It also flags retracted works and the same work cited under two keys. It sees only scholarly indexes, so your job is to feed it the right input, confirm the entries it cannot place, and report.

## 1. Find the references

Target: `$ARGUMENTS` (if empty, the current project).

- **LaTeX or Quarto/R Markdown project, or an Overleaf `.zip`.** Pass the directory or zip itself. The checker finds every `.bib` file inside and checks only the keys the `.tex`/`.md`/`.qmd` sources cite; a shared `.bib` often holds hundreds of uncited entries that never reach the PDF.
- **A lone `.bib` file.** Pass it, plus `--cited-in <sources>` if you know where the sources are.
- **A reference-manager export** (`.ris` from EndNote, Zotero or Mendeley, or EndNote `.xml`). Pass it directly; every reference in it is checked. Suggest this route to Word users.
- **PDF, Word, or a pasted list** (for example a paper under review). Extract each reference into a JSON list, `[{"key": "...", "title": "...", "authors": ["..."], "year": 2020, "doi": "...", "arxiv": "...", "url": "..."}]`, copying every field exactly as printed, including a trailing "et al." and the venue in `"venue"`. Do not correct anything while extracting: the check is of what the paper says. Write it to `${CLAUDE_PLUGIN_DATA}/refs.json` and check that file.

## 2. Run the checker

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/citecheck.py <directory|file.bib|refs.json|refs.ris|paper.zip> --json ${CLAUDE_PLUGIN_DATA}/report.json
```

Give the Bash call a 10-minute timeout: rate limits make it take up to a few seconds per reference. Lookups are cached for 7 days, so a rerun after fixes is fast. Exit status 1 means at least one entry is NOT_FOUND or MISMATCH; 3 means some entries could not be checked (ERROR).

| Verdict | Meaning |
|---|---|
| VERIFIED | title, authors and year agree with an indexed record |
| CHECK | the work exists but a field differs (a title word, the year, an author or an author's first name, a truncated author list, the venue, or a preprint cited as published), it has been retracted, or it is not an indexed paper (live URL, thesis, report) |
| MISMATCH | the title exists but most cited authors are not on it, the DOI/arXiv ID points to a different work or to nothing, or the year is in the future |
| NOT_FOUND | no index has a work with this title |
| ERROR | too few sources answered |

If the output ends with `hint:` lines (missing free API keys, or no local DBLP index), pass them on to the user once. `--build-dblp` downloads about 1.1 GB and takes around 15 minutes, so ask before running it.

## 3. Confirm what the indexes could not place

For every NOT_FOUND, MISMATCH and ERROR entry, search the web for the exact title (and first author) before reporting it:

- A real source turns up (court ruling, standard, book, thesis, blog post, dataset or software page, OpenReview-only or workshop paper): report it as real but not indexed, with the link, and suggest adding a `url` or `doi` field so the next run can check it.
- Nothing turns up: report it as **likely fabricated**, and say which searches you ran.
- MISMATCH: name the wrong field and give the value from the record (the `match` object in the JSON report).

Read the JSON report for details; the text summary shortens titles.

## 4. Report

Lead with the entries that could get the paper desk-rejected: likely fabricated, wrong authors, or a DOI or arXiv ID resolving to nothing or to another paper. Then list retracted works and the other CHECK items, then duplicate keys, then one line with the count of VERIFIED entries. For each flagged entry give the key, the problem, and the fix. Do not list VERIFIED entries one by one. When reviewing someone else's paper, phrase findings as observations for the review, not as edits.

## 5. Fix only when asked

- Take every corrected value from the checker's record or from a page you opened, never from memory. Writing references from memory is how hallucinated references are made. Rerunning with `--fixes ${CLAUDE_PLUGIN_DATA}/fixes.bib` writes corrected entries built from the matched records, with the original keys; apply them only after the user agrees, one entry at a time.
- Do not swap a fabricated reference for a different real paper without the user's say-so: the sentence citing it may not hold for the substitute. Offer candidates and let the user choose.
- Rerun the checker after editing and report the new counts.

## When you write references yourself

If you add or edit a reference in this session, in any format, run the checker on it before you tell the user the work is done.

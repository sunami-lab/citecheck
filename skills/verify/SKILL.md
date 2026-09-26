---
name: verify
description: Check a paper's references for hallucinated or wrong citations before submission. Use when the user asks to verify, check or audit references, citations, a bibliography or a .bib file, worries about desk rejection over hallucinated references, or after you add or edit BibTeX entries or reference lists yourself.
argument-hint: "[refs.bib | paper directory | paper.pdf]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/citecheck.py *)
---

# Verify references

The bundled checker looks up every reference in Semantic Scholar, OpenAlex, Crossref and arXiv (DOIs through doi.org, arXiv IDs through the arXiv API) and compares title, authors and year with the indexed record. It sees only scholarly indexes, so your job is to feed it the right file, confirm the entries it cannot place, and report.

## 1. Find the references

Target: `$ARGUMENTS` (if empty, look in the current project).

- **LaTeX project or `.bib` file.** Find the bibliography through `\bibliography{...}` or `\addbibresource{...}`. Pass the project's `.tex` files (or its directory) with `--cited-in`, so only cited entries are checked; a shared `.bib` often holds hundreds of uncited ones that never reach the PDF.
- **PDF, Word, Markdown or a pasted list.** Extract each reference into a JSON list, `[{"key": "...", "title": "...", "authors": ["..."], "year": 2020, "doi": "...", "arxiv": "...", "url": "..."}]`, copying every field exactly as printed. Do not correct anything while extracting: the check is of what the paper says. Write the list to `${CLAUDE_PLUGIN_DATA}/refs.json` and check that file.

## 2. Run the checker

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/citecheck.py <file.bib|refs.json> [--cited-in <tex files or dir>] --json ${CLAUDE_PLUGIN_DATA}/report.json
```

It takes about a second per reference because of API rate limits, so give the Bash call a 10-minute timeout. Responses are cached for 7 days, so a rerun after fixes is fast. Exit status 1 means at least one entry is NOT_FOUND or MISMATCH.

| Verdict | Meaning |
|---|---|
| VERIFIED | title, authors and year agree with an indexed record |
| CHECK | the work exists but a field differs (title wording, year, an author), or it is not an indexed paper but its URL is live |
| MISMATCH | the title exists but most cited authors are not on it, or the DOI/arXiv ID points to a different work or to nothing |
| NOT_FOUND | no index has a work with this title |
| ERROR | too few sources answered; rerun later, or ask the user to set `S2_API_KEY` or `OPENALEX_API_KEY` (both free) |

## 3. Confirm what the indexes could not place

For every NOT_FOUND, MISMATCH and ERROR entry, search the web for the exact title (and first author) before reporting it:

- A real source turns up (court ruling, standard, book, thesis, blog post, dataset or software page): report it as real but not indexed, with the link, and suggest adding a `url` field so the next run marks it CHECK instead.
- Nothing turns up: report it as **likely fabricated**. Say which searches you ran.
- MISMATCH: name the wrong field and give the value from the record (the `match` object in the JSON report).

Read the JSON report for details; the text summary shortens titles.

## 4. Report

Lead with the entries that could get the paper desk-rejected (likely fabricated, wrong authors, DOI or arXiv ID resolving to nothing or to another paper), then the CHECK items, then one line with the count of VERIFIED entries. For each flagged entry give the key, the problem, and the fix. Do not list VERIFIED entries one by one.

## 5. Fix only when asked

- Take every corrected value from the checker's record or from a page you opened, never from memory. Writing references from memory is how hallucinated references are made.
- Do not swap a fabricated reference for a different real paper without the user's say-so: the sentence citing it may not hold for the substitute. Offer candidates and let the user choose.
- Rerun the checker after editing and report the new counts.

## When you write references yourself

If you add or edit a reference in this session, in any format, run the checker on it before you tell the user the work is done.

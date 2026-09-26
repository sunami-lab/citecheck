---
name: claims
description: Check whether each cited paper supports the sentence that cites it, catching real but wrong papers and claims the source does not make. Use when the user asks whether their citations support their claims, about citation accuracy or relevance, or wants a citation check beyond whether references exist.
argument-hint: "[paper directory | overleaf.zip]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/citecheck.py *) Read
---

# Check that citations support their sentences

A reference can be real and still be the wrong one for the sentence that cites it. This skill pairs every citing sentence with the cited work's abstract and judges the pairs.

## 1. Collect sentences and abstracts

Target: `$ARGUMENTS` (if empty, the current project). The checker needs the LaTeX, Quarto or Markdown sources, not only the `.bib`.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/citecheck.py <paper directory|paper.zip> --claims ${CLAUDE_PLUGIN_DATA}/claims.json --json ${CLAUDE_PLUGIN_DATA}/report.json
```

Give the Bash call a 10-minute timeout. If the output shows NOT_FOUND or MISMATCH references, report those first, as the `verify` skill does: a fabricated reference cannot support anything.

## 2. Judge each pair

Read the rubric at `${CLAUDE_PLUGIN_ROOT}/skills/claims/rubric.md` and apply it to every sentence in `claims.json` (each entry has `contexts`, the citing sentences with file and line, and `abstract`). Work through the file in chunks of about 25 references. Skip entries whose verdict is NOT_FOUND, MISMATCH or ERROR, and entries without an abstract (count them as UNCHECKED).

## 3. Confirm before reporting

For each UNSUPPORTED or CONTRADICTED pair, open the cited work's full text when it is available (the arXiv abstract page or HTML version, or the DOI landing page) and check whether the claim appears beyond the abstract. Drop the flag if it does, and say so in the summary.

## 4. Report

List only the pairs still UNSUPPORTED or CONTRADICTED. For each give `file:line`, the sentence (shortened), the key, one line on what the cited work actually does, and a fix: reword the sentence to match what the work shows, or cite a work that states the claim. Find replacement candidates in an index or on the web, never from memory, and let the user choose. End with one line of counts per label, including UNCHECKED.

State the limit once: the judgment rests on abstracts and whatever full text you opened, so a claim buried deep in a paper can be missed. Do not edit the paper unless asked.

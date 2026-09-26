# citecheck

Catch hallucinated references before you submit.

The NeurIPS 2026 handbook calls hallucinated citations a Code of Conduct violation, and papers are being desk-rejected over them: one NeurIPS 2026 area chair reported that 5 of the 8 submissions in their batch had two or more ([Pruthi, 2026](https://x.com/danish037/status/2089642898438693185)). A scan of accepted 2025 papers found at least two likely hallucinated references in 5.1% of NeurIPS papers and 3.4% of ICML papers ([arXiv:2607.00738](https://arxiv.org/abs/2607.00738)).

citecheck looks up every cited entry in Semantic Scholar, OpenAlex, Crossref and arXiv, and reports the ones that do not exist or whose authors, DOI, arXiv ID or year are wrong. It is a Claude Code plugin and a standalone Python script with no dependencies.

## Install the plugin

In Claude Code:

```
/plugin marketplace add sunami-lab/citecheck
/plugin install citecheck@citecheck
```

Then, in your paper's directory, ask "check my references before I submit" or run `/citecheck:verify refs.bib`. Claude finds the `.bib` file, checks only the entries your `.tex` files cite, searches the web for anything the indexes cannot place, and reports what to fix. It works on PDF or Word reference lists too: Claude extracts the references first. The skill also tells Claude to check any reference it writes itself.

## Run the script directly

```bash
python3 scripts/citecheck.py refs.bib --cited-in paper/ --json report.json
```

Python 3.8+, standard library only. Exit status is 1 when any entry is NOT_FOUND or MISMATCH, so it can gate CI or a pre-commit hook. It takes about a second per reference (API rate limits); lookups are cached for 7 days in `~/.cache/citecheck/`, so reruns after fixes are fast.

Input is a `.bib` file, or a `.json` list of `{"key", "title", "authors": [...], "year", "doi", "arxiv", "url"}`.

Optional environment variables: `S2_API_KEY` and `OPENALEX_API_KEY` (free keys; the unauthenticated Semantic Scholar tier is often rate-limited) and `CITECHECK_MAILTO` (an email for the Crossref and OpenAlex polite pools).

## How an entry is checked

1. If the entry has a DOI, resolve it through doi.org; if it has an arXiv ID (in `eprint`, `url`, `journal = {arXiv preprint arXiv:...}` or DBLP's `volume = {abs/...}`), fetch it from arXiv. An identifier that resolves to nothing, or to a different title, is a MISMATCH.
2. Search the title in Semantic Scholar, OpenAlex, Crossref and arXiv until a record agrees on title, authors and year.
3. If no record found so far lists the cited authors, search OpenAlex again restricted to the first author. This gets past same-title noise: the real *Generative Adversarial Nets* is not in any index's top five title hits.
4. Compare the best record with the citation: title similarity (0.95 or more is the same title; 0.85 or more is the same work with different wording), the share of cited family names on the record, and the year (a cited year up to 2 years after the record's is accepted, since preprints are published later).

| Verdict | Meaning |
|---|---|
| VERIFIED | title, authors and year agree with an indexed record |
| CHECK | the work exists but a field differs (title wording, year, an author), or it is not an indexed paper but its URL is live |
| MISMATCH | the title exists but most cited authors are not on it, or the DOI/arXiv ID resolves to a different work or to nothing |
| NOT_FOUND | no index has a work with this title |
| ERROR | too few sources answered to decide |

NOT_FOUND needs at least two sources to have answered, one of them Semantic Scholar or OpenAlex. A source outage gives ERROR, never NOT_FOUND.

## How well it works

Tested on 26 September 2026. The samples are small: read these as a smoke test, not a benchmark.

**Real bibliographies.** The 165 cited references of three arXiv papers (DDPM, LoRA, and *Phantom References*) gave 155 VERIFIED, 9 CHECK, 1 NOT_FOUND and 0 MISMATCH.

- The NOT_FOUND is a court ruling (*Mata v. Avianca*) cited without a URL. The plugin's web-search pass finds it; the script alone cannot.
- Four CHECKs are blog and policy pages with live URLs.
- Three CHECKs are real errors in the published bibliographies. LoRA spells Jihun Hamm as "Ham" and cites Adam (2014) as 2017. *Phantom References* cites arXiv:2604.03159 as "BibTeX Citation Hallucinations in…", but its arXiv title is "BibTeX Citation Errors in…".
- Two CHECKs are index artefacts. The GAN paper's arXiv title says "Networks" where the NeurIPS title says "Nets", and Semantic Scholar's GPT-1 record lists two of its four authors.

**Planted errors** ([`tests/fixtures/planted.bib`](tests/fixtures/planted.bib)). This fixture mixes 11 real references with 10 errors of the kinds LLMs make. All 10 were flagged:

- The 3 fabricated titles were NOT_FOUND.
- MISMATCH covered 2 real titles with another paper's authors, a nonexistent arXiv ID, an arXiv ID pointing to BERT, a fabricated DOI, and a garbled title ("Language Models are Few-Shot *Reasoners*").
- A wrong year was CHECK.

All 11 real entries were VERIFIED, except a GitHub repository (CHECK, live URL).

Run the tests with `python3 -m unittest discover tests` (offline) and `CITECHECK_LIVE=1 python3 -m unittest tests.test_live` (live APIs, about a minute).

## Limitations

- It sees only scholarly indexes. Court rulings, standards, many books and web pages come back NOT_FOUND unless the entry has a URL (then CHECK). In Claude Code, the skill confirms these with a web search; the standalone script does not.
- The venue is not compared. Venue strings vary too much ("NeurIPS", "Advances in Neural Information Processing Systems", "Neural Information Processing Systems") to compare without a curated alias table.
- Authors are compared by family name, with fuzzy matching, so a wrong given name passes.
- A fabricated title within 0.85 similarity of a real one is reported as that real work with "title differs" (CHECK) or wrong authors (MISMATCH), not as NOT_FOUND. It is still flagged.
- Index records have their own errors (see the GPT-1 record above), so a correct citation can get CHECK.
- DBLP, the cleanest index for computer science, now puts a proof-of-work bot challenge in front of scripted clients, so citecheck does not use it.

## Why a database of every reference isn't enough

The database already exists. OpenAlex holds 327 million works under a CC0 licence, and Crossref holds 187 million DOI records. Every citation format is a deterministic rendering of that metadata (CSL styles), so storing each work in every format would add nothing.

References still get hallucinated for two reasons:

- **Nobody looks them up.** A language model writes the citation from memory instead of querying an index.
- **Checking is a matching problem, not a lookup.** A hallucinated reference usually looks like a real one: real authors, a plausible title, a real venue. Real references vary in ways a strict lookup would reject: preprint and published titles differ, names are spelled several ways, and different papers share titles. The rest of this tool is those matching rules.

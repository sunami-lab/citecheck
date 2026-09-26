# citecheck 🔎 — Cite papers that exist.

> Catches hallucinated references before your reviewers do.

<p align="center">
  <img src="assets/banner.png" alt="citecheck checking five references: two verified, one fabricated paper not found, one real title with the wrong authors, one wrong year" width="900">
</p>

<p align="center">
  <a href="#install"><img src="https://img.shields.io/badge/Claude_Code-plugin-d97757?style=flat-square" alt="Claude Code plugin"></a>
  <a href="scripts/citecheck.py"><img src="https://img.shields.io/badge/python-3.8%2B-0a0a0c?style=flat-square" alt="Python 3.8+"></a>
  <a href="scripts/citecheck.py"><img src="https://img.shields.io/badge/dependencies-0-0a0a0c?style=flat-square" alt="Zero dependencies"></a>
  <a href="#does-it-work"><img src="https://img.shields.io/badge/HALLMARK_F1-0.967-3ecf8e?style=flat-square" alt="HALLMARK test F1 0.967"></a>
</p>

citecheck looks up every reference your paper cites in DBLP, Crossref, arXiv, Semantic Scholar and OpenAlex. It tells you which ones **don't exist**, and which have the **wrong authors, DOI, arXiv ID, year or venue**. It also flags retracted papers and the same paper cited under two keys. It ships as a Claude Code plugin and as a single Python file with no dependencies.

```console
$ python3 scripts/citecheck.py examples/
[1/5] VERIFIED  vaswani2017attention
[2/5] VERIFIED  ho2020denoising
[3/5] NOT_FOUND zhang2024retrieval
[4/5] MISMATCH  song2020denoising
[5/5] CHECK     kingma2011adam
citecheck 0.2.0: 5 references in examples/
  NOT_FOUND 1   MISMATCH 1   CHECK 1   VERIFIED 2
  checking the 5 entries cited in the sources (--all checks every entry)
  warning: the same work is cited under several keys: ho2020denoising, song2020denoising
  note: stopped asking OpenAlex after repeated failures (rate limit, quota or outage)
  hint: OpenAlex allows about 100 searches a day without a key; a free key (OPENALEX_API_KEY) allows 1,000

NOT_FOUND  zhang2024retrieval  "Retrieval-Augmented Diffusion Transformers for Long-Horizon Planning"
           - no work with this title in DBLP, Crossref, arXiv, Semantic Scholar
           note: closest title: "Refining Compositional Diffusion for Reliable Long-Horizon Planning" (DBLP, similarity 0.637)

MISMATCH   song2020denoising  "Denoising Diffusion Probabilistic Models"
           - 2 of 2 cited authors are not on the record: Song, Yang; Ermon, Stefano
           record: "Denoising Diffusion Probabilistic Models" -- Jonathan Ho, Ajay Jain, Pieter Abbeel | 2020 | NeurIPS | https://dblp.org/rec/conf/nips/HoJA20 [DBLP]

CHECK      kingma2011adam  "Adam: A Method for Stochastic Optimization"
           - cited year 2011, record year 2014
           record: "Adam: A Method for Stochastic Optimization" -- Diederik P. Kingma, Jimmy Ba | 2014 | arXiv | https://arxiv.org/abs/1412.6980 [arXiv]
```

## Why

- **Hallucinated references get papers rejected.** The NeurIPS 2026 handbook calls them a Code of Conduct violation. One NeurIPS 2026 area chair reported that 5 of the 8 submissions in their batch had two or more ([Pruthi](https://x.com/danish037/status/2089642898438693185)). ACL, ICML, ICLR and EMNLP treat them as grounds for desk rejection too.
- **They survive peer review.** Among accepted 2025 papers, 5.1% at NeurIPS and 3.4% at ICML had at least two likely hallucinated references ([arXiv:2607.00738](https://arxiv.org/abs/2607.00738)).
- **They look real.** Language models reuse real authors, real venues and titles that are nearly right. "*Denoising Diffusion Probabilistic Models*, Song and Ermon, 2020" reads fine, but it is Ho, Jain and Abbeel's paper.
- **Checkers that cry wolf get ignored.** citecheck is tuned to flag hallucinations without flagging real references: on the HALLMARK benchmark it wrongly flags about 1 real reference in 40.

## Example

You ask a language model to tighten the related-work paragraph of your draft ([`examples/paper.tex`](examples/paper.tex)). The result compiles and reads well:

> Attention-only architectures (Vaswani et al., 2017) and denoising diffusion (Ho et al., 2020) are now standard building blocks. Retrieval-augmented diffusion transformers extend both to long-horizon planning (Zhang and Kumar, 2024), and the denoising objective has been analysed through score matching (Song and Ermon, 2020). We train all models with Adam (Kingma and Ba, 2011).

Three of the five citations are wrong:

| Citation | What's wrong | Verdict |
| --- | --- | --- |
| Zhang and Kumar, *Retrieval-Augmented Diffusion Transformers for Long-Horizon Planning*, ICLR 2024 | the paper does not exist | ✗ NOT_FOUND |
| Song and Ermon, *Denoising Diffusion Probabilistic Models*, 2020 | real title, but it's by Ho, Jain and Abbeel | ✗ MISMATCH |
| Kingma and Ba, *Adam: A Method for Stochastic Optimization*, 2011 | Adam appeared in 2014 | ! CHECK |

The script catches all three (output above). It also notices that `song2020denoising` and `ho2020denoising` are now the same paper. In Claude Code, just ask:

> I'm submitting this to NeurIPS tomorrow. Can you check my references?

Claude runs citecheck, searches the web for anything the indexes can't place, and reports back. Here is an excerpt from a real run (53 seconds):

> **1. `zhang2024retrieval`: probably fabricated.**
> "Retrieval-Augmented Diffusion Transformers for Long-Horizon Planning" by Wei Zhang and Rahul Kumar (ICLR 2024) isn't in Semantic Scholar, OpenAlex, Crossref or arXiv. I also searched the web three ways […]. None found a paper with this title.
>
> **2. `song2020denoising`: wrong authors, and a duplicate title.**
> "Denoising Diffusion Probabilistic Models" is by Jonathan Ho, Ajay Jain and Pieter Abbeel (2020). That's the paper already in your bib as `ho2020denoising`. […]
>
> **3. `kingma2011adam`: wrong year.** The bib says 2011, but the arXiv record (1412.6980, which your entry cites) is 2014.
>
> Should I apply the Adam fix and remove `zhang2024retrieval` now? […]

It doesn't edit anything until you say so. It also won't swap a fabricated reference for some other real paper, because the sentence citing it may not hold for the substitute.

## Install

### Claude Code plugin

```
/plugin marketplace add sunami-lab/citecheck
/plugin install citecheck@citecheck
```

Then ask "check my references", or run `/citecheck:verify` with a paper folder, a `.bib` file or an Overleaf `.zip`.

For PDF, Word or pasted reference lists, Claude first extracts the references and then checks them. That also works for reviewers checking a submission. The skill also has Claude check any reference it writes itself.

### Standalone script

```bash
git clone git@github.com:sunami-lab/citecheck.git
python3 citecheck/scripts/citecheck.py path/to/paper/
```

This needs Python 3.8+ and nothing else.

### One-time setup (recommended)

The free APIs are rationed, so do these once:

- **`python3 scripts/citecheck.py --build-dblp`.** This downloads dblp's monthly release (1.1 GB) and indexes its 8.5 million computer-science records into a local SQLite file (about 7 minutes, 2.2 GB). After that, most machine-learning references are checked offline in milliseconds.
- **A free [Semantic Scholar key](https://www.semanticscholar.org/product/api#api-key) as `S2_API_KEY`.** Anonymous requests are often refused with HTTP 429.
- **A free [OpenAlex key](https://openalex.org/) as `OPENALEX_API_KEY`.** Without one, OpenAlex allows about 100 searches a day; with one, 1,000.

Without these steps citecheck still works through Crossref, arXiv and doi.org. It just answers ERROR for some references instead of guessing.

## Verdicts

| | Verdict | Meaning |
| --- | --- | --- |
| ✓ | **VERIFIED** | title, authors, year and venue agree with an indexed record |
| ! | **CHECK** | the paper exists but something differs: a title word, the year, an author, a truncated author list, the venue, or a preprint cited as published. Also used for retracted papers, and for work that isn't indexed but has a live URL, or is a thesis or report |
| ✗ | **MISMATCH** | the title exists but most cited authors aren't on it, the DOI or arXiv ID points to a different paper or to nothing, or the year is in the future |
| ✗ | **NOT_FOUND** | no index has a paper with this title |
| ? | **ERROR** | too few sources answered to decide |

MISMATCH and NOT_FOUND are the errors that get papers desk-rejected. CHECK items are worth fixing before camera-ready.

**Exit status:** 0 if the bibliography is clean, 1 if any MISMATCH or NOT_FOUND, 3 if some references couldn't be checked, 2 for a usage error. This lets it gate CI or a pre-commit hook.

## How it checks

1. **Collect the cited keys.** From a folder or `.zip`, it reads every `.bib` file and keeps only the keys that the `.tex`, `.md`, `.qmd` or `.Rmd` sources cite.
2. **Resolve identifiers.** DOIs resolve through doi.org, which also reports retractions from Retraction Watch. arXiv IDs are looked up in batches.
   - An identifier that points to nothing is a MISMATCH.
   - So is one that points to an unrelated paper.
   - If it points to a paper with the same authors and a related title, citecheck treats it as a preprint or retitled version.
3. **Search by title, cheapest source first.** The order is the local DBLP index, Crossref, arXiv, Semantic Scholar, then OpenAlex, and it stops at the first record that agrees on everything. If no record lists the cited authors, it searches again among the first author's papers, which gets past same-title noise.
4. **Compare field by field.**
   - **Title:** compared word by word. Added or dropped stopwords, hyphenation, plurals and British/American spelling don't count; a swapped word does.
   - **Authors:** compared by family name.
   - **Year:** the cited year may be one year earlier or up to two years later than the record's, since preprints get published later.
   - **Venue:** about 30 major computer-science and machine-learning venues are recognised by name and abbreviation.

## Options

| Flag or variable | What it does |
| --- | --- |
| `PATH…` | `.bib` or `.json` files, a paper folder, or an Overleaf `.zip` (Menu → Download → Source) |
| `--cited-in SRC…` | check only keys cited in these sources (automatic for a folder or `.zip`) |
| `--all` | check every entry, cited or not |
| `--fixes OUT.bib` | write corrected entries, built from the matched records, for MISMATCH and CHECK references. Your bibliography is never modified |
| `--json OUT` | write per-entry results, including the matched record |
| `--build-dblp` | build the local DBLP index, then exit |
| `--no-cache` | bypass the 7-day lookup cache in `~/.cache/citecheck/` |
| `S2_API_KEY`, `OPENALEX_API_KEY` | free API keys (see [setup](#one-time-setup-recommended)) |
| `CITECHECK_MAILTO` | an email address for the Crossref and OpenAlex polite pools |

Input can also be JSON: `[{"key", "title", "authors": [...], "year", "venue", "doi", "arxiv", "url"}]`.

## Does it work?

Tested on 26 September 2026. The setup was the local DBLP index with no API keys, and OpenAlex's anonymous quota ran out partway through.

**[HALLMARK](https://github.com/rpatrik96/hallmark), test split** (831 entries: 312 valid, 519 with one of 14 kinds of error):

| | Detection | False-positive rate | F1 |
| --- | --- | --- | --- |
| citecheck: MISMATCH, NOT_FOUND or CHECK counts as a detection | 0.950 | 0.026 | **0.967** |
| citecheck: only MISMATCH or NOT_FOUND counts | 0.570 | 0.006 | 0.725 |
| bibtex-updater (the benchmark's reference tool) | 0.877 | 0.115 | – |
| Claude Sonnet 4.6 + bibtex-updater (best agent in the paper, dev split) | 0.990 | 0.431 | 0.841 |

The dev split, which the rules were tuned on, gives 0.962 detection, 0.027 false positives and F1 0.969.

Twelve of the 14 error types are caught at 85% or better, and ten of them at 100%. The weakest are made-up venue names (76%) and preprints cited under the wrong version (84%). I built the rules on the dev split, but I did look at about 15 test-split misses from an earlier version, so read the test figure as slightly optimistic.

Most of citecheck's "false positives" are errors in the benchmark's own valid entries. Of the 8 valid entries it flagged on the test split, 6 are wrong:

- 2 have DOIs that resolve to other papers.
- 3 have the wrong venue: LLaMA was never at ICLR, LLaMA 2 was not at NeurIPS, and MiniGPT-4 appeared at ICLR 2024, not CVPR.
- 1 lists co-authors who aren't on the paper.

**Real-world hallucinations.**

- **GPTZero's list from accepted NeurIPS 2025 papers:** all 97 hallucinated references are flagged, 90 of them as MISMATCH or NOT_FOUND.
- **One of those papers** (not named here). Run on its `.bib`, citecheck flags 24 of the 65 cited references as NOT_FOUND or MISMATCH:
  - That includes all 13 that GPTZero reported, plus 11 more.
  - The authors deleted or corrected 22 of the 24 in their own v2.
  - The other 2 still list co-authors who aren't on the paper.
  - Its CHECK items also caught a misspelled author ("Pennin" for Pennington), a title missing a word, and CoQA cited at NAACL, although it was published in TACL.

Run the offline tests with `python3 -m unittest discover tests`. The benchmark harness is [`bench/hallmark.py`](bench/hallmark.py).

## Limitations

- **Only scholarly indexes.** Court rulings, standards, many books and web pages come back NOT_FOUND unless the entry has a URL (then CHECK). The Claude Code skill confirms these with a web search; the standalone script cannot.
- **Venues are checked only for about 30 major computer-science and machine-learning venues.** Journals and smaller conferences are not compared.
- **Authors are compared by family name.** A wrong given name, or a changed author order, passes.
- **New papers can lag.** A paper accepted this year may not be in the indexes yet, so a correct citation can get CHECK ("only a preprint version was found").
- **Indexes make mistakes.** Semantic Scholar's GPT-1 record lists two of its four authors, so a correct citation of it gets CHECK.

## Why not just a database of every reference?

It's the right idea, and it mostly exists. OpenAlex holds 327 million works under a CC0 licence, Crossref holds 187 million DOI records, and dblp publishes all 8.5 million of its computer-science records every month. Every citation style is a deterministic rendering of that metadata, so storing each work in every format would add nothing.

Two things stand in the way:

- **Access is rationed.** The live APIs are being throttled, partly because of agent traffic. dblp's API now sits behind a bot challenge, OpenAlex bills its searches, and Semantic Scholar refuses anonymous bursts. So citecheck can keep a local copy of dblp: for computer science, the "file of every reference" is a 7-minute download.
- **Checking is a matching problem, not a lookup.** A fake reference borrows real authors and a nearly right title. A real one varies between its preprint and published versions, spells names several ways, and shares its title with other papers. Most of citecheck is the rules for telling those two cases apart.

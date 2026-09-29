#!/usr/bin/env python3
"""Check that every reference in a bibliography exists and matches its indexed record.

Each entry is looked up by DOI (doi.org) and arXiv ID (arXiv API) when it carries one,
then searched by title in Semantic Scholar, OpenAlex, Crossref and arXiv. The
best-matching record is compared with the citation field by field: title, author
family names and initials, and year. Input is BibTeX, JSON, RIS or EndNote XML, a paper
folder, or an Overleaf .zip.

Verdicts
  VERIFIED   title, authors and year agree with an indexed record
  CHECK      the work exists but a field differs (title wording, year, an author), or it
             is not an indexed paper but its URL is live
  MISMATCH   the title exists but most cited authors are not on it, or the DOI/arXiv ID
             resolves to a different work or to nothing
  NOT_FOUND  no index has a work with this title: the signature of a fabricated reference
  ERROR      too few sources answered to decide
  SKIPPED    the entry has no title

Standard library only (Python 3.8+). Exit status is 1 when any entry is MISMATCH or
NOT_FOUND, so the script can gate a pre-submission check or CI job.

Environment (all optional): S2_API_KEY, OPENALEX_API_KEY, CITECHECK_MAILTO (sent to
Crossref and OpenAlex for their polite pools), XDG_CACHE_HOME.
"""
from __future__ import annotations

import argparse
import difflib
import gzip
import html
import http.client
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter

__version__ = "0.4.1"

TITLE_SAME = 0.95  # title similarity at or above this: same title
TITLE_NEAR = 0.85  # at or above this: same work, reworded or mistyped title
AUTHORS_MIN = 0.5  # below this share of cited authors on the record: MISMATCH
YEAR_LAG = (-1, 2)  # cited year minus record year: preprints are often published 1-2 years later
THIS_YEAR = time.localtime().tm_year
# For ablation studies only: comma-separated components to switch off
# (dblp, ids, anchored, venue, names, retitle).
ABLATE = set(filter(None, os.environ.get("CITECHECK_ABLATE", "").split(",")))
CACHE_TTL = 7 * 86400
USER_AGENT = f"citecheck/{__version__} (+https://github.com/sunami-lab/citecheck)"
MAILTO = os.environ.get("CITECHECK_MAILTO", "")
FLAGGED = ("NOT_FOUND", "MISMATCH")
ORDER = ("NOT_FOUND", "MISMATCH", "ERROR", "CHECK", "SKIPPED", "VERIFIED")
# Entry types the indexes cover poorly: when unfound they get CHECK, not NOT_FOUND.
UNINDEXED_TYPES = {"phdthesis", "mastersthesis", "thesis", "techreport", "report", "manual", "software",
                   "online", "www", "unpublished", "patent", "dataset", "standard",
                   "book", "inbook", "mvbook", "incollection", "collection"}
# ...and the same kinds of work exported as @article/@misc (Google Scholar: journal = {Tech. Rep.}).
UNINDEXED_VENUE = re.compile(r"tech(nical)?\.?\s*rep|working paper|thesis|dissertation|lecture notes|white ?paper", re.I)
BOOK_TYPES = {"book", "inbook", "mvbook"}  # reprints and new editions: years are not compared
NOTICES = {"retraction": "retracted", "withdrawal": "withdrawn", "removal": "removed",
           "expression_of_concern": "subject to an expression of concern"}


# ---------------------------------------------------------------- text normalisation

_TRANSLIT = str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE",
                           "œ": "oe", "Œ": "OE", "ß": "ss", "đ": "d", "Đ": "D", "ı": "i"})
# \"o, \'{e}, \v{s}, {\'\i}: an accent command followed by one letter. Letter-named accents
# (\u, \v, \c, ...) must be followed by a brace or space so \textbf is not read as \t + e.
_ACCENT = re.compile(r"\\(?:[`'^\"~=.]|[uvHtcdbrk](?=[\s{]))\s*\{?\s*\\?([A-Za-z])\s*\}?")
_LETTER_CMD = {"ss": "ss", "o": "o", "O": "O", "l": "l", "L": "L", "ae": "ae", "AE": "AE",
               "oe": "oe", "OE": "OE", "aa": "a", "AA": "A", "i": "i", "j": "j"}


def detex(s: str) -> str:
    """BibTeX/LaTeX field text -> plain text."""
    s = re.sub(r"\\href\s*\{[^}]*\}\s*\{", "{", s or "")  # \href{url}{text} -> text
    s = re.sub(r"\\url\s*\{[^}]*\}", " ", s)
    s = _ACCENT.sub(r"\1", s)
    s = re.sub(r"\\(ss|oe|OE|ae|AE|aa|AA|o|O|l|L|i|j)(?![A-Za-z])", lambda m: _LETTER_CMD[m.group(1)], s)
    s = re.sub(r"\\([&%$#_])", r"\1", s)
    s = re.sub(r"\\[A-Za-z]+\*?", " ", s)  # drop remaining commands, keep their arguments
    s = s.replace("~", " ").replace("--", "-")
    s = re.sub(r"[{}$]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def norm(s: str) -> str:
    """Lowercase ASCII words for comparison: no accents, markup or punctuation."""
    s = html.unescape(html.unescape(re.sub(r"<[^>]+>", " ", s or "")))  # <i>, <sub>..., and "&amp;apos;"
    s = unicodedata.normalize("NFKD", detex(s).translate(_TRANSLIT))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _main_title(s: str) -> str:
    """Title before a subtitle separator ('Main: Subtitle', 'Main - Subtitle')."""
    return norm(re.split(r"\s*[:?!.]\s+|\s+-{1,3}\s+", detex(s), maxsplit=1)[0])


def title_sim(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio()
    short, long_ = sorted((na, nb), key=len)
    subtitle_only = (len(short.split()) >= 4 and long_.startswith(short + " ")) or any(
        len(main.split()) >= 2 and main == other for main, other in ((_main_title(a), nb), (_main_title(b), na)))
    return max(ratio, TITLE_NEAR) if subtitle_only else ratio


def title_changes(a: str, b: str) -> list:
    """Words that differ between two titles, ignoring added or dropped stopwords, hyphenation and
    spelling variants (modelling/modeling, network/networks)."""
    wa, wb = norm(a).split(), norm(b).split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        x, y = wa[i1:i2], wb[j1:j2]
        if op != "replace":  # an added or dropped word matters only if it is a content word
            x, y = [w for w in x if w not in _STOP], [w for w in y if w not in _STOP]
        if (not x and not y) or "".join(x) == "".join(y):
            continue
        if len(x) == len(y) and all(difflib.SequenceMatcher(None, p, q).ratio() >= 0.8 for p, q in zip(x, y)):
            continue
        out.append((" ".join(wa[i1:i2]), " ".join(wb[j1:j2])))
    return out


def word_overlap(a: str, b: str) -> float:
    """Shared content words, as a share of the shorter title's content words."""
    x, y = set(norm(a).split()) - _STOP, set(norm(b).split()) - _STOP
    return len(x & y) / min(len(x), len(y)) if x and y else 0.0


# Major CS/ML venues, matched against normalised venue strings (DBLP abbreviations, full names).
VENUES = {
    "NeurIPS": r"\bneurips\b|\bnips\b|neural information processing systems|\badv neural inf process syst\b",
    "ICML": r"\bicml\b|international conference on machine learning(?! and applications)|\bint conf mach learn\b",
    "ICLR": r"\biclr\b|international conference on learning representations|\bint conf learn represent\b",
    "CVPR": r"\bcvpr\b|computer vision and pattern recognition|\bcomput vis pattern recognit\b",
    "ICCV": r"\biccv\b|international conference on computer vision",
    "ECCV": r"\beccv\b|european conference on computer vision",
    "AAAI": r"\baaai\b",
    "IJCAI": r"\bijcai\b|international joint conference on artificial intelligence",
    "ACL": r"\bacl\b|annual meeting of the association for computational linguistics",
    "EMNLP": r"\bemnlp\b|empirical methods in natural language processing",
    "NAACL": r"\bnaacl\b|north american chapter",
    "EACL": r"\beacl\b|european chapter of the association",
    "COLING": r"\bcoling\b",
    "KDD": r"\bkdd\b|knowledge discovery and data mining",
    "WWW": r"\bwww\b|world wide web conference|\bthe web conference\b",
    "SIGIR": r"\bsigir\b",
    "AISTATS": r"\baistats\b|artificial intelligence and statistics",
    "UAI": r"\buai\b|uncertainty in artificial intelligence",
    "COLT": r"\bcolt\b|conference on learning theory|computational learning theory",
    "CoRL": r"\bcorl\b|conference on robot learning",
    "ICRA": r"\bicra\b|international conference on robotics and automation",
    "IROS": r"\biros\b|intelligent robots and systems",
    "JMLR": r"\bjmlr\b|journal of machine learning research|\bj mach learn res\b",
    "TMLR": r"\btmlr\b|transactions on machine learning research|\btrans mach learn res\b",
    "TPAMI": r"\btpami\b|pattern analysis and machine intelligence|\bpattern anal mach intell\b",
    "arXiv": r"\barxiv\b|\bcorr\b|\bpreprint\b",
}
_VENUE_RES = {name: re.compile(pat) for name, pat in VENUES.items()}
# Publishers and platforms that stand for several venues.
_VENUE_GROUPS = [(re.compile(r"\bpmlr\b|proceedings of machine learning research"), {"ICML", "AISTATS", "COLT", "CoRL", "UAI"}),
                 (re.compile(r"\bopenreview\b"), {"ICLR", "NeurIPS", "TMLR"})]


def venue_ids(text: str) -> set:
    t = norm(text)
    if not t:
        return set()
    return ({name for name, pat in _VENUE_RES.items() if pat.search(t)}
            | {v for pat, group in _VENUE_GROUPS if pat.search(t) for v in group})


def split_names(field: str) -> list:
    """BibTeX author field -> raw names, splitting on 'and' outside braces."""
    names, buf, depth, i = [], [], 0, 0
    while i < len(field):
        c = field[i]
        if depth == 0 and c.isspace():
            m = re.match(r"\s+and\s+", field[i:], re.I)
            if m:
                names.append("".join(buf))
                buf, i = [], i + m.end()
                continue
        depth += (c == "{") - (c == "}")
        buf.append(c)
        i += 1
    names.append("".join(buf))
    split = []
    # a field written "Last, First" elsewhere ("Smith, John") makes "Da Silva, Ana Maria" one name, while
    # "Le Song, Alex Smola" on its own is two people
    last_first_field = any(len(p) == 2 and len(p[0].split()) == 1 and p[1]
                           for p in ([q.strip() for q in n.split(",")] for n in names))
    for n in names:  # "Franz Aichberger, Lily Chen, and John Smith": full names separated by commas
        pieces = [p.strip() for p in n.split(",")]
        filled = [p for p in pieces if p]
        first = filled[0].split()[0] if filled else ""
        particle = first in _PARTICLES or first.lower() in _CASED_PARTICLES or (
            first.lower() in _PARTICLES and last_first_field)
        last_first = (len(filled) == 2 and particle
                      and not re.fullmatch(r"(?:[A-Z]\.?-?){1,4}", filled[0].split()[-1]))  # "de Winter, Joost CF"
        if "{" not in n and len(filled) >= 2 and all(len(p.split()) >= 2 for p in filled) and not last_first:
            split += filled
        else:
            split.append(n)
    names = [re.sub(r",?\s*\bet\.?\s+al\.?$", "", n.strip()) for n in split]  # "A. Smith et al."
    return [n.strip() for n in names if n.strip() and norm(n) not in ("others", "et al")]


def family_name(raw: str) -> str:
    """Normalised family name of one BibTeX name ('Last, First', 'First Last', '{Org}')."""
    raw = raw.strip()
    if raw.startswith("{") and _close_of(raw, 0) == len(raw) - 1:  # {Corporate Author}
        return norm(raw)
    name = re.sub(r"\s+\d{4}$", "", detex(raw))  # dblp's homonym number: "Wei Zhang 0001"
    if "," in name:
        return norm(name.split(",")[0])
    raw_words = name.split()
    if len(raw_words) > 1 and all(re.fullmatch(r"(?:[A-Z]\.?-?){1,3}", w) for w in raw_words[1:]):
        return norm(raw_words[0])  # "James G", "Hastie TJ": family name first, initials after
    words = norm(name).split()
    if len(words) > 1 and words[-1] in ("jr", "sr", "ii", "iii", "iv"):
        words.pop()
    return words[-1] if words else ""


_ORG = re.compile(r"\b(ai|inc|llc|ltd|labs?|research|team|collaboration|consortium|council|institute|university|"
                  r"department|foundation|agency|committee|association|society|organi[sz]ation|corporation|"
                  r"company|ministry|government|white house|openai|anthropic|deepmind|google|microsoft|nvidia)\b", re.I)


_KNOWN_ORGS = set("""google meta microsoft anthropic deepmind openai amazon apple ibm intel nvidia baidu alibaba tencent
huawei samsung mistral cohere eleutherai deepseek qwen xai who unesco oecd nasa cern nih cdc fda epoch""".split())
_PLACEHOLDERS = {"unknown", "anonymous", "anon", "author", "authors", "others", "et al", "na", "n a", "tba", "tbd"}


def is_org(raw: str) -> bool:
    """A corporate author: {Braced Name}, an organisation keyword, a known organisation, or one word with an
    internal capital ("OpenAI", "DeepSeek-AI"). Bare surnames, initials and placeholders are not."""
    raw = raw.strip()
    name = detex(raw)
    if norm(name) in _PLACEHOLDERS or len(norm(name)) < 3:
        return False
    if raw.startswith("{") and _close_of(raw, 0) == len(raw) - 1:
        return True
    if "," in name:
        return False
    if len(name.split()) == 1:
        return norm(name) in _KNOWN_ORGS or bool(re.search(r"[a-z][A-Z]|^[A-Z]{3,}$|-AI$", name))
    return bool(_ORG.search(name))


def _named_org(raw: str) -> bool:
    """Clearly an organisation, never a bare surname ("LeCun"): a braced name of two or more words, an
    organisation keyword ("Collaboration", "Institute") or a known organisation ("OpenAI")."""
    name = detex(raw).strip()
    braced = raw.strip().startswith("{") and len(name.split()) >= 2
    if not braced and "," in name:  # "Ai, Qingyao"
        return False
    return braced or bool(_ORG.search(name)) or norm(name) in _KNOWN_ORGS


def author_overlap(cited: list, record: list):
    """Share of cited authors whose family name appears on the record, and the misses. None when
    the lists cannot be compared (no authors, or an organisation on one side and people on the other)."""
    if not cited or not record:
        return None, []
    if all(is_org(a) for a in cited) or (len(record) == 1 and is_org(record[0]) and len(cited) > 1):
        return None, []
    tokens = {t for name in record for t in norm(name).split()}
    missing = []
    for raw in cited:
        fam = family_name(raw)
        last = fam.split()[-1] if fam else ""
        if not last:
            continue
        if last in tokens or (len(last) >= 4 and any(
                len(t) >= 4 and difflib.SequenceMatcher(None, last, t).ratio() >= 0.85 for t in tokens)):
            continue
        missing.append(detex(raw))
    return round(1 - len(missing) / len(cited), 3), missing


_CASED_PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "dos", "das"}  # rarely a given name
_PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "di", "da", "du", "la", "le", "dos", "das",
              "jr", "sr", "ii", "iii", "iv"}
_INITIALS = re.compile(r"(?:[A-Z]\.?-?){1,3}")


def _initials(raw: str, family: set) -> set:
    """First letters of the given names in one name, leaving out the family-name words."""
    out = set()
    for w in re.sub(r"\s+\d{4}$", "", detex(raw)).replace(",", " ").split():
        tokens = norm(w).split()
        if not tokens or set(tokens) <= family:
            continue
        if _INITIALS.fullmatch(w):  # "J.", "TJ", "J.-P."
            out |= {c.lower() for c in w if c.isalpha()}
        else:
            out |= {t[0] for t in tokens if t not in family and t not in _PARTICLES}
    return out


def given_name_conflicts(cited: list, record: list) -> list:
    """Cited authors whose family name is on the record but whose given names share no initial with any
    record author of that family name ("Ethan Moreno" for "Fábio Moreno"). Names without given names, names
    that are only initials, and name orders that swap family and given name, never conflict."""
    people = [r for r in record if not is_org(r)]
    out = []
    for raw in cited:
        fam = set(family_name(raw).split())
        mine = _initials(raw, fam) if any(len(w) > 1 for w in fam) and not is_org(raw) else set()
        same = [r for r in people if fam <= set(norm(detex(r)).split())] if mine else []
        theirs = [_initials(r, fam) for r in same]
        if same and all(t and not (t & mine) for t in theirs):
            out.append(f"{detex(raw).strip()} (record: {detex(same[0]).strip()})")
    return out


def record_within(cited: list, record: list) -> bool:
    """True when the record lists fewer authors than cited and every one of them is cited: an
    incomplete index record rather than a wrong author list."""
    if not cited or not record or len(record) >= len(cited):
        return False
    tokens = {t for raw in cited for t in norm(detex(raw)).split()}
    return all(family_name(a).split()[-1:] and family_name(a).split()[-1] in tokens for a in record)


def _year(v):
    m = re.search(r"(1[89]\d\d|20\d\d)", str(v or ""))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- BibTeX parsing

_ENTRY = re.compile(r"@\s*([A-Za-z]+)\s*([{(])")
_FIELD = re.compile(r"\s*([A-Za-z0-9_:+.\-]+)\s*=\s*")
_TOKEN = re.compile(r"[^\s,#{}()\"=]+")
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def _close_of(text: str, pos: int) -> int:
    """Index of the brace closing the one at text[pos]."""
    depth = 0
    for i in range(pos, len(text)):
        depth += (text[i] == "{") - (text[i] == "}")
        if depth == 0:
            return i
    raise ValueError("unbalanced braces")


def _skip_ws(text: str, pos: int) -> int:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def _read_value(text: str, pos: int, macros: dict):
    parts = []
    while True:
        pos = _skip_ws(text, pos)
        if text[pos] == "{":
            end = _close_of(text, pos)
            parts.append(text[pos + 1:end])
        elif text[pos] == '"':
            end, depth = pos + 1, 0
            while text[end] != '"' or depth:
                depth += (text[end] == "{") - (text[end] == "}")
                end += 1
            parts.append(text[pos + 1:end])
        else:
            tok = _TOKEN.match(text, pos)
            if not tok:
                raise ValueError("expected a value")
            word = tok.group(0)
            parts.append(word if word.isdigit() else macros.get(word.lower(), word))
            end = tok.end() - 1
        pos = _skip_ws(text, end + 1)
        if pos < len(text) and text[pos] == "#":
            pos += 1
            continue
        return "".join(parts), pos


class ParseError(str):
    """A message about an entry that could not be parsed, carrying the entry's key when it was read."""
    key = ""


def parse_bibtex(text: str):
    """Parse BibTeX -> (entries, errors). Entries are dicts of lowercased field names plus
    'key' and 'type'. Handles nested braces, quoted values, @string macros and '#'
    concatenation; skips @comment and @preamble. Malformed entries are reported in
    errors (with line numbers) and skipped rather than aborting the parse."""
    macros = {m: m for m in MONTHS}
    entries, errors, pos = [], [], 0
    while True:
        m = _ENTRY.search(text, pos)
        if not m:
            return entries, errors
        kind, close = m.group(1).lower(), "}" if m.group(2) == "{" else ")"
        pos, key = m.end(), ""
        try:
            if kind == "comment":
                pos = _close_of(text, m.end() - 1) + 1 if m.group(2) == "{" else pos
                continue
            if kind == "preamble":
                _, pos = _read_value(text, pos, macros)
                continue
            if kind == "string":
                f = _FIELD.match(text, pos)
                value, pos = _read_value(text, f.end(), macros)
                macros[f.group(1).lower()] = value
                continue
            ends = [i for i in (text.find(",", pos), text.find(close, pos)) if i >= 0]
            key_end = min(ends)
            key = text[pos:key_end].strip()
            fields = {"type": kind, "key": key}
            pos = key_end
            while True:
                pos = _skip_ws(text, pos)
                if text[pos] == close:
                    pos += 1
                    break
                if text[pos] == ",":
                    pos += 1
                    continue
                if text[pos] == "%":  # a commented-out field line
                    newline = text.find("\n", pos)
                    pos = len(text) if newline < 0 else newline
                    continue
                f = _FIELD.match(text, pos)
                if not f:
                    raise ValueError("expected 'name = value'")
                fields[f.group(1).lower()], pos = _read_value(text, f.end(), macros)
            entries.append(fields)
        except (ValueError, IndexError, AttributeError) as err:
            line = text.count("\n", 0, m.start()) + 1
            key = key or (re.match(r"\s*([^\s,{}()]+)", text[m.end():]) or [""])[0].strip()
            error = ParseError(f"line {line}: could not parse @{kind} entry{' ' + key if key else ''} "
                               f"({err or 'truncated'})")
            error.key = key
            errors.append(error)


# ---------------------------------------------------------------- identifiers

_DOI = re.compile(r"(10\.\d{4,9}/[^\s\"<>{}]+)")
_DOI_FIELD = re.compile(r"(10\.\d{4,9}/[^\s\"{}]+)")  # in the doi field itself: old Wiley DOIs contain < and >
_ARXIV_ID = r"(\d{4}\.\d{4,5}|[a-z][a-z\-]+(?:\.[A-Z]{2})?/\d{7})"
_ARXIV_IN_TEXT = re.compile(r"arxiv(?:\.org/(?:abs|pdf)/|[\s:.]*)" + _ARXIV_ID, re.I)


def find_arxiv(f: dict) -> str:
    eprint = f.get("eprint", "").strip()
    m = re.fullmatch(r"(?:arxiv:)?" + _ARXIV_ID + r"(?:v\d+)?", eprint, re.I)
    if m and f.get("archiveprefix", "arxiv").lower() == "arxiv":
        return m.group(1)
    for name in ("journal", "volume", "url", "note", "howpublished", "doi", "booktitle", "publisher", "number"):
        m = _ARXIV_IN_TEXT.search(f.get(name, ""))
        if m:
            return m.group(1)
    if f.get("journal", "").strip().lower() == "corr":  # DBLP: journal = {CoRR}, volume = {abs/1706.03762}
        m = re.search(r"\d{4}\.\d{4,5}", f.get("volume", ""))
        if m:
            return m.group(0)
    return ""


def find_doi(f: dict) -> str:
    for name in ("doi", "url", "note", "howpublished", "title"):
        m = (_DOI_FIELD if name == "doi" else _DOI).search(urllib.parse.unquote(f.get(name, "")))
        if m:
            doi = m.group(1).rstrip(".,;")
            return "" if doi.lower().startswith("10.48550/arxiv.") else doi  # arXiv DOIs: use the ID
    return ""


def find_url(f: dict) -> str:
    for name in ("url", "howpublished", "note", "title"):
        m = re.search(r"https?://[^\s{}\\]+", f.get(name, ""))
        if m:
            return m.group(0)
    return ""


def to_entry(f: dict) -> dict:
    """Bib fields -> the record citecheck compares."""
    return {
        "key": f.get("key", ""),
        "type": f.get("type", ""),
        "title": detex(f.get("title", "")),
        "authors": split_names(f.get("author") or f.get("editor") or ""),
        "year": _year(f.get("year") or f.get("date")),
        "doi": find_doi(f),
        "arxiv": find_arxiv(f),
        "url": find_url(f),
        "venue": detex(f.get("journal") or f.get("booktitle") or f.get("howpublished") or ""),
        "venue_text": detex(" ".join(f.get(k, "") for k in ("journal", "booktitle", "howpublished", "institution",
                                                             "school", "note", "type"))),
        "etal": bool(re.search(r"\band\s+others\b|\bet\.?\s+al\b", f.get("author", ""), re.I)),
        "generated": bool(f.get("generated")),  # key made up by citecheck (RIS, EndNote, JSON without keys)
        "key_base": f.get("key_base", ""),
    }


# ---------------------------------------------------------------- RIS and EndNote XML (reference-manager exports)

_RIS_LINE = re.compile(r"^([A-Z][A-Z0-9])\s{1,2}-\s?(.*)$")
_RIS_TYPES = {"JOUR": "article", "JFULL": "article", "EJOUR": "article", "MGZN": "article", "NEWS": "article",
              "CONF": "inproceedings", "CPAPER": "inproceedings", "BOOK": "book", "EBOOK": "book", "EDBOOK": "book",
              "CHAP": "incollection", "ECHAP": "incollection", "THES": "phdthesis", "RPRT": "techreport",
              "COMP": "software", "UNPB": "unpublished", "MANSCPT": "unpublished", "PAT": "patent", "ELEC": "online",
              "BLOG": "online", "DATA": "dataset", "STAND": "standard", "GOVDOC": "techreport", "GEN": "misc"}
_ENDNOTE_TYPES = {"journal article": "article", "electronic article": "article", "magazine article": "article",
                  "conference proceedings": "inproceedings", "conference paper": "inproceedings", "book": "book",
                  "edited book": "book", "book section": "incollection", "thesis": "phdthesis", "report": "techreport",
                  "computer program": "software", "unpublished work": "unpublished", "manuscript": "unpublished",
                  "patent": "patent", "web page": "online", "dataset": "dataset", "standard": "standard",
                  "electronic book": "book", "government document": "techreport"}


def _manager_author(name: str) -> str:
    """One author from a reference manager, as BibTeX. EndNote marks a corporate author with a trailing comma
    ("World Health Organization,"); an organisation whose name contains "and" is braced so it stays one."""
    name = name.strip()
    if name.endswith(","):
        return "{" + name.rstrip(", ") + "}"
    if re.search(r"\band\b", name, re.I) and "," not in name:
        return "{" + name + "}"
    return name


def _reference_fields(i, typ, key, title, authors, year, container, doi, url, note=""):
    """Fields of a reference-manager record, named as in BibTeX."""
    f = {"key": key or f"ref{i}", "type": typ, "title": title, "year": year, "doi": doi, "url": url,
         "annote": note,  # free text: never searched for a DOI or arXiv ID
         "author": " and ".join(_manager_author(a) for a in authors)}
    f["journal" if typ == "article" else "booktitle"] = container
    return f


def _free_key(base: str, taken) -> str:
    """base, or base with the first free suffix: b, c, ... as BibTeX tools do for the same author and year."""
    for suffix in [""] + list("bcdefghijklmnopqrstuvwxyz"):
        if base + suffix not in taken:
            return base + suffix
    return next(base + str(n) for n in range(2, len(taken) + 3) if base + str(n) not in taken)


def _readable_keys(fields: list) -> list:
    """Keys like 'lecun2015deep' for reference-manager exports, whose record numbers mean nothing in a report."""
    taken = set()
    for f in fields:
        authors = split_names(f["author"])
        word = next((w for w in norm(f["title"]).split() if w not in _STOP), "")
        base = (family_name(authors[0]).replace(" ", "") if authors else "") + str(_year(f["year"]) or "") + word
        f["key_base"] = base or f["key"]
        f["key"] = _free_key(f["key_base"], taken)
        f["generated"] = True
        taken.add(f["key"])
    return fields


def parse_ris(text: str):
    """RIS, as exported by EndNote, Zotero, Mendeley and most databases."""
    records, cur = [], None
    for line in text.splitlines():
        m = _RIS_LINE.match(line.strip("\ufeff"))
        if not m:
            continue
        tag, value = m.group(1), m.group(2).strip()
        if tag == "TY":
            if cur:  # the previous record had no ER
                records.append(cur)
            cur = {"TY": [value]}
        elif tag == "ER":
            if cur:
                records.append(cur)
            cur = None
        elif cur is not None:
            cur.setdefault(tag, []).append(value)
    if cur:
        records.append(cur)
    first = lambda r, *tags: next((r[t][0] for t in tags if r.get(t)), "")  # noqa: E731
    fields = []
    for i, r in enumerate(records, 1):
        typ = _RIS_TYPES.get(first(r, "TY").upper(), "misc")
        authors = r.get("AU") or r.get("A1") or r.get("A2") or r.get("ED") or []
        fields.append(_reference_fields(i, typ, "", first(r, "TI", "T1", "CT", "BT"), authors,
                                        first(r, "PY", "Y1", "DA"), first(r, "JF", "T2", "JO", "JA", "J2", "BT"),
                                        first(r, "DO"), first(r, "UR", "L1", "L2"), first(r, "N1")))
    return _readable_keys(fields), ([] if records or not text.strip() else ["no RIS records (TY ... ER) found"])


def parse_endnote_xml(text: str):
    """EndNote's XML export (File > Export > XML)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as err:
        return [], [f"not readable as EndNote XML: {err}"]
    txt = lambda el, path: "".join(el.find(path).itertext()).strip() if el.find(path) is not None else ""  # noqa: E731
    fields = []
    for i, rec in enumerate(root.iter("record"), 1):
        ref_type = rec.find("ref-type")
        typ = _ENDNOTE_TYPES.get((ref_type.get("name", "") if ref_type is not None else "").lower(), "misc")
        authors = ["".join(a.itertext()).strip() for a in rec.findall("contributors/authors/author")]
        container = txt(rec, "titles/secondary-title") or txt(rec, "periodical/full-title")
        fields.append(_reference_fields(i, typ, "",
                                        txt(rec, "titles/title"), authors, txt(rec, "dates/year"), container,
                                        txt(rec, "electronic-resource-num"), txt(rec, "urls/related-urls/url"),
                                        txt(rec, "notes")))
    return _readable_keys(fields), ([] if fields else ["no EndNote records found"])


_BIBTEX_ENTRY = re.compile(r"^[ \t]*@[A-Za-z]+[ \t]*[{(][ \t]*[^\s,=(){}\"]+[ \t]*,", re.M)  # "@mvbook{key,"


def load_entries(path: str):
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        text = fh.read()
    lower = path.lower()
    if lower.endswith(".ris") or (lower.endswith(".txt") and re.match(r"\s*TY\s{1,2}-", text)):
        fields, errors = parse_ris(text)
        return [to_entry(f) for f in fields], errors
    if lower.endswith(".xml"):
        fields, errors = parse_endnote_xml(text)
        return [to_entry(f) for f in fields], errors
    if lower.endswith(".json"):
        return json_entries(path, text), []
    if lower.endswith((".bib", ".bibtex")) or (  # also /dev/stdin, <(...), refs.txt
            os.path.splitext(lower)[1] in ("", ".txt") and "\x00" not in text
            and not text.startswith(("%PDF", "PK\x03\x04")) and _BIBTEX_ENTRY.search(text)):
        fields, errors = parse_bibtex(text)
        return [to_entry(f) for f in fields], errors
    raise InputError(f"{path}: unsupported file type. Give a .bib, .json, .ris or EndNote .xml file, a paper "
                     "folder or an Overleaf .zip; for a PDF or Word document, export or extract the references first")


def json_entries(path: str, text: str) -> list:
    """citecheck's JSON format: a list of {"key", "title", "authors": [...], "year", "venue", "doi", "arxiv", "url"}."""
    try:
        data = json.loads(text)
    except ValueError as err:
        raise InputError(f"{path}: not valid JSON ({err})") from None
    if isinstance(data, dict) and isinstance(data.get("references"), list):
        data = data["references"]
    if not isinstance(data, list):
        raise InputError(f'{path}: expected a JSON list of references, or an object with a "references" list')
    entries = []
    for i, d in enumerate(data, 1):
        if not isinstance(d, dict):
            raise InputError(f"{path}: reference {i} is not an object with a title and authors")
        if "authors" not in d and isinstance(d.get("author"), str):  # a common slip for "authors"
            d = dict(d, authors=d["author"])
        if "authors" not in d and ("author" in d or "issued" in d):
            raise InputError(f"{path}: this looks like CSL-JSON (Zotero's JSON export), which citecheck does not "
                             "read; export RIS or BibTeX instead")
        authors = d.get("authors") or []
        text_fields = all(d.get(k) is None or isinstance(d[k], (str, int)) for k in
                          ("key", "title", "venue", "doi", "arxiv", "url"))
        year_ok = d.get("year") is None or isinstance(d["year"], (str, int, float))
        names_ok = isinstance(authors, str) or (isinstance(authors, list) and all(isinstance(a, str) for a in authors))
        if not (text_fields and year_ok and names_ok):
            raise InputError(f'{path}: reference {i}: fields must be text, and "authors" a list of names')
        f = {"key": str(d.get("key") or f"ref{i}"), "type": "json", "title": str(d.get("title") or ""),
             "author": authors if isinstance(authors, str) else " and ".join(authors),
             "year": str(d.get("year") or ""), "doi": str(d.get("doi") or ""), "eprint": str(d.get("arxiv") or ""),
             "url": str(d.get("url") or ""), "journal": str(d.get("venue") or "")}
        if not d.get("key"):
            f.update(generated=True, key_base=f["key"])
        entries.append(to_entry(f))
    return entries


_CITE = re.compile(r"\\(?:[A-Za-z]*cite[A-Za-z]*|nocite)\*?\s*(?:\[[^\]]*\]\s*){0,2}\{([^}]*)\}")
# Pandoc/Quarto/R Markdown: [@key], @key, [-@key]. Quarto cross-references (@fig-x, @tbl-x) are not citations.
_PANDOC_CITE = re.compile(r"(?<![\w@])-?@([A-Za-z0-9_][\w:.#$%&\-+?<>~/]*)")
_CROSSREF = re.compile(r"(fig|tbl|sec|eq|lst|thm|lem|cor|prp|cnj|def|exm|exr)-")
SOURCE_EXTS = (".tex", ".md", ".qmd", ".rmd", ".markdown")


def _walk(root: str, exts: tuple) -> list:
    out = []
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".")]  # skip .git and friends
        out += [os.path.join(d, n) for n in names if n.lower().endswith(exts)]
    return sorted(out)


def cited_keys(paths: list) -> set:
    """Citation keys used in LaTeX and Markdown sources (directories are searched recursively)."""
    files = []
    for p in paths:
        files += _walk(p, SOURCE_EXTS) if os.path.isdir(p) else [p]
    keys = set()
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        if path.lower().endswith(".tex"):
            for m in _CITE.finditer(re.sub(r"(?<!\\)%.*", "", text)):
                keys.update(k.strip() for k in m.group(1).split(",") if k.strip())
        else:
            text = re.sub(r"(?ms)^(```|~~~).*?^\1", "", text)  # fenced code: decorators, emails
            for m in _PANDOC_CITE.finditer(text):
                key = m.group(1).rstrip(".:;,?")
                if not _CROSSREF.match(key):
                    keys.add(key)
    return keys


# Sentence boundaries: ". X" but not after "et al.", "e.g.", "Fig." or an initial ("J. Smith").
_SENT_END = re.compile(r"[.!?](?=\s+[A-Z\\])")
_NOT_END = re.compile(r"(?:\bet al|\be\.g|\bi\.e|\bcf|\bvs|\betc|\bFigs?|\bEqs?|\bSecs?|\bTab|\bRefs?|\bresp|"
                      r"\bapprox|\bNo|\b[A-Z])$")
_BLOCK = re.compile(r"\n\s*\n|\\(?:sub)*section\*?\{[^}]*\}|\\paragraph\*?\{[^}]*\}|\\(?:begin|end)\{[^}]*\}"
                    r"|\\item\b|\\caption\{|^#+ .*$", re.M)


def _sentence_around(text: str, pos: int) -> tuple:
    """(start, end) of the sentence containing text[pos]."""
    start = max([m.end() for m in _BLOCK.finditer(text, max(0, pos - 3000), pos)] or [max(0, pos - 3000)])
    for m in _SENT_END.finditer(text, start, pos):
        if not _NOT_END.search(text[max(0, m.start() - 8):m.start()]):
            start = m.end()
    end = min([m.start() for m in _BLOCK.finditer(text, pos, pos + 3000)] or [min(len(text), pos + 3000)])
    for m in _SENT_END.finditer(text, pos, end):
        if not _NOT_END.search(text[max(0, m.start() - 8):m.start()]):
            end = m.end()
            break
    return start, end


def citation_contexts(paths: list) -> dict:
    """{key: [{"file", "line", "sentence"}]}: every sentence in the LaTeX/Markdown sources that cites
    the key, with citations shown as [key, ...] and other markup removed."""
    files = []
    for p in paths:
        files += _walk(p, SOURCE_EXTS) if os.path.isdir(p) else [p]
    out = {}
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        tex = path.lower().endswith(".tex")
        if tex:
            text = re.sub(r"(?<!\\)%.*", "", text)
            cites = [(m.start(), m.end(), [k.strip() for k in m.group(1).split(",") if k.strip()])
                     for m in _CITE.finditer(text) if not m.group(0).startswith("\\nocite")]
        else:
            text = re.sub(r"(?ms)^(```|~~~).*?^\1", "", text)
            cites = [(m.start(), m.end(), [m.group(1).rstrip(".:;,?")]) for m in _PANDOC_CITE.finditer(text)
                     if not _CROSSREF.match(m.group(1))]
        for pos, _end, keys in cites:
            start, end = _sentence_around(text, pos)
            raw = text[start:end]
            if tex:
                raw = _CITE.sub(lambda m: " [" + ", ".join(k.strip() for k in m.group(1).split(",")) + "] ", raw)
                raw = re.sub(r"\\(?:label|ref|eqref|autoref|cref|Cref)\{[^}]*\}", " ", raw)
                sentence = detex(raw)
            else:
                sentence = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", raw)  # [text](url) -> text
            sentence = re.sub(r"\s+([,.;:])", r"\1", re.sub(r"\s+", " ", sentence)).strip()[:700]
            where = {"file": os.path.relpath(path, paths[0] if os.path.isdir(paths[0]) else os.path.dirname(path)),
                     "line": text.count("\n", 0, pos) + 1, "sentence": sentence}
            for key in keys:
                if where not in out.setdefault(key, []):
                    out[key].append(where)
    return out


class InputError(Exception):
    """A path the user gave cannot be used."""


def collect_inputs(paths: list, tmpdir: str):
    """Resolve .bib/.json files, paper directories and .zip archives (e.g. an Overleaf download)
    into (bibliography files, directories whose sources decide which keys are cited)."""
    bibs, roots, real = [], [], set()
    for p in paths:
        if not os.path.exists(p):
            raise InputError(f"no such file or directory: {p}")
        if p.lower().endswith(".zip"):
            out = os.path.join(tmpdir, f"zip{len(roots)}")
            with zipfile.ZipFile(p) as z:
                z.extractall(out)
            p = out
        if os.path.isdir(p):
            found = _walk(p, (".bib",))
            if not found:
                raise InputError(f"no .bib files in {p}")
            roots.append(p)
        else:
            found = [p]
        for f in found:  # a file given twice, or also found in a given folder, is read once
            if os.path.realpath(f) not in real:
                bibs.append(f)
                real.add(os.path.realpath(f))
    return bibs, roots


# ---------------------------------------------------------------- HTTP

class SourceError(Exception):
    """A source could not be queried (network error, rate limit, outage)."""


class Http:
    """GET with per-host spacing, retry on 429/5xx, and a small SQLite response cache."""
    SPACING = {"api.semanticscholar.org": 1.1, "export.arxiv.org": 3.0}

    def __init__(self, cache_path=None):
        self.next_ok = {}
        self.db = None
        self.cert_error = False  # a TLS certificate could not be verified (e.g. python.org Python on macOS)
        if cache_path:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            self.db = sqlite3.connect(cache_path)
            self.db.execute("CREATE TABLE IF NOT EXISTS http (url TEXT PRIMARY KEY, ts REAL, status INTEGER, body TEXT)")

    def _wait(self, host: str, delay: float):
        pause = self.next_ok.get(host, 0) - time.monotonic()
        if pause > 0:
            time.sleep(pause)
        self.next_ok[host] = time.monotonic() + delay

    def get(self, url: str, headers=None, cache_key=None):
        key = cache_key or url
        if self.db:
            row = self.db.execute("SELECT ts, status, body FROM http WHERE url = ?", (key,)).fetchone()
            if row and time.time() - row[0] < CACHE_TTL:
                return row[1], row[2]
        host = urllib.parse.urlsplit(url).hostname or ""
        status, body = 0, ""
        for attempt in range(4):
            self._wait(host, self.SPACING.get(host, 0.2))
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    status, body = resp.status, resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                status, body = e.code, e.read().decode("utf-8", "replace")
                if status == 429 or status >= 500:
                    retry = e.headers.get("Retry-After", "")
                    if retry.isdigit() and int(retry) > 60:  # e.g. a daily quota: waiting will not help
                        break
                    self.next_ok[host] = time.monotonic() + (int(retry) if retry.isdigit() else 2 ** attempt)
                    continue
            except (OSError, http.client.HTTPException) as e:
                status, body = 0, str(e)
                self.cert_error = self.cert_error or "CERTIFICATE_VERIFY_FAILED" in body
                if attempt >= 1:  # unreachable twice: offline or blocked, fail fast
                    break
                time.sleep(1)
                continue
            break
        if self.db and status in (200, 404):
            self.db.execute("INSERT OR REPLACE INTO http VALUES (?, ?, ?, ?)", (key, time.time(), status, body))
            self.db.commit()
        return status, body

    def status(self, url: str) -> int:
        """HTTP status of a URL (0 if unreachable); nothing is cached."""
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code
        except (OSError, ValueError, http.client.HTTPException):
            return 0


# ---------------------------------------------------------------- sources
# Each returns a list of records; an empty list is a definitive "no match".

def _record(source, title, authors, year, venue="", doi="", arxiv="", url="", notices=(), abstract=""):
    return {"source": source, "title": re.sub(r"\s+", " ", title or "").strip(),
            "authors": [a for a in authors if a], "year": _year(year), "venue": venue or "",
            "doi": doi or "", "arxiv": arxiv or "", "url": url or "", "notices": sorted(set(notices)),
            "abstract": _plain(abstract)}


def _plain(text: str) -> str:
    """Markup-free text (JATS/HTML tags, entities, whitespace)."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _crossref_notices(item: dict) -> list:
    """Retraction Watch / publisher notices from a Crossref or CSL record's 'updated-by'."""
    return [NOTICES[u["type"]] for u in item.get("updated-by") or [] if u.get("type") in NOTICES]


def _json(status: int, body: str, source: str):
    if status != 200:
        raise SourceError(f"{source} HTTP {status or 'unreachable'}")
    try:
        return json.loads(body)
    except ValueError:
        raise SourceError(f"{source} returned non-JSON") from None


def s2_match(http, title):
    params = {"query": title, "fields": "title,year,authors,venue,externalIds,url"}
    key = os.environ.get("S2_API_KEY")
    status, body = http.get("https://api.semanticscholar.org/graph/v1/paper/search/match?"
                            + urllib.parse.urlencode(params), {"x-api-key": key} if key else None)
    if status == 404:  # {"error": "Title match not found"}
        return []
    out = []
    for p in _json(status, body, "Semantic Scholar").get("data", []):
        ext = p.get("externalIds") or {}
        out.append(_record("Semantic Scholar", p.get("title"), [a.get("name") for a in p.get("authors") or []],
                           p.get("year"), p.get("venue"), ext.get("DOI"), ext.get("ArXiv"), p.get("url")))
    return out


def crossref_search(http, title, first_author, year, anchored=False):
    if anchored:  # restrict to works by this author, to get past same-title noise
        params = {"query.title": title, "query.author": first_author}
    else:
        params = {"query.bibliographic": " ".join(str(x) for x in (title, first_author, year) if x)}
    params.update({"rows": 5, "select": "DOI,title,author,issued,container-title,updated-by"})
    if MAILTO:
        params["mailto"] = MAILTO
    status, body = http.get("https://api.crossref.org/works?" + urllib.parse.urlencode(params))
    out = []
    for it in _json(status, body, "Crossref")["message"].get("items", []):
        authors = [a.get("name") or f"{a.get('given', '')} {a.get('family', '')}".strip() for a in it.get("author", [])]
        year = ((it.get("issued") or {}).get("date-parts") or [[None]])[0][0]
        out.append(_record("Crossref", (it.get("title") or [""])[0], authors, year,
                           (it.get("container-title") or [""])[0], it.get("DOI"), url=f"https://doi.org/{it.get('DOI')}",
                           notices=_crossref_notices(it)))
    return out


def openalex_search(http, title, author=""):
    params = {"search": title, "per_page": 5,
              "select": "doi,display_name,publication_year,authorships,primary_location,is_retracted"}
    if author:  # restrict to works with this author name, to get past same-title noise
        params["filter"] = f"raw_author_name.search:{author}"
    cache_key = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    if os.environ.get("OPENALEX_API_KEY"):
        params["api_key"] = os.environ["OPENALEX_API_KEY"]
    if MAILTO:
        params["mailto"] = MAILTO
    status, body = http.get("https://api.openalex.org/works?" + urllib.parse.urlencode(params), cache_key=cache_key)
    out = []
    for w in _json(status, body, "OpenAlex").get("results", []):
        venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
        doi = (w.get("doi") or "").replace("https://doi.org/", "")
        authors = [(a.get("author") or {}).get("display_name") for a in w.get("authorships") or []]
        out.append(_record("OpenAlex", w.get("display_name"), authors, w.get("publication_year"), venue, doi,
                           url=f"https://doi.org/{doi}" if doi else "",
                           notices=["retracted"] if w.get("is_retracted") else []))
    return out


_ATOM = "{http://www.w3.org/2005/Atom}"


def _arxiv_query(http, params):
    status, body = http.get("https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params))
    if status != 200:
        raise SourceError(f"arXiv HTTP {status or 'unreachable'}")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise SourceError("arXiv returned malformed XML") from None
    out = []
    for e in root.findall(_ATOM + "entry"):
        aid = re.sub(r"v\d+$", "", (e.findtext(_ATOM + "id") or "").split("/abs/")[-1])
        title = e.findtext(_ATOM + "title") or ""
        if title.strip() == "Error":
            continue
        authors = [a.findtext(_ATOM + "name") for a in e.findall(_ATOM + "author")]
        out.append(_record("arXiv", title, authors, e.findtext(_ATOM + "published"), "arXiv",
                           e.findtext("{http://arxiv.org/schemas/atom}doi"), aid, f"https://arxiv.org/abs/{aid}",
                           abstract=e.findtext(_ATOM + "summary") or ""))
    return out


def arxiv_title(http, title):
    return _arxiv_query(http, {"search_query": f'ti:"{norm(title)}"', "max_results": 5})


def arxiv_ids(http, ids):
    """Batch lookup: ({arxiv_id: record} for the IDs that exist, set of IDs that could not be checked)."""
    ids, found, failed = sorted(set(ids)), {}, set()
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        try:
            for r in _arxiv_query(http, {"id_list": ",".join(chunk), "max_results": len(chunk)}):
                found[r["arxiv"]] = r
        except SourceError:
            failed.update(chunk)
    return found, failed


def arxiv_first_version(http, aid):
    """The record of version 1 of an arXiv paper (its title may differ from the current version's)."""
    found = _arxiv_query(http, {"id_list": f"{aid}v1", "max_results": 1})
    return found[0] if found else None


def doi_lookup(http, doi):
    """CSL-JSON via doi.org content negotiation (Crossref, DataCite, mEDRA...); None if the DOI does not exist."""
    status, body = http.get("https://doi.org/" + urllib.parse.quote(doi, safe="/"),
                            {"Accept": "application/vnd.citationstyles.csl+json"})
    if status == 404:
        return None
    m = _json(status, body, "doi.org")
    first = lambda v: (v[0] if v else "") if isinstance(v, list) else (v or "")  # noqa: E731
    authors = [a.get("literal") or f"{a.get('given', '')} {a.get('family', '')}".strip() for a in m.get("author", [])]
    year = ((m.get("issued") or {}).get("date-parts") or [[None]])[0][0]
    return _record("DOI", first(m.get("title")), authors, year, first(m.get("container-title")), doi,
                   url=f"https://doi.org/{doi}", notices=_crossref_notices(m), abstract=m.get("abstract") or "")


def openalex_abstract(http, doi):
    """Abstract of a DOI from OpenAlex's single-work endpoint (free of charge), '' if it has none."""
    params = {"select": "abstract_inverted_index"}
    if MAILTO:
        params["mailto"] = MAILTO
    status, body = http.get(f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi, safe='/')}?"
                            + urllib.parse.urlencode(params))
    if status == 404:
        return ""
    index = _json(status, body, "OpenAlex").get("abstract_inverted_index") or {}
    words = sorted((pos, word) for word, positions in index.items() for pos in positions)
    return " ".join(word for _, word in words)


# ---------------------------------------------------------------- local DBLP index (optional)
# `citecheck --build-dblp` downloads dblp's monthly XML release (~1.1 GB) from Schloss Dagstuhl and
# indexes it in SQLite FTS5 (one-off, ~15 min, ~3 GB). Computer-science references are then
# checked offline, before any rate-limited API is asked.

DBLP_RELEASES = "https://drops.dagstuhl.de/entities/collection/dblp"
_DBLP_RECORD = re.compile(r'<(article|inproceedings|incollection|book|phdthesis|mastersthesis|data)\s[^>]*?key="([^"]+)"')
_DBLP_FIELD = re.compile(r"<(author|title|year|journal|booktitle|ee|volume)(?:\s[^>]*)?>(.*)</\1>")
_STOP = set("a an and are as at by for from in into is its of on or the to toward towards using via with".split())
_dblp = None


def cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "citecheck")


def dblp_db():
    """The local DBLP index, or None if it has not been built."""
    global _dblp
    if "dblp" in ABLATE:
        return None
    path = os.path.join(cache_dir(), "dblp.sqlite")
    if _dblp is None and os.path.exists(path):
        _dblp = sqlite3.connect(path)
        _dblp.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.vocab USING fts5vocab(main, pub_fts, 'row')")
    return _dblp


def _download_dblp(dest: str) -> str:
    def fetch(url):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        return urllib.request.urlopen(req, timeout=60)
    page = fetch(DBLP_RELEASES).read().decode("utf-8", "replace")
    artifact = re.search(r"https://drops\.dagstuhl\.de/entities/artifact/10\.4230/dblp\.xml\.\d{4}-\d{2}-\d{2}", page)
    link = re.search(r'href="([^"]+\.xml\.gz)"', fetch(artifact.group(0)).read().decode("utf-8", "replace"))
    url = urllib.parse.urljoin(artifact.group(0), link.group(1))
    print(f"downloading {url}", file=sys.stderr, flush=True)
    with fetch(url) as resp, open(dest, "wb") as out:
        total, done = int(resp.headers.get("Content-Length") or 0), 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if done % (100 << 20) < (1 << 20):
                print(f"  {done >> 20} / {total >> 20} MB", file=sys.stderr, flush=True)
    return dest


def build_dblp(dump=None):
    """Download (unless a local dump is given) and index dblp.xml.gz into cache_dir()/dblp.sqlite."""
    os.makedirs(cache_dir(), exist_ok=True)
    dump = dump or _download_dblp(os.path.join(cache_dir(), "dblp.xml.gz"))
    path = os.path.join(cache_dir(), "dblp.sqlite")
    tmp = path + ".building"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.executescript("""PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        CREATE TABLE pub(id INTEGER PRIMARY KEY, key TEXT, title TEXT, authors TEXT, year INTEGER,
                         venue TEXT, doi TEXT, arxiv TEXT);
        CREATE VIRTUAL TABLE pub_fts USING fts5(title, content='pub', content_rowid='id');""")
    insert = "INSERT INTO pub(key, title, authors, year, venue, doi, arxiv) VALUES (?, ?, ?, ?, ?, ?, ?)"
    rows, rec, count = [], None, 0
    print("indexing (takes a while)", file=sys.stderr, flush=True)
    with gzip.open(dump, "rt", encoding="iso-8859-1") as fh:  # other characters are XML entities
        for line in fh:
            if rec is not None and line.startswith("</" + rec["tag"] + ">"):
                if rec.get("title"):
                    doi = next((u.split("doi.org/", 1)[1] for u in rec["ee"] if "doi.org/10." in u), "")
                    arxiv = next((u.split("/abs/", 1)[1] for u in rec["ee"] if "arxiv.org/abs/" in u), "")
                    if not arxiv and rec.get("volume", "").startswith("abs/"):
                        arxiv = rec["volume"][4:]
                    rows.append((rec["key"], rec["title"], "|".join(rec["author"]), _year(rec.get("year")),
                                 rec.get("journal") or rec.get("booktitle", ""), doi, arxiv))
                rec = None
                if len(rows) >= 100000:
                    db.executemany(insert, rows)
                    count += len(rows)
                    rows = []
                    print(f"  {count:,} records", file=sys.stderr, flush=True)
                line = line.split(">", 1)[1]  # the next record often opens on the same line
            if rec is None:
                m = _DBLP_RECORD.match(line)
                if m:
                    rec = {"tag": m.group(1), "key": m.group(2), "author": [], "ee": []}
                continue
            m = _DBLP_FIELD.match(line)
            if m:
                name, value = m.group(1), html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
                if name == "author":
                    rec["author"].append(re.sub(r"\s\d{4}$", "", value))  # "Wei Zhang 0001"
                elif name == "ee":
                    rec["ee"].append(value)
                else:
                    rec[name] = value.rstrip(".") if name == "title" else value
    db.executemany(insert, rows)
    print(f"  {count + len(rows):,} records; building the full-text index", file=sys.stderr, flush=True)
    db.execute("INSERT INTO pub_fts(pub_fts) VALUES('rebuild')")
    db.commit()
    db.close()
    os.replace(tmp, path)
    print(f"done: {path}", file=sys.stderr, flush=True)


def dblp_search(_http, title):
    """Title search in the local DBLP index: all words first, then any of the five rarest words
    (fast, and enough to find a title with one word changed)."""
    db = dblp_db()
    words = [w for w in norm(title).split() if w not in _STOP][:12]
    if not words:
        return []
    sql = ("SELECT p.title, p.authors, p.year, p.venue, p.doi, p.arxiv, p.key FROM pub_fts JOIN pub p "
           "ON p.id = pub_fts.rowid WHERE pub_fts MATCH ? ORDER BY rank LIMIT 10")
    try:
        rows = db.execute(sql, (" ".join(f'"{w}"' for w in words),)).fetchall()
        if not any(title_sim(title, r[0]) >= TITLE_NEAR for r in rows) and len(words) > 2:
            docs = dict(db.execute(f"SELECT term, doc FROM temp.vocab WHERE term IN ({','.join('?' * len(words))})",
                                   words).fetchall())
            rare = sorted(set(words), key=lambda w: docs.get(w, 0))[:5]
            rows += db.execute(sql, (" OR ".join(f'"{w}"' for w in rare),)).fetchall()
    except sqlite3.Error as err:
        raise SourceError(f"DBLP index: {err}") from None
    return [_record("DBLP", r[0], r[1].split("|") if r[1] else [], r[2], r[3], r[4], r[5],
                    f"https://dblp.org/rec/{r[6]}") for r in rows]


# ---------------------------------------------------------------- checking

class Checker:
    def __init__(self, http):
        self.http = http
        self.failures = Counter()
        self.disabled = set()
        self.arxiv, self.arxiv_failed = {}, set()

    def _call(self, name, fn, *args):
        """Run a source query; three failures in a row switch that source off for the run."""
        if name in self.disabled:
            raise SourceError(f"{name} unavailable (switched off after repeated failures)")
        try:
            out = fn(self.http, *args)
        except SourceError:
            self.failures[name] += 1
            if self.failures[name] >= 3:
                self.disabled.add(name)
            raise
        self.failures[name] = 0
        return out

    def prefetch_arxiv(self, entries):
        ids = [e["arxiv"] for e in entries if e["arxiv"]]
        if not ids or "ids" in ABLATE:
            return
        self.arxiv, self.arxiv_failed = self._call("arXiv", arxiv_ids, ids)

    def _searches(self, e):
        """Free and local sources first; rationed ones last, so they are only asked when needed."""
        first = family_name(e["authors"][0]) if e["authors"] else ""
        keyed_s2 = bool(os.environ.get("S2_API_KEY"))
        s2 = [("Semantic Scholar", s2_match, (e["title"],))]
        return ([("DBLP", dblp_search, (e["title"],))] if dblp_db() else []) + (s2 if keyed_s2 else []) + [
            ("Crossref", crossref_search, (e["title"], first, e["year"])),
            ("arXiv", arxiv_title, (e["title"],))] + ([] if keyed_s2 else s2) + [
            ("OpenAlex", openalex_search, (e["title"],))]  # $0.001 a search; ~100 a day without a key

    @staticmethod
    def _score(e, rec):
        share, missing = author_overlap(e["authors"], rec["authors"])
        ydiff = e["year"] - rec["year"] if e["year"] and rec["year"] and e["type"] not in BOOK_TYPES else None
        sim = round(title_sim(e["title"], rec["title"]), 3)
        return {"record": rec, "title_sim": sim, "author_share": share, "missing_authors": missing,
                "record_within": record_within(e["authors"], rec["authors"]),
                "year_diff": ydiff, "title_diff": title_changes(e["title"], rec["title"]) if sim >= TITLE_NEAR else []}

    @staticmethod
    def _consistent(c):
        return c["title_sim"] >= TITLE_NEAR and (c["author_share"] is None or c["author_share"] >= AUTHORS_MIN)

    @staticmethod
    def _year_ok(c):
        return c["year_diff"] is None or YEAR_LAG[0] <= c["year_diff"] <= YEAR_LAG[1]

    def _clean(self, c):
        return (c["title_sim"] >= TITLE_SAME and not c["title_diff"] and c["author_share"] in (None, 1.0)
                and self._year_ok(c))

    def _check_id(self, e, label, rec, cands, problems, notes):
        if rec is None:
            problems.append(f"{label} does not exist")
            return
        c = self._score(e, rec)
        if c["title_sim"] >= TITLE_NEAR:
            cands.append(c)
        elif self._first_version_matches(e, label, c, cands, notes):
            return
        elif (c["author_share"] is not None and c["author_share"] >= AUTHORS_MIN and self._year_ok(c)
              and word_overlap(e["title"], rec["title"]) >= 0.4):  # same authors, related title
            notes.append(f'{label} is titled "{rec["title"]}" there; same authors, so taken as a preprint '
                         f"or retitled version")
            cands.append(dict(c, title_sim=TITLE_NEAR, retitled=True))
        else:
            problems.append(f'{label} resolves to a different work: "{rec["title"]}"')

    def _first_version_matches(self, e, label, c, cands, notes) -> bool:
        """An arXiv paper renamed in a later version, cited under its first title by the same authors."""
        if not label.startswith("arXiv:") or not c["author_share"] or c["author_share"] < 1.0 or "retitle" in ABLATE:
            return False
        try:
            first = self._call("arXiv", arxiv_first_version, e["arxiv"])
        except SourceError:
            return False
        if not first or title_sim(e["title"], first["title"]) < TITLE_NEAR:
            return False
        notes.append(f'{label}: the cited title is that of version 1; the current version is titled '
                     f'"{c["record"]["title"]}"')
        cands.append(self._score(e, dict(c["record"], title=first["title"], authors=first["authors"])))
        return True

    def check(self, e):
        res = {"key": e["key"], "verdict": "", "issues": [], "notes": [], "match": None,
               "cited": {k: e[k] for k in ("type", "title", "authors", "year", "doi", "arxiv", "url")}}
        if not e["title"]:
            res.update(verdict="SKIPPED", issues=["entry has no title"])
            return res
        cands, id_problems, answered = [], [], []
        res["id_unchecked"] = False
        if e["year"] and e["year"] > THIS_YEAR + 1:
            id_problems.append(f"cited year {e['year']} is in the future")

        if e["doi"].startswith("10.5555/"):  # ACM Digital Library IDs, never registered at doi.org
            res["notes"].append(f"DOI {e['doi']} is an ACM identifier that doi.org does not resolve; not checked")
        elif e["doi"] and "ids" not in ABLATE:
            try:
                rec = self._call("doi.org", doi_lookup, e["doi"])
                self._check_id(e, f"DOI {e['doi']}", rec, cands, id_problems, res["notes"])
            except SourceError as err:
                res["notes"].append(f"DOI not checked: {err}")
                res["id_unchecked"] = True
        if e["arxiv"] and "ids" not in ABLATE:
            if e["arxiv"] in self.arxiv_failed:
                res["notes"].append("arXiv ID not checked: arXiv unavailable")
                res["id_unchecked"] = True
            else:
                self._check_id(e, f"arXiv:{e['arxiv']}", self.arxiv.get(e["arxiv"]), cands, id_problems, res["notes"])

        if not any(self._clean(c) for c in cands if not c.get("retitled")):
            for name, fn, args in self._searches(e):
                try:
                    recs = self._call(name, fn, *args)
                except SourceError as err:
                    res["notes"].append(str(err))
                    continue
                answered.append(name)
                cands += [self._score(e, r) for r in recs]
                if any(self._clean(c) for c in cands):
                    break
            first = family_name(e["authors"][0]).split() if e["authors"] else []
            # title search found nothing by these authors: search within the first author's works
            for name, fn, args in (("Crossref", crossref_search, (e["title"], first[-1] if first else "", None, True)),
                                   ("OpenAlex", openalex_search, (e["title"], first[-1] if first else ""))):
                if not first or "anchored" in ABLATE or any(self._consistent(c) for c in cands):
                    break
                try:
                    cands += [self._score(e, r) for r in self._call(name, fn, *args)]
                except SourceError as err:
                    res["notes"].append(str(err))

        if e["venue"] and "http" not in e["venue"] and "DBLP" not in answered and dblp_db():
            try:  # the venue check needs every indexed version of the paper, not only the one an ID gave
                cands += [self._score(e, r) for r in self._call("DBLP", dblp_search, e["title"])]
                answered.append("DBLP")
            except SourceError as err:
                res["notes"].append(str(err))
        # a record with other authors is evidence only if its title is the cited title; a merely similar
        # title by other people is a different work
        # (for books, a same-title record by other people is usually a review or another book, and a
        # short title such as "LangChain" or "Deep learning" is shared by many unrelated works)
        distinctive = len(set(norm(e["title"]).split()) - _STOP) >= 3
        near = [c for c in cands if c["title_sim"] >= TITLE_NEAR and
                (self._consistent(c) or (c["title_sim"] >= TITLE_SAME and not c["title_diff"]
                                         and e["type"] not in UNINDEXED_TYPES and distinctive))]
        if not near:
            return self._unfound(e, res, cands, id_problems, answered)
        best = max(near, key=lambda c: (self._consistent(c), not c.get("retitled"), c["title_sim"] >= TITLE_SAME,
                                        1.0 if c["author_share"] is None else c["author_share"],
                                        -abs(c["year_diff"] or 0), c["title_sim"]))
        res["match"] = {**best["record"], "title_sim": best["title_sim"], "author_share": best["author_share"]}
        rec, issues = best["record"], list(id_problems)
        if not self._consistent(best) and best["record_within"]:  # the record is incomplete
            issues.append(f"cited author(s) not on the record: {'; '.join(best['missing_authors'])} "
                          f"(the record lists only {len(rec['authors'])} author(s))")
        elif not self._consistent(best):
            issues.append(f"{len(best['missing_authors'])} of {len(e['authors'])} cited authors are not on the "
                          f"record: {'; '.join(best['missing_authors'])}")
            res.update(verdict="MISMATCH", issues=issues)
            return res
        if best.get("retitled"):
            issues.append(f'the cited identifier\'s record is titled "{rec["title"]}" (same authors: a preprint '
                          f'or retitled version?)')
        elif best["title_sim"] < TITLE_SAME or best["title_diff"]:
            changed = "; ".join(f'missing "{b}"' if not a else f'extra "{a}"' if not b else f'"{a}" vs "{b}"'
                                for a, b in best["title_diff"][:3])
            issues.append(f'title differs from the record{f" ({changed})" if changed else ""}: "{rec["title"]}"')
        if (e["authors"] and not e["etal"] and len(rec["authors"]) > len(e["authors"])
                and not all(_named_org(a) for a in e["authors"])):
            issues.append(f"cites {len(e['authors'])} of the record's {len(rec['authors'])} authors, without 'and others'")
        if best["missing_authors"]:
            issues.append(f"cited author(s) not on the record: {'; '.join(best['missing_authors'])}")
        conflicts = given_name_conflicts(e["authors"], rec["authors"]) if "names" not in ABLATE else []
        if conflicts:
            issues.append(f"given name differs from the record: {'; '.join(conflicts)}")
        if not self._year_ok(best):
            issues.append(f"cited year {e['year']}, record year {rec['year']}")
        venue = self._venue_issue(e, near, answered)
        if venue:
            issues.append(venue)
        notices = sorted({n for c in near if self._consistent(c) for n in c["record"]["notices"]})
        if notices:
            issues.append(f"the work has been {' and '.join(notices)} (Crossref / Retraction Watch)")
        if best["author_share"] is None and e["authors"]:
            res["notes"].append("record lists no authors; authors not compared")
        verdict = "MISMATCH" if id_problems else ("CHECK" if issues else "VERIFIED")
        res.update(verdict=verdict, issues=issues)
        return res

    def _venue_issue(self, e, near, answered) -> str:
        """A cited major venue that no version of the paper appeared at."""
        text = e["venue"].strip()
        if not text or "http" in text or "venue" in ABLATE:
            return ""
        cited = venue_ids(text)
        # DBLP writes workshops as "WMT@EACL": the host conference is not where the paper appeared
        found = [set() if "@" in c["record"]["venue"] else venue_ids(c["record"]["venue"])
                 for c in near if self._consistent(c)]
        published = set().union(*found) - {"arXiv"} if found else set()
        if not cited:  # a venue name we do not recognise, for a paper that appeared at venues we do
            authoritative = "DBLP" in answered or any(c["record"]["source"] == "DOI" for c in near)
            if published and all(found) and authoritative:
                return f'cited at "{_short(text, 60)}", but the paper appeared at {"/".join(sorted(published))}'
            return ""
        cited -= {"arXiv"}
        if not cited or any(cited & v for v in found):
            return ""
        if published:
            return f"cited at {'/'.join(sorted(cited))}, but the record is from {'/'.join(sorted(published))}"
        if found and "DBLP" in answered and e["year"] and e["year"] <= THIS_YEAR:
            only = "only a preprint version was found" if all(found) else "no version there was found"
            return (f"cited at {'/'.join(sorted(cited))}, but {only}"
                    + (" (the index may lag for this year's venues)" if e["year"] == THIS_YEAR else ""))
        return ""

    def _unfound(self, e, res, cands, id_problems, answered):
        res["issues"] = list(id_problems)
        closest = max(cands, key=lambda c: c["title_sim"], default=None)
        if closest and closest["title_sim"] >= 0.6:
            res["notes"].append(f'closest title: "{closest["record"]["title"]}" '
                                f'({closest["record"]["source"]}, similarity {closest["title_sim"]})')
        if e["url"]:
            code = self.http.status(e["url"])
            if 200 <= code < 400:
                res["issues"].append(f"not an indexed paper, but the URL is live (HTTP {code}): confirm it is the cited work")
                res["verdict"] = "CHECK"
                return res
            if code not in (0, 404, 410):
                res["issues"].append(f"not an indexed paper; the URL refused an automated check (HTTP {code})")
                res["verdict"] = "CHECK"
                return res
            res["issues"].append(f"URL is dead ({'unreachable' if code == 0 else f'HTTP {code}'})")
        # a broad index must have answered; DBLP covers computer science only, so it counts with arXiv
        broad = {"Semantic Scholar", "OpenAlex"} & set(answered) or {"DBLP", "arXiv"} <= set(answered)
        if len(answered) >= 2 and broad:
            same_title = any(c["title_sim"] >= TITLE_SAME and not c["title_diff"] for c in cands)
            res["issues"].insert(0, f"only works by other authors have this title in {', '.join(answered)}"
                                 if same_title else f"no work with this title in {', '.join(answered)}")
            grey = e["authors"] and all(is_org(a) for a in e["authors"])
            if res.get("id_unchecked") and not id_problems:
                res["issues"].append("the entry's own DOI or arXiv ID could not be checked (source unavailable)")
                res["verdict"] = "ERROR"
            elif grey and not id_problems:
                res["issues"].append("organisation-authored documents (reports, model cards, blog posts) are rarely "
                                     "indexed: confirm it exists, and add a url so the next run can check it")
                res["verdict"] = "CHECK"
            elif (e["type"] in UNINDEXED_TYPES or UNINDEXED_VENUE.search(e["venue_text"])) and not id_problems:
                res["issues"].append(f"@{e['type']} entries are often not indexed: confirm it exists, "
                                     f"and add a url or doi so the next run can check it")
                res["verdict"] = "CHECK"
            else:
                res["verdict"] = "NOT_FOUND"
        else:
            res["issues"].insert(0, "could not reach enough sources to decide")
            res["verdict"] = "ERROR"
        return res


# ---------------------------------------------------------------- report

_OUTAGE = re.compile(r" HTTP |unavailable \(switched off|not checked: ")


def _short(s: str, n: int = 90) -> str:
    return s if len(s) <= n else s[:n - 3] + "..."


def _describe(m: dict) -> str:
    names = m["authors"][:3] + (["et al."] if len(m["authors"]) > 3 else [])
    parts = [", ".join(names) or "(no authors)", str(m["year"] or "n.d."), m["venue"], m["url"]]
    return f'"{_short(m["title"], 80)}" -- ' + " | ".join(p for p in parts if p) + f" [{m['source']}]"


def duplicates(results) -> list:
    """Groups of keys whose best match is the same work (same title and first author)."""
    groups = {}
    for r in results:
        m = r["match"]
        if m and m["authors"]:
            ident = (norm(m["title"]), family_name(m["authors"][0]))
            groups.setdefault(ident, []).append(r["key"])
    return [keys for keys in groups.values() if len(keys) > 1]


def hints(results, checker) -> list:
    """Advice when rationed sources limited the run."""
    failures = [n for r in results for n in r["notes"] if " HTTP " in n]
    if failures and all("unreachable" in n for n in failures):
        if getattr(checker.http, "cert_error", False) is True:
            return ["no source could be reached because Python could not verify their SSL certificates. With "
                    "Python from python.org on macOS, run 'Install Certificates.command' in its Applications folder; "
                    "behind a company proxy, point SSL_CERT_FILE at the proxy's certificate bundle"]
        return ["no source could be reached: check the network connection or proxy"]
    trouble = " ".join(n for r in results for n in r["notes"]) + " ".join(checker.disabled)
    out = []
    if "Semantic Scholar" in trouble and not os.environ.get("S2_API_KEY"):
        out.append("Semantic Scholar rate-limits anonymous use; a free key (S2_API_KEY) makes it reliable")
    if "OpenAlex" in trouble and not os.environ.get("OPENALEX_API_KEY"):
        out.append("OpenAlex allows about 100 searches a day without a key; a free key (OPENALEX_API_KEY) allows 1,000")
    if (out or any(r["verdict"] == "ERROR" for r in results)) and not dblp_db():
        out.append("run `citecheck --build-dblp` once to check computer-science references offline")
    return out


def suggested_bibtex(r) -> str:
    """A corrected entry for a flagged reference, built only from the matched record's metadata."""
    m, c = r["match"], r["cited"]
    kind = c["type"] if c["type"] and c["type"] != "json" else "article"
    fields = [("title", "{" + m["title"] + "}"), ("author", " and ".join(m["authors"])), ("year", m["year"]),
              ("booktitle" if kind == "inproceedings" else "journal", m["venue"]),
              ("doi", m["doi"]), ("eprint", m["arxiv"]), ("archiveprefix", "arXiv" if m["arxiv"] else ""),
              ("url", "" if m["source"] == "DBLP" else m["url"])]  # a DBLP link is a catalogue page
    body = ",\n".join(f"  {k:<13} = {{{v}}}" for k, v in fields if v)
    notes = "".join(f"% {line}\n" for line in r["issues"])
    return f"% citecheck {r['verdict']}: {r['key']} (from {m['source']})\n{notes}@{kind}{{{r['key']},\n{body}\n}}\n"


def render(results, source, extra_lines):
    counts = Counter(r["verdict"] for r in results)
    lines = [f"citecheck {__version__}: {len(results)} reference{'' if len(results) == 1 else 's'} in {source}",
             "  " + "   ".join(f"{v} {counts[v]}" for v in ORDER if counts[v])] + extra_lines
    for r in sorted(results, key=lambda r: ORDER.index(r["verdict"])):
        if r["verdict"] == "VERIFIED":
            continue
        lines += ["", f"{r['verdict']:<9}  {r['key']}  \"{_short(r['cited']['title'])}\""]
        lines += [f"           - {i}" for i in r["issues"]]
        if r["match"]:
            lines.append(f"           record: {_describe(r['match'])}")
        # source outages are summarised in the header; the JSON report keeps every note
        lines += [f"           note: {n}" for n in dict.fromkeys(r["notes"]) if not _OUTAGE.search(n)]
    if results and not counts.keys() - {"VERIFIED"}:
        lines += ["", "Every reference matched an indexed record."]
    return "\n".join(lines)


def claims_payload(checker, results, contexts: dict) -> list:
    """For each checked reference: the sentences that cite it and the cited work's abstract, for a
    reader (Claude, in the claims skill) to judge whether the work supports what is attributed to it."""
    matched = [r["match"] for r in results if r["match"]]
    for m in matched:  # conference records often carry no identifier: borrow the arXiv twin's from DBLP
        if not (m["arxiv"] or m["doi"] or m.get("abstract")) and dblp_db():
            try:
                twin = [t for t in dblp_search(None, m["title"]) if t["arxiv"] and title_sim(m["title"], t["title"]) >= TITLE_SAME]
            except SourceError:
                twin = []
            m["arxiv"] = twin[0]["arxiv"] if twin else ""
    missing = [m["arxiv"] for m in matched if m["arxiv"] and not m.get("abstract")]
    if missing:
        try:
            found, _ = arxiv_ids(checker.http, missing)
        except SourceError:
            found = {}
        for m in matched:
            if not m.get("abstract") and m["arxiv"] in found:
                m["abstract"], m["abstract_source"] = found[m["arxiv"]]["abstract"], "arXiv"
    for m in matched:
        if not m.get("abstract") and m["doi"]:
            try:
                m["abstract"], m["abstract_source"] = openalex_abstract(checker.http, m["doi"]), "OpenAlex"
            except SourceError:
                pass
    out = []
    for r in results:
        m = r["match"] or {}
        out.append({"key": r["key"], "verdict": r["verdict"], "cited_title": r["cited"]["title"],
                    "work": {k: m.get(k) for k in ("title", "year", "venue", "url")} if m else None,
                    "work_authors": (m.get("authors") or [])[:6],
                    "abstract": m.get("abstract", ""), "abstract_source": m.get("abstract_source") or m.get("source", ""),
                    "contexts": contexts.get(r["key"], [])})
    return out


_BIB_INPUT = (".bib", ".bibtex", ".json", ".ris", ".xml", ".zip")
HELP_EPILOG = """verdicts:
  VERIFIED   title, authors and year agree with an indexed record
  CHECK      the work exists but a field differs (a title word, the year, an author or first name,
             a truncated author list, the venue, a preprint cited as published), it is retracted,
             or it is not an indexed paper (a live URL, thesis, report or book)
  MISMATCH   the title exists but most cited authors are not on it, or the DOI/arXiv ID points
             to a different work or to nothing
  NOT_FOUND  no index has a work with this title
  ERROR      too few sources answered to decide

exit status: 0 no problems found (CHECK items may still need a look), 1 at least one NOT_FOUND or
  MISMATCH, 2 unusable input, 3 some references could not be checked

environment: S2_API_KEY and OPENALEX_API_KEY (free keys; fewer ERROR verdicts), CITECHECK_MAILTO
  (an email address for the Crossref and OpenAlex polite pools)
cache and dblp index: ~/.cache/citecheck (or $XDG_CACHE_HOME/citecheck)
"""


def split_inputs(inputs: list, cited_in):
    """--cited-in takes several values, so it swallows a bibliography given after it ("--cited-in main.tex refs.bib")."""
    if inputs or not cited_in:
        return inputs, cited_in
    moved = [p for p in cited_in if p.lower().endswith(_BIB_INPUT)]
    return moved, [p for p in cited_in if p not in moved]


def assign_keys(files: list):
    """Merge the entries of several files. Your own keys are kept (a duplicate keeps its first entry); a key
    citecheck made up (RIS, EndNote, JSON without keys) gets the first free variant of its base, so it never
    displaces another reference. Returns (entries, duplicate keys)."""
    taken = {e["key"] for loaded in files for e in loaded if not e["generated"]}
    entries, earlier, dupes = [], set(), set()
    for loaded in files:
        here = set()
        for e in loaded:
            if e["generated"]:
                e["key"] = _free_key(e["key_base"] or e["key"], taken)
                taken.add(e["key"])
            elif e["key"] in earlier:
                dupes.add(e["key"])
                continue
            here.add(e["key"])
            entries.append(e)
        earlier |= here
    return entries, sorted(dupes)


def prepare_outputs(paths: list):
    """Create the folders of the output files before the run, so a long run cannot fail at the end."""
    for p in paths:
        if p and os.path.isdir(p):
            raise InputError(f"{p} is a folder; give a file name for the output")
        if p:
            os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)


def exit_status(verdicts, unparsed: int = 0) -> int:
    """1: a reference is NOT_FOUND or MISMATCH; 3: some references could not be checked; 0: none of these."""
    verdicts = set(verdicts)
    return 1 if verdicts & set(FLAGGED) else 3 if "ERROR" in verdicts or unparsed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="citecheck", description=__doc__.split("\n\n")[0], epilog=HELP_EPILOG,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="*", metavar="PATH",
                    help=".bib, .json, .ris or EndNote .xml files, a paper directory, or an Overleaf .zip download")
    ap.add_argument("--cited-in", nargs="+", metavar="SRC",
                    help="check only keys cited in these .tex/.md/.qmd files or directories "
                         "(the default for a directory or .zip input)")
    ap.add_argument("--all", action="store_true", help="check every entry, cited or not")
    ap.add_argument("--json", metavar="OUT", help="also write per-entry results to this JSON file")
    ap.add_argument("--claims", metavar="OUT.json",
                    help="also write, for each reference, the sentences that cite it and the cited work's abstract "
                         "(input for the claims skill)")
    ap.add_argument("--fixes", metavar="OUT.bib",
                    help="write corrected entries for MISMATCH/CHECK references that matched a real record "
                         "(your bibliography is never modified)")
    ap.add_argument("--no-cache", action="store_true", help="bypass the 7-day HTTP cache")
    ap.add_argument("--build-dblp", nargs="?", const="", metavar="DUMP",
                    help="download dblp's monthly release (~1.1 GB) and index it for offline checks, then exit; "
                         "DUMP is a dblp.xml.gz you already downloaded. The download and the index (about 3.3 GB "
                         "together) are kept in the cache folder; the download can be deleted afterwards")
    ap.add_argument("--version", action="version", version=f"citecheck {__version__}")
    args = ap.parse_args(argv)
    args.inputs, args.cited_in = split_inputs(args.inputs, args.cited_in)
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    if args.build_dblp is not None:
        build_dblp(args.build_dblp or None)
        return 0
    if not args.inputs:
        ap.error("give a .bib, .json, .ris or EndNote .xml file, a paper directory or a .zip")

    with tempfile.TemporaryDirectory() as tmp:
        try:
            prepare_outputs([args.json, args.fixes, args.claims])
            bibs, roots = collect_inputs(args.inputs, tmp)
            files, extra, errors, unparsed_keys = [], [], [], set()
            for path in bibs:
                loaded, errors = load_entries(path)
                extra += [f"  warning: {os.path.basename(path)}: {e}" for e in errors]
                unparsed_keys |= {getattr(e, "key", "") or f"?{path}:{i}" for i, e in enumerate(errors)}
                files.append(loaded)
            entries, dupes = assign_keys(files)
            seen = {e["key"] for e in entries}
            if dupes:
                extra.append(f"  warning: duplicate keys (first one kept): {', '.join(dupes)}")
            if not entries:
                raise InputError(f"no references found in {', '.join(args.inputs)}"
                                 + (f" ({errors[0]})" if errors else ""))
            sources = args.cited_in or roots
            cited = cited_keys(sources) if sources and not args.all else {"*"}
            contexts = citation_contexts(sources) if args.claims and sources else {}  # before a .zip's folder goes
        except (InputError, OSError, ValueError, zipfile.BadZipFile) as err:
            print(f"citecheck: {err}", file=sys.stderr)
            return 2
    unparsed_keys -= seen  # a key that parsed in another file was checked
    unparsed = len(unparsed_keys)
    if "*" not in cited:
        broken = sorted(cited & unparsed_keys)
        missing = sorted(cited - seen - unparsed_keys)
        unparsed = len(broken)  # an entry the paper does not cite does not matter
        if broken:
            extra.append(f"  warning: cited but could not be parsed: {', '.join(broken)}")
        total, entries = len(entries), [e for e in entries if e["key"] in cited]
        if not entries:
            print(f"citecheck: none of the {total} entries in the bibliography is cited in the sources; "
                  f"use --all to check every entry", file=sys.stderr)
            return 2
        extra.append(f"  checking the {len(entries)} entries cited in the sources (--all checks every entry)")
        if missing:
            extra.append(f"  warning: cited but not in the bibliography: {', '.join(missing)}")

    checker = Checker(Http(None if args.no_cache else os.path.join(cache_dir(), "http.sqlite")))
    checker.prefetch_arxiv(entries)
    results = []
    for i, e in enumerate(entries, 1):
        r = checker.check(e)
        results.append(r)
        print(f"[{i}/{len(entries)}] {r['verdict']:<9} {e['key']}", file=sys.stderr, flush=True)
    undecided = [i for i, r in enumerate(results) if r["verdict"] == "ERROR"]
    if undecided and checker.disabled:  # give switched-off sources one more chance
        print(f"retrying {len(undecided)} undecided entries", file=sys.stderr, flush=True)
        checker.disabled.clear()
        checker.failures.clear()
        for i in undecided:
            results[i] = checker.check(entries[i])
    extra += [f"  warning: the same work is cited under several keys: {', '.join(keys)}" for keys in duplicates(results)]
    if checker.disabled:
        extra.append(f"  note: stopped asking {', '.join(sorted(checker.disabled))} after repeated failures "
                     f"(rate limit, quota or outage)")
    extra += [f"  hint: {h}" for h in hints(results, checker)]

    print(render(results, ", ".join(args.inputs), extra))
    fixable = [r for r in results if r["verdict"] in ("MISMATCH", "CHECK") and r["match"]]
    if args.fixes and fixable:
        with open(args.fixes, "w", encoding="utf-8") as fh:
            fh.write("% Corrected entries suggested by citecheck from index metadata. Review each one:\n"
                     "% a MISMATCH may mean you meant a different paper, not that these authors are right.\n\n")
            fh.write("\n".join(suggested_bibtex(r) for r in fixable))
        print(f"\n{len(fixable)} suggested corrections written to {args.fixes}")
    if args.claims:
        payload = claims_payload(checker, results, contexts)
        with open(args.claims, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, ensure_ascii=False)
        n_ctx = sum(len(p["contexts"]) for p in payload)
        n_abs = sum(1 for p in payload if p["abstract"])
        print(f"\nclaims: {n_ctx} citing sentences for {len(payload)} references, {n_abs} with an abstract, "
              f"written to {args.claims}")
    for r in results:  # abstracts are for --claims only; keep the report small
        if r["match"]:
            r["match"] = {k: v for k, v in r["match"].items() if k not in ("abstract", "abstract_source")}
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"version": __version__, "inputs": args.inputs, "duplicates": duplicates(results),
                       "results": results}, fh, indent=2, ensure_ascii=False)
    return exit_status((r["verdict"] for r in results), unparsed)


if __name__ == "__main__":
    sys.exit(main())

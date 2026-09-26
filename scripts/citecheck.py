#!/usr/bin/env python3
"""Check that every reference in a bibliography exists and matches its indexed record.

Each entry is looked up by DOI (doi.org) and arXiv ID (arXiv API) when it carries one,
then searched by title in Semantic Scholar, OpenAlex, Crossref and arXiv. The
best-matching record is compared with the citation field by field: title, author
family names and year.

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
import html
import http.client
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter

__version__ = "0.1.0"

TITLE_SAME = 0.95  # title similarity at or above this: same title
TITLE_NEAR = 0.85  # at or above this: same work, reworded or mistyped title
AUTHORS_MIN = 0.5  # below this share of cited authors on the record: MISMATCH
YEAR_LAG = (-1, 2)  # cited year minus record year: preprints are often published 1-2 years later
CACHE_TTL = 7 * 86400
USER_AGENT = f"citecheck/{__version__} (+https://github.com/sunami-lab/citecheck)"
MAILTO = os.environ.get("CITECHECK_MAILTO", "")
FLAGGED = ("NOT_FOUND", "MISMATCH")
ORDER = ("NOT_FOUND", "MISMATCH", "ERROR", "CHECK", "SKIPPED", "VERIFIED")


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
    s = _ACCENT.sub(r"\1", s or "")
    s = re.sub(r"\\(ss|oe|OE|ae|AE|aa|AA|o|O|l|L|i|j)(?![A-Za-z])", lambda m: _LETTER_CMD[m.group(1)], s)
    s = re.sub(r"\\([&%$#_])", r"\1", s)
    s = re.sub(r"\\[A-Za-z]+\*?", " ", s)  # drop remaining commands, keep their arguments
    s = s.replace("~", " ").replace("--", "-")
    s = re.sub(r"[{}$]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def norm(s: str) -> str:
    """Lowercase ASCII words for comparison: no accents, markup or punctuation."""
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))  # Crossref titles carry <i>, <sub>...
    s = unicodedata.normalize("NFKD", detex(s).translate(_TRANSLIT))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _main_title(s: str) -> str:
    """Title before a subtitle separator ('Main: Subtitle', 'Main - Subtitle')."""
    return norm(re.split(r"\s*[:?!]\s+|\s+-{1,3}\s+", detex(s), maxsplit=1)[0])


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
    return [n.strip() for n in names if n.strip() and norm(n) != "others"]


def family_name(raw: str) -> str:
    """Normalised family name of one BibTeX name ('Last, First', 'First Last', '{Org}')."""
    raw = raw.strip()
    if raw.startswith("{") and _close_of(raw, 0) == len(raw) - 1:  # {Corporate Author}
        return norm(raw)
    name = detex(raw)
    if "," in name:
        return norm(name.split(",")[0])
    words = norm(name).split()
    if len(words) > 1 and words[-1] in ("jr", "sr", "ii", "iii", "iv"):
        words.pop()
    return words[-1] if words else ""


def author_overlap(cited: list, record: list):
    """Share of cited authors whose family name appears on the record, and the misses."""
    if not cited or not record:
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
        pos = m.end()
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
            fields = {"type": kind, "key": text[pos:key_end].strip()}
            pos = key_end
            while True:
                pos = _skip_ws(text, pos)
                if text[pos] == close:
                    pos += 1
                    break
                if text[pos] == ",":
                    pos += 1
                    continue
                f = _FIELD.match(text, pos)
                if not f:
                    raise ValueError("expected 'name = value'")
                fields[f.group(1).lower()], pos = _read_value(text, f.end(), macros)
            entries.append(fields)
        except (ValueError, IndexError, AttributeError) as err:
            line = text.count("\n", 0, m.start()) + 1
            errors.append(f"line {line}: could not parse @{kind} entry ({err or 'truncated'})")


# ---------------------------------------------------------------- identifiers

_DOI = re.compile(r"(10\.\d{4,9}/[^\s\"<>{}]+)")
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
    for name in ("doi", "url", "note", "howpublished"):
        m = _DOI.search(urllib.parse.unquote(f.get(name, "")))
        if m:
            doi = m.group(1).rstrip(".,;")
            return "" if doi.lower().startswith("10.48550/arxiv.") else doi  # arXiv DOIs: use the ID
    return ""


def find_url(f: dict) -> str:
    for name in ("url", "howpublished", "note"):
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
    }


def load_entries(path: str):
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if not path.lower().endswith(".json"):
        fields, errors = parse_bibtex(text)
        return [to_entry(f) for f in fields], errors
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("references", [])
    entries = []
    for i, d in enumerate(data, 1):
        authors = d.get("authors") or []
        f = {"key": str(d.get("key") or f"ref{i}"), "type": "json", "title": d.get("title") or "",
             "author": authors if isinstance(authors, str) else " and ".join(authors),
             "year": str(d.get("year") or ""), "doi": d.get("doi") or "", "eprint": d.get("arxiv") or "",
             "url": d.get("url") or "", "journal": d.get("venue") or ""}
        entries.append(to_entry(f))
    return entries, []


_CITE = re.compile(r"\\(?:[A-Za-z]*cite[A-Za-z]*|nocite)\*?\s*(?:\[[^\]]*\]\s*){0,2}\{([^}]*)\}")


def cited_keys(paths: list) -> set:
    """Citation keys used in .tex files (directories are searched recursively)."""
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += [os.path.join(d, n) for d, _, names in os.walk(p) for n in names if n.endswith(".tex")]
        else:
            files.append(p)
    keys = set()
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = re.sub(r"(?<!\\)%.*", "", fh.read())
        for m in _CITE.finditer(text):
            keys.update(k.strip() for k in m.group(1).split(",") if k.strip())
    return keys


# ---------------------------------------------------------------- HTTP

class SourceError(Exception):
    """A source could not be queried (network error, rate limit, outage)."""


class Http:
    """GET with per-host spacing, retry on 429/5xx, and a small SQLite response cache."""
    SPACING = {"api.semanticscholar.org": 1.1, "export.arxiv.org": 3.0}

    def __init__(self, cache_path=None):
        self.next_ok = {}
        self.db = None
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
                    self.next_ok[host] = time.monotonic() + (min(int(retry), 60) if retry.isdigit() else 2 ** attempt)
                    continue
            except (OSError, http.client.HTTPException) as e:
                status, body = 0, str(e)
                time.sleep(2 ** attempt)
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

def _record(source, title, authors, year, venue="", doi="", arxiv="", url=""):
    return {"source": source, "title": re.sub(r"\s+", " ", title or "").strip(),
            "authors": [a for a in authors if a], "year": _year(year), "venue": venue or "",
            "doi": doi or "", "arxiv": arxiv or "", "url": url or ""}


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


def crossref_search(http, title, first_author, year):
    query = " ".join(str(x) for x in (title, first_author, year) if x)
    params = {"query.bibliographic": query, "rows": 5, "select": "DOI,title,author,issued,container-title"}
    if MAILTO:
        params["mailto"] = MAILTO
    status, body = http.get("https://api.crossref.org/works?" + urllib.parse.urlencode(params))
    out = []
    for it in _json(status, body, "Crossref")["message"].get("items", []):
        authors = [a.get("name") or f"{a.get('given', '')} {a.get('family', '')}".strip() for a in it.get("author", [])]
        year = ((it.get("issued") or {}).get("date-parts") or [[None]])[0][0]
        out.append(_record("Crossref", (it.get("title") or [""])[0], authors, year,
                           (it.get("container-title") or [""])[0], it.get("DOI"), url=f"https://doi.org/{it.get('DOI')}"))
    return out


def openalex_search(http, title, author=""):
    params = {"search": title, "per_page": 5,
              "select": "doi,display_name,publication_year,authorships,primary_location"}
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
                           url=f"https://doi.org/{doi}" if doi else ""))
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
                           e.findtext("{http://arxiv.org/schemas/atom}doi"), aid, f"https://arxiv.org/abs/{aid}"))
    return out


def arxiv_title(http, title):
    return _arxiv_query(http, {"search_query": f'ti:"{norm(title)}"', "max_results": 5})


def arxiv_ids(http, ids):
    """Batch lookup: {arxiv_id: record} for the IDs that exist."""
    ids, found = sorted(set(ids)), {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        for r in _arxiv_query(http, {"id_list": ",".join(chunk), "max_results": len(chunk)}):
            found[r["arxiv"]] = r
    return found


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
                   url=f"https://doi.org/{doi}")


# ---------------------------------------------------------------- checking

class Checker:
    def __init__(self, http):
        self.http = http
        self.failures = Counter()
        self.disabled = set()
        self.arxiv = {}

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
        if not ids:
            return
        try:
            self.arxiv = self._call("arXiv", arxiv_ids, ids)
        except SourceError:
            self.arxiv = None

    def _searches(self, e):
        first = family_name(e["authors"][0]) if e["authors"] else ""
        return [("Semantic Scholar", s2_match, (e["title"],)),
                ("OpenAlex", openalex_search, (e["title"],)),
                ("Crossref", crossref_search, (e["title"], first, e["year"])),
                ("arXiv", arxiv_title, (e["title"],))]

    @staticmethod
    def _score(e, rec):
        share, missing = author_overlap(e["authors"], rec["authors"])
        ydiff = e["year"] - rec["year"] if e["year"] and rec["year"] else None
        return {"record": rec, "title_sim": round(title_sim(e["title"], rec["title"]), 3),
                "author_share": share, "missing_authors": missing, "year_diff": ydiff}

    @staticmethod
    def _consistent(c):
        return c["title_sim"] >= TITLE_NEAR and (c["author_share"] is None or c["author_share"] >= AUTHORS_MIN)

    @staticmethod
    def _clean(c):
        return (c["title_sim"] >= TITLE_SAME and c["author_share"] in (None, 1.0)
                and (c["year_diff"] is None or YEAR_LAG[0] <= c["year_diff"] <= YEAR_LAG[1]))

    def _check_id(self, e, label, rec, cands, problems):
        if rec is None:
            problems.append(f"{label} does not exist")
            return
        c = self._score(e, rec)
        if c["title_sim"] >= TITLE_NEAR:
            cands.append(c)
        else:
            problems.append(f'{label} resolves to a different work: "{rec["title"]}"')

    def check(self, e):
        res = {"key": e["key"], "verdict": "", "issues": [], "notes": [], "match": None,
               "cited": {k: e[k] for k in ("title", "authors", "year", "doi", "arxiv", "url")}}
        if not e["title"]:
            res.update(verdict="SKIPPED", issues=["entry has no title"])
            return res
        cands, id_problems, answered = [], [], []

        if e["doi"]:
            try:
                self._check_id(e, f"DOI {e['doi']}", self._call("doi.org", doi_lookup, e["doi"]), cands, id_problems)
            except SourceError as err:
                res["notes"].append(f"DOI not checked: {err}")
        if e["arxiv"]:
            if self.arxiv is None:
                res["notes"].append("arXiv ID not checked: arXiv unavailable")
            else:
                self._check_id(e, f"arXiv:{e['arxiv']}", self.arxiv.get(e["arxiv"]), cands, id_problems)

        if not any(self._clean(c) for c in cands):
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
            if first and not any(self._consistent(c) for c in cands):
                try:  # title search found nothing by these authors: search within the first author's works
                    recs = self._call("OpenAlex", openalex_search, e["title"], first[-1])
                    cands += [self._score(e, r) for r in recs]
                except SourceError as err:
                    res["notes"].append(str(err))

        near = [c for c in cands if c["title_sim"] >= TITLE_NEAR]
        if not near:
            return self._unfound(e, res, cands, id_problems, answered)
        best = max(near, key=lambda c: (c["author_share"] is None or c["author_share"] >= AUTHORS_MIN,
                                        c["title_sim"] >= TITLE_SAME,
                                        1.0 if c["author_share"] is None else c["author_share"],
                                        -abs(c["year_diff"] or 0), c["title_sim"]))
        res["match"] = {**best["record"], "title_sim": best["title_sim"], "author_share": best["author_share"]}
        rec, issues = best["record"], list(id_problems)
        if best["author_share"] is not None and best["author_share"] < AUTHORS_MIN:
            issues.append(f"{len(best['missing_authors'])} of {len(e['authors'])} cited authors are not on the "
                          f"record: {'; '.join(best['missing_authors'])}")
            res.update(verdict="MISMATCH", issues=issues)
            return res
        if best["title_sim"] < TITLE_SAME:
            issues.append(f'title differs from the record: "{rec["title"]}"')
        if best["missing_authors"]:
            issues.append(f"cited author(s) not on the record: {'; '.join(best['missing_authors'])}")
        if best["year_diff"] is not None and not YEAR_LAG[0] <= best["year_diff"] <= YEAR_LAG[1]:
            issues.append(f"cited year {e['year']}, record year {rec['year']}")
        if best["author_share"] is None and e["authors"]:
            res["notes"].append("record lists no authors; authors not compared")
        verdict = "MISMATCH" if id_problems else ("CHECK" if issues else "VERIFIED")
        res.update(verdict=verdict, issues=issues)
        return res

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
        broad = {"Semantic Scholar", "OpenAlex"} & set(answered)
        if len(answered) >= 2 and broad:
            res["issues"].insert(0, f"no work with this title in {', '.join(answered)}")
            res["verdict"] = "NOT_FOUND"
        else:
            res["issues"].insert(0, "could not reach enough sources to decide (rerun later, or set S2_API_KEY)")
            res["verdict"] = "ERROR"
        return res


# ---------------------------------------------------------------- report

def _short(s: str, n: int = 90) -> str:
    return s if len(s) <= n else s[:n - 3] + "..."


def _describe(m: dict) -> str:
    names = m["authors"][:3] + (["et al."] if len(m["authors"]) > 3 else [])
    parts = [", ".join(names) or "(no authors)", str(m["year"] or "n.d."), m["venue"], m["url"]]
    return f'"{_short(m["title"], 80)}" -- ' + " | ".join(p for p in parts if p) + f" [{m['source']}]"


def render(results, source, extra_lines):
    counts = Counter(r["verdict"] for r in results)
    lines = [f"citecheck {__version__}: {len(results)} references in {source}",
             "  " + "   ".join(f"{v} {counts[v]}" for v in ORDER if counts[v])] + extra_lines
    for r in sorted(results, key=lambda r: ORDER.index(r["verdict"])):
        if r["verdict"] == "VERIFIED":
            continue
        lines += ["", f"{r['verdict']:<9}  {r['key']}  \"{_short(r['cited']['title'])}\""]
        lines += [f"           - {i}" for i in r["issues"]]
        if r["match"]:
            lines.append(f"           record: {_describe(r['match'])}")
        lines += [f"           note: {n}" for n in r["notes"]]
    if not counts.keys() - {"VERIFIED"}:
        lines += ["", "Every reference matched an indexed record."]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="citecheck", description=__doc__.split("\n\n")[0])
    ap.add_argument("file", help=".bib file, or .json list of {key, title, authors, year, doi, arxiv, url}")
    ap.add_argument("--cited-in", nargs="+", metavar="TEX",
                    help="check only keys cited in these .tex files or directories")
    ap.add_argument("--json", metavar="OUT", help="also write per-entry results to this JSON file")
    ap.add_argument("--no-cache", action="store_true", help="bypass the 7-day HTTP cache")
    ap.add_argument("--version", action="version", version=f"citecheck {__version__}")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass

    entries, errors = load_entries(args.file)
    extra = [f"  warning: {e}" for e in errors]
    keys = [e["key"] for e in entries]
    dupes = sorted(k for k, n in Counter(keys).items() if n > 1)
    if dupes:
        extra.append(f"  warning: duplicate keys: {', '.join(dupes)}")
    if args.cited_in:
        cited = cited_keys(args.cited_in)
        if "*" not in cited:
            missing = sorted(cited - set(keys))
            entries = [e for e in entries if e["key"] in cited]
            extra.append(f"  checking the {len(entries)} entries cited in the .tex sources")
            if missing:
                extra.append(f"  warning: cited but not in {os.path.basename(args.file)}: {', '.join(missing)}")

    cache_dir = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    checker = Checker(Http(None if args.no_cache else os.path.join(cache_dir, "citecheck", "http.sqlite")))
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
    if checker.disabled:
        extra.append(f"  warning: switched off after repeated failures: {', '.join(sorted(checker.disabled))}")

    print(render(results, args.file, extra))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"version": __version__, "source": args.file, "results": results}, fh, indent=2, ensure_ascii=False)
    return 1 if any(r["verdict"] in FLAGGED for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())

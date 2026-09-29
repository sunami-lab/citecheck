"""Offline tests: parsing, normalisation, identifier extraction and verdict logic.
Sources are replaced with fakes, so nothing here touches the network.

    python3 -m unittest discover tests
"""
import io
import os
import re
import ssl
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import citecheck as cc  # noqa: E402

BIB = r"""
@string{neurips = "Advances in Neural Information Processing Systems"}
@comment{ignored {nested} text}
@preamble{ "\newcommand{\noop}[1]{}" }
@inproceedings{vaswani2017,
  title     = {Attention is {All} you Need},
  author    = {Vaswani, Ashish and Shazeer, Noam and Parmar, Niki and others},
  booktitle = neurips # " 30",
  year      = 2017,
  month     = dec,
}
@article{lecun2015, title="Deep learning", author="Yann LeCun and Yoshua Bengio and Geoffrey Hinton",
  journal={Nature}, doi={https://doi.org/10.1038/nature14539}, year={2015}}
@misc(ho2020, title = {Denoising Diffusion Probabilistic Models},
  author = {Jonathan Ho and Ajay Jain and Pieter Abbeel},
  journal = {arXiv preprint arXiv:2006.11239}, year = {2020})
@article{broken, title = {Unclosed {brace}, year = 2020
@article{after_broken, title = {Still Parsed}, author = {{OpenAI}}, year = {2023}}
"""


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.fields, self.errors = cc.parse_bibtex(BIB)
        self.by_key = {f["key"]: f for f in self.fields}

    def test_entries_and_macros(self):
        self.assertIn("vaswani2017", self.by_key)
        self.assertEqual(self.by_key["vaswani2017"]["booktitle"],
                         "Advances in Neural Information Processing Systems 30")
        self.assertEqual(self.by_key["vaswani2017"]["year"], "2017")
        self.assertEqual(self.by_key["ho2020"]["type"], "misc")  # @misc( ... ) form

    def test_malformed_entry_is_reported_not_fatal(self):
        self.assertTrue(any("line" in e for e in self.errors))
        self.assertIn("after_broken", self.by_key)

    def test_to_entry(self):
        e = cc.to_entry(self.by_key["vaswani2017"])
        self.assertEqual(e["title"], "Attention is All you Need")
        self.assertEqual(len(e["authors"]), 3)  # "others" dropped
        self.assertEqual(cc.to_entry(self.by_key["lecun2015"])["doi"], "10.1038/nature14539")
        self.assertEqual(cc.to_entry(self.by_key["ho2020"])["arxiv"], "2006.11239")
        self.assertEqual(cc.family_name(cc.to_entry(self.by_key["after_broken"])["authors"][0]), "openai")


class TextTests(unittest.TestCase):
    def test_detex(self):
        self.assertEqual(cc.detex(r"Sch{\"o}lkopf"), "Schölkopf".replace("ö", "o"))
        self.assertEqual(cc.detex(r"\textbf{Bold} \emph{text}"), "Bold text")
        self.assertEqual(cc.detex(r"Garc{\'\i}a-Dur{\'a}n"), "Garcia-Duran")

    def test_norm_handles_unicode_and_markup(self):
        self.assertEqual(cc.norm("Łukasz Kaiser"), "lukasz kaiser")
        self.assertEqual(cc.norm("<i>In vivo</i> imaging &amp; more"), "in vivo imaging more")
        self.assertEqual(cc.norm("patients&amp;apos; risk"), "patients risk")  # entity escaped twice

    def test_title_sim(self):
        self.assertEqual(cc.title_sim("Attention is {All} you Need", "Attention Is All You Need"), 1.0)
        near = cc.title_sim("Deep Residual Learning for Image Classification",
                            "Deep Residual Learning for Image Recognition")
        self.assertTrue(cc.TITLE_NEAR <= near < cc.TITLE_SAME, near)
        self.assertGreaterEqual(cc.title_sim(  # DOI record without the subtitle
            "Grassmann discriminant analysis: a unifying view on subspace-based learning",
            "Grassmann discriminant analysis"), cc.TITLE_NEAR)
        self.assertLess(cc.title_sim("Language Models are Few-Shot Learners",
                                     "Language Models are Unsupervised Multitask Learners"), cc.TITLE_NEAR)

    def test_family_names(self):
        self.assertEqual(cc.family_name("van der Maaten, Laurens"), "van der maaten")
        self.assertEqual(cc.family_name("Laurens van der Maaten"), "maaten")
        self.assertEqual(cc.family_name("Martin Luther King Jr."), "king")
        self.assertEqual(cc.family_name("Rohit Agrawal 0002"), "agrawal")  # dblp homonym number
        self.assertEqual(cc.split_names("{Barnes and Noble} and Doe, J."), ["{Barnes and Noble}", "Doe, J."])

    def test_vancouver_names_and_organisations(self):
        self.assertEqual([cc.family_name(n) for n in ("James G", "Hastie TJ", "Aidan N Gomez", "Pyatkov S.G.")],
                         ["james", "hastie", "gomez", "pyatkov"])
        self.assertTrue(all(cc.is_org(n) for n in ("OpenAI", "{Google DeepMind}", "The White House", "Google Research",
                                                    "DeepSeek-AI", "Google", "NVIDIA")))
        self.assertFalse(any(cc.is_org(n) for n in ("Vaswani, Ashish", "Ashish Vaswani", "House, Thomas", "A", "X",
                                                     "Unknown", "Sahoo", "Anonymous")))
        self.assertEqual(cc.split_names("Franz Aichberger, Lily Chen, and John Smith"),
                         ["Franz Aichberger", "Lily Chen", "John Smith"])
        self.assertEqual(cc.split_names("van der Maaten, Laurens and Smith, John A."),
                         ["van der Maaten, Laurens", "Smith, John A."])
        self.assertEqual(cc.split_names("Happee, Riender and de Winter, Joost CF"),
                         ["Happee, Riender", "de Winter, Joost CF"])
        self.assertEqual(cc.split_names("de Winter JCF, Happee R, Dodou D"), ["de Winter JCF", "Happee R", "Dodou D"])
        self.assertEqual(cc.split_names("Da Silva, Ana Maria and Smith, John"), ["Da Silva, Ana Maria", "Smith, John"])
        for field in ("Le Song, Alex Smola", "Di He, Tie-Yan Liu", "Du Tran, Lubomir Bourdev"):
            self.assertEqual(len(cc.split_names(field)), 2, field)
        self.assertEqual(cc.split_names("De Winter, Joost CF"), ["De Winter, Joost CF"])
        self.assertEqual(cc.split_names("de Winter JCFW, Happee R"), ["de Winter JCFW", "Happee R"])
        self.assertEqual(cc.split_names("van der BERG, Jan Willem"), ["van der BERG, Jan Willem"])
        self.assertEqual(cc.split_names("van der BERG, J. W."), ["van der BERG, J. W."])
        self.assertEqual(cc.split_names("DE LUCA, Maria G. and Smith, J."), ["DE LUCA, Maria G.", "Smith, J."])
        self.assertEqual(cc.author_overlap(["Guo, Daya", "Yang, Dejian"], ["DeepSeek-AI"]), (None, []))
        self.assertEqual(cc.author_overlap(["{OpenCitations}"], ["Chiara Di Giambattista"]), (None, []))

    def test_author_overlap(self):
        record = ["Ashish Vaswani", "Noam Shazeer", "Aidan N.Gomez", "Łukasz Kaiser"]
        self.assertEqual(cc.author_overlap(["Vaswani, A.", "Gomez, Aidan", "Kaiser, Lukasz"], record), (1.0, []))
        share, missing = cc.author_overlap(["Smith, John", "Vaswani, Ashish"], record)
        self.assertEqual((share, missing), (0.5, ["Smith, John"]))
        self.assertEqual(cc.author_overlap([], record), (None, []))

    def test_identifiers(self):
        self.assertEqual(cc.find_arxiv({"journal": "CoRR", "volume": "abs/1706.03762"}), "1706.03762")
        self.assertEqual(cc.find_arxiv({"url": "https://arxiv.org/pdf/1706.03762v5.pdf"}), "1706.03762")
        self.assertEqual(cc.find_arxiv({"eprint": "hep-th/9901001", "archiveprefix": "arXiv"}), "hep-th/9901001")
        self.assertEqual(cc.find_arxiv({"doi": "10.48550/arXiv.2106.09685"}), "2106.09685")
        self.assertEqual(cc.find_doi({"doi": "10.48550/arXiv.2106.09685"}), "")
        self.assertEqual(cc.find_url({"howpublished": r"\url{https://gptzero.me/news/iclr-2026/}"}),
                         "https://gptzero.me/news/iclr-2026/")
        self.assertEqual(cc.find_doi({"url": "https://doi.org/10.1145/3292500.3330701."}), "10.1145/3292500.3330701")
        wiley = "10.1002/(SICI)1097-0193(1999)8:4<182::AID-HBM3>3.0.CO;2-M"  # < and > belong to the DOI
        self.assertEqual(cc.find_doi({"doi": wiley}), wiley)

    def test_comment_line_inside_entry(self):
        fields, errors = cc.parse_bibtex("@article{a,\n  title = {T},\n  % note = {old},\n  year = 2017\n}")
        self.assertEqual((errors, fields[0]["year"]), ([], "2017"))

    def test_subtitle_after_period(self):
        self.assertGreaterEqual(cc.title_sim("Planck 2018 results. VI. Cosmological parameters",
                                             "Planck 2018 results"), cc.TITLE_NEAR)

    def test_cited_keys_markdown(self):
        import tempfile
        text = ("As in [@smith2020; -@doe2019, p. 3] and @lee2021.\nSee @fig-plot and @tbl-one.\n"
                "Mail me@example.com.\n```python\n@property\n```\n")
        with tempfile.NamedTemporaryFile("w", suffix=".qmd", delete=False) as fh:
            fh.write(text)
        try:
            self.assertEqual(cc.cited_keys([fh.name]), {"smith2020", "doe2019", "lee2021"})
        finally:
            os.unlink(fh.name)

    def test_collect_inputs(self):
        import tempfile
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "paper", "sections"))
            for name, text in (("refs.bib", "@misc{a, title={T}}"), ("sections/more.bib", "@misc{b, title={U}}")):
                with open(os.path.join(d, "paper", name), "w") as fh:
                    fh.write(text)
            bibs, roots = cc.collect_inputs([os.path.join(d, "paper")], d)
            self.assertEqual([os.path.basename(b) for b in bibs], ["refs.bib", "more.bib"])
            with zipfile.ZipFile(os.path.join(d, "overleaf.zip"), "w") as z:
                z.writestr("main.tex", "\\cite{a}")
                z.writestr("refs.bib", "@misc{a, title={T}}")
            bibs, roots = cc.collect_inputs([os.path.join(d, "overleaf.zip")], d)
            self.assertEqual(cc.cited_keys(roots), {"a"})
            with self.assertRaises(cc.InputError):
                cc.collect_inputs([os.path.join(d, "missing.bib")], d)

    def test_cited_keys(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".tex", delete=False) as fh:
            fh.write("As shown \\citep[see][p.~3]{a, b} and \\citet*{c}. % \\cite{commented}\n\\nocite{d}")
        try:
            self.assertEqual(cc.cited_keys([fh.name]), {"a", "b", "c", "d"})
        finally:
            os.unlink(fh.name)


REAL = cc._record("Semantic Scholar", "Attention Is All You Need",
                  ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"], 2017, "NeurIPS", arxiv="1706.03762")


def entry(**kw):
    base = {"key": "k", "type": "article", "title": "Attention Is All You Need",
            "authors": ["Vaswani, Ashish", "Shazeer, Noam", "Parmar, Niki"], "year": 2017, "doi": "", "arxiv": "", "url": "",
            "venue": "", "venue_text": "", "etal": False}
    base.update(kw)
    return base


RIS = """TY  - JOUR
AU  - Ho, Jonathan
AU  - Jain, Ajay
TI  - Denoising Diffusion Probabilistic Models
JO  - Adv Neural Inf Process Syst
PY  - 2020
DO  - https://doi.org/10.5555/3495724.3496298
ID  - 7
ER  - 

TY  - BOOK
AU  - Goodfellow, Ian
T1  - Deep Learning
PY  - 2016///
UR  - https://www.deeplearningbook.org
ER  - 
"""

ENDNOTE = """<?xml version="1.0" encoding="UTF-8"?><xml><records><record><rec-number>12</rec-number>
<ref-type name="Journal Article">17</ref-type><contributors><authors><author><style>LeCun, Yann</style></author>
<author><style>Bengio, Yoshua</style></author></authors></contributors><titles><title><style>Deep learning</style></title>
<secondary-title><style>Nature</style></secondary-title></titles><dates><year><style>2015</style></year></dates>
<electronic-resource-num><style>10.1038/nature14539</style></electronic-resource-num></record></records></xml>"""


class ReferenceManagerTests(unittest.TestCase):
    def test_ris(self):
        fields, errors = cc.parse_ris(RIS)
        self.assertEqual(errors, [])
        a, b = [cc.to_entry(f) for f in fields]
        self.assertEqual((a["key"], a["type"], a["year"], a["venue"]), ("ho2020denoising", "article", 2020, "Adv Neural Inf Process Syst"))
        self.assertEqual(a["authors"], ["Ho, Jonathan", "Jain, Ajay"])
        self.assertEqual(a["doi"], "10.5555/3495724.3496298")
        self.assertEqual((b["type"], b["title"], b["year"], b["venue"]), ("book", "Deep Learning", 2016, ""))
        self.assertEqual(b["url"], "https://www.deeplearningbook.org")

    def test_ris_keys_are_readable_and_unique(self):
        fields, _ = cc.parse_ris(RIS + RIS)
        self.assertEqual([f["key"] for f in fields],
                         ["ho2020denoising", "goodfellow2016deep", "ho2020denoisingb", "goodfellow2016deepb"])

    def test_ris_notes_are_not_identifiers(self):
        ris = ("TY  - JOUR\nAU  - LeCun, Yann\nTI  - Deep learning\nPY  - 2015\nDO  - 10.1038/nature14539\n"
               "N1  - Compare https://arxiv.org/abs/2005.14165\nER  - \n"
               "TY  - CHAP\nAU  - Smith, J\nTI  - A chapter\nPY  - 2020\nN1  - see doi:10.1016/j.cell.2020.01.001\nER  - \n")
        a, b = [cc.to_entry(f) for f in cc.parse_ris(ris)[0]]
        self.assertEqual((a["doi"], a["arxiv"]), ("10.1038/nature14539", ""))
        self.assertEqual(b["doi"], "")

    def test_ris_types_organisations_and_missing_er(self):
        ris = ("TY  - PAT\nAU  - Department of Health and Human Services\nTI  - A patent\nPY  - 2020\n"
               "TY  - ELEC\nTI  - A web page\nPY  - 2021\nER  - \n"
               "TY  - DATA\nTI  - A dataset\nPY  - 2022\n")
        entries = [cc.to_entry(f) for f in cc.parse_ris(ris)[0]]
        self.assertEqual([e["type"] for e in entries], ["patent", "online", "dataset"])
        self.assertEqual(entries[0]["authors"], ["{Department of Health and Human Services}"])
        self.assertEqual(cc._ENDNOTE_TYPES["unpublished work"], cc._RIS_TYPES["UNPB"])

    def test_load_bom_bibtex_sniffing(self):
        with tempfile.TemporaryDirectory() as d:
            files = {"refs.json": "\ufeff[{\"title\": \"A paper\", \"authors\": [\"Smith, J\"]}]",
                     "export.txt": "\ufeff\nTY  - JOUR\nTI  - A paper\nER  - \n",
                     "refs": "@article{a, title={A}, author={B, C}, year={2020}}",
                     "refs.txt": "@article{a, title={A}, author={B, C}, year={2020}}"}
            for name, text in files.items():
                path = os.path.join(d, name)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                entries, _ = cc.load_entries(path)
                self.assertEqual(len(entries), 1, name)

    def test_json_leniency(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "refs.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('[{"title": "A paper", "author": "Smith, J", "year": 2020.0}]')
            (e,), _ = cc.load_entries(path)
            self.assertEqual((e["authors"], e["year"]), (["Smith, J"], 2020))

    def test_endnote_corporate_author_with_trailing_comma(self):
        ris = ("TY  - RPRT\nAU  - World Health Organization,\nTI  - A report\nER  - \n"
               "TY  - RPRT\nAU  - Department of Health and Human Services,\nTI  - Another report\nER  - \n")
        a, b = [cc.to_entry(f) for f in cc.parse_ris(ris)[0]]
        self.assertEqual(a["authors"], ["{World Health Organization}"])
        self.assertEqual(b["authors"], ["{Department of Health and Human Services}"])
        self.assertEqual(cc._manager_author("Smith, John,"), "Smith, John")
        for org in ("University of California, San Francisco,", "National Academies of Sciences, Engineering, and Medicine,"):
            self.assertEqual(cc._manager_author(org), "{" + org.rstrip(",") + "}")

    def test_bibtex_sniffing_accepts_biblatex_types(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "export")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("% exported\n\n@mvbook{k, title={A}, author={B, C}, year={2020}}\n@article{\n  k2,\n  title={B}}\n")
            self.assertEqual(len(cc.load_entries(path)[0]), 2)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("@article{12,\n  title={Numeric keys}, author={B, C}, year={2020}}\n")
            self.assertEqual(len(cc.load_entries(path)[0]), 1)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("@retry(3, delay=1)\ndef fetch(): pass\n")
            with self.assertRaises(cc.InputError):
                cc.load_entries(path)

    def test_endnote_xml(self):
        fields, errors = cc.parse_endnote_xml(ENDNOTE)
        e = cc.to_entry(fields[0])
        self.assertEqual((e["key"], e["type"], e["title"], e["year"], e["venue"], e["doi"]),
                         ("lecun2015deep", "article", "Deep learning", 2015, "Nature", "10.1038/nature14539"))
        self.assertEqual(e["authors"], ["LeCun, Yann", "Bengio, Yoshua"])

    def test_load_by_extension(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for name, text in (("refs.ris", RIS), ("export.txt", RIS), ("library.xml", ENDNOTE)):
                path = os.path.join(d, name)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                entries, _ = cc.load_entries(path)
                self.assertTrue(entries and entries[0]["title"])


class CommandLineTests(unittest.TestCase):
    def run_main(self, *argv):
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stderr", err), mock.patch.object(sys, "stdout", out):
            code = cc.main(list(argv))
        return code, err.getvalue() + out.getvalue()

    def test_unreadable_input_is_an_error_not_a_clean_bill(self):
        with tempfile.TemporaryDirectory() as d:
            cases = {"paper.pdf": "%PDF-1.4", "refs.txt": "Smith J. A paper. 2020.", "empty.bib": "",
                     "one.json": '{"title": "A paper"}', "bad.json": '[{"title": }]',
                     "strings.json": '["Attention is all you need"]', "null.json": "[null]",
                     "s2.json": '[{"title": "A paper", "authors": [{"name": "Ann Lee"}]}]',
                     "numdoi.json": '[{"title": "A paper", "doi": 10.1038}]',
                     "csl.json": '[{"title": "A paper", "author": [{"family": "Smith"}], "issued": {"date-parts": [[2011]]}}]',
                     "numauthors.json": '[{"title": "A paper", "authors": 5}]'}
            for name, text in cases.items():
                path = os.path.join(d, name)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                code, err = self.run_main(path)
                self.assertEqual(code, 2, name)
                self.assertIn(name, err)

    def test_binary_and_lookalike_files_are_not_bibtex(self):
        with tempfile.TemporaryDirectory() as d:
            cases = {"paper.pdf": b"%PDF-1.4\n1 0 obj @article{x, title={y}}", "notes.docx": b"PK\x03\x04\x00\x00@misc{a,",
                     "stdin": b"\x00\x01binary @article{x, title={y}}", "macros": b"\\def\\@maketitle{\\title}",
                     "code.txt": b"@dataclass(frozen=True, order=True)\nclass A: pass\n"}
            for name, data in cases.items():
                path = os.path.join(d, name)
                with open(path, "wb") as fh:
                    fh.write(data)
                self.assertEqual(self.run_main(path)[0], 2, name)

    def test_nothing_cited_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "refs.bib"), "w", encoding="utf-8") as fh:
                fh.write("@article{a, title={A}, author={B, C}, year={2020}}")
            with open(os.path.join(d, "main.tex"), "w", encoding="utf-8") as fh:
                fh.write("No citations here.")
            code, err = self.run_main(d)
            self.assertEqual(code, 2)
            self.assertIn("--all", err)

    def fake_checker(self):
        fake = mock.Mock(disabled=set(), failures={})
        fake.check.side_effect = lambda e: {"key": e["key"], "verdict": "VERIFIED", "issues": [], "notes": [], "match": None,
                                            "cited": {k: e[k] for k in ("type", "title", "authors", "year", "doi", "arxiv", "url")}}
        return mock.patch.object(cc, "Checker", return_value=fake)

    def test_uncited_malformed_entry_does_not_change_exit_status(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "refs.bib"), "w", encoding="utf-8") as fh:
                fh.write("@article{good, title={A}, author={B, C}, year={2020}}\n@article{bad, title={A\n")
            with open(os.path.join(d, "main.tex"), "w", encoding="utf-8") as fh:
                fh.write("\\cite{good}")
            with self.fake_checker():
                self.assertEqual(self.run_main(d)[0], 0)
                self.assertEqual(self.run_main(d, "--all")[0], 3)

    def test_generated_keys_unique_across_files(self):
        with tempfile.TemporaryDirectory() as d:
            for name, title in (("ch1.ris", "Deep nets one"), ("ch2.ris", "Deep nets two")):
                with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                    fh.write(f"TY  - JOUR\nAU  - Smith, J\nTI  - {title}\nPY  - 2020\nER  - \n")
            with self.fake_checker():
                code, err = self.run_main(os.path.join(d, "ch1.ris"), os.path.join(d, "ch2.ris"))
            self.assertIn("smith2020deep\n", err)
            self.assertIn("smith2020deepb\n", err)

    def test_made_up_keys_never_displace_a_reference(self):
        ris = "TY  - JOUR\nAU  - Smith, J\nTI  - Deep nets {n}\nPY  - 2020\nER  - \n"
        with tempfile.TemporaryDirectory() as d:
            files = {"a.ris": ris.format(n=1), "b.ris": ris.format(n=2) + ris.format(n=3),
                     "c.bib": "@article{smith2020deep, title={Deep nets 4}, author={Smith, J}, year={2020}}",
                     "d.json": '[{"title": "Paper five"}]', "e.json": '[{"title": "Paper six"}]'}
            for name, text in files.items():
                with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                    fh.write(text)
            with self.fake_checker():
                code, out = self.run_main(*[os.path.join(d, n) for n in sorted(files)])
            keys = re.findall(r"VERIFIED\s+(\S+)", out.split("citecheck ")[0])
            self.assertEqual(len(keys), 6, out)
            self.assertEqual(len(set(keys)), 6, keys)
            self.assertIn("smith2020deep", keys)  # the .bib entry keeps its own key

    def test_cited_entry_that_cannot_be_parsed(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "refs.bib"), "w", encoding="utf-8") as fh:
                fh.write("@article{good, title={A}, author={B, C}, year={2020}}\n@article{bad, title={A\n")
            with open(os.path.join(d, "main.tex"), "w", encoding="utf-8") as fh:
                fh.write("\\cite{good,bad}")
            with self.fake_checker():
                code, out = self.run_main(d)
            self.assertEqual(code, 3)
            self.assertIn("could not be parsed: bad", out)
            self.assertNotIn("not in the bibliography: bad", out)

    def test_truncated_entry_keeps_its_key(self):
        _, errors = cc.parse_bibtex("@article{good, title={A}}\n@article{bad")
        self.assertEqual([e.key for e in errors], ["bad"])

    def test_key_parsed_in_another_file_or_given_twice(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a.bib"), os.path.join(d, "b.bib")
            with open(a, "w", encoding="utf-8") as fh:
                fh.write("@article{x, title={A}, author={B, C}, year={2020}}")
            with open(b, "w", encoding="utf-8") as fh:
                fh.write("@article{x, title={A\n")
            tex = os.path.join(d, "main.tex")
            with open(tex, "w", encoding="utf-8") as fh:
                fh.write("\\cite{x}")
            with self.fake_checker() as checker:
                self.assertEqual(self.run_main(a, b, "--cited-in", tex)[0], 0)
                checker.return_value.check.reset_mock()
                self.run_main(a, a)
                self.assertEqual(checker.return_value.check.call_count, 1)
                checker.return_value.check.reset_mock()
                self.run_main(d, a, "--all")  # a.bib is also inside the folder
                self.assertEqual(checker.return_value.check.call_count, 1)
            z = os.path.join(d, "paper.zip")
            with zipfile.ZipFile(z, "w") as zf:
                zf.write(a, "a.bib")
            with self.fake_checker():
                code, out = self.run_main(z, z, "--all")
            self.assertNotIn("duplicate keys", out)

    @unittest.skipIf(os.name == "nt", "named pipes are POSIX")
    def test_piped_input_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            fifo = os.path.join(d, "stdin")
            os.mkfifo(fifo)
            import threading
            def feed():
                with open(fifo, "w", encoding="utf-8") as fh:
                    fh.write("@article{a, title={A}, author={B, C}, year={2020}}\n")
            threading.Thread(target=feed, daemon=True).start()
            found = []
            def read():  # a pipe that was already drained would block here forever
                bibs, _ = cc.collect_inputs([fifo], d)
                found.extend(cc.load_entries(bibs[0])[0])
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            reader.join(timeout=5)
            self.assertEqual(len(found), 1, "the pipe was consumed before it was parsed")

    def test_output_path_that_is_a_folder_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(cc.InputError):
                cc.prepare_outputs([d])

    def test_bibliography_after_cited_in_is_the_input(self):
        self.assertEqual(cc.split_inputs([], ["main.tex", "refs.bib", "sec/"]), (["refs.bib"], ["main.tex", "sec/"]))
        self.assertEqual(cc.split_inputs(["refs.bib"], ["main.tex"]), (["refs.bib"], ["main.tex"]))

    def test_output_folders_are_created(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "new", "deeper", "report.json")
            cc.prepare_outputs([out, None])
            self.assertTrue(os.path.isdir(os.path.dirname(out)))

    def test_exit_status(self):
        self.assertEqual(cc.exit_status(["VERIFIED", "CHECK"], unparsed=0), 0)
        self.assertEqual(cc.exit_status(["VERIFIED"], unparsed=1), 3)
        self.assertEqual(cc.exit_status(["ERROR"], unparsed=0), 3)
        self.assertEqual(cc.exit_status(["MISMATCH", "ERROR"], unparsed=1), 1)

    def test_help_explains_verdicts_exit_codes_and_keys(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out), self.assertRaises(SystemExit):
            cc.main(["--help"])
        for word in ("VERIFIED", "NOT_FOUND", "exit status", "S2_API_KEY", "OPENALEX_API_KEY", ".cache/citecheck"):
            self.assertIn(word, out.getvalue())


class HintTests(unittest.TestCase):
    def test_certificate_failure_is_named(self):
        """macOS Python from python.org ships without CA certificates: say so instead of blaming the network."""
        http = cc.Http()
        err = cc.urllib.error.URLError(ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
        with mock.patch.object(cc.urllib.request, "urlopen", side_effect=err), mock.patch.object(cc.time, "sleep"):
            self.assertEqual(http.get("https://api.crossref.org/works?query=x")[0], 0)
        self.assertTrue(http.cert_error)
        results = [{"notes": ["Crossref unavailable (Crossref HTTP unreachable)"]}]
        checker = mock.Mock(http=http, disabled=set())
        self.assertIn("certificate", cc.hints(results, checker)[0])
        http.cert_error = False
        self.assertIn("network connection", cc.hints(results, checker)[0])


class NameTests(unittest.TestCase):
    def test_given_name_conflicts(self):
        self.assertEqual(cc.given_name_conflicts(["Ethan Moreno", "Marco Tulio Lima"], ["Fábio Moreno", "Ian Lima"]),
                         ["Ethan Moreno (record: Fábio Moreno)", "Marco Tulio Lima (record: Ian Lima)"])
        for cited, record in ((["Moreno, F."], ["Fábio Moreno"]), (["Wei Zhang"], ["Zhang Wei"]),
                              (["J. Robert Oppenheimer"], ["Robert Oppenheimer"]), (["Hastie TJ"], ["Trevor J. Hastie"]),
                              (["van der Maaten, Laurens"], ["Laurens van der Maaten"]), (["Smith"], ["John Smith"]),
                              (["Jean-Pierre Serre"], ["J.-P. Serre"]), (["Rohit Agrawal 0002"], ["Rohit Agrawal"]),
                              (["Garcia", "J. P."], ["J. Garcia"])):  # "Garcia, J. P." split in two
            self.assertEqual(cc.given_name_conflicts(cited, record), [], cited)


class VerdictTests(unittest.TestCase):
    """Drive Checker.check with fake sources."""

    def run_check(self, e, s2=(), openalex=(), crossref=(), arxiv=(), doi="unused", arxiv_ids=None, url_status=0,
                  first_version=None):
        http = mock.Mock()
        http.status.return_value = url_status
        checker = cc.Checker(http)
        checker.arxiv = arxiv_ids or {}
        patches = [mock.patch.object(cc, "dblp_db", return_value=None),
                   mock.patch.object(cc, "s2_match", side_effect=self._fake(s2)),
                   mock.patch.object(cc, "openalex_search", side_effect=self._fake(openalex)),
                   mock.patch.object(cc, "crossref_search", side_effect=self._fake(crossref)),
                   mock.patch.object(cc, "arxiv_title", side_effect=self._fake(arxiv)),
                   mock.patch.object(cc, "doi_lookup", return_value=doi),
                   mock.patch.object(cc, "arxiv_first_version", return_value=first_version)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return checker.check(e)

    @staticmethod
    def _fake(value):
        def fn(_http, *args):
            if isinstance(value, Exception):
                raise value
            return value(*args) if callable(value) else list(value)
        return fn

    def test_verified(self):
        self.assertEqual(self.run_check(entry(), s2=[REAL])["verdict"], "VERIFIED")

    def test_fabricated_title_not_found(self):
        r = self.run_check(entry(title="Recursive Transformers for Quantum Protein Folding"))
        self.assertEqual(r["verdict"], "NOT_FOUND")

    def test_collaboration_author_is_not_a_truncated_list(self):
        rec = cc._record("Crossref", "Observation of Gravitational Waves from a Binary Black Hole Merger",
                         [f"Author {i}" for i in range(1000)], 2016, "Physical Review Letters")
        e = entry(title="Observation of Gravitational Waves from a Binary Black Hole Merger",
                  authors=["{LIGO Scientific Collaboration and Virgo Collaboration}"], year=2016)
        r = self.run_check(e, crossref=[rec])
        self.assertEqual(r["verdict"], "VERIFIED", r["issues"])

    def test_bare_surname_is_not_an_organisation(self):
        rec = cc._record("Crossref", "Deep learning", ["Yann LeCun", "Yoshua Bengio", "Geoffrey Hinton", "A B"], 2015)
        for author in ("LeCun", "McDonald"):
            r = self.run_check(entry(title="Deep learning", authors=[author], year=2015), crossref=[rec])
            self.assertEqual(r["verdict"], "CHECK", author)
        r = self.run_check(entry(title="Deep learning", authors=["OpenAI"], year=2015), crossref=[rec])
        self.assertFalse(any("without 'and others'" in i for i in r["issues"]))

    def test_named_org_needs_more_than_a_keyword_surname(self):
        self.assertFalse(cc._named_org("Ai, Qingyao"))
        self.assertTrue(cc._named_org("{LIGO Scientific Collaboration}"))
        fields, _ = cc.parse_ris("TY  - JOUR\nAU  - Smith, John and Doe, Jane\nTI  - A paper\nER  - \n")
        self.assertEqual(cc.to_entry(fields[0])["authors"], ["Smith, John", "Doe, Jane"])

    def test_chimeric_authors_mismatch(self):
        r = self.run_check(entry(authors=["Smith, John", "Doe, Jane"]), s2=[REAL])  # chimeric
        self.assertEqual(r["verdict"], "MISMATCH")

    def test_wrong_year_and_title_typo_check(self):
        self.assertEqual(self.run_check(entry(year=2019), s2=[REAL])["verdict"], "VERIFIED")  # published later
        r = self.run_check(entry(year=2014), s2=[REAL])  # cited before the work existed
        self.assertEqual(r["verdict"], "CHECK")
        r = self.run_check(entry(title="Attention Is All You Want"), s2=[REAL])
        self.assertEqual(r["verdict"], "CHECK")

    def test_doi_pointing_elsewhere_is_mismatch(self):
        other = cc._record("DOI", "Deep learning", ["Yann LeCun"], 2015, doi="10.1038/nature14539")
        r = self.run_check(entry(doi="10.1038/nature14539"), s2=[REAL], doi=other)
        self.assertEqual(r["verdict"], "MISMATCH")
        self.assertIn("different work", r["issues"][0])

    def test_nonexistent_arxiv_id_is_mismatch(self):
        r = self.run_check(entry(arxiv="2399.99999"), s2=[REAL], arxiv_ids={})
        self.assertEqual(r["verdict"], "MISMATCH")

    def test_arxiv_id_match_skips_search(self):
        rec = dict(REAL, source="arXiv")
        r = self.run_check(entry(arxiv="1706.03762"), s2=cc.SourceError("should not be called"),
                           arxiv_ids={"1706.03762": rec})
        self.assertEqual(r["verdict"], "VERIFIED")

    def test_outage_is_error_not_not_found(self):
        down = cc.SourceError("HTTP 429")
        r = self.run_check(entry(title="Some Real ML Paper Not In Crossref"), s2=down, openalex=down)
        self.assertEqual(r["verdict"], "ERROR")

    def test_live_url_downgrades_to_check(self):
        r = self.run_check(entry(title="nanoGPT", url="https://github.com/karpathy/nanoGPT"), url_status=200)
        self.assertEqual(r["verdict"], "CHECK")

    def test_author_anchored_search_gets_past_same_title_noise(self):
        junk = cc._record("Semantic Scholar", "Generative Adversarial Nets", ["Raphael Labaca-Castro"], 2023)
        real = cc._record("OpenAlex", "Generative Adversarial Networks", ["Ian Goodfellow", "Jean Pouget-Abadie"], 2014)
        e = entry(title="Generative adversarial nets", authors=["Goodfellow, Ian", "Pouget-Abadie, Jean"], year=2014)
        r = self.run_check(e, s2=[junk], openalex=lambda title, author="": [real] if author == "goodfellow" else [junk])
        self.assertEqual(r["verdict"], "CHECK")  # found; only the wording differs (Nets vs Networks)
        self.assertEqual(r["match"]["authors"][0], "Ian Goodfellow")

    def test_retitled_preprint_is_not_a_wrong_id(self):
        preprint = cc._record("arXiv", "Is the 2MASS clustering dipole convergent?",
                              ["Maciej Bilicki", "Michal Chodorowski"], 2011, arxiv="1102.4356")
        e = entry(title="Is the Two Micron All Sky Survey Clustering Dipole Convergent?",
                  authors=["Bilicki, Maciej", "Chodorowski, Micha{\\l}"], year=2011, arxiv="1102.4356")
        published = cc._record("Crossref", e["title"], ["Maciej Bilicki", "Michał Chodorowski"], 2011)
        r = self.run_check(e, crossref=[published], arxiv_ids={"1102.4356": preprint})
        self.assertEqual(r["verdict"], "VERIFIED")
        self.assertTrue(any("retitled" in n for n in r["notes"]))

    def test_given_name_swapped_for_namesake_is_check(self):
        rec = cc._record("arXiv", "Prompt Injection Attacks on Retrieval Pipelines",
                         ["Fábio Moreno", "Ian Lima"], 2022, arxiv="2211.00001")
        e = entry(title="Prompt injection attacks on retrieval pipelines",
                  authors=["Ethan Moreno", "Marco Tulio Lima"], year=2022)
        r = self.run_check(e, s2=[rec])
        self.assertEqual(r["verdict"], "CHECK")
        self.assertIn("given name differs", r["issues"][0])

    def test_arxiv_paper_renamed_after_version_1(self):
        current = cc._record("arXiv", "Impact of shape fluctuations on nuclear Schiff moments",
                             ["Zhou", "Yao", "Engel", "Meng"], 2025, arxiv="2507.01369")
        v1 = cc._record("arXiv", "Effects of beyond-mean-field correlations on nuclear Schiff moments",
                        ["Zhou", "Yao", "Engel", "Meng"], 2025, arxiv="2507.01369")
        e = entry(title="Effects of beyond-mean-field correlations on nuclear Schiff moments",
                  authors=["Zhou", "Yao", "Engel", "Meng"], year=2025, arxiv="2507.01369")
        r = self.run_check(e, arxiv_ids={"2507.01369": current}, first_version=v1)
        self.assertEqual(r["verdict"], "VERIFIED")
        self.assertTrue(any("version 1" in n for n in r["notes"]))
        # authors added after version 1: the citation of version 1 lists only its authors
        current = dict(current, authors=["Zhou", "Yao", "Engel", "Meng", "Li", "Wang"])
        r = self.run_check(e, arxiv_ids={"2507.01369": current}, first_version=v1)
        self.assertEqual(r["verdict"], "VERIFIED", r["issues"])

    def test_unindexed_type_is_check_not_not_found(self):
        r = self.run_check(entry(type="phdthesis", title="Selected Topics in Relativistic Cosmology"))
        self.assertEqual(r["verdict"], "CHECK")
        r = self.run_check(entry(title="A note on importance sampling using standardized weights",
                                 venue_text="University of Chicago, Dept. of Statistics, Tech. Rep"))
        self.assertEqual(r["verdict"], "CHECK")

    def test_book_year_not_compared(self):
        book = cc._record("Crossref", "Gravitation", ["C. W. Misner", "K. S. Thorne"], 1973)
        e = entry(type="book", title="Gravitation", authors=["Misner, Charles W.", "Thorne, Kip S."], year=2017)
        self.assertEqual(self.run_check(e, crossref=[book])["verdict"], "VERIFIED")

    def test_retracted_work_is_flagged(self):
        retracted = dict(REAL, notices=["retracted"])
        r = self.run_check(entry(), s2=[retracted])
        self.assertEqual(r["verdict"], "CHECK")
        self.assertIn("retracted", r["issues"][0])

    def test_suggested_bibtex_uses_record_and_keeps_key(self):
        r = self.run_check(entry(key="song2020", authors=["Song, Yang", "Ermon, Stefano"]), s2=[REAL])
        bib = cc.suggested_bibtex(r)
        fields, errors = cc.parse_bibtex(bib)
        self.assertEqual((errors, fields[0]["key"]), ([], "song2020"))
        self.assertIn("Ashish Vaswani", fields[0]["author"])
        self.assertIn("% 2 of 2 cited authors", bib)

    def test_fabricated_title_with_the_authors_real_doi_is_mismatch(self):
        other = cc._record("DOI", "Overcoming Catastrophic Forgetting in Graph Neural Networks with Experience Replay",
                           ["Fan Zhou", "Chengtai Cao"], 2021, doi="10.1609/aaai.v35i5.16602")
        e = entry(title="Prompt-Tuning Strategies for Instruction-Following Models", year=2021,
                  authors=["Zhou, Fan", "Cao, Chengtai"], doi="10.1609/aaai.v35i5.16602")
        r = self.run_check(e, doi=other)
        self.assertIn(r["verdict"], ("MISMATCH", "NOT_FOUND"))
        self.assertTrue(any("different work" in i for i in r["issues"]))

    def test_future_year_is_mismatch(self):
        self.assertEqual(self.run_check(entry(year=cc.THIS_YEAR + 5), s2=[REAL])["verdict"], "MISMATCH")

    def test_changed_title_word_is_check_but_hyphenation_is_not(self):
        rec = cc._record("DBLP", "Structural Multiplane Image: Bridging Neural View Synthesis and 3D Reconstruction",
                         ["Mingfang Zhang", "Jinglu Wang"], 2023)
        e = entry(title="Structural Multiplane Visual: Bridging Neural View Synthesis and 3D Reconstruction",
                  authors=["Zhang, Mingfang", "Wang, Jinglu"], year=2023)
        r = self.run_check(e, s2=[rec])
        self.assertEqual(r["verdict"], "CHECK")
        self.assertIn('"visual" vs "image"', r["issues"][0])
        e2 = dict(e, title="Structural Multi-plane Image: Bridging Neural View-Synthesis and 3D Reconstructions")
        self.assertEqual(self.run_check(e2, s2=[rec])["verdict"], "VERIFIED")

    def test_truncated_author_list(self):
        rec = cc._record("DBLP", "Attention Is All You Need",
                         ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar", "Jakob Uszkoreit"], 2017)
        self.assertEqual(self.run_check(entry(), s2=[rec])["verdict"], "CHECK")
        self.assertEqual(self.run_check(entry(etal=True), s2=[rec])["verdict"], "VERIFIED")

    def test_venue(self):
        rec = cc._record("DBLP", "Attention Is All You Need", ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"], 2017,
                         venue="NIPS")
        self.assertEqual(self.run_check(entry(venue="Advances in Neural Information Processing Systems"),
                                        s2=[rec])["verdict"], "VERIFIED")
        r = self.run_check(entry(venue="ICML"), s2=[rec])
        self.assertEqual(r["verdict"], "CHECK")
        self.assertIn("ICML", r["issues"][0])
        self.assertEqual(cc.venue_ids("Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern "
                                      "Recognition (CVPR)"), {"CVPR"})
        self.assertEqual(cc.venue_ids("J. Mach. Learn. Res."), {"JMLR"})
        self.assertEqual(cc.venue_ids("Proc. 37th Int. Conf. Mach. Learn."), {"ICML"})
        self.assertIn("ICML", cc.venue_ids("Proceedings of Machine Learning Research"))

    def test_unrecognised_cited_venue(self):
        rec = cc._record("DBLP", "Attention Is All You Need", ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"], 2017,
                         venue="NIPS")
        with mock.patch.object(cc, "dblp_db", return_value=object()), \
                mock.patch.object(cc, "dblp_search", return_value=[rec]):
            r = cc.Checker(mock.Mock()).check(entry(venue="Symposium on Neural Scaling Laws"))
        self.assertEqual(r["verdict"], "CHECK")
        self.assertIn("Symposium on Neural Scaling Laws", r["issues"][0])

    def test_stopword_swap_in_title(self):
        self.assertEqual(cc.title_changes("Learning to Summarize with Human Feedback",
                                          "Learning to summarize from human feedback"), [("with", "from")])
        self.assertEqual(cc.title_changes("A Survey of Graph Networks", "Survey of Graph Networks"), [])

    def test_et_al_in_names(self):
        self.assertEqual(cc.split_names("A. Smith et al."), ["A. Smith"])
        self.assertEqual(cc.split_names("Smith, A. and et al."), ["Smith, A."])

    def test_grey_literature_is_check(self):
        r = self.run_check(entry(title="Introducing OpenAI o1", authors=["OpenAI"], year=2024))
        self.assertEqual(r["verdict"], "CHECK")

    def test_similar_title_by_other_authors_is_not_a_mismatch(self):
        other = cc._record("DBLP", "Thinking Fast and Slow in AI", ["Grady Booch", "Francesco Fabiano"], 2021)
        e = entry(title="Thinking, Fast and Slow", authors=["Kahneman, Daniel"], year=2011)
        self.assertEqual(self.run_check(e, s2=[other])["verdict"], "NOT_FOUND")
        self.assertEqual(self.run_check(dict(e, type="book"), s2=[other])["verdict"], "CHECK")  # books: unindexed

    def test_book_with_same_title_review_is_not_a_mismatch(self):
        review = cc._record("Crossref", "Fundamentals of qualitative research", ["Some Reviewer"], 2012)
        e = entry(title="Fundamentals of qualitative research", authors=["Saldana, Johnny"], year=2011, type="book")
        self.assertEqual(self.run_check(e, crossref=[review])["verdict"], "CHECK")

    def test_short_title_by_other_authors_is_not_wrong_authors(self):
        other = cc._record("Crossref", "LangChain", ["Someone Else"], 2023)
        e = entry(type="misc", title="LangChain", authors=["Chase, Harrison"], year=2022,
                  url="https://github.com/langchain-ai/langchain")
        r = self.run_check(e, crossref=[other], url_status=200)
        self.assertEqual(r["verdict"], "CHECK")
        r = self.run_check(dict(e, url=""), crossref=[other])
        self.assertEqual(r["verdict"], "NOT_FOUND")
        self.assertIn("only works by other authors", r["issues"][0])

    def test_incomplete_record_is_check_not_mismatch(self):
        rec = cc._record("Crossref", "Markov chain models for threshold exceedances", ["Richard L. Smith"], 1997)
        e = entry(title="Markov chain models for threshold exceedances", year=1997,
                  authors=["Smith, Richard L", "Tawn, Jonathan A", "Coles, Stuart G"])
        self.assertEqual(self.run_check(e, crossref=[rec])["verdict"], "CHECK")
        self.assertFalse(cc.record_within(["Smith, J.", "Doe, A."], ["John Smith", "Mary Roe"]))

    def test_href_titles(self):
        f = {"key": "k", "type": "misc", "title": r"\href{https://doi.org/10.1038/s41467-020-16941-y}{Scientists' warning}"}
        e = cc.to_entry(f)
        self.assertEqual((e["title"], e["doi"]), ("Scientists' warning", "10.1038/s41467-020-16941-y"))

    def test_dblp_needs_arxiv_to_decide_not_found(self):
        with mock.patch.object(cc, "dblp_db", return_value=object()), \
                mock.patch.object(cc, "dblp_search", return_value=[]), \
                mock.patch.object(cc, "crossref_search", return_value=[]), \
                mock.patch.object(cc, "arxiv_title", side_effect=cc.SourceError("arXiv HTTP 429")), \
                mock.patch.object(cc, "s2_match", side_effect=cc.SourceError("Semantic Scholar HTTP 429")), \
                mock.patch.object(cc, "openalex_search", side_effect=cc.SourceError("OpenAlex HTTP 429")):
            r = cc.Checker(mock.Mock()).check(entry(title="Higgsino Above the Sea of Fog", authors=["Fan, JiJi"]))
        self.assertEqual(r["verdict"], "ERROR")

    def test_unchecked_own_identifier_is_error(self):
        http = mock.Mock()
        checker = cc.Checker(http)
        checker.arxiv_failed = {"2502.14499"}
        with mock.patch.object(cc, "dblp_db", return_value=None), \
                mock.patch.object(cc, "s2_match", return_value=[]), \
                mock.patch.object(cc, "openalex_search", return_value=[]), \
                mock.patch.object(cc, "crossref_search", return_value=[]), \
                mock.patch.object(cc, "arxiv_title", return_value=[]):
            r = checker.check(entry(title="MLGym: A framework and benchmark", arxiv="2502.14499"))
        self.assertEqual(r["verdict"], "ERROR")

    def test_crossref_author_search_rescues_same_title_book(self):
        wrong = cc._record("Crossref", "Modern cosmology", ["George Ellis", "Jean-Philippe Uzan"], 2024)
        right = cc._record("Crossref", "Modern Cosmology", ["Scott Dodelson", "Fabian Schmidt"], 2020)
        e = entry(title="Modern cosmology", authors=["Scott Dodelson", "Fabian Schmidt"], year=2020, type="book")
        crossref = lambda title, author, year, anchored=False: [right] if anchored else [wrong]  # noqa: E731
        r = self.run_check(e, crossref=crossref, openalex=cc.SourceError("OpenAlex HTTP 429"))
        self.assertEqual(r["verdict"], "VERIFIED")

    def test_duplicates(self):
        r1 = {"key": "ho2020", "match": REAL}
        r2 = {"key": "song2020", "match": dict(REAL, source="Crossref")}
        self.assertEqual(cc.duplicates([r1, r2, {"key": "x", "match": None}]), [["ho2020", "song2020"]])

    def test_same_title_different_paper_prefers_author_match(self):
        lecun = cc._record("Semantic Scholar", "Deep learning", ["Yann LeCun", "Yoshua Bengio"], 2015)
        book = cc._record("Crossref", "Deep Learning", ["Ian Goodfellow", "Yoshua Bengio", "Aaron Courville"], 2016)
        e = entry(title="Deep Learning", authors=["Goodfellow, Ian", "Bengio, Yoshua", "Courville, Aaron"], year=2016)
        r = self.run_check(e, s2=[lecun], crossref=[book])
        self.assertEqual(r["verdict"], "VERIFIED")
        self.assertEqual(r["match"]["source"], "Crossref")


if __name__ == "__main__":
    unittest.main()

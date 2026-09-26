"""Offline tests: parsing, normalisation, identifier extraction and verdict logic.
Sources are replaced with fakes, so nothing here touches the network.

    python3 -m unittest discover tests
"""
import os
import sys
import unittest
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
        self.assertEqual(cc.split_names("{Barnes and Noble} and Doe, J."), ["{Barnes and Noble}", "Doe, J."])

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
            "authors": ["Vaswani, Ashish", "Shazeer, Noam"], "year": 2017, "doi": "", "arxiv": "", "url": ""}
    base.update(kw)
    return base


class VerdictTests(unittest.TestCase):
    """Drive Checker.check with fake sources."""

    def run_check(self, e, s2=(), openalex=(), crossref=(), arxiv=(), doi="unused", arxiv_ids=None, url_status=0):
        http = mock.Mock()
        http.status.return_value = url_status
        checker = cc.Checker(http)
        checker.arxiv = arxiv_ids or {}
        patches = [mock.patch.object(cc, "s2_match", side_effect=self._fake(s2)),
                   mock.patch.object(cc, "openalex_search", side_effect=self._fake(openalex)),
                   mock.patch.object(cc, "crossref_search", side_effect=self._fake(crossref)),
                   mock.patch.object(cc, "arxiv_title", side_effect=self._fake(arxiv)),
                   mock.patch.object(cc, "doi_lookup", return_value=doi)]
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

    def test_chimeric_authors_mismatch(self):
        r = self.run_check(entry(authors=["Smith, John", "Doe, Jane"]), s2=[REAL])
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

    def test_same_title_different_paper_prefers_author_match(self):
        lecun = cc._record("Semantic Scholar", "Deep learning", ["Yann LeCun", "Yoshua Bengio"], 2015)
        book = cc._record("Crossref", "Deep Learning", ["Ian Goodfellow", "Yoshua Bengio", "Aaron Courville"], 2016)
        e = entry(title="Deep Learning", authors=["Goodfellow, Ian", "Courville, Aaron"], year=2016)
        r = self.run_check(e, s2=[lecun], crossref=[book])
        self.assertEqual(r["verdict"], "VERIFIED")
        self.assertEqual(r["match"]["source"], "Crossref")


if __name__ == "__main__":
    unittest.main()

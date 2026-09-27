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


class VerdictTests(unittest.TestCase):
    """Drive Checker.check with fake sources."""

    def run_check(self, e, s2=(), openalex=(), crossref=(), arxiv=(), doi="unused", arxiv_ids=None, url_status=0):
        http = mock.Mock()
        http.status.return_value = url_status
        checker = cc.Checker(http)
        checker.arxiv = arxiv_ids or {}
        patches = [mock.patch.object(cc, "dblp_db", return_value=None),
                   mock.patch.object(cc, "s2_match", side_effect=self._fake(s2)),
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

from xml.etree import ElementTree
from types import SimpleNamespace

from omegaconf import open_dict

from zotero_arxiv_daily.construct_email import render_email
from zotero_arxiv_daily.dedup import deduplicate_papers
from zotero_arxiv_daily.executor import (
    apply_journal_quality_bonus,
    filter_topic_papers,
    filter_topic_papers_by_rules,
    select_with_published_priority,
)
from zotero_arxiv_daily.journal_metrics import load_journal_metrics, match_journal_metric
from zotero_arxiv_daily.protocol import Paper
from zotero_arxiv_daily.retriever.pubmed_retriever import PubmedRetriever


def _paper(**overrides):
    values = {
        "source": "medrxiv",
        "title": "Alpha-synuclein biomarkers in Parkinson disease",
        "authors": ["Doe J"],
        "abstract": "A Parkinson disease cohort study.",
        "url": "https://example.test/article",
        "pdf_url": "https://example.test/article.pdf",
    }
    values.update(overrides)
    return Paper(**values)


def _pubmed_xml(publication_type="Journal Article", journal="Movement Disorders", pmid="12345678"):
    return ElementTree.fromstring(
        f"""
        <PubmedArticle>
          <MedlineCitation>
            <PMID>{pmid}</PMID>
            <Article>
              <ArticleTitle>Alpha-synuclein biomarkers in Parkinson disease</ArticleTitle>
              <Abstract><AbstractText Label="BACKGROUND">A clinical cohort study.</AbstractText></Abstract>
              <AuthorList><Author><LastName>Doe</LastName><Initials>J</Initials></Author></AuthorList>
              <Journal>
                <JournalIssue><PubDate><Year>2026</Year><Month>Jul</Month><Day>30</Day></PubDate></JournalIssue>
                <Title>{journal}</Title>
              </Journal>
              <PublicationTypeList><PublicationType>{publication_type}</PublicationType></PublicationTypeList>
            </Article>
          </MedlineCitation>
          <PubmedData>
            <ArticleIdList><ArticleId IdType="doi">10.1000/example</ArticleId></ArticleIdList>
          </PubmedData>
        </PubmedArticle>
        """
    )


def test_topic_filter_keeps_disease_relevant_preprints():
    relevant = _paper()
    unrelated = _paper(title="Kidney organoid atlas", abstract="Renal development.")
    assert filter_topic_papers([relevant, unrelated], ["Parkinson", "Alzheimer"]) == [relevant]


def test_respiratory_topic_rules_require_lung_context_for_mechanisms():
    copd = _paper(title="COPD exacerbation cohort", abstract="Clinical outcomes.")
    lung_lactylation = _paper(
        title="Histone lactylation in alveolar macrophages",
        abstract="A pulmonary inflammation study.",
    )
    unrelated_lactylation = _paper(
        title="Histone lactylation in glioma",
        abstract="A brain tumor study.",
    )

    selected = filter_topic_papers_by_rules(
        [copd, lung_lactylation, unrelated_lactylation],
        ["COPD", "chronic obstructive pulmonary disease"],
        ["lactylation", "cuproptosis"],
        ["lung", "pulmonary", "alveolar", "airway"],
    )

    assert selected == [copd, lung_lactylation]


def test_dedup_prefers_formally_published_pubmed_version():
    preprint = _paper(doi="10.1000/example")
    published = _paper(source="pubmed", doi="https://doi.org/10.1000/example", pmid="12345678")
    assert deduplicate_papers([preprint, published]) == [published]


def test_pubmed_retriever_applies_sjr_threshold_and_metadata(config):
    with open_dict(config):
        config.source.pubmed.api_key = None
    retriever = PubmedRetriever(config)
    paper = retriever.convert_to_paper(_pubmed_xml())
    assert paper is not None
    assert paper.source == "pubmed"
    assert paper.journal == "Movement Disorders"
    assert paper.journal_metric_name == "SJR"
    assert paper.journal_metric_value == 2.988
    assert paper.journal_quartile == "Q1"
    assert paper.evidence_level == "peer_reviewed"


def test_respiratory_core_journal_is_retained_but_still_requires_topic(config):
    with open_dict(config):
        config.source.pubmed.api_key = None
        config.source.pubmed.journal_metrics_file = (
            "config/respiratory_journals_sjr_2025.csv"
        )
        config.source.pubmed.min_sjr = 1.5
        config.source.pubmed.core_journals = ["COPD"]
        config.source.pubmed.core_journals_bypass_topic = False
    retriever = PubmedRetriever(config)
    retriever.core_pmids = {"12345678"}

    paper = retriever.convert_to_paper(
        _pubmed_xml(journal="COPD", pmid="12345678")
    )

    assert paper is not None
    assert paper.journal_metric_value == 0.622
    assert not paper.topic_bypass


def test_pubmed_retriever_rejects_unlisted_or_review_articles(config):
    with open_dict(config):
        config.source.pubmed.api_key = None
    retriever = PubmedRetriever(config)
    assert retriever.convert_to_paper(_pubmed_xml(journal="Unlisted Journal")) is None
    assert retriever.convert_to_paper(_pubmed_xml(publication_type="Review")) is None


def test_pubmed_retriever_passes_api_key_to_search_and_fetch(config, monkeypatch):
    with open_dict(config):
        config.source.pubmed.api_key = "fake-ncbi-api-key"
        config.source.pubmed.priority_query = None
    retriever = PubmedRetriever(config)
    requests = []

    class FakeResponse:
        content = (
            b"<PubmedArticleSet>"
            + ElementTree.tostring(_pubmed_xml())
            + b"</PubmedArticleSet>"
        )

        @staticmethod
        def json():
            return {"esearchresult": {"idlist": ["12345678"]}}

    def fake_request(endpoint, params):
        requests.append((endpoint, params))
        return FakeResponse()

    monkeypatch.setattr(retriever, "_request", fake_request)

    papers = retriever._retrieve_raw_papers()

    assert len(papers) == 1
    assert [endpoint for endpoint, _ in requests] == [
        "esearch.fcgi",
        "esearch.fcgi",
        "esearch.fcgi",
        "esearch.fcgi",
        "efetch.fcgi",
    ]
    assert all(params["api_key"] == "fake-ncbi-api-key" for _, params in requests)
    search_params = [params for endpoint, params in requests if endpoint == "esearch.fcgi"]
    assert [(params["datetype"], params["reldate"]) for params in search_params] == [
        ("pdat", 7),
        ("edat", 14),
        ("pdat", 7),
        ("edat", 14),
    ]
    assert all(params["retmax"] == 500 for params in search_params)


def test_pubmed_search_unions_publication_and_entry_date_results(config, monkeypatch):
    with open_dict(config):
        config.source.pubmed.api_key = None
        config.source.pubmed.entry_lookback_days = 14
    retriever = PubmedRetriever(config)

    class FakeResponse:
        def __init__(self, ids):
            self.ids = ids

        def json(self):
            return {"esearchresult": {"idlist": self.ids}}

    def fake_request(endpoint, params):
        assert endpoint == "esearch.fcgi"
        if params["datetype"] == "pdat":
            return FakeResponse(["published-recently", "seen-in-both"])
        return FakeResponse(["indexed-recently", "seen-in-both"])

    monkeypatch.setattr(retriever, "_request", fake_request)

    assert retriever._search_ids("Alzheimer disease") == [
        "published-recently",
        "seen-in-both",
        "indexed-recently",
    ]


def test_pubmed_general_search_is_restricted_to_metric_journals(config, monkeypatch):
    with open_dict(config):
        config.source.pubmed.api_key = None
        config.source.pubmed.priority_query = None
    retriever = PubmedRetriever(config)
    searched_terms = []

    def fake_search(term):
        searched_terms.append(term)
        return []

    monkeypatch.setattr(retriever, "_search_ids", fake_search)

    assert retriever._retrieve_raw_papers() == []
    assert len(searched_terms) == 2
    general_query, core_query = searched_terms
    assert '"Alzheimer\'s & dementia : the journal of the Alzheimer\'s Association"[Journal]' not in general_query
    assert '"Alzheimer\'s & dementia : the journal of the Alzheimer\'s Association"[Journal]' in core_query
    assert '"Movement disorders : official journal of the Movement Disorder Society"[Journal]' in core_query
    assert '"Neurotherapeutics : the journal of the American Society for Experimental NeuroTherapeutics"[Journal]' in core_query
    assert '"Nature Neuroscience"[Journal]' in searched_terms[0]
    assert '"Neurodegenerative Diseases"[Mesh]' in general_query
    assert '"Neurodegenerative Diseases"[Mesh]' not in core_query


def test_core_journal_papers_bypass_local_topic_filter(config):
    with open_dict(config):
        config.source.pubmed.api_key = None
    retriever = PubmedRetriever(config)
    retriever.core_pmids = {"12345678"}
    paper = retriever.convert_to_paper(
        _pubmed_xml(journal="Movement Disorders", pmid="12345678")
    )
    assert paper is not None
    paper.title = "Unrelated wording"
    paper.abstract = "No configured disease keyword appears here."
    assert filter_topic_papers([paper], ["Alzheimer", "Parkinson"]) == [paper]


def test_core_journal_reviews_are_included(config):
    with open_dict(config):
        config.source.pubmed.api_key = None
    retriever = PubmedRetriever(config)
    retriever.core_pmids = {"12345678"}

    review = retriever.convert_to_paper(
        _pubmed_xml(
            publication_type="Review",
            journal="Nature Reviews Neurology",
            pmid="12345678",
        )
    )

    assert review is not None
    assert review.topic_bypass


def test_pubmed_uses_post_for_long_search_terms(config, monkeypatch):
    with open_dict(config):
        config.source.pubmed.api_key = None
        config.source.pubmed.retry_attempts = 1
    retriever = PubmedRetriever(config)
    calls = []
    response = SimpleNamespace(raise_for_status=lambda: None)

    def fake_post(url, data, timeout):
        calls.append(("post", url, data, timeout))
        return response

    def fail_get(*args, **kwargs):
        raise AssertionError("long PubMed searches must not use GET")

    monkeypatch.setattr(retriever.session, "post", fake_post)
    monkeypatch.setattr(retriever.session, "get", fail_get)

    assert retriever._request("esearch.fcgi", {"term": "A" * 1501}) is response
    assert calls[0][0] == "post"


def test_pubmed_uses_post_for_large_fetch_batches(config, monkeypatch):
    with open_dict(config):
        config.source.pubmed.api_key = None
        config.source.pubmed.retry_attempts = 1
    retriever = PubmedRetriever(config)
    response = SimpleNamespace(raise_for_status=lambda: None)
    calls = []

    def fake_post(url, data, timeout):
        calls.append((url, data, timeout))
        return response

    monkeypatch.setattr(retriever.session, "post", fake_post)
    monkeypatch.setattr(
        retriever.session,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("large PubMed fetches must not use GET")
        ),
    )

    assert retriever._request("efetch.fcgi", {"id": "1," * 1000}) is response
    assert calls[0][1]["id"].startswith("1,")


def test_huntington_priority_pubmed_includes_reviews_and_meta_analyses(config):
    with open_dict(config):
        config.source.pubmed.api_key = None
    retriever = PubmedRetriever(config)
    retriever.priority_pmids = {"87654321", "87654322", "87654323"}

    paper = retriever.convert_to_paper(
        _pubmed_xml(journal="Journal Outside Whitelist", pmid="87654321")
    )
    review = retriever.convert_to_paper(
        _pubmed_xml(
            publication_type="Review",
            journal="Journal Outside Whitelist",
            pmid="87654322",
        )
    )
    meta_analysis = retriever.convert_to_paper(
        _pubmed_xml(
            publication_type="Meta-Analysis",
            journal="Journal Outside Whitelist",
            pmid="87654323",
        )
    )

    assert paper is not None
    assert paper.journal == "Journal Outside Whitelist"
    assert paper.journal_metric_value is None
    assert paper.special_topic == "Huntington disease"
    assert review is not None
    assert meta_analysis is not None


def test_huntington_priority_still_rejects_editorials(config):
    with open_dict(config):
        config.source.pubmed.api_key = None
    retriever = PubmedRetriever(config)
    retriever.priority_pmids = {"87654324"}

    editorial = retriever.convert_to_paper(
        _pubmed_xml(
            publication_type="Editorial",
            journal="Journal Outside Whitelist",
            pmid="87654324",
        )
    )

    assert editorial is None


def test_expanded_whitelist_matches_pubmed_journal_names():
    metrics = load_journal_metrics("config/neurology_journals_sjr_2024.csv")
    expected = {
        "Cell": 22.612,
        "Nature medicine": 18.333,
        "Nature": 18.288,
        "Nature neuroscience": 11.197,
        "Science (New York, N.Y.)": 10.416,
        "Nature aging": 7.081,
        "Neuron": 6.755,
        "Molecular neurodegeneration": 5.488,
        "Brain : a journal of neurology": 4.720,
        "Annals of Neurology": 3.736,
        "Neurology": 2.401,
        "Alzheimer's & dementia : the journal of the Alzheimer's Association": 3.600,
        "Journal of neurology, neurosurgery, and psychiatry": 3.379,
        "NPJ Parkinson's disease": 2.914,
        "Movement disorders : official journal of the Movement Disorder Society": 2.988,
        "Translational Neurodegeneration": 3.850,
        "Alzheimer's Research & Therapy": 2.709,
        "Acta Neuropathol Commun": 2.588,
        "Neurobiology of Disease": 2.009,
        "Neurotherapeutics": 1.620,
        "Neurotherapeutics : the journal of the American Society for Experimental NeuroTherapeutics": 1.620,
        "GeroScience": 1.564,
        "Science Translational Medicine": 6.722,
        "Trends in Neurosciences": 4.726,
        "Nature Communications": 4.761,
        "Science Advances": 4.324,
        "Proceedings of the National Academy of Sciences of the United States of America": 3.414,
    }

    for journal, sjr in expected.items():
        metric = match_journal_metric(journal, metrics)
        assert metric is not None
        assert metric.sjr == sjr
        assert metric.quartile == "Q1"


def test_respiratory_whitelist_matches_pubmed_full_journal_names():
    metrics = load_journal_metrics("config/respiratory_journals_sjr_2025.csv")
    expected = {
        "The Lancet. Respiratory medicine": (7.652, "Q1"),
        "American journal of respiratory and critical care medicine": (5.423, "Q1"),
        "European respiratory journal": (5.076, "Q1"),
        "American journal of respiratory cell and molecular biology": (1.863, "Q1"),
        "International journal of chronic obstructive pulmonary disease": (1.145, "Q2"),
    }

    for journal, (sjr, quartile) in expected.items():
        metric = match_journal_metric(journal, metrics)
        assert metric is not None
        assert metric.sjr == sjr
        assert metric.quartile == quartile


def test_sjr_bonus_prioritizes_top_journal_without_removing_preprints():
    preprint = _paper(score=8.0)
    published = _paper(
        source="pubmed",
        score=7.0,
        journal_metric_value=11.729,
    )
    ranked = apply_journal_quality_bonus([preprint, published], 0.12)
    assert ranked == [published, preprint]


def test_email_limit_prioritizes_published_then_fills_with_preprints():
    preprints = [
        _paper(title=f"Preprint {index}", score=100.0 - index)
        for index in range(30)
    ]
    published = [
        _paper(source="pubmed", title=f"Published {index}", score=10.0 - index)
        for index in range(3)
    ]

    selected = select_with_published_priority(preprints + published, 30)

    assert selected[:3] == published
    assert selected[3:] == preprints[:27]
    assert len(selected) == 30


def test_email_limit_uses_only_top_published_when_they_fill_the_quota():
    published = [
        _paper(source="pubmed", title=f"Published {index}", score=100.0 - index)
        for index in range(35)
    ]
    preprint = _paper(title="Preprint", score=200.0)

    selected = select_with_published_priority(published + [preprint], 30)

    assert selected == published[:30]
    assert preprint not in selected


def test_huntington_papers_are_additive_to_normal_quota():
    regular = [
        _paper(source="pubmed", title=f"Published {index}", score=100.0 - index)
        for index in range(30)
    ]
    huntington = [
        _paper(
            title=f"Huntingtin mechanism {index}",
            abstract="Mutant HTT in Huntington disease.",
            score=5.0 - index,
        )
        for index in range(3)
    ]

    selected = select_with_published_priority(
        regular + huntington,
        30,
        ["Huntington disease", "huntingtin", "HTT"],
    )

    assert selected[:3] == huntington
    assert selected[3:] == regular
    assert len(selected) == 33


def test_email_separates_peer_reviewed_and_preprint_evidence():
    published = _paper(
        source="pubmed",
        score=8.0,
        tldr="正式发表摘要",
        pmid="12345678",
        doi="10.1000/example",
        journal="Movement Disorders",
        journal_metric_name="SJR",
        journal_metric_value=2.988,
        journal_metric_year=2024,
        journal_quartile="Q1",
    )
    preprint = _paper(score=7.0, tldr="预印本摘要")
    html = render_email([published, preprint])
    assert "正式发表（PubMed：顶刊筛选 + 亨廷顿病专题全覆盖）" in html
    assert "预印本（未经同行评议）" in html
    assert "PMID 12345678" in html
    assert "期刊影响指标：SJR 2024 2.988 · Q1" in html


def test_email_labels_huntington_topic_and_missing_metric():
    huntington = _paper(
        source="pubmed",
        score=8.0,
        tldr="亨廷顿病研究摘要",
        pmid="87654321",
        journal="Journal Outside Whitelist",
        special_topic="Huntington disease",
    )

    html = render_email([huntington])

    assert "亨廷顿病专题 · 不受期刊SJR阈值限制" in html
    assert "期刊影响指标：SJR未收录" in html
    assert "SJR 是期刊影响指标，不等同于 Clarivate JIF" in html


def test_email_supports_respiratory_digest_without_huntington_copy():
    paper = _paper(
        source="pubmed",
        pmid="87654321",
        journal="Thorax",
        score=8.0,
        tldr="呼吸研究摘要",
        journal_metric_name="SJR",
        journal_metric_value=2.479,
        journal_metric_year=2025,
        journal_quartile="Q1",
    )

    html = render_email(
        [paper],
        digest_name="呼吸病学顶刊每日文献推送",
        priority_topic_label=None,
        max_paper_num=30,
    )

    assert "呼吸病学顶刊每日文献推送" in html
    assert "正式发表（PubMed：目标期刊筛选）" in html
    assert "每日文献上限30篇" in html
    assert "亨廷顿病" not in html


def test_email_contains_journal_daily_report_pending_queue_and_alert():
    html = render_email(
        [],
        journal_report=[
            {
                "journal": "Movement Disorders",
                "core": True,
                "retrieved": 4,
                "filtered": 1,
                "sent": 2,
                "pending": 1,
            }
        ],
        journal_alerts=[{"journal": "Brain", "zero_days": 3}],
        pending_count=1,
    )

    assert "目标期刊运行日报" in html
    assert "Movement Disorders" in html
    assert "核心期刊抓取报警" in html
    assert "Brain：连续 3 天抓取为 0" in html
    assert "当前总待发送：1 篇" in html

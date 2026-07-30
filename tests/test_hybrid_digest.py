from xml.etree import ElementTree

from omegaconf import open_dict

from zotero_arxiv_daily.construct_email import render_email
from zotero_arxiv_daily.dedup import deduplicate_papers
from zotero_arxiv_daily.executor import (
    apply_journal_quality_bonus,
    filter_topic_papers,
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


def _pubmed_xml(publication_type="Journal Article", journal="Movement Disorders"):
    return ElementTree.fromstring(
        f"""
        <PubmedArticle>
          <MedlineCitation>
            <PMID>12345678</PMID>
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


def test_pubmed_retriever_rejects_unlisted_or_review_articles(config):
    with open_dict(config):
        config.source.pubmed.api_key = None
    retriever = PubmedRetriever(config)
    assert retriever.convert_to_paper(_pubmed_xml(journal="Unlisted Journal")) is None
    assert retriever.convert_to_paper(_pubmed_xml(publication_type="Review")) is None


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
    }

    for journal, sjr in expected.items():
        metric = match_journal_metric(journal, metrics)
        assert metric is not None
        assert metric.sjr == sjr
        assert metric.quartile == "Q1"


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

    selected = select_with_published_priority(preprints + published, 25)

    assert selected[:3] == published
    assert selected[3:] == preprints[:22]
    assert len(selected) == 25


def test_email_limit_uses_only_top_published_when_they_fill_the_quota():
    published = [
        _paper(source="pubmed", title=f"Published {index}", score=100.0 - index)
        for index in range(30)
    ]
    preprint = _paper(title="Preprint", score=200.0)

    selected = select_with_published_priority(published + [preprint], 25)

    assert selected == published[:25]
    assert preprint not in selected


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
    assert "正式发表（PubMed，SJR ≥ 1.5）" in html
    assert "预印本（未经同行评议）" in html
    assert "PMID 12345678" in html
    assert "SJR 2024 2.988 · Q1" in html

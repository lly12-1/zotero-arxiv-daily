"""Tests for zotero_arxiv_daily.executor: normalize_path_patterns, filter_corpus, fetch_zotero_corpus, E2E."""

from datetime import datetime

import pytest
from omegaconf import OmegaConf

from zotero_arxiv_daily.executor import Executor, normalize_path_patterns
from zotero_arxiv_daily.protocol import CorpusPaper
from zotero_arxiv_daily.protocol import Paper


# ---------------------------------------------------------------------------
# normalize_path_patterns — migrated from test_include_path.py
# ---------------------------------------------------------------------------


def test_normalize_path_patterns_rejects_single_string_for_include_path():
    with pytest.raises(TypeError, match="config.zotero.include_path must be a list"):
        normalize_path_patterns("2026/survey/**", "include_path")


def test_normalize_path_patterns_accepts_list_config_for_include_path():
    include_path = OmegaConf.create(["2026/survey/**", "2026/reading-group/**"])
    assert normalize_path_patterns(include_path, "include_path") == [
        "2026/survey/**",
        "2026/reading-group/**",
    ]


def test_normalize_path_patterns_rejects_single_string_for_ignore_path():
    with pytest.raises(TypeError, match="config.zotero.ignore_path must be a list"):
        normalize_path_patterns("archive/**", "ignore_path")


def test_normalize_path_patterns_accepts_list_config_for_ignore_path():
    ignore_path = OmegaConf.create(["archive/**", "2025/**"])
    assert normalize_path_patterns(ignore_path, "ignore_path") == ["archive/**", "2025/**"]


def test_normalize_path_patterns_accepts_empty_list():
    assert normalize_path_patterns([], "ignore_path") == []


def test_normalize_path_patterns_accepts_none():
    assert normalize_path_patterns(None, "include_path") is None


# ---------------------------------------------------------------------------
# filter_corpus — migrated from test_include_path.py
# ---------------------------------------------------------------------------


def _make_executor(include_patterns=None, ignore_patterns=None):
    executor = Executor.__new__(Executor)
    executor.include_path_patterns = normalize_path_patterns(include_patterns, "include_path") if include_patterns else None
    executor.ignore_path_patterns = normalize_path_patterns(ignore_patterns, "ignore_path") if ignore_patterns else None
    return executor


def test_filter_corpus_matches_any_path_against_any_pattern():
    executor = _make_executor(include_patterns=["2026/survey/**", "2026/reading-group/**"])
    corpus = [
        CorpusPaper(title="Survey Paper", abstract="", added_date=datetime(2026, 1, 1), paths=["2026/survey/topic-a", "archive/misc"]),
        CorpusPaper(title="Reading Group Paper", abstract="", added_date=datetime(2026, 1, 2), paths=["notes/inbox", "2026/reading-group/week-1"]),
        CorpusPaper(title="Excluded Paper", abstract="", added_date=datetime(2026, 1, 3), paths=["2025/other/topic"]),
    ]
    filtered = executor.filter_corpus(corpus)
    assert [p.title for p in filtered] == ["Survey Paper", "Reading Group Paper"]


def test_filter_corpus_excludes_papers_matching_ignore_path():
    executor = _make_executor(ignore_patterns=["archive/**", "2025/**"])
    corpus = [
        CorpusPaper(title="Active Paper", abstract="", added_date=datetime(2026, 1, 1), paths=["2026/survey/topic-a"]),
        CorpusPaper(title="Archived Paper", abstract="", added_date=datetime(2026, 1, 2), paths=["archive/misc"]),
        CorpusPaper(title="Old Paper", abstract="", added_date=datetime(2026, 1, 3), paths=["2025/other/topic"]),
    ]
    filtered = executor.filter_corpus(corpus)
    assert [p.title for p in filtered] == ["Active Paper"]


def test_filter_corpus_ignore_path_takes_precedence_over_include_path():
    executor = _make_executor(include_patterns=["2026/**"], ignore_patterns=["2026/ignore/**"])
    corpus = [
        CorpusPaper(title="Included Paper", abstract="", added_date=datetime(2026, 1, 1), paths=["2026/survey/topic-a"]),
        CorpusPaper(title="Ignored Paper", abstract="", added_date=datetime(2026, 1, 2), paths=["2026/ignore/topic-b"]),
    ]
    filtered = executor.filter_corpus(corpus)
    assert [p.title for p in filtered] == ["Included Paper"]


def test_filter_corpus_no_filters_returns_all():
    executor = _make_executor()
    corpus = [
        CorpusPaper(title="Paper A", abstract="", added_date=datetime(2026, 1, 1), paths=["foo"]),
        CorpusPaper(title="Paper B", abstract="", added_date=datetime(2026, 1, 2), paths=["bar"]),
    ]
    filtered = executor.filter_corpus(corpus)
    assert filtered == corpus


def test_run_skips_failed_source_and_uses_remaining_source(config, monkeypatch):
    from types import SimpleNamespace

    executor = Executor.__new__(Executor)
    executor.config = config
    executor.openai_client = SimpleNamespace()
    executor.fetch_zotero_corpus = lambda: [
        CorpusPaper(
            title="Parkinson corpus",
            abstract="Parkinson disease",
            added_date=datetime(2026, 1, 1),
            paths=[],
        )
    ]
    executor.filter_corpus = lambda corpus: corpus

    def _fail():
        raise ValueError("truncated JSON")

    paper = Paper(
        source="arxiv",
        title="Parkinson study",
        authors=["Doe J"],
        abstract="Parkinson disease",
        url="https://example.test",
        score=8.0,
    )
    paper.generate_tldr = lambda *args: setattr(paper, "tldr", "summary")
    paper.generate_affiliations = lambda *args: None
    executor.retrievers = {
        "biorxiv": SimpleNamespace(retrieve_papers=_fail),
        "arxiv": SimpleNamespace(retrieve_papers=lambda: [paper]),
    }
    executor.reranker = SimpleNamespace(rerank=lambda papers, corpus: papers)
    sent = []
    monkeypatch.setattr(
        "zotero_arxiv_daily.executor.send_email",
        lambda cfg, body: sent.append(body),
    )

    executor.run()

    assert len(sent) == 1
    assert "Parkinson study" in sent[0]


def test_run_does_not_mark_complete_when_all_sources_fail(config):
    from pathlib import Path
    from types import SimpleNamespace

    executor = Executor.__new__(Executor)
    executor.config = config
    executor.fetch_zotero_corpus = lambda: [
        CorpusPaper(
            title="Parkinson corpus",
            abstract="Parkinson disease",
            added_date=datetime(2026, 1, 1),
            paths=[],
        )
    ]
    executor.filter_corpus = lambda corpus: corpus

    def _fail():
        raise RuntimeError("source unavailable")

    executor.retrievers = {
        "pubmed": SimpleNamespace(retrieve_papers=_fail),
        "arxiv": SimpleNamespace(retrieve_papers=_fail),
    }

    with pytest.raises(RuntimeError, match="All configured paper sources failed"):
        executor.run()

    assert not Path(config.executor.sent_history_path).exists()


def test_run_records_history_only_after_email_succeeds(config, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    executor = Executor.__new__(Executor)
    executor.config = config
    executor.openai_client = SimpleNamespace()
    executor.fetch_zotero_corpus = lambda: [
        CorpusPaper(
            title="Parkinson corpus",
            abstract="Parkinson disease",
            added_date=datetime(2026, 1, 1),
            paths=[],
        )
    ]
    executor.filter_corpus = lambda corpus: corpus
    paper = Paper(
        source="pubmed",
        title="Parkinson history test",
        authors=["Doe J"],
        abstract="Parkinson disease",
        url="https://pubmed.ncbi.nlm.nih.gov/12345678/",
        pmid="12345678",
        evidence_level="peer_reviewed",
        journal="Movement Disorders",
        journal_metric_name="SJR",
        journal_metric_value=2.988,
        journal_metric_year=2024,
        journal_quartile="Q1",
        score=8.0,
    )
    paper.generate_tldr = lambda *args: setattr(paper, "tldr", "summary")
    paper.generate_affiliations = lambda *args: None
    executor.retrievers = {
        "pubmed": SimpleNamespace(retrieve_papers=lambda: [paper]),
    }
    executor.reranker = SimpleNamespace(rerank=lambda papers, corpus: papers)
    monkeypatch.setattr(
        "zotero_arxiv_daily.executor.send_email",
        lambda *_: (_ for _ in ()).throw(RuntimeError("SMTP failed")),
    )

    with pytest.raises(RuntimeError, match="SMTP failed"):
        executor.run()

    assert not Path(config.executor.sent_history_path).exists()


def test_seed_history_records_without_sending_email(config, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from omegaconf import open_dict

    with open_dict(config):
        config.executor.seed_history_only = True
        config.executor.seed_exclude_pmids = "99999999"
    executor = Executor.__new__(Executor)
    executor.config = config
    executor.openai_client = SimpleNamespace()
    executor.fetch_zotero_corpus = lambda: [
        CorpusPaper(
            title="Parkinson corpus",
            abstract="Parkinson disease",
            added_date=datetime(2026, 1, 1),
            paths=[],
        )
    ]
    executor.filter_corpus = lambda corpus: corpus
    paper = Paper(
        source="pubmed",
        title="Parkinson seed test",
        authors=["Doe J"],
        abstract="Parkinson disease",
        url="https://pubmed.ncbi.nlm.nih.gov/87654321/",
        pmid="87654321",
        evidence_level="peer_reviewed",
        score=8.0,
    )
    excluded_paper = Paper(
        source="pubmed",
        title="New Parkinson seed test",
        authors=["Roe J"],
        abstract="Parkinson disease",
        url="https://pubmed.ncbi.nlm.nih.gov/99999999/",
        pmid="99999999",
        evidence_level="peer_reviewed",
        score=7.0,
    )
    executor.retrievers = {
        "pubmed": SimpleNamespace(retrieve_papers=lambda: [paper, excluded_paper]),
    }
    executor.reranker = SimpleNamespace(rerank=lambda papers, corpus: papers)
    sent = []
    monkeypatch.setattr(
        "zotero_arxiv_daily.executor.send_email",
        lambda *args: sent.append(args),
    )

    executor.run()

    assert sent == []
    history_path = Path(config.executor.sent_history_path)
    assert history_path.exists()
    from zotero_arxiv_daily.sent_history import SentHistory

    history = SentHistory(history_path)
    assert history.was_sent(paper)
    assert not history.was_sent(excluded_paper)


def test_run_skips_when_daily_marker_already_exists(config, monkeypatch):
    from zotero_arxiv_daily.sent_history import SentHistory

    run_date = "2026-07-31"
    history = SentHistory(config.executor.sent_history_path)
    history.mark_completed(run_date)
    history.save()
    monkeypatch.setattr(
        "zotero_arxiv_daily.executor._current_run_date",
        lambda _: run_date,
    )
    executor = Executor.__new__(Executor)
    executor.config = config
    executor.fetch_zotero_corpus = lambda: pytest.fail(
        "duplicate trigger should skip before Zotero retrieval"
    )

    executor.run()


def test_mark_today_complete_only_skips_retrieval(config, monkeypatch):
    from omegaconf import open_dict
    from zotero_arxiv_daily.sent_history import SentHistory

    run_date = "2026-07-31"
    with open_dict(config):
        config.executor.mark_today_complete_only = True
    monkeypatch.setattr(
        "zotero_arxiv_daily.executor._current_run_date",
        lambda _: run_date,
    )
    executor = Executor.__new__(Executor)
    executor.config = config
    executor.fetch_zotero_corpus = lambda: pytest.fail(
        "marker-only mode should not retrieve Zotero"
    )

    executor.run()

    history = SentHistory(config.executor.sent_history_path)
    assert history.completed_on(run_date)


# ---------------------------------------------------------------------------
# fetch_zotero_corpus
# ---------------------------------------------------------------------------


def test_fetch_zotero_corpus(config, monkeypatch):
    from tests.canned_responses import make_stub_zotero_client

    stub_zot = make_stub_zotero_client()
    monkeypatch.setattr("zotero_arxiv_daily.executor.zotero.Zotero", lambda *a, **kw: stub_zot)

    executor = Executor.__new__(Executor)
    executor.config = config
    corpus = executor.fetch_zotero_corpus()

    assert len(corpus) == 2
    assert corpus[0].title == "Stub Paper 1"
    assert "survey/topic-a" in corpus[0].paths[0]


def test_fetch_zotero_corpus_paper_with_zero_collections(config, monkeypatch):
    from tests.canned_responses import make_stub_zotero_client

    items = [
        {
            "data": {
                "title": "No Collection Paper",
                "abstractNote": "Abstract.",
                "dateAdded": "2026-03-01T00:00:00Z",
                "collections": [],
            }
        }
    ]
    stub_zot = make_stub_zotero_client(items=items)
    monkeypatch.setattr("zotero_arxiv_daily.executor.zotero.Zotero", lambda *a, **kw: stub_zot)

    executor = Executor.__new__(Executor)
    executor.config = config
    corpus = executor.fetch_zotero_corpus()

    assert len(corpus) == 1
    assert corpus[0].paths == []


# ---------------------------------------------------------------------------
# E2E: Executor.run()
# ---------------------------------------------------------------------------


def test_run_end_to_end(config, monkeypatch):
    """Full pipeline: Zotero fetch -> filter -> retrieve -> rerank -> TLDR -> email."""
    import smtplib

    from omegaconf import open_dict

    from tests.canned_responses import (
        make_sample_corpus,
        make_sample_paper,
        make_stub_openai_client,
        make_stub_smtp,
        make_stub_zotero_client,
    )

    # Config: source=["arxiv"], reranker="api", send_empty=false
    with open_dict(config):
        config.executor.source = ["arxiv"]
        config.executor.reranker = "api"
        config.executor.send_empty = False
        config.executor.topic_keywords = []

    # 1. Stub pyzotero
    stub_zot = make_stub_zotero_client()
    monkeypatch.setattr("zotero_arxiv_daily.executor.zotero.Zotero", lambda *a, **kw: stub_zot)

    # 2. Stub OpenAI (for reranker + TLDR/affiliations)
    stub_client = make_stub_openai_client()
    monkeypatch.setattr("zotero_arxiv_daily.executor.OpenAI", lambda **kw: stub_client)
    monkeypatch.setattr("zotero_arxiv_daily.reranker.api.OpenAI", lambda **kw: stub_client)
    retrieved = [
        make_sample_paper(title="E2E Paper 1", score=None),
        make_sample_paper(title="E2E Paper 2", score=None),
    ]

    # Import to register the arxiv retriever
    import zotero_arxiv_daily.retriever.arxiv_retriever  # noqa: F401

    from zotero_arxiv_daily.retriever.base import registered_retrievers

    monkeypatch.setattr(
        registered_retrievers["arxiv"],
        "retrieve_papers",
        lambda self: retrieved,
    )

    # 4. Stub SMTP
    sent = []
    monkeypatch.setattr(smtplib, "SMTP", make_stub_smtp(sent))

    # 5. Stub sleep (reranker/retriever)
    monkeypatch.setattr("zotero_arxiv_daily.retriever.base.sleep", lambda _: None)

    # 6. Run
    executor = Executor(config)
    executor.run()

    # Assertions
    assert len(sent) == 1, "Email should have been sent"
    _, _, email_body = sent[0]
    assert "text/html" in email_body


def test_run_no_papers_send_empty_false(config, monkeypatch):
    """When no papers are found and send_empty=false, no email is sent."""
    import smtplib

    from omegaconf import open_dict

    from tests.canned_responses import make_stub_openai_client, make_stub_smtp, make_stub_zotero_client

    with open_dict(config):
        config.executor.source = ["arxiv"]
        config.executor.reranker = "api"
        config.executor.send_empty = False
    run_date = "2026-07-31"
    monkeypatch.setattr(
        "zotero_arxiv_daily.executor._current_run_date",
        lambda _: run_date,
    )

    stub_zot = make_stub_zotero_client()
    monkeypatch.setattr("zotero_arxiv_daily.executor.zotero.Zotero", lambda *a, **kw: stub_zot)

    stub_client = make_stub_openai_client()
    monkeypatch.setattr("zotero_arxiv_daily.executor.OpenAI", lambda **kw: stub_client)
    monkeypatch.setattr("zotero_arxiv_daily.reranker.api.OpenAI", lambda **kw: stub_client)

    import zotero_arxiv_daily.retriever.arxiv_retriever  # noqa: F401

    from zotero_arxiv_daily.retriever.base import registered_retrievers

    monkeypatch.setattr(registered_retrievers["arxiv"], "retrieve_papers", lambda self: [])

    sent = []
    monkeypatch.setattr(smtplib, "SMTP", make_stub_smtp(sent))
    monkeypatch.setattr("zotero_arxiv_daily.retriever.base.sleep", lambda _: None)

    executor = Executor(config)
    executor.run()

    assert len(sent) == 0, "No email should be sent when no papers and send_empty=false"
    from zotero_arxiv_daily.sent_history import SentHistory

    history = SentHistory(config.executor.sent_history_path)
    assert history.completed_on(run_date)


def test_run_no_papers_send_empty_true(config, monkeypatch):
    """When no papers are found and send_empty=true, empty email is sent."""
    import smtplib

    from omegaconf import open_dict

    from tests.canned_responses import make_stub_openai_client, make_stub_smtp, make_stub_zotero_client

    with open_dict(config):
        config.executor.source = ["arxiv"]
        config.executor.reranker = "api"
        config.executor.send_empty = True

    stub_zot = make_stub_zotero_client()
    monkeypatch.setattr("zotero_arxiv_daily.executor.zotero.Zotero", lambda *a, **kw: stub_zot)

    stub_client = make_stub_openai_client()
    monkeypatch.setattr("zotero_arxiv_daily.executor.OpenAI", lambda **kw: stub_client)
    monkeypatch.setattr("zotero_arxiv_daily.reranker.api.OpenAI", lambda **kw: stub_client)

    import zotero_arxiv_daily.retriever.arxiv_retriever  # noqa: F401

    from zotero_arxiv_daily.retriever.base import registered_retrievers

    monkeypatch.setattr(registered_retrievers["arxiv"], "retrieve_papers", lambda self: [])

    sent = []
    monkeypatch.setattr(smtplib, "SMTP", make_stub_smtp(sent))
    monkeypatch.setattr("zotero_arxiv_daily.retriever.base.sleep", lambda _: None)

    executor = Executor(config)
    executor.run()

    assert len(sent) == 1, "Email should be sent even with no papers when send_empty=true"
    _, _, body = sent[0]
    assert "text/html" in body
    from email import message_from_string
    from email.header import decode_header, make_header

    message = message_from_string(body)
    subject = str(make_header(decode_header(message["Subject"])))
    html = message.get_payload(decode=True).decode(message.get_content_charset())
    assert "今日无新增文献" in subject
    assert "今日无新增文献" in html

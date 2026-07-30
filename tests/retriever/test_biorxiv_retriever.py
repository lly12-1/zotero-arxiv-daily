"""Tests for BiorxivRetriever."""

import pytest
from omegaconf import open_dict

from zotero_arxiv_daily.retriever.biorxiv_retriever import BiorxivRetriever
from tests.canned_responses import SAMPLE_BIORXIV_API_RESPONSE


def test_biorxiv_retrieve(config, mock_biorxiv_api, monkeypatch):
    monkeypatch.setattr("zotero_arxiv_daily.retriever.base.sleep", lambda _: None)
    with open_dict(config.source):
        config.source.biorxiv = {"category": ["bioinformatics"]}
    retriever = BiorxivRetriever(config)
    papers = retriever.retrieve_papers()
    # Only latest date + matching category
    assert len(papers) == 1
    assert papers[0].title == "A biorxiv paper"


def test_biorxiv_empty_response(config, monkeypatch):
    import requests
    from types import SimpleNamespace

    empty = {"messages": [{"status": "ok"}], "collection": []}

    def _patched(url, **kw):
        resp = SimpleNamespace(status_code=200, raise_for_status=lambda: None)
        resp.json = lambda: empty
        return resp

    monkeypatch.setattr(requests, "get", _patched)
    monkeypatch.setattr("zotero_arxiv_daily.retriever.base.sleep", lambda _: None)

    with open_dict(config.source):
        config.source.biorxiv = {"category": ["bioinformatics"]}
    retriever = BiorxivRetriever(config)
    papers = retriever.retrieve_papers()
    assert papers == []


def test_biorxiv_convert_to_paper(config):
    with open_dict(config.source):
        config.source.biorxiv = {"category": ["bioinformatics"]}
    retriever = BiorxivRetriever(config)
    raw = SAMPLE_BIORXIV_API_RESPONSE["collection"][0]
    paper = retriever.convert_to_paper(raw)
    assert paper.title == "A biorxiv paper"
    assert paper.source == "biorxiv"
    assert "biorxiv.org" in paper.pdf_url
    assert paper.authors == ["Smith, J.", "Doe, A.", "Lee, K."]


def test_biorxiv_requires_category(config):
    with open_dict(config.source):
        config.source.biorxiv = {"category": None}
    with pytest.raises(ValueError, match="category must be specified"):
        BiorxivRetriever(config)


def test_biorxiv_retries_truncated_json(config, monkeypatch):
    from types import SimpleNamespace

    calls = []

    def _patched(url, **kwargs):
        calls.append(kwargs)
        response = SimpleNamespace(status_code=200, raise_for_status=lambda: None)
        if len(calls) == 1:
            response.json = lambda: (_ for _ in ()).throw(
                ValueError("Unterminated string")
            )
        else:
            response.json = lambda: SAMPLE_BIORXIV_API_RESPONSE
        return response

    monkeypatch.setattr(
        "zotero_arxiv_daily.retriever.biorxiv_retriever.requests.get",
        _patched,
    )
    monkeypatch.setattr(
        "zotero_arxiv_daily.retriever.biorxiv_retriever.sleep",
        lambda _: None,
    )
    with open_dict(config.source):
        config.source.biorxiv = {
            "category": ["bioinformatics"],
            "retry_attempts": 2,
            "retry_delay_seconds": 0,
            "timeout_seconds": 30,
        }

    raw_papers = BiorxivRetriever(config)._retrieve_raw_papers()

    assert len(raw_papers) == 1
    assert len(calls) == 2
    assert calls[0]["timeout"] == 30

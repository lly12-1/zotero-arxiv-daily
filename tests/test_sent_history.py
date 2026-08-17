import json
from datetime import datetime, timedelta, timezone

import pytest

from zotero_arxiv_daily.protocol import Paper
from zotero_arxiv_daily.sent_history import SentHistory, paper_fingerprints


NOW = datetime(2026, 7, 31, 1, 0, tzinfo=timezone.utc)


def _paper(**overrides):
    values = {
        "source": "medrxiv",
        "title": "Alpha-synuclein biomarkers in Parkinson disease",
        "authors": ["Doe J"],
        "abstract": "A Parkinson disease cohort study.",
        "url": "https://www.medrxiv.org/content/10.1101/2026.07.30.12345678v1",
        "doi": "10.1101/2026.07.30.12345678",
        "evidence_level": "preprint",
    }
    values.update(overrides)
    return Paper(**values)


def test_history_blocks_repeat_across_processes(tmp_path):
    path = tmp_path / "sent-history.json"
    paper = _paper()
    history = SentHistory(path, now=NOW)
    history.record([paper])
    history.save()

    restored = SentHistory(path, now=NOW + timedelta(days=1))
    unsent, skipped = restored.filter_unsent([paper])

    assert unsent == []
    assert skipped == 1
    payload = json.loads(path.read_text())
    assert payload["version"] == 1
    assert paper.title not in path.read_text()


def test_history_matches_arxiv_revisions_and_normalized_titles(tmp_path):
    path = tmp_path / "sent-history.json"
    original = _paper(
        doi=None,
        url="https://arxiv.org/abs/2607.12345v1",
    )
    revision = _paper(
        doi=None,
        title="Alpha synuclein biomarkers in Parkinson disease",
        url="https://arxiv.org/pdf/2607.12345v3.pdf",
    )
    history = SentHistory(path, now=NOW)
    history.record([original])

    assert history.was_sent(revision)
    assert paper_fingerprints(original) & paper_fingerprints(revision)


def test_published_upgrade_is_allowed_once(tmp_path):
    path = tmp_path / "sent-history.json"
    preprint = _paper()
    published = _paper(
        source="pubmed",
        url="https://pubmed.ncbi.nlm.nih.gov/12345678/",
        pmid="12345678",
        evidence_level="peer_reviewed",
    )
    history = SentHistory(path, now=NOW)
    history.record([preprint])

    assert not history.was_sent(published)

    history.record([published])
    assert history.was_sent(published)
    assert history.was_sent(preprint)


def test_expired_history_does_not_block_delivery(tmp_path):
    path = tmp_path / "sent-history.json"
    paper = _paper()
    old_history = SentHistory(path, retention_days=30, now=NOW - timedelta(days=31))
    old_history.record([paper])
    old_history.save()

    current_history = SentHistory(path, retention_days=30, now=NOW)

    assert not current_history.was_sent(paper)
    assert current_history.records == {}


def test_corrupt_history_fails_closed(tmp_path):
    path = tmp_path / "sent-history.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="Cannot read sent-history"):
        SentHistory(path, now=NOW)


def test_daily_completion_marker_persists(tmp_path):
    path = tmp_path / "sent-history.json"
    history = SentHistory(path, now=NOW)
    history.mark_completed("2026-07-31")
    history.save()

    restored = SentHistory(path, now=NOW + timedelta(hours=1))

    assert restored.completed_on("2026-07-31")
    assert not restored.completed_on("2026-08-01")


def test_daily_delivery_marker_persists_separately_from_completion(tmp_path):
    path = tmp_path / "sent-history.json"
    history = SentHistory(path, now=NOW)
    history.mark_delivered("2026-07-31")
    history.save()

    restored = SentHistory(path, now=NOW + timedelta(hours=1))

    assert restored.delivered_on("2026-07-31")
    assert not restored.completed_on("2026-07-31")

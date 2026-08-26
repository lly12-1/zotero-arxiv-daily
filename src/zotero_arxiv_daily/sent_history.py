from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .dedup import deduplicate_papers, normalize_doi, normalize_title
from .protocol import Paper


_ARXIV_ID_PATTERN = re.compile(
    r"arxiv\.org/(?:abs|pdf)/([^?#/]+(?:/[^?#/]+)?)",
    flags=re.IGNORECASE,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_peer_reviewed(paper: Paper) -> bool:
    return paper.source == "pubmed" or paper.evidence_level == "peer_reviewed"


def _arxiv_id(paper: Paper) -> str | None:
    for value in (paper.url, paper.pdf_url):
        if not value:
            continue
        match = _ARXIV_ID_PATTERN.search(value)
        if match:
            identifier = match.group(1).removesuffix(".pdf")
            return re.sub(r"v\d+$", "", identifier, flags=re.IGNORECASE).casefold()
    return None


def paper_fingerprints(paper: Paper) -> set[str]:
    """Return hashed stable identifiers without exposing titles in the state file."""
    identifiers: set[str] = set()
    if paper.pmid:
        identifiers.add(f"pmid:{paper.pmid.strip().casefold()}")
    if doi := normalize_doi(paper.doi):
        identifiers.add(f"doi:{doi}")
    if arxiv_id := _arxiv_id(paper):
        identifiers.add(f"arxiv:{arxiv_id}")
    if title := normalize_title(paper.title):
        identifiers.add(f"title:{title}")
    return {
        hashlib.sha256(identifier.encode("utf-8")).hexdigest()
        for identifier in identifiers
    }


class SentHistory:
    def __init__(
        self,
        path: str | Path,
        retention_days: int = 30,
        now: datetime | None = None,
    ):
        self.path = Path(path)
        self.retention_days = retention_days
        self.now = (now or _utc_now()).astimezone(timezone.utc)
        payload = self._load()
        self.records = payload["records"]
        self.last_completed_date = payload.get("last_completed_date")
        self.last_delivery_date = payload.get("last_delivery_date")
        paper_fields = {field.name for field in fields(Paper)}
        self.pending_papers = [
            Paper(**{key: value for key, value in item.items() if key in paper_fields})
            for item in payload.get("pending_papers", [])
            if isinstance(item, dict)
        ]
        self.journal_zero_streaks = payload.get("journal_zero_streaks", {})
        self.journal_last_checked = payload.get("journal_last_checked", {})
        self._prune()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "records": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read sent-history file {self.path}: {exc}") from exc
        if payload.get("version") != 1 or not isinstance(payload.get("records"), dict):
            raise ValueError(f"Unsupported sent-history format in {self.path}")
        return payload

    def _prune(self) -> None:
        cutoff = self.now - timedelta(days=self.retention_days)
        retained: dict[str, dict[str, str]] = {}
        for fingerprint, record in self.records.items():
            try:
                sent_at = datetime.fromisoformat(record["sent_at"]).astimezone(timezone.utc)
            except (KeyError, TypeError, ValueError):
                continue
            if sent_at >= cutoff:
                retained[fingerprint] = record
        self.records = retained

    def was_sent(self, paper: Paper) -> bool:
        matches = [
            self.records[fingerprint]
            for fingerprint in paper_fingerprints(paper)
            if fingerprint in self.records
        ]
        if not matches:
            return False
        if _is_peer_reviewed(paper):
            # A formally published article is a meaningful upgrade over a
            # previously delivered preprint and may be sent once more.
            return any(record.get("evidence_level") == "peer_reviewed" for record in matches)
        return True

    def filter_unsent(self, papers: list[Paper]) -> tuple[list[Paper], int]:
        unsent = [paper for paper in papers if not self.was_sent(paper)]
        return unsent, len(papers) - len(unsent)

    def record(self, papers: list[Paper]) -> None:
        sent_at = self.now.isoformat()
        for paper in papers:
            evidence_level = "peer_reviewed" if _is_peer_reviewed(paper) else "preprint"
            record = {
                "sent_at": sent_at,
                "evidence_level": evidence_level,
            }
            for fingerprint in paper_fingerprints(paper):
                self.records[fingerprint] = record

    def get_pending(self) -> list[Paper]:
        pending, _ = self.filter_unsent(self.pending_papers)
        return pending

    def set_pending(self, papers: list[Paper], max_papers: int = 2000) -> None:
        unsent, _ = self.filter_unsent(papers)
        self.pending_papers = deduplicate_papers(unsent)[:max_papers]

    def update_journal_zero_streaks(
        self,
        local_date: str,
        core_journals: list[str],
        retrieved_counts: dict[str, int],
        alert_after_days: int,
    ) -> list[dict[str, int | str]]:
        alerts: list[dict[str, int | str]] = []
        for journal in core_journals:
            count = int(retrieved_counts.get(journal, 0))
            last_checked = self.journal_last_checked.get(journal)
            streak = int(self.journal_zero_streaks.get(journal, 0))
            if count > 0:
                streak = 0
            elif last_checked != local_date:
                streak += 1
            self.journal_zero_streaks[journal] = streak
            self.journal_last_checked[journal] = local_date
            if streak >= alert_after_days:
                alerts.append({"journal": journal, "zero_days": streak})
        return alerts

    def completed_on(self, local_date: str) -> bool:
        return self.last_completed_date == local_date

    def mark_completed(self, local_date: str) -> None:
        self.last_completed_date = local_date

    def delivered_on(self, local_date: str) -> bool:
        return self.last_delivery_date == local_date

    def mark_delivered(self, local_date: str) -> None:
        self.last_delivery_date = local_date

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        payload = {
            "version": 1,
            "updated_at": self.now.isoformat(),
            "retention_days": self.retention_days,
            "last_completed_date": self.last_completed_date,
            "last_delivery_date": self.last_delivery_date,
            "records": self.records,
            "pending_papers": [asdict(paper) for paper in self.pending_papers],
            "journal_zero_streaks": self.journal_zero_streaks,
            "journal_last_checked": self.journal_last_checked,
        }
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(self.path)

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .dedup import normalize_doi, normalize_title
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
        self.records = self._load()
        self._prune()

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read sent-history file {self.path}: {exc}") from exc
        if payload.get("version") != 1 or not isinstance(payload.get("records"), dict):
            raise ValueError(f"Unsupported sent-history format in {self.path}")
        return payload["records"]

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

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        payload = {
            "version": 1,
            "updated_at": self.now.isoformat(),
            "retention_days": self.retention_days,
            "records": self.records,
        }
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(self.path)

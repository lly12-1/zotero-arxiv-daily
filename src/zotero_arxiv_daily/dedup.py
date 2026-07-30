from __future__ import annotations

import re
from difflib import SequenceMatcher

from .protocol import Paper


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    value = doi.casefold().strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    return value.removeprefix("doi:").strip().rstrip(".")


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()


def normalize_author(author: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", author.casefold())


def papers_are_duplicates(left: Paper, right: Paper) -> bool:
    left_doi, right_doi = normalize_doi(left.doi), normalize_doi(right.doi)
    if left_doi and right_doi and left_doi == right_doi:
        return True

    left_title, right_title = normalize_title(left.title), normalize_title(right.title)
    if not left_title or not right_title:
        return False
    title_match = SequenceMatcher(None, left_title, right_title).ratio() >= 0.94
    if not title_match:
        return False

    if not left.authors or not right.authors:
        return True
    return normalize_author(left.authors[0]) == normalize_author(right.authors[0])


def _preferred(left: Paper, right: Paper) -> Paper:
    if left.source == "pubmed" and right.source != "pubmed":
        return left
    if right.source == "pubmed" and left.source != "pubmed":
        return right
    return left


def deduplicate_papers(papers: list[Paper]) -> list[Paper]:
    unique: list[Paper] = []
    for paper in papers:
        for index, existing in enumerate(unique):
            if papers_are_duplicates(paper, existing):
                unique[index] = _preferred(existing, paper)
                break
        else:
            unique.append(paper)
    return unique

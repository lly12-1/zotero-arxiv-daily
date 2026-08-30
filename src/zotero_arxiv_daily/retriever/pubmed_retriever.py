from __future__ import annotations

from collections import Counter
from time import sleep
from typing import Any
from xml.etree import ElementTree

import requests
from loguru import logger

from .base import BaseRetriever, register_retriever
from ..journal_metrics import (
    load_journal_metrics,
    load_journal_search_names,
    match_journal_metric,
    normalize_journal_name,
)
from ..protocol import Paper


EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def _node_text(node: ElementTree.Element | None) -> str:
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def _publication_date(article: ElementTree.Element) -> str | None:
    pub_date = article.find("./MedlineCitation/Article/Journal/JournalIssue/PubDate")
    if pub_date is None:
        return None
    medline_date = _node_text(pub_date.find("MedlineDate"))
    if medline_date:
        return medline_date
    values = [_node_text(pub_date.find(part)) for part in ("Year", "Month", "Day")]
    return "-".join(value for value in values if value) or None


@register_retriever("pubmed")
class PubmedRetriever(BaseRetriever):
    def __init__(self, config):
        super().__init__(config)
        self.session = requests.Session()
        self.session.trust_env = False
        self.metrics = load_journal_metrics(self.retriever_config.journal_metrics_file)
        self.journal_search_names = load_journal_search_names(
            self.retriever_config.journal_metrics_file
        )
        self.core_journals = [
            str(value) for value in self.retriever_config.get("core_journals", [])
        ]
        self.core_journal_search_names = load_journal_search_names(
            self.retriever_config.journal_metrics_file,
            self.core_journals,
        )
        core_normalized = {
            normalize_journal_name(value) for value in self.core_journal_search_names
        }
        self.general_journal_search_names = [
            name
            for name in self.journal_search_names
            if normalize_journal_name(name) not in core_normalized
        ]
        self.min_sjr = float(self.retriever_config.min_sjr)
        self.priority_pmids: set[str] = set()
        self.core_pmids: set[str] = set()
        self.raw_journal_counts: Counter[str] = Counter()
        self.source_filtered_journal_counts: Counter[str] = Counter()

    def _request(self, endpoint: str, params: dict[str, Any]) -> requests.Response:
        timeout = int(self.retriever_config.get("timeout_seconds", 30))
        attempts = int(self.retriever_config.get("retry_attempts", 4))
        for attempt in range(1, attempts + 1):
            try:
                url = f"{EUTILS_BASE}/{endpoint}"
                long_payload = max(
                    len(str(params.get("term", ""))),
                    len(str(params.get("id", ""))),
                ) > 1500
                if endpoint in {"esearch.fcgi", "efetch.fcgi"} and long_payload:
                    response = self.session.post(url, data=params, timeout=timeout)
                else:
                    response = self.session.get(url, params=params, timeout=timeout)
                response.raise_for_status()
                return response
            except requests.RequestException:
                if attempt == attempts:
                    raise
                wait = attempt * 3
                logger.warning(
                    f"PubMed {endpoint} failed ({attempt}/{attempts}); retrying in {wait}s"
                )
                sleep(wait)
        raise RuntimeError("PubMed request failed")

    def _search_ids(self, term: str) -> list[str]:
        date_windows = [
            ("pdat", int(self.retriever_config.get("lookback_days", 3))),
        ]
        entry_lookback_days = int(
            self.retriever_config.get("entry_lookback_days", 0)
        )
        if entry_lookback_days > 0:
            date_windows.append(("edat", entry_lookback_days))

        ids: list[str] = []
        for date_type, lookback_days in date_windows:
            params: dict[str, Any] = {
                "db": "pubmed",
                "retmode": "json",
                "sort": "pub date",
                "retmax": int(self.retriever_config.get("retmax", 200)),
                "reldate": lookback_days,
                "datetype": date_type,
                "term": term,
                "tool": "zotero_arxiv_daily",
            }
            api_key = self.retriever_config.get("api_key")
            if api_key:
                params["api_key"] = str(api_key)
            search = self._request("esearch.fcgi", params).json()
            ids.extend(search.get("esearchresult", {}).get("idlist", []))
        return list(dict.fromkeys(ids))

    def _retrieve_raw_papers(self) -> list[ElementTree.Element]:
        topic_query = str(self.retriever_config.query)
        if self.retriever_config.get("restrict_search_to_journal_metrics", True):
            general_journal_query = " OR ".join(
                f'"{name}"[Journal]' for name in self.general_journal_search_names
            )
            general_query = f"({topic_query}) AND ({general_journal_query})"
        else:
            general_query = topic_query
        ids = self._search_ids(general_query)

        if self.core_journal_search_names:
            core_query = " OR ".join(
                f'"{name}"[Journal]' for name in self.core_journal_search_names
            )
            core_ids = self._search_ids(f"({core_query})")
            self.core_pmids = set(core_ids)
            ids = list(dict.fromkeys(core_ids + ids))
        priority_query = self.retriever_config.get("priority_query")
        if priority_query:
            priority_ids = self._search_ids(str(priority_query))
            self.priority_pmids = set(priority_ids)
            ids = list(dict.fromkeys(priority_ids + ids))
        if self.config.executor.debug:
            ids = ids[:10]
        if not ids:
            return []

        fetch_params: dict[str, Any] = {
            "db": "pubmed",
            "retmode": "xml",
            "id": ",".join(ids),
            "tool": "zotero_arxiv_daily",
        }
        api_key = self.retriever_config.get("api_key")
        if api_key:
            fetch_params["api_key"] = str(api_key)
        root = ElementTree.fromstring(self._request("efetch.fcgi", fetch_params).content)
        return list(root.findall("./PubmedArticle"))

    def convert_to_paper(self, raw_paper: ElementTree.Element) -> Paper | None:
        citation = raw_paper.find("./MedlineCitation")
        article = raw_paper.find("./MedlineCitation/Article")
        if citation is None or article is None:
            return None

        publication_types = {
            _node_text(node).casefold()
            for node in article.findall("./PublicationTypeList/PublicationType")
        }
        excluded = {
            str(value).casefold()
            for value in self.retriever_config.get("exclude_publication_types", [])
        }
        pmid = _node_text(citation.find("./PMID"))
        priority_paper = pmid in self.priority_pmids
        core_paper = pmid in self.core_pmids
        if priority_paper or core_paper:
            allowed_key = (
                "priority_allowed_publication_types"
                if priority_paper
                else "core_allowed_publication_types"
            )
            allowed_types = {
                str(value).casefold()
                for value in self.retriever_config.get(allowed_key, [])
            }
            excluded -= allowed_types
        if publication_types & excluded:
            return None

        journal = _node_text(article.find("./Journal/Title"))
        metric = match_journal_metric(journal, self.metrics)
        if (
            not priority_paper
            and not core_paper
            and (metric is None or metric.sjr < self.min_sjr)
        ):
            return None
        title = _node_text(article.find("./ArticleTitle"))
        abstracts = []
        for node in article.findall("./Abstract/AbstractText"):
            text = _node_text(node)
            if text:
                label = node.attrib.get("Label")
                abstracts.append(f"{label}: {text}" if label else text)
        abstract = "\n".join(abstracts)
        if not title or not abstract:
            return None

        authors: list[str] = []
        for author in article.findall("./AuthorList/Author"):
            collective = _node_text(author.find("./CollectiveName"))
            if collective:
                authors.append(collective)
                continue
            last_name = _node_text(author.find("./LastName"))
            initials = _node_text(author.find("./Initials"))
            name = " ".join(value for value in (last_name, initials) if value)
            if name:
                authors.append(name)

        doi = None
        for article_id in raw_paper.findall("./PubmedData/ArticleIdList/ArticleId"):
            if article_id.attrib.get("IdType") == "doi":
                doi = _node_text(article_id)
                break

        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        return Paper(
            source=self.name,
            title=title,
            authors=authors,
            abstract=abstract,
            url=url,
            pdf_url=url,
            doi=doi,
            pmid=pmid,
            journal=metric.journal if metric else journal,
            publication_date=_publication_date(raw_paper),
            evidence_level="peer_reviewed",
            journal_metric_name="SJR" if metric else None,
            journal_metric_value=metric.sjr if metric else None,
            journal_metric_year=metric.year if metric else None,
            journal_quartile=metric.quartile if metric else None,
            special_topic="Huntington disease" if priority_paper else None,
            topic_bypass=priority_paper or (
                core_paper
                and bool(
                    self.retriever_config.get(
                        "core_journals_bypass_topic", True
                    )
                )
            ),
        )

    def retrieve_papers(self) -> list[Paper]:
        self.raw_journal_counts.clear()
        self.source_filtered_journal_counts.clear()
        papers: list[Paper] = []
        for raw_paper in self._retrieve_raw_papers():
            article = raw_paper.find("./MedlineCitation/Article")
            raw_journal = _node_text(article.find("./Journal/Title")) if article is not None else ""
            metric = match_journal_metric(raw_journal, self.metrics)
            journal = metric.journal if metric else raw_journal or "Unknown journal"
            self.raw_journal_counts[journal] += 1
            try:
                paper = self.convert_to_paper(raw_paper)
            except Exception as exc:
                logger.warning(f"Skipping malformed PubMed record: {exc}")
                self.source_filtered_journal_counts[journal] += 1
                continue
            if paper is not None:
                papers.append(paper)
            else:
                self.source_filtered_journal_counts[journal] += 1
        return papers

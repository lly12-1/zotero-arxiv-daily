from __future__ import annotations

from time import sleep
from typing import Any
from xml.etree import ElementTree

import requests
from loguru import logger

from .base import BaseRetriever, register_retriever
from ..journal_metrics import load_journal_metrics, match_journal_metric
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
        self.min_sjr = float(self.retriever_config.min_sjr)
        self.priority_pmids: set[str] = set()

    def _request(self, endpoint: str, params: dict[str, Any]) -> requests.Response:
        timeout = int(self.retriever_config.get("timeout_seconds", 30))
        attempts = int(self.retriever_config.get("retry_attempts", 4))
        for attempt in range(1, attempts + 1):
            try:
                response = self.session.get(
                    f"{EUTILS_BASE}/{endpoint}",
                    params=params,
                    timeout=timeout,
                )
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
        params: dict[str, Any] = {
            "db": "pubmed",
            "retmode": "json",
            "sort": "pub date",
            "retmax": int(self.retriever_config.get("retmax", 200)),
            "reldate": int(self.retriever_config.get("lookback_days", 3)),
            "datetype": "pdat",
            "term": term,
            "tool": "zotero_arxiv_daily",
        }
        api_key = self.retriever_config.get("api_key")
        if api_key:
            params["api_key"] = str(api_key)
        search = self._request("esearch.fcgi", params).json()
        return search.get("esearchresult", {}).get("idlist", [])

    def _retrieve_raw_papers(self) -> list[ElementTree.Element]:
        ids = self._search_ids(str(self.retriever_config.query))
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
        if priority_paper:
            allowed_priority_types = {
                str(value).casefold()
                for value in self.retriever_config.get(
                    "priority_allowed_publication_types",
                    [],
                )
            }
            excluded -= allowed_priority_types
        if publication_types & excluded:
            return None

        journal = _node_text(article.find("./Journal/Title"))
        metric = match_journal_metric(journal, self.metrics)
        if (
            not priority_paper
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
        )

    def retrieve_papers(self) -> list[Paper]:
        papers: list[Paper] = []
        for raw_paper in self._retrieve_raw_papers():
            try:
                paper = self.convert_to_paper(raw_paper)
            except Exception as exc:
                logger.warning(f"Skipping malformed PubMed record: {exc}")
                continue
            if paper is not None:
                papers.append(paper)
        return papers

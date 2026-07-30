from loguru import logger
from pyzotero import zotero
from omegaconf import DictConfig, ListConfig
from .utils import glob_match
from .retriever import get_retriever_cls
from .protocol import CorpusPaper
from .dedup import deduplicate_papers
import random
import re
from datetime import datetime
from time import sleep
from .reranker import get_reranker_cls
from .construct_email import render_email
from .utils import send_email
from openai import DefaultHttpxClient, OpenAI
from tqdm import tqdm
import httpx


def _normalized_search_text(value: str) -> str:
    value = value.casefold().replace("α", "alpha").replace("β", "beta")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def filter_topic_papers(papers, keywords) -> list:
    normalized_keywords = [_normalized_search_text(str(keyword)) for keyword in keywords]
    selected = []
    for paper in papers:
        text = _normalized_search_text(f"{paper.title} {paper.abstract}")
        padded = f" {text} "
        if any(f" {keyword} " in padded for keyword in normalized_keywords if keyword):
            selected.append(paper)
    return selected


def apply_journal_quality_bonus(papers, bonus_per_sjr: float) -> list:
    for paper in papers:
        if paper.score is None:
            continue
        if paper.source == "pubmed" and paper.journal_metric_value is not None:
            paper.score += min(float(paper.journal_metric_value), 12.0) * bonus_per_sjr
    return sorted(papers, key=lambda paper: paper.score or 0.0, reverse=True)


def select_with_published_priority(papers, max_paper_num: int) -> list:
    published = [paper for paper in papers if paper.source == "pubmed"]
    preprints = [paper for paper in papers if paper.source != "pubmed"]
    return (published + preprints)[:max_paper_num]


def normalize_path_patterns(patterns: list[str] | ListConfig | None, config_key: str) -> list[str] | None:
    if patterns is None:
        return None

    if not isinstance(patterns, (list, ListConfig)):
        raise TypeError(
            f"config.zotero.{config_key} must be a list of glob patterns or null, "
            'for example ["2026/survey/**"]. Single strings are not supported.'
        )

    if any(not isinstance(pattern, str) for pattern in patterns):
        raise TypeError(f"config.zotero.{config_key} must contain only glob pattern strings.")

    return list(patterns)


class Executor:
    def __init__(self, config:DictConfig):
        self.config = config
        self.include_path_patterns = normalize_path_patterns(config.zotero.include_path, "include_path")
        self.ignore_path_patterns = normalize_path_patterns(config.zotero.ignore_path, "ignore_path")
        self.retrievers = {
            source: get_retriever_cls(source)(config) for source in config.executor.source
        }
        self.reranker = get_reranker_cls(config.executor.reranker)(config)
        self.openai_client = OpenAI(
            api_key=config.llm.api.key,
            base_url=config.llm.api.base_url,
            timeout=120,
            max_retries=3,
            # The same local proxy that disrupts Zotero also breaks TLS to
            # DeepSeek. GitHub Actions works with this direct client as well.
            http_client=DefaultHttpxClient(trust_env=False),
        )

    @staticmethod
    def _fetch_zotero_with_retry(fetch, label: str, attempts: int = 5):
        for attempt in range(1, attempts + 1):
            try:
                return fetch()
            except httpx.HTTPError as exc:
                if attempt == attempts:
                    raise
                wait_seconds = attempt * 5
                logger.warning(
                    f"Zotero {label} request failed "
                    f"({attempt}/{attempts}): {exc}. "
                    f"Retrying in {wait_seconds} seconds."
                )
                sleep(wait_seconds)

    def fetch_zotero_corpus(self) -> list[CorpusPaper]:
        logger.info("Fetching zotero corpus")
        # Some local proxy configurations terminate TLS for authenticated
        # Zotero API requests. Zotero is a public HTTPS API, so use a direct
        # client here while leaving proxy behavior unchanged for other sources.
        zotero_client = httpx.Client(follow_redirects=True, trust_env=False)
        zot = zotero.Zotero(
            self.config.zotero.user_id,
            'user',
            self.config.zotero.api_key,
            client=zotero_client,
        )
        collections = self._fetch_zotero_with_retry(
            lambda: zot.everything(zot.collections()),
            "collections",
        )
        collections = {c['key']:c for c in collections}
        corpus = self._fetch_zotero_with_retry(
            lambda: zot.everything(
                zot.items(itemType='conferencePaper || journalArticle || preprint')
            ),
            "items",
        )
        corpus = [c for c in corpus if c['data']['abstractNote'] != '']
        def get_collection_path(col_key:str) -> str:
            if p := collections[col_key]['data']['parentCollection']:
                return get_collection_path(p) + '/' + collections[col_key]['data']['name']
            else:
                return collections[col_key]['data']['name']
        for c in corpus:
            paths = [get_collection_path(col) for col in c['data']['collections']]
            c['paths'] = paths
        logger.info(f"Fetched {len(corpus)} zotero papers")
        return [CorpusPaper(
            title=c['data']['title'],
            abstract=c['data']['abstractNote'],
            added_date=datetime.strptime(c['data']['dateAdded'], '%Y-%m-%dT%H:%M:%SZ'),
            paths=c['paths']
        ) for c in corpus]
    
    def filter_corpus(self, corpus:list[CorpusPaper]) -> list[CorpusPaper]:
        if self.include_path_patterns:
            logger.info(f"Selecting zotero papers matching include_path: {self.include_path_patterns}")
            corpus = [
                c for c in corpus
                if any(
                    glob_match(path, pattern)
                    for path in c.paths
                    for pattern in self.include_path_patterns
                )
            ]
        if self.ignore_path_patterns:
            logger.info(f"Excluding zotero papers matching ignore_path: {self.ignore_path_patterns}")
            corpus = [
                c for c in corpus
                if not any(
                    glob_match(path, pattern)
                    for path in c.paths
                    for pattern in self.ignore_path_patterns
                )
            ]
        if self.include_path_patterns or self.ignore_path_patterns:
            samples = random.sample(corpus, min(5, len(corpus)))
            samples = '\n'.join([c.title + ' - ' + '\n'.join(c.paths) for c in samples])
            logger.info(f"Selected {len(corpus)} zotero papers:\n{samples}\n...")
        return corpus

    
    def run(self):
        corpus = self.fetch_zotero_corpus()
        corpus = self.filter_corpus(corpus)
        if len(corpus) == 0:
            logger.error(f"No zotero papers found. Please check your zotero settings:\n{self.config.zotero}")
            return
        all_papers = []
        for source, retriever in self.retrievers.items():
            logger.info(f"Retrieving {source} papers...")
            try:
                papers = retriever.retrieve_papers()
            except Exception as exc:
                # A transient outage in one public source should not suppress
                # papers successfully retrieved from the remaining sources.
                logger.error(
                    f"Skipping unavailable source {source} after retries: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue
            if len(papers) == 0:
                logger.info(f"No {source} papers found")
                continue
            logger.info(f"Retrieved {len(papers)} {source} papers")
            all_papers.extend(papers)
        logger.info(f"Total {len(all_papers)} papers retrieved from all sources")
        topic_keywords = self.config.executor.get("topic_keywords", [])
        if topic_keywords:
            before_filter = len(all_papers)
            all_papers = filter_topic_papers(all_papers, topic_keywords)
            logger.info(
                f"Topic filter retained {len(all_papers)} of {before_filter} papers"
            )
        before_dedup = len(all_papers)
        all_papers = deduplicate_papers(all_papers)
        logger.info(
            f"Cross-source deduplication retained {len(all_papers)} "
            f"of {before_dedup} papers"
        )
        reranked_papers = []
        if len(all_papers) > 0:
            logger.info("Reranking papers...")
            reranked_papers = self.reranker.rerank(all_papers, corpus)
            reranked_papers = apply_journal_quality_bonus(
                reranked_papers,
                float(self.config.executor.get("pubmed_sjr_bonus", 0.12)),
            )
            reranked_papers = select_with_published_priority(
                reranked_papers,
                self.config.executor.max_paper_num,
            )
            published_count = sum(
                paper.source == "pubmed" for paper in reranked_papers
            )
            logger.info(
                f"Selected {len(reranked_papers)} papers for email: "
                f"{published_count} published and "
                f"{len(reranked_papers) - published_count} preprints"
            )
            logger.info("Generating TLDR and affiliations...")
            for p in tqdm(reranked_papers):
                p.generate_tldr(self.openai_client, self.config.llm)
                p.generate_affiliations(self.openai_client, self.config.llm)
        elif not self.config.executor.send_empty:
            logger.info("No new papers found. No email will be sent.")
            return
        logger.info("Sending email...")
        email_content = render_email(reranked_papers)
        send_email(self.config, email_content)
        logger.info("Email sent successfully")

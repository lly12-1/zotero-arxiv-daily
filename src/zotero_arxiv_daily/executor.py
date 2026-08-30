from loguru import logger
from collections import Counter
from pyzotero import zotero
from omegaconf import DictConfig, ListConfig
from .utils import glob_match
from .retriever import get_retriever_cls
from .protocol import CorpusPaper, Paper
from .dedup import deduplicate_papers
from .sent_history import SentHistory
import random
import re
from datetime import datetime
from zoneinfo import ZoneInfo
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
        if paper.topic_bypass:
            selected.append(paper)
            continue
        text = _normalized_search_text(f"{paper.title} {paper.abstract}")
        padded = f" {text} "
        if any(f" {keyword} " in padded for keyword in normalized_keywords if keyword):
            selected.append(paper)
    return selected


def filter_topic_papers_by_rules(
    papers,
    direct_keywords,
    mechanism_keywords,
    context_keywords,
) -> list:
    """Keep direct disease hits or mechanism hits in the requested organ context."""
    direct = [_normalized_search_text(str(value)) for value in direct_keywords]
    mechanisms = [_normalized_search_text(str(value)) for value in mechanism_keywords]
    contexts = [_normalized_search_text(str(value)) for value in context_keywords]

    def matches(padded: str, values: list[str]) -> bool:
        return any(f" {value} " in padded for value in values if value)

    selected = []
    for paper in papers:
        if paper.topic_bypass:
            selected.append(paper)
            continue
        text = _normalized_search_text(f"{paper.title} {paper.abstract}")
        padded = f" {text} "
        if matches(padded, direct) or (
            matches(padded, mechanisms) and matches(padded, contexts)
        ):
            selected.append(paper)
    return selected


def apply_journal_quality_bonus(papers, bonus_per_sjr: float) -> list:
    for paper in papers:
        if paper.score is None:
            continue
        if paper.source == "pubmed" and paper.journal_metric_value is not None:
            paper.score += min(float(paper.journal_metric_value), 12.0) * bonus_per_sjr
    return sorted(papers, key=lambda paper: paper.score or 0.0, reverse=True)


def _matches_priority_topic(paper, keywords) -> bool:
    text = _normalized_search_text(f"{paper.title} {paper.abstract}")
    padded = f" {text} "
    return any(
        f" {_normalized_search_text(str(keyword))} " in padded
        for keyword in keywords
        if _normalized_search_text(str(keyword))
    )


def select_with_published_priority(
    papers,
    max_paper_num: int,
    priority_keywords=None,
) -> list:
    priority_keywords = priority_keywords or []
    priority = [
        paper for paper in papers
        if _matches_priority_topic(paper, priority_keywords)
    ]
    regular = [paper for paper in papers if paper not in priority]
    # Huntington disease coverage is additive: keep every matching paper,
    # then retain the normal daily quota for the remaining topics.
    regular_published = [paper for paper in regular if paper.source == "pubmed"]
    regular_preprints = [paper for paper in regular if paper.source != "pubmed"]
    return priority + (regular_published + regular_preprints)[:max_paper_num]


def _current_run_date(timezone_name: str) -> str:
    return datetime.now(ZoneInfo(timezone_name)).date().isoformat()


def _journal_counts(papers: list[Paper]) -> Counter[str]:
    return Counter(
        paper.journal
        for paper in papers
        if paper.source == "pubmed" and paper.journal
    )


def _journal_catalog(pubmed_retriever) -> list[str]:
    if pubmed_retriever is None or not hasattr(pubmed_retriever, "metrics"):
        return []
    return list(
        dict.fromkeys(metric.journal for metric in pubmed_retriever.metrics.values())
    )


def _build_journal_report(
    journals: list[str],
    core_journals: list[str],
    retrieved: Counter[str],
    eligible: Counter[str],
    sent: Counter[str],
    pending: Counter[str],
) -> list[dict[str, int | str | bool]]:
    core_set = set(core_journals)
    return [
        {
            "journal": journal,
            "core": journal in core_set,
            "retrieved": int(retrieved.get(journal, 0)),
            "filtered": max(
                int(retrieved.get(journal, 0)) - int(eligible.get(journal, 0)),
                0,
            ),
            "sent": int(sent.get(journal, 0)),
            "pending": int(pending.get(journal, 0)),
        }
        for journal in journals
    ]


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
        sent_history = SentHistory(
            self.config.executor.get(
                "sent_history_path",
                ".state/sent-history.json",
            ),
            int(self.config.executor.get("sent_history_days", 30)),
        )
        run_date = _current_run_date(
            str(self.config.executor.get("daily_timezone", "Asia/Shanghai"))
        )
        if self.config.executor.get("mark_today_complete_only", False):
            sent_history.mark_completed(run_date)
            sent_history.save()
            logger.info(
                f"Marked daily digest complete for {run_date}; "
                "no retrieval or email was performed"
            )
            return
        seed_history_only = self.config.executor.get("seed_history_only", False)
        force_run = self.config.executor.get("force_run", False)
        if not seed_history_only and not force_run and sent_history.completed_on(run_date):
            logger.info(
                f"Daily digest already completed for {run_date}; "
                "skipping duplicate trigger"
            )
            return

        ranking_profile = list(self.config.executor.get("ranking_profile", []))
        if ranking_profile:
            now = datetime.now()
            corpus = [
                CorpusPaper(
                    title=f"Configured topic profile {index}",
                    abstract=str(value),
                    added_date=now,
                    paths=["configured-topic-profile"],
                )
                for index, value in enumerate(ranking_profile, start=1)
            ]
            logger.info(
                f"Using {len(corpus)} configured topic profiles for reranking"
            )
        else:
            corpus = self.fetch_zotero_corpus()
            corpus = self.filter_corpus(corpus)
        if len(corpus) == 0:
            logger.error(f"No zotero papers found. Please check your zotero settings:\n{self.config.zotero}")
            return
        all_papers = []
        successful_source_count = 0
        failed_sources = []
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
                failed_sources.append(source)
                continue
            successful_source_count += 1
            if len(papers) == 0:
                logger.info(f"No {source} papers found")
                continue
            logger.info(f"Retrieved {len(papers)} {source} papers")
            all_papers.extend(papers)
        if successful_source_count == 0:
            raise RuntimeError(
                "All configured paper sources failed; daily completion was not marked"
            )
        logger.info(f"Total {len(all_papers)} papers retrieved from all sources")
        pubmed_retriever = self.retrievers.get("pubmed")
        journal_order = _journal_catalog(pubmed_retriever)
        core_journals = (
            list(getattr(pubmed_retriever, "core_journals", []))
            if pubmed_retriever is not None
            else []
        )
        retrieved_journal_counts = Counter(
            getattr(pubmed_retriever, "raw_journal_counts", {})
        )
        journal_alerts = []
        if pubmed_retriever is not None and "pubmed" not in failed_sources:
            journal_alerts = sent_history.update_journal_zero_streaks(
                run_date,
                core_journals,
                retrieved_journal_counts,
                int(self.config.executor.get("journal_zero_alert_days", 3)),
            )
            for alert in journal_alerts:
                logger.warning(
                    f"Core journal zero-retrieval alert: {alert['journal']} has "
                    f"reported zero records for {alert['zero_days']} consecutive days"
                )
        topic_rules = self.config.executor.get("topic_rules")
        topic_keywords = self.config.executor.get("topic_keywords", [])
        if topic_rules:
            before_filter = len(all_papers)
            all_papers = filter_topic_papers_by_rules(
                all_papers,
                topic_rules.get("direct_keywords", []),
                topic_rules.get("mechanism_keywords", []),
                topic_rules.get("context_keywords", []),
            )
            logger.info(
                f"Rule-based topic filter retained {len(all_papers)} of "
                f"{before_filter} papers"
            )
        elif topic_keywords:
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
        all_papers, previously_sent_count = sent_history.filter_unsent(all_papers)
        logger.info(
            f"Sent-history filter excluded {previously_sent_count} previously "
            f"delivered papers; {len(all_papers)} remain"
        )
        current_eligible_journal_counts = _journal_counts(all_papers)
        pending_papers = sent_history.get_pending()
        if pending_papers:
            logger.info(
                f"Restored {len(pending_papers)} papers from the persistent pending queue"
            )
        all_papers = deduplicate_papers(pending_papers + all_papers)
        all_papers, pending_sent_count = sent_history.filter_unsent(all_papers)
        if pending_sent_count:
            logger.info(
                f"Removed {pending_sent_count} already delivered papers while merging "
                "the pending queue"
            )
        sent_history.set_pending(
            all_papers,
            int(self.config.executor.get("pending_max_papers", 2000)),
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
                self.config.executor.get("priority_topic_keywords", []),
            )
            selected_ids = {id(paper) for paper in reranked_papers}
            remaining_papers = [
                paper for paper in all_papers if id(paper) not in selected_ids
            ]
            sent_history.set_pending(
                remaining_papers,
                int(self.config.executor.get("pending_max_papers", 2000)),
            )
            published_count = sum(
                paper.source == "pubmed" for paper in reranked_papers
            )
            logger.info(
                f"Selected {len(reranked_papers)} papers for email: "
                f"{published_count} published and "
                f"{len(reranked_papers) - published_count} preprints"
            )
            if seed_history_only:
                excluded_pmids = {
                    value.strip()
                    for value in str(
                        self.config.executor.get("seed_exclude_pmids", "")
                    ).split(",")
                    if value.strip()
                }
                seed_papers = [
                    paper
                    for paper in reranked_papers
                    if not paper.pmid or paper.pmid not in excluded_pmids
                ]
                sent_history.record(seed_papers)
                sent_history.save()
                logger.info(
                    f"Seeded sent history with {len(seed_papers)} papers; "
                    f"excluded {len(reranked_papers) - len(seed_papers)} explicitly "
                    "new PubMed papers; no email was sent"
                )
                return
            logger.info("Generating TLDR and affiliations...")
            for p in tqdm(reranked_papers):
                p.generate_tldr(self.openai_client, self.config.llm)
                p.generate_affiliations(self.openai_client, self.config.llm)
        elif seed_history_only:
            sent_history.save()
            logger.info("No papers available to seed; no email was sent")
            return
        elif failed_sources:
            sent_history.save()
            logger.warning(
                "No new papers were found from the available sources; "
                f"daily completion was not marked because these sources failed: "
                f"{', '.join(failed_sources)}"
            )
            return
        elif sent_history.delivered_on(run_date):
            sent_history.mark_completed(run_date)
            sent_history.save()
            logger.info(
                "No additional papers found after an earlier partial delivery; "
                f"marked daily digest complete for {run_date} without sending "
                "a duplicate empty notification"
            )
            return
        elif not self.config.executor.send_empty:
            sent_history.mark_completed(run_date)
            sent_history.save()
            logger.info("No new papers found. No email will be sent.")
            logger.info(f"Marked daily digest complete for {run_date}")
            return
        sent_journal_counts = _journal_counts(reranked_papers)
        pending_journal_counts = _journal_counts(sent_history.get_pending())
        journal_report = _build_journal_report(
            journal_order,
            core_journals,
            retrieved_journal_counts,
            current_eligible_journal_counts,
            sent_journal_counts,
            pending_journal_counts,
        )
        logger.info(
            "Journal report: "
            + "; ".join(
                f"{row['journal']} retrieved={row['retrieved']} "
                f"filtered={row['filtered']} sent={row['sent']} "
                f"pending={row['pending']}"
                for row in journal_report
            )
        )
        logger.info("Sending email...")
        email_content = render_email(
            reranked_papers,
            journal_report=journal_report,
            journal_alerts=journal_alerts,
            pending_count=len(sent_history.get_pending()),
            digest_name=str(
                self.config.email.get("digest_name", "神经退行性疾病文献推送")
            ),
            priority_topic_label=self.config.email.get(
                "priority_topic_label", "亨廷顿病"
            ),
            max_paper_num=int(self.config.executor.max_paper_num),
        )
        if journal_alerts:
            send_email(
                self.config,
                email_content,
                subject=str(
                    self.config.email.get(
                        "alert_subject", "⚠️ 核心期刊抓取报警及今日文献推送"
                    )
                ),
            )
        elif len(reranked_papers) == 0:
            send_email(
                self.config,
                email_content,
                subject=str(
                    self.config.email.get("empty_subject", "今日无新增文献")
                ),
            )
        else:
            send_email(self.config, email_content)
        logger.info("Email sent successfully")
        sent_history.record(reranked_papers)
        sent_history.mark_delivered(run_date)
        if not failed_sources:
            sent_history.mark_completed(run_date)
        sent_history.save()
        if failed_sources:
            logger.warning(
                f"Persisted sent history for {len(reranked_papers)} delivered papers; "
                "daily completion was not marked so the fallback can retry failed "
                f"sources: {', '.join(failed_sources)}"
            )
        else:
            logger.info(
                f"Persisted sent history for {len(reranked_papers)} delivered papers "
                f"and marked {run_date} complete"
            )

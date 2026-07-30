import requests
from .base import BaseRetriever, register_retriever
from ..protocol import Paper
from loguru import logger
from typing import Any
from time import sleep

@register_retriever("biorxiv")
class BiorxivRetriever(BaseRetriever):
    server = "biorxiv"

    def __init__(self, config):
        super().__init__(config)
        if self.retriever_config.category is None:
            raise ValueError(f"category must be specified for {self.name}")

    def _retrieve_raw_papers(self) -> list[dict[str, Any]]:
        api_url = f"https://api.biorxiv.org/details/{self.server}/2d"
        retry_num = int(self.retriever_config.get("retry_attempts", 5))
        delay_time = int(self.retriever_config.get("retry_delay_seconds", 5))
        timeout = int(self.retriever_config.get("timeout_seconds", 60))
        for i in range(retry_num):
            try:
                response = requests.get(api_url, timeout=timeout)
                response.raise_for_status()
                # A successful HTTP status does not guarantee a complete JSON
                # body. bioRxiv occasionally closes the response mid-string,
                # so parsing must be part of the retry boundary.
                result = response.json()
                if not isinstance(result, dict) or "collection" not in result:
                    raise ValueError("bioRxiv response has no collection field")
                break
            except (requests.RequestException, ValueError) as e:
                if i == retry_num - 1:
                    raise e
                else:
                    logger.warning(
                        f"Failed to retrieve or parse {self.server} papers "
                        f"({i + 1}/{retry_num}): {e}. "
                        f"Retry in {delay_time} seconds."
                    )
                    sleep(delay_time)
        collection = result['collection']
        if len(collection) == 0:
            logger.warning(f"No paper found. API Message: {result['messages']}")
            return []
        all_dates = set(c['date'] for c in collection)
        latest_date = sorted(all_dates)[-1]
        collection = [c for c in collection if c['date'] == latest_date]
        categories = [c.lower() for c in self.retriever_config.category]
        collection = [c for c in collection if c['category'] in categories]
        if self.config.executor.debug:
            collection = collection[:10]
        return collection


    def convert_to_paper(self, raw_paper:dict[str, Any]) -> Paper | None:
        title = raw_paper['title']
        authors = [a.strip() for a in raw_paper['authors'].split(';')]
        abstract = raw_paper['abstract']
        pdf_url = f"https://www.{self.server}.org/content/{raw_paper['doi']}v{raw_paper['version']}.full.pdf"
        full_text = None # biorxiv forbids scraping its pdf
        return Paper(
            source=self.name,
            title=title,
            authors=authors,
            abstract=abstract,
            url=pdf_url,
            pdf_url=pdf_url,
            full_text=full_text,
            doi=raw_paper["doi"],
            publication_date=raw_paper.get("date"),
            evidence_level="preprint",
        )

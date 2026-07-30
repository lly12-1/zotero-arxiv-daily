from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class JournalMetric:
    journal: str
    sjr: float
    quartile: str
    year: int
    source_url: str


def normalize_journal_name(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "", value)


def resolve_metrics_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parents[2] / path


def load_journal_metrics(path_value: str) -> dict[str, JournalMetric]:
    path = resolve_metrics_path(path_value)
    metrics: dict[str, JournalMetric] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            metric = JournalMetric(
                journal=row["journal"],
                sjr=float(row["sjr"]),
                quartile=row["quartile"],
                year=int(row["year"]),
                source_url=row["source_url"],
            )
            names = [row["journal"], *row.get("aliases", "").split("|")]
            for name in names:
                if name.strip():
                    metrics[normalize_journal_name(name)] = metric
    return metrics


def match_journal_metric(
    journal: str,
    metrics: dict[str, JournalMetric],
) -> JournalMetric | None:
    return metrics.get(normalize_journal_name(journal))

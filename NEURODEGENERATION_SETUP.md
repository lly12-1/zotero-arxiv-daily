# Neurodegeneration daily digest

This checkout sends a daily hybrid literature digest for neurodegenerative
disease, Alzheimer disease, Parkinson disease and movement disorders, and
Huntington disease.

## What is included

The daily candidate pool contains two clearly separated evidence layers:

1. **Formally published articles** from PubMed. Records must match the disease
   query, be an original article rather than a review/editorial/letter, and
   match the public 2024 SJR journal snapshot at `SJR >= 1.5`.
2. **Preprints** from arXiv, bioRxiv, and medRxiv. They must match the disease
   keyword allowlist but do not need a journal metric. Every preprint is
   explicitly labelled as not peer reviewed.

Candidates are deduplicated by DOI and then by normalized title plus first
author. If a preprint and its PubMed version match, the formally published
version is retained. Semantic relevance to the Zotero library is then scored,
with a small transparent SJR bonus for PubMed records. The merged email remains
capped at **30 papers total**.

## Journal metric policy

`config/neurology_journals_sjr_2024.csv` is a versioned, auditable snapshot of
selected high-impact neurology and neuroscience journals. Each row records the
journal name and aliases, public 2024 SJR value, quartile, and SCImago source
URL.

SJR is a public journal-prestige proxy and is **not** a Journal Impact Factor.
The active threshold is configured in `config/custom.yaml`:

```yaml
source:
  pubmed:
    journal_metrics_file: config/neurology_journals_sjr_2024.csv
    min_sjr: 1.5
```

Update the snapshot deliberately when a new annual edition is available; do
not silently compare values from different years.

## Recommendation precision

The disease keyword allowlist is applied before ranking. Semantic ranking still
depends on the Zotero corpus. For best precision:

1. Create a top-level Zotero collection named `Neurodegeneration`.
2. Add representative clinical and basic-research articles, with abstracts.
3. Set this in `config/custom.yaml`:

   ```yaml
   zotero:
     include_path: ["Neurodegeneration", "Neurodegeneration/**"]
   ```

Without `include_path`, all Zotero journal articles, conference papers, and
preprints containing abstracts are used as the preference corpus.

## Local run

Copy `.env.example` to `.env` and replace every placeholder. `.env` is ignored
by Git and must never be committed.

```bash
# Limited source fetch
DEBUG=true .venv/bin/python src/zotero_arxiv_daily/main.py

# Normal daily job
.venv/bin/python src/zotero_arxiv_daily/main.py
```

## GitHub Actions

Add these repository secrets:

- `ZOTERO_ID`
- `ZOTERO_KEY`
- `SENDER`
- `RECEIVER`
- `SENDER_PASSWORD`
- `OPENAI_API_KEY`
- `NCBI_API_KEY` (optional)

`SMTP_SERVER`, `SMTP_PORT`, `OPENAI_API_BASE`, and `LLM_MODEL` may be repository
variables; the committed defaults target QQ Mail and DeepSeek. The workflow now
uses the committed `config/custom.yaml` directly, avoiding a stale
`CUSTOM_CONFIG` variable overwriting the formal configuration.

The schedule is `0 0 * * *`, equivalent to 08:00 in Asia/Shanghai.

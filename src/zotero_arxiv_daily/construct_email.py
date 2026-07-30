from .protocol import Paper
import math
from html import escape


framework = """
<!DOCTYPE HTML>
<html>
<head>
  <style>
    .star-wrapper {
      font-size: 1.3em; /* 调整星星大小 */
      line-height: 1; /* 确保垂直对齐 */
      display: inline-flex;
      align-items: center; /* 保持对齐 */
    }
    .half-star {
      display: inline-block;
      width: 0.5em; /* 半颗星的宽度 */
      overflow: hidden;
      white-space: nowrap;
      vertical-align: middle;
    }
    .full-star {
      vertical-align: middle;
    }
  </style>
</head>
<body>

<div>
    __CONTENT__
</div>

<br><br>
<div style="color:#777;font-size:12px;">
正式发表文献来自 PubMed，并按公开 SJR 快照筛选；预印本未经同行评议。取消订阅请移除 GitHub Actions 中的收件地址。
</div>

</body>
</html>
"""

def get_empty_html():
  block_template = """
  <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family: Arial, sans-serif; border: 1px solid #ddd; border-radius: 8px; padding: 16px; background-color: #f9f9f9;">
  <tr>
    <td style="font-size: 20px; font-weight: bold; color: #333;">
        No Papers Today. Take a Rest!
    </td>
  </tr>
  </table>
  """
  return block_template

def get_block_html(
    title: str,
    authors: str,
    rate: str,
    tldr: str,
    pdf_url: str,
    affiliations: str = None,
    metadata: str = "",
    link_label: str = "原文",
):
    block_template = """
    <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family: Arial, sans-serif; border: 1px solid #ddd; border-radius: 8px; padding: 16px; background-color: #f9f9f9;">
    <tr>
        <td style="font-size: 20px; font-weight: bold; color: #333;">
            {title}
        </td>
    </tr>
    <tr>
        <td style="font-size: 14px; color: #666; padding: 8px 0;">
            {authors}
            <br>
            <i>{affiliations}</i>
            <br>
            <span style="color:#555;">{metadata}</span>
        </td>
    </tr>
    <tr>
        <td style="font-size: 14px; color: #333; padding: 8px 0;">
            <strong>相关性：</strong> {rate}
        </td>
    </tr>
    <tr>
        <td style="font-size: 14px; color: #333; padding: 8px 0;">
            <strong>一句话摘要：</strong> {tldr}
        </td>
    </tr>

    <tr>
        <td style="padding: 8px 0;">
            <a href="{pdf_url}" style="display: inline-block; text-decoration: none; font-size: 14px; font-weight: bold; color: #fff; background-color: #d9534f; padding: 8px 16px; border-radius: 4px;">{link_label}</a>
        </td>
    </tr>
</table>
"""
    return block_template.format(
        title=title,
        authors=authors,
        rate=rate,
        tldr=tldr,
        pdf_url=pdf_url,
        affiliations=affiliations,
        metadata=metadata,
        link_label=link_label,
    )

def get_stars(score:float):
    full_star = '<span class="full-star">⭐</span>'
    half_star = '<span class="half-star">⭐</span>'
    low = 6
    high = 8
    if score <= low:
        return ''
    elif score >= high:
        return full_star * 5
    else:
        interval = (high-low) / 10
        star_num = math.ceil((score-low) / interval)
        full_star_num = int(star_num/2)
        half_star_num = star_num - full_star_num * 2
        return '<div class="star-wrapper">'+full_star * full_star_num + half_star * half_star_num + '</div>'


def render_email(papers:list[Paper]) -> str:
    if len(papers) == 0 :
        return framework.replace('__CONTENT__', get_empty_html())

    published = [paper for paper in papers if paper.source == "pubmed"]
    preprints = [paper for paper in papers if paper.source != "pubmed"]

    def render_group(group: list[Paper]) -> list[str]:
      parts = []
      for p in group:
        #rate = get_stars(p.score)
        rate = round(p.score, 1) if p.score is not None else 'Unknown'
        author_list = [a for a in p.authors]
        num_authors = len(author_list)
        if num_authors <= 5:
            authors = ", ".join(escape(author) for author in author_list)
        else:
            authors = ", ".join(
                escape(author) for author in author_list[:3] + ["..."] + author_list[-2:]
            )
        if p.affiliations is not None:
            affiliations = p.affiliations[:5]
            affiliations = ", ".join(escape(value) for value in affiliations)
            if len(p.affiliations) > 5:
                affiliations += ", ..."
        else:
            affiliations = "作者单位未提取（Unknown Affiliation）"

        if p.source == "pubmed":
            metric = (
                f"{p.journal_metric_name} {p.journal_metric_year} "
                f"{p.journal_metric_value:.3f} · {p.journal_quartile}"
            )
            identifiers = [f"PMID {p.pmid}" if p.pmid else "", f"DOI {p.doi}" if p.doi else ""]
            metadata = " · ".join(
                value
                for value in [
                    "正式发表 · PubMed",
                    escape(p.journal or ""),
                    metric,
                    escape(p.publication_date or ""),
                    *(escape(value) for value in identifiers if value),
                ]
                if value
            )
        else:
            metadata = " · ".join(
                value
                for value in [
                    "预印本 · 未经同行评议",
                    escape(p.source),
                    escape(p.publication_date or ""),
                    f"DOI {escape(p.doi)}" if p.doi else "",
                ]
                if value
            )
        parts.append(
            get_block_html(
                escape(p.title),
                authors,
                str(rate),
                escape(p.tldr or p.abstract or "暂无摘要"),
                escape(p.pdf_url or p.url),
                affiliations,
                metadata,
            )
        )
      return parts

    sections = [
        (
            f"<h2>正式发表（PubMed，SJR ≥ 1.5）· {len(published)} 篇</h2>"
            + "<br>".join(render_group(published))
        )
        if published
        else "",
        (
            f"<h2>预印本（未经同行评议）· {len(preprints)} 篇</h2>"
            + "<br>".join(render_group(preprints))
        )
        if preprints
        else "",
    ]
    summary = (
        f"<p><strong>今日共 {len(papers)} 篇</strong>：正式发表 {len(published)} 篇，"
        f"预印本 {len(preprints)} 篇；总上限 25 篇。</p>"
    )
    content = summary + "<br>".join(section for section in sections if section)
    return framework.replace('__CONTENT__', content)

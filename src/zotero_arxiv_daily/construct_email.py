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
正式发表文献来自 PubMed，并按公开 SJR 快照筛选；SJR 是期刊影响指标，不等同于 Clarivate JIF。预印本未经同行评议。取消订阅请移除 GitHub Actions 中的收件地址。
</div>

</body>
</html>
"""

def get_empty_html():
  block_template = """
  <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family: Arial, sans-serif; border: 1px solid #ddd; border-radius: 8px; padding: 16px; background-color: #f9f9f9;">
  <tr>
    <td style="font-size: 20px; font-weight: bold; color: #333;">
        今日无新增文献
    </td>
  </tr>
  <tr>
    <td style="font-size: 14px; color: #666; padding-top: 10px;">
        今日检索已完成，符合条件的文章均已推送过。系统将在明日继续检索。
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


def _render_journal_report(
    journal_report: list[dict] | None,
    journal_alerts: list[dict] | None,
    pending_count: int,
) -> str:
    parts = []
    if journal_alerts:
        alerts = "<br>".join(
            f"⚠️ {escape(str(alert['journal']))}：连续 "
            f"{int(alert['zero_days'])} 天抓取为 0"
            for alert in journal_alerts
        )
        parts.append(
            '<div style="border:1px solid #d9534f;background:#fff3f3;'
            'padding:12px;margin:16px 0;color:#a94442;">'
            f"<strong>核心期刊抓取报警</strong><br>{alerts}</div>"
        )
    if journal_report:
        rows = []
        for row in journal_report:
            label = "核心刊" if row.get("core") else "主题刊"
            rows.append(
                "<tr>"
                f"<td style='padding:5px;border:1px solid #ddd;'>{escape(str(row['journal']))}</td>"
                f"<td style='padding:5px;border:1px solid #ddd;'>{label}</td>"
                f"<td style='padding:5px;border:1px solid #ddd;text-align:center;'>{int(row['retrieved'])}</td>"
                f"<td style='padding:5px;border:1px solid #ddd;text-align:center;'>{int(row['filtered'])}</td>"
                f"<td style='padding:5px;border:1px solid #ddd;text-align:center;'>{int(row['sent'])}</td>"
                f"<td style='padding:5px;border:1px solid #ddd;text-align:center;'>{int(row['pending'])}</td>"
                "</tr>"
            )
        parts.append(
            "<h2>目标期刊运行日报</h2>"
            "<p style='font-size:13px;color:#666;'>抓取为本次PubMed原始记录数；过滤包含文章类型、"
            "主题、去重及已发送历史；待发送队列会跨天保留。"
            f" 当前总待发送：{pending_count} 篇。</p>"
            "<table style='border-collapse:collapse;width:100%;font-size:12px;'>"
            "<tr><th style='padding:5px;border:1px solid #ddd;'>期刊</th>"
            "<th style='padding:5px;border:1px solid #ddd;'>策略</th>"
            "<th style='padding:5px;border:1px solid #ddd;'>抓取</th>"
            "<th style='padding:5px;border:1px solid #ddd;'>过滤</th>"
            "<th style='padding:5px;border:1px solid #ddd;'>发送</th>"
            "<th style='padding:5px;border:1px solid #ddd;'>待发送</th></tr>"
            + "".join(rows)
            + "</table>"
        )
    return "<br>".join(parts)


def render_email(
    papers: list[Paper],
    journal_report: list[dict] | None = None,
    journal_alerts: list[dict] | None = None,
    pending_count: int = 0,
    digest_name: str = "神经退行性疾病文献推送",
    priority_topic_label: str | None = "亨廷顿病",
    max_paper_num: int = 30,
) -> str:

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
                f"期刊影响指标：{p.journal_metric_name} {p.journal_metric_year} "
                f"{p.journal_metric_value:.3f} · {p.journal_quartile}"
                if p.journal_metric_value is not None
                else "期刊影响指标：SJR未收录"
            )
            special_topic = (
                f"{priority_topic_label}专题 · 不受期刊SJR阈值限制"
                if p.special_topic and priority_topic_label
                else ""
            )
            identifiers = [f"PMID {p.pmid}" if p.pmid else "", f"DOI {p.doi}" if p.doi else ""]
            metadata = " · ".join(
                value
                for value in [
                    "正式发表 · PubMed",
                    special_topic,
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

    published_strategy = "目标期刊筛选"
    if priority_topic_label:
        published_strategy = f"顶刊筛选 + {priority_topic_label}专题全覆盖"
    sections = [
        (
            f"<h2>正式发表（PubMed：{published_strategy}）· {len(published)} 篇</h2>"
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
    if papers:
        quota_note = f"每日文献上限{max_paper_num}篇"
        if priority_topic_label:
            quota_note += f"，{priority_topic_label}专题另行全量保留"
        summary = (
            f"<p><strong>今日共 {len(papers)} 篇</strong>：正式发表 {len(published)} 篇，"
            f"预印本 {len(preprints)} 篇；{quota_note}。"
            f" 跨日待发送 {pending_count} 篇。</p>"
        )
        content = summary + "<br>".join(section for section in sections if section)
    else:
        content = get_empty_html()
    report = _render_journal_report(journal_report, journal_alerts, pending_count)
    if report:
        content += "<br>" + report
    content = f"<h1>{escape(digest_name)}</h1>" + content
    return framework.replace('__CONTENT__', content)

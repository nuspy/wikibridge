"""Conversione HTML → markdown.

Strategia a due livelli:
1. trafilatura: estrae il contenuto principale (toglie nav, footer, ads)
   con output markdown — ottimo per pagine "rumorose".
2. fallback markdownify sull'intero <body> quando trafilatura non trova
   abbastanza contenuto (pagine molto corte o layout inusuali).

extract_links() raccoglie i link <a href> assoluti per la BFS del crawler.
"""
from __future__ import annotations

from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup
from markdownify import markdownify

MIN_EXTRACT_CHARS = 200


def html_to_markdown(html: str, url: str = "") -> tuple[str, str]:
    """Ritorna (markdown, titolo)."""
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(strip=True) if soup.title else "") or url

    md = ""
    try:
        import trafilatura
        md = trafilatura.extract(
            html, url=url or None, output_format="markdown",
            include_links=True, include_tables=True,
            include_images=False, favor_recall=True) or ""
    except Exception:  # noqa: BLE001 — qualsiasi problema → fallback
        md = ""

    if len(md.strip()) < MIN_EXTRACT_CHARS:
        body = soup.body or soup
        for tag in body.select("script, style, nav, footer, header, aside"):
            tag.decompose()
        md = markdownify(str(body), heading_style="ATX", strip=["img"])

    return md.strip(), title


def extract_links(html: str, base_url: str) -> list[str]:
    """Link assoluti, senza frammenti, dedup nell'ordine di apparizione."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urldefrag(urljoin(base_url, href)).url
        if absolute.startswith("http") and absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out

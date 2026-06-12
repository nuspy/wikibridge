"""Adapter Notion (API ufficiale, integration token).

- detect: host notion.so/notion.site (serve un profilo bearer col token
  dell'integrazione, condivisa con le pagine da importare)
- fetch: GET /v1/blocks/<id>/children ricorsivo → renderer block→markdown
  proprio (paragrafi, heading, liste, todo, code, quote, callout, tabelle);
  i blocchi child_page diventano link/figli per la BFS
- revisione = last_edited_time della pagina
- rate limit Notion ≈ 3 rps: gestito dal ThrottledSession + Retry-After
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from .base import PageContent, PageRef, SourceAdapter

log = logging.getLogger("wikibridge.notion")

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
_ID_RE = re.compile(r"([0-9a-f]{32})|([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
MAX_BLOCK_DEPTH = 8


def _dash_id(raw: str) -> str:
    s = raw.replace("-", "")
    return f"{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}"


def _rich_text(rt: list[dict]) -> str:
    out = []
    for t in rt or []:
        txt = t.get("plain_text", "")
        ann = t.get("annotations", {})
        if ann.get("code"):
            txt = f"`{txt}`"
        if ann.get("bold"):
            txt = f"**{txt}**"
        if ann.get("italic"):
            txt = f"*{txt}*"
        if ann.get("strikethrough"):
            txt = f"~~{txt}~~"
        href = t.get("href")
        if href:
            txt = f"[{txt}]({href})"
        out.append(txt)
    return "".join(out)


class NotionAdapter(SourceAdapter):
    name = "notion"

    @classmethod
    def detect(cls, url: str, session) -> bool:
        host = urlparse(url).netloc.lower()
        return "notion.so" in host or "notion.site" in host

    def _headers(self) -> None:
        # il bearer arriva dal profilo auth; qui solo la versione API
        self.session.headers.setdefault("Notion-Version", VERSION)

    def _get(self, path: str, **params) -> dict:
        self._headers()
        r = self.session.get(f"{API}{path}", params=params)
        if r.status_code == 401:
            raise PermissionError(
                "Notion: token mancante o non valido (serve un profilo auth "
                "bearer con il token dell'integrazione, e la pagina deve "
                "essere condivisa con l'integrazione)")
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _page_id_from_url(url: str) -> str:
        m = _ID_RE.search(url)
        if not m:
            # gli URL notion hanno l'id (32 hex) in coda allo slug
            tail = urlparse(url).path.rsplit("-", 1)[-1].rsplit("/", 1)[-1]
            if re.fullmatch(r"[0-9a-f]{32}", tail):
                return _dash_id(tail)
            raise ValueError(f"id pagina Notion non ricavabile: {url}")
        return _dash_id(m.group(0))

    # -- rendering blocchi -------------------------------------------------------

    def _render_blocks(self, block_id: str, depth: int,
                       indent: str = "") -> tuple[list[str], list[PageRef]]:
        """Ritorna (righe markdown, child_pages)."""
        if depth > MAX_BLOCK_DEPTH:
            return [], []
        lines: list[str] = []
        children: list[PageRef] = []
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self._get(f"/blocks/{block_id}/children", **params)
            for b in data.get("results", []):
                t = b.get("type")
                payload = b.get(t, {})
                txt = _rich_text(payload.get("rich_text", []))
                if t == "paragraph":
                    lines += [f"{indent}{txt}", ""]
                elif t in ("heading_1", "heading_2", "heading_3"):
                    lines += [f"{'#' * int(t[-1])} {txt}", ""]
                elif t == "bulleted_list_item":
                    lines.append(f"{indent}- {txt}")
                elif t == "numbered_list_item":
                    lines.append(f"{indent}1. {txt}")
                elif t == "to_do":
                    box = "x" if payload.get("checked") else " "
                    lines.append(f"{indent}- [{box}] {txt}")
                elif t == "code":
                    lang = payload.get("language", "")
                    lines += [f"```{lang}", txt, "```", ""]
                elif t == "quote":
                    lines += [f"> {txt}", ""]
                elif t == "callout":
                    lines += [f"> 💡 {txt}", ""]
                elif t == "divider":
                    lines += ["---", ""]
                elif t == "table":
                    rows, _ = self._render_table(b["id"])
                    lines += rows + [""]
                elif t == "child_page":
                    title = payload.get("title", "")
                    children.append(PageRef(
                        id=b["id"], url=f"https://www.notion.so/{b['id'].replace('-', '')}",
                        title=title, depth=0))
                    lines.append(f"{indent}- 📄 {title}")
                elif t == "child_database":
                    lines.append(f"{indent}- 🗃 {payload.get('title', 'database')}")
                # blocchi con figli annidati (toggle, liste, ecc.)
                if b.get("has_children") and t not in ("child_page", "child_database", "table"):
                    sub, sub_children = self._render_blocks(
                        b["id"], depth + 1, indent + "  ")
                    lines += sub
                    children += sub_children
            cursor = data.get("next_cursor")
            if not data.get("has_more"):
                break
        return lines, children

    def _render_table(self, table_id: str) -> tuple[list[str], None]:
        rows: list[str] = []
        data = self._get(f"/blocks/{table_id}/children", page_size=100)
        for i, row in enumerate(data.get("results", [])):
            cells = row.get("table_row", {}).get("cells", [])
            rendered = [_rich_text(c) for c in cells]
            rows.append("| " + " | ".join(rendered) + " |")
            if i == 0:
                rows.append("|" + "---|" * len(rendered))
        return rows, None

    # -- contratto --------------------------------------------------------------

    def resolve(self, url: str, scope: str) -> list[PageRef]:
        pid = self._page_id_from_url(url)
        return [PageRef(id=pid, url=url, depth=0)]
        # subtree/space: i child_page emergono dai links durante la BFS
        # (depth fa da estensione); per Notion scope e depth coincidono.

    def fetch(self, ref: PageRef) -> PageContent:
        page = self._get(f"/pages/{ref.id}")
        title = ""
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                title = _rich_text(prop.get("title", []))
                break
        lines, children = self._render_blocks(ref.id, 0)
        md = "\n".join([f"# {title}", ""] + lines).strip()
        if len(md.encode()) > self.limits.max_page_mb * 1024 * 1024:
            raise ValueError(f"pagina oltre {self.limits.max_page_mb} MB")
        ref.title = title or ref.title
        for c in children:
            c.depth = ref.depth + 1
        return PageContent(ref=ref, markdown=md, links=children,
                           revision=page.get("last_edited_time"))

    def changed_since(self, ref: PageRef, revision: str | None) -> bool:
        if not revision:
            return True
        try:
            page = self._get(f"/pages/{ref.id}")
            return page.get("last_edited_time") != revision
        except Exception:  # noqa: BLE001
            return True

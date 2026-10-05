"""Bounded, dependency-free extraction of useful text from HTML pages."""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin
import hashlib
import re

from friday.research.models import ExtractedPage, FetchResult


_IGNORED_TAGS = {"script", "style", "noscript", "svg", "canvas", "iframe", "form", "button"}
_IGNORED_HINTS = re.compile(r"(?:^|[-_ ])(?:nav|menu|advert|ads?|banner|cookie|consent|breadcrumb|social|share|footer|sidebar)(?:$|[-_ ])", re.I)
_DATE_KEYS = {"article:published_time", "datepublished", "date", "pubdate", "parsely-pub-date", "og:published_time"}


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.body_parts: list[str] = []
        self.main_parts: list[str] = []
        self.headings: list[str] = []
        self.metadata: dict[str, str] = {}
        self.ignored_depth = 0
        self.main_depth = 0
        self.title_depth = 0
        self.head_depth = 0
        self.heading_depth = 0
        self.heading_parts: list[str] = []
        self._tag_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {key.casefold(): value or "" for key, value in attrs}
        if self.ignored_depth:
            if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
                self.ignored_depth += 1
            return
        hint = " ".join((attrs_map.get("class", ""), attrs_map.get("id", ""), attrs_map.get("role", "")))
        if tag in _IGNORED_TAGS or attrs_map.get("aria-hidden", "").casefold() == "true" or _IGNORED_HINTS.search(hint):
            self.ignored_depth = 1
            self._tag_stack.append(tag)
            return
        self._tag_stack.append(tag)
        if tag == "head":
            self.head_depth += 1
        if tag == "title":
            self.title_depth += 1
        if tag in {"main", "article"} or attrs_map.get("role", "").casefold() == "main":
            self.main_depth += 1
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.heading_depth += 1
            self.heading_parts = []
        if tag == "meta":
            key = attrs_map.get("property", "").casefold() or attrs_map.get("name", "").casefold()
            content = _clean(attrs_map.get("content", ""))
            if key and content:
                self.metadata.setdefault(key, content)
        elif tag == "link" and "canonical" in attrs_map.get("rel", "").casefold().split():
            self.metadata.setdefault("canonical", attrs_map.get("href", ""))
        elif tag == "time" and attrs_map.get("datetime"):
            self.metadata.setdefault("datetime", attrs_map["datetime"])

    def handle_endtag(self, tag: str) -> None:
        if self.ignored_depth:
            self.ignored_depth -= 1
            if self._tag_stack:
                self._tag_stack.pop()
            return
        if tag == "title" and self.title_depth:
            self.title_depth -= 1
        if tag == "head" and self.head_depth:
            self.head_depth -= 1
        if tag in {"main", "article"} and self.main_depth:
            self.main_depth -= 1
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self.heading_depth:
            heading = _clean(" ".join(self.heading_parts))
            if heading:
                self.headings.append(heading[:300])
            self.heading_depth -= 1
            self.heading_parts = []
        if self._tag_stack:
            # Malformed pages can close an ancestor early; unwind through that tag.
            try:
                index = len(self._tag_stack) - 1 - self._tag_stack[::-1].index(tag)
                del self._tag_stack[index:]
            except ValueError:
                pass

    def handle_data(self, data: str) -> None:
        if self.ignored_depth or not data.strip():
            return
        if self.title_depth:
            self.title_parts.append(data)
        if self.head_depth:
            return
        self.body_parts.append(data)
        if self.main_depth:
            self.main_parts.append(data)
        if self.heading_depth:
            self.heading_parts.append(data)


def extract_page(result: FetchResult, *, max_text_chars: int = 100_000) -> ExtractedPage:
    """Extract title, headings, main text, dates, author, and canonical URL."""
    content_type = (result.content_type or "").casefold()
    if content_type.startswith("text/plain"):
        charset_match = re.search(r"charset\s*=\s*['\"]?([^;\s'\"]+)", content_type)
        charset = charset_match.group(1) if charset_match else "utf-8"
        try:
            text = result.body.decode(charset, errors="replace")
        except LookupError:
            text = result.body.decode("utf-8", errors="replace")
        title = ""
        headings: tuple[str, ...] = ()
        author = published = None
        canonical = result.final_url
    elif content_type.startswith(("text/html", "application/xhtml+xml")):
        charset_match = re.search(r"charset\s*=\s*['\"]?([^;\s'\"]+)", content_type)
        charset = charset_match.group(1) if charset_match else "utf-8"
        try:
            html = result.body.decode(charset, errors="replace")
        except LookupError:
            html = result.body.decode("utf-8", errors="replace")
        parser = _ArticleParser()
        parser.feed(html)
        parser.close()
        title = _clean(" ".join(parser.title_parts))[:500]
        text = _clean(" ".join(parser.main_parts or parser.body_parts))[:max_text_chars]
        headings = tuple(parser.headings[:100])
        metadata = parser.metadata
        author = metadata.get("author") or metadata.get("article:author") or None
        published = next((metadata[key] for key in _DATE_KEYS if metadata.get(key)), None)
        published = published or metadata.get("datetime") or None
        canonical = urljoin(result.final_url, metadata.get("canonical", "")) if metadata.get("canonical") else result.final_url
    else:
        raise ValueError("Only HTML, XHTML, and plain text pages can be extracted.")
    normalized = _clean(text)
    if not normalized:
        raise ValueError("The page did not contain extractable text.")
    if not title:
        title = headings[0] if headings else result.final_url
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return ExtractedPage(
        result.final_url, canonical, title, headings, author, published,
        normalized, digest,
    )

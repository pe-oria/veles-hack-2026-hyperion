"""Download the HyperAI IDE tutorial pages into knowledge/ as markdown-ish text.

Run once (needs internet):  uv run python scripts/fetch_docs.py
These pages are the most useful RAG source: DSL spec (native + device apps),
cookbook YAML examples, quick start, and the Hyperion action protocol.
"""

from html.parser import HTMLParser
from pathlib import Path

import httpx

BASE = "https://ide-tutorial.hyperai.di.uoa.gr"
PAGES = {
    "tutorial_intro": "/",
    "tutorial_quick_start": "/quick-start-demo/",
    "tutorial_ide_guide": "/ide-guide/",
    "dsl_native_apps": "/dsl/native-apps/",
    "dsl_device_apps": "/dsl/devices/",
    "cookbook_examples": "/cookbook/",
    "hyperion_actions": "/hyperion-agent/",
}
OUT = Path(__file__).resolve().parent.parent / "knowledge"

BLOCK = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "table", "ul", "ol", "br"}


class ArticleText(HTMLParser):
    """Extract text from <article>, keeping headings, table cells and code blocks."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0  # >0 while inside <article>
        self.in_pre = False
        self.out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "article":
            self.depth += 1
        if not self.depth:
            return
        if tag == "pre":
            self.in_pre = True
            self.out.append("\n```\n")
        elif tag in ("h1", "h2", "h3", "h4"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if not self.depth:
            return
        if tag == "pre":
            self.in_pre = False
            self.out.append("\n```\n")
        elif tag in BLOCK:
            self.out.append("\n")
        if tag == "article":
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.out.append(data if self.in_pre else data.replace("¶", ""))

    def text(self) -> str:
        raw = "".join(self.out)
        lines = [ln.rstrip() for ln in raw.splitlines()]
        cleaned, blank = [], 0
        for ln in lines:
            blank = blank + 1 if not ln.strip() else 0
            if blank <= 1:
                cleaned.append(ln)
        return "\n".join(cleaned).strip()


def main() -> None:
    OUT.mkdir(exist_ok=True)
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        for name, path in PAGES.items():
            html = client.get(BASE + path).raise_for_status().text
            parser = ArticleText()
            parser.feed(html)
            body = parser.text()
            (OUT / f"{name}.md").write_text(
                f"---\nsource: {BASE}{path}\ntitle: {name}\n---\n\n{body}\n", encoding="utf-8"
            )
            print(f"saved {name}.md ({len(body)} chars)")


if __name__ == "__main__":
    main()

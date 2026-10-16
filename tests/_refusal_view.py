"""Read a refused write as a browser shows it, for the tests of its page and of its box.

`render_refused_write` gives HTML. A test that searched the text for a name would pass while the
name sat in a tooltip, in the closed part of the expander, or in a tag that a value smuggled in.
This parser reads the markup as a browser renders it instead: character references become text,
each `<details>` is one refused write with its summary line and what its expander holds, and every
tag is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional


@dataclass
class Cell:
    """A table cell as a browser shows it: its text, and the tooltip of the term in it."""

    text: str = ""
    title: Optional[str] = None


@dataclass
class Shown:
    """One refused write as a browser shows it: the summary line, which is always visible, and
    what its expander holds: the results of the table, field by field, the notes, and the
    preformatted report."""

    id: str
    preserved: bool
    summary: str = ""
    results: list[dict[str, Cell]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    report: str = ""


class BrowserView(HTMLParser):
    """Reads HTML as a browser renders it: character references become text, and every tag that
    the markup holds is recorded, so a value that smuggled markup in shows up as a tag."""

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.refused: list[Shown] = []
        self.text = ""
        self._open: list[str] = []
        self._row: list[Cell] = []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self.tags.append(tag)
        a = dict(attrs)
        if tag == "details":
            self.refused.append(Shown(id=a.get("id") or "", preserved="hx-preserve" in a))
        elif tag == "tr":
            self._row = []
        elif tag in ("th", "td"):
            self._row.append(Cell())
        elif tag == "span" and self._row and "td" in self._open:
            self._row[-1].title = a.get("title")
        elif tag == "br" and self._row:
            self._row[-1].text += "\n"
        elif tag == "p" and "details" in self._open:
            self.refused[-1].notes.append("")
        if tag not in ("br", "input", "meta"):
            self._open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._open:
            while self._open.pop() != tag:
                pass
        if tag == "tr" and self.refused:
            shown = self.refused[-1]
            if len(self._row) == 1:
                shown.results.append({})
            elif len(self._row) == 2:
                shown.results[-1][self._row[0].text] = self._row[1]

    def handle_data(self, data: str) -> None:
        self.text += data
        if not self.refused or "details" not in self._open:
            return
        shown = self.refused[-1]
        if "summary" in self._open:
            shown.summary += data
        elif "pre" in self._open:
            shown.report += data
        elif self._open and self._open[-1] in ("th", "td", "span") and self._row:
            self._row[-1].text += data
        elif "p" in self._open:
            shown.notes[-1] += data

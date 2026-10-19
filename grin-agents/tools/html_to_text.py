"""
SEC filing HTML → plain text for `grinos ingest`, keeping each table row on one line:

    Long-term debt, net | 1,327,273 | 1,212,533

so a claim can quote a label with its value. EDGAR splits "$" and ")" into their own cells;
they are rejoined ("$ 267,679", "(36,569)"). Stdlib only.

    python tools/html_to_text.py filing.htm out.txt
"""
import re
import sys
from html.parser import HTMLParser

BLOCK = {"p", "div", "br", "h1", "h2", "h3", "h4", "li", "table"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.buf: list[str] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.skip = 0

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        if text:
            self.lines.append(text)
        self.buf = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag == "tr":
            self._flush()
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag in BLOCK and self.cell is None:
            self._flush()

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1
        elif tag in ("td", "th") and self.cell is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            line = join_row(self.row)
            if line:
                self.lines.append(line)
            self.row = None
        elif tag in BLOCK and self.cell is None:
            self._flush()

    def handle_data(self, data):
        if self.skip:
            return
        (self.cell if self.cell is not None else self.buf).append(data)


def join_row(cells: list[str]) -> str:
    out: list[str] = []
    pending = ""
    for c in cells:
        if not c:
            continue
        if c in ("$", "("):
            pending += c
            continue
        if out and c in (")", "%", ")%"):
            out[-1] += c
            continue
        out.append((pending + " " + c if pending == "$" else pending + c) if pending else c)
        pending = ""
    return " | ".join(out)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    p._flush()
    return "\n".join(p.lines).replace("\xa0", " ") + "\n"


if __name__ == "__main__":
    src, dst = sys.argv[1:3]
    with open(src, encoding="utf-8", errors="replace") as f, open(dst, "w", encoding="utf-8") as g:
        g.write(html_to_text(f.read()))

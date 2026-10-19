import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from html_to_text import html_to_text  # noqa: E402

HTML = """<p>(In thousands)</p><table>
<tr><td><p>Operating income</p></td><td>$</td><td>267,679</td><td></td><td>$</td><td>272,700</td></tr>
<tr><td>Purchases of property</td><td>(36,569</td><td>)</td><td>(27,390</td><td>)</td></tr>
<tr><td>Gross margin</td><td>29.7</td><td>%</td></tr></table><p>Net sales rose&nbsp;2%.</p>"""


def test_rows_stay_on_one_line_with_split_cells_rejoined():
    lines = html_to_text(HTML).splitlines()
    assert lines == ["(In thousands)", "Operating income | $ 267,679 | $ 272,700",
                     "Purchases of property | (36,569) | (27,390)", "Gross margin | 29.7%", "Net sales rose 2%."]

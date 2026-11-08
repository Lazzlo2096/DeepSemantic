import re
from html import escape
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter


def article_body(source):
    article = BeautifulSoup(source, "html.parser").find("article", class_="ltx_document")
    if article is None or article.find("h1") is None:
        raise ValueError("HTML has no arXiv article body")
    return article


class ArticleConverter(MarkdownConverter):
    def __init__(self, url: str):
        super().__init__(heading_style="ATX")
        self.url = url

    def convert_math(self, el, text, parent_tags):
        annotation = el.select_one('annotation[encoding="application/x-tex"]')
        latex = (annotation.get_text() if annotation else el.get("alttext", "")).strip()
        if not latex:
            return text
        if el.get("display") == "block":
            return f"\n\n$$\n{latex}\n$$\n\n"
        return f"${latex}$"

    def convert_table(self, el, text, parent_tags):
        if "ltx_equation" in el.get("class", []):
            formulas = el.find_all("math")
            if formulas:
                label = el.select_one(".ltx_tag_equation")
                number = f"{label.get_text(strip=True)}\n\n" if label else ""
                return " ".join(self.convert_soup(math) for math in formulas) + number
            return text

        # Keep merged cells and strip presentation attributes from the table.
        table = BeautifulSoup(str(el), "html.parser").table
        for link in table.find_all("a"):
            link.unwrap()
        for cell in table.find_all(["th", "td"]):
            content = self.convert(cell.decode_contents()).strip()
            cell.clear()
            cell.append(content)
        for node in [table, *table.find_all(True)]:
            node.attrs = {key: value for key, value in node.attrs.items() if key in {"rowspan", "colspan"}}

        figure = el.find_parent("figure")
        caption = figure.find("figcaption") if figure else None
        if caption is None:
            return f"\n\n{table}\n\n"
        caption_html = BeautifulSoup(caption.decode_contents(), "html.parser")
        for marker in caption_html.select(".ltx_note_mark, .ltx_tag_note"):
            marker.decompose()
        notes = [self.convert(note.decode_contents()).strip() for note in caption_html.select(".ltx_note_content")]
        for note in caption_html.select(".ltx_note"):
            note.decompose()
        for link in caption_html.find_all("a"):
            link.unwrap()
        label = self.convert_soup(caption_html).strip()
        body = f"<table><caption>{escape(label)}</caption>{table.decode_contents()}</table>"
        return "\n\n" + "\n\n".join([body, *notes]) + "\n\n"

    def convert_svg(self, el, text, parent_tags):
        return ""

    def convert_object(self, el, text, parent_tags):
        if el.get("type") == "image/svg+xml" and el.get("data"):
            return f"\n\n![SVG]({urljoin(self.url, el['data'])})\n\n"
        return text

    def convert_figure(self, el, text, parent_tags):
        if el.find("svg") and el.get("id"):
            return f"\n\n[Figure]({self.url}#{el['id']})\n\n{text.strip()}\n\n"
        return f"\n\n{text.strip()}\n\n"

    def convert_figcaption(self, el, text, parent_tags):
        if el.find_parent("figure", class_="ltx_table"):
            return ""
        return f"\n\n{text.strip()}\n\n"


def extract_markdown(source, url: str) -> str:
    article = article_body(source)
    for node in article.select("script, style, nav, button, template"):
        node.decompose()
    for img in article.select("img[src]"):
        img["src"] = urljoin(url, img["src"])
    for link in article.select("a[href]"):
        link["href"] = urljoin(url, link["href"])
    markdown = ArticleConverter(url).convert_soup(article).strip()
    if not markdown:
        raise ValueError("HTML has no extractable text")
    return markdown + "\n"


BLOCK = re.compile(r"(^\$\$[ \t]*\n.*?^\$\$[ \t]*$|^<table\b.*?</table>)", re.MULTILINE | re.DOTALL)
TABLE_CHUNK_SIZE = 700  # HTML tags and LaTeX use more tokens per character than prose.


def table_parts(block: str, size: int):
    table = BeautifulSoup(block, "html.parser").table
    if table.select_one("[rowspan]"):
        yield block
        return
    prefix = "<table>"
    for tag in table.find_all(["caption", "thead"], recursive=False):
        prefix += str(tag)
    rows = [row for row in table.find_all("tr") if row.find_parent("thead") is None]
    if table.thead is None and rows:
        header_count = 2 if rows[0].select_one("[colspan]") else 1
        prefix += "<thead>" + "".join(str(row) for row in rows[:header_count]) + "</thead>"
        rows = rows[header_count:]
    if not rows:
        yield block
        return
    part = prefix
    for row in rows:
        row_html = str(row)
        if part != prefix and len(part) + len(row_html) + len("</table>") > size:
            yield part + "</table>"
            part = prefix
        part += row_html
    if part != prefix:
        yield part + "</table>"


def split_paragraph(block: str, size: int):
    start = 0
    boundary = 0
    for match in re.finditer(r"\$[^$\n]+\$|!?\[[^\]\n]*\]\([^\n]+?\)|\s+", block):
        if not match[0].isspace():
            continue
        if boundary > start and match.start() - start > size:
            yield block[start:boundary].rstrip()
            start = boundary
        boundary = match.end()
    if boundary > start and len(block) - start > size:
        yield block[start:boundary].rstrip()
        start = boundary
    if start < len(block):
        yield block[start:]


def split_markdown(markdown: str, size: int):
    chunk = ""
    for index, part in enumerate(BLOCK.split(markdown)):
        blocks = [part] if index % 2 else re.split(r"\n[ \t]*\n", part)
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            if chunk and (block.startswith(("#", "<table>")) or len(chunk) + 2 + len(block) > size):
                yield chunk
                chunk = ""
            if block.startswith("<table>"):
                block = re.sub(r">\s+<", "><", block)
                if len(block) > min(size, TABLE_CHUNK_SIZE):
                    yield from table_parts(block, min(size, TABLE_CHUNK_SIZE))
                else:
                    yield block
            elif not index % 2 and len(block) > size:
                yield from split_paragraph(block, size)
            else:
                chunk = f"{chunk}\n\n{block}" if chunk else block
    if chunk:
        yield chunk

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

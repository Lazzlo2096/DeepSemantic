import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from bs4 import BeautifulSoup
from tokenizers import Tokenizer


CHUNK_BYTES = 1000
MARKDOWN_BLOCK = re.compile(
    r"(^\$\$[ \t]*\n.*?^\$\$[ \t]*$|<table(?=[\s>]).*?</table>)", re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n|\n(?=#{1,6}[ \t])")
WORD_BREAK = re.compile(r"\s+")
NORMALIZE = re.compile(r"\r\x00*\n|\x00")


@dataclass(frozen=True)
class PaperFragment:
    text: str
    source_start: int
    source_end: int


@dataclass(frozen=True)
class SourceText:
    text: str
    starts: Sequence[int]
    ends: Sequence[int]

    @classmethod
    def literal(cls, text: str, start: int):
        return cls(text, range(start, start + len(text)), range(start + 1, start + len(text) + 1))

    @classmethod
    def transformed(cls, text: str, start: int, end: int):
        return cls(text, (start,) * len(text), (end,) * len(text))

    def slice(self, start: int, end: int | None = None):
        return SourceText(self.text[start:end], self.starts[start:end], self.ends[start:end])

    def strip(self):
        return self.slice(len(self.text) - len(self.text.lstrip()), len(self.text.rstrip()))


def join_text(parts: Sequence[SourceText], separator: str) -> SourceText:
    texts = []
    starts = []
    ends = []
    for part in parts:
        if not part.text:
            continue
        if texts:
            starts.extend([starts[-1]] * len(separator))
            ends.extend([ends[-1]] * len(separator))
        texts.append(part.text)
        starts.extend(part.starts)
        ends.extend(part.ends)
    return SourceText(separator.join(texts), starts, ends)


def normalize_source(document: str) -> SourceText:
    if not NORMALIZE.search(document):
        return SourceText.literal(document, 0)
    parts = []
    start = 0
    for match in NORMALIZE.finditer(document):
        parts.append(SourceText.literal(document[start:match.start()], start))
        if match[0].startswith("\r"):
            parts.append(SourceText.transformed("\n", match.start(), match.end()))
        start = match.end()
    parts.append(SourceText.literal(document[start:], start))
    return join_text(parts, "")


def split_boundary(prefix: str) -> int:
    minimum = len(prefix) // 2
    line_end = prefix.rfind("\n", minimum)
    if line_end >= 0:
        return line_end
    return max((match.start() for match in WORD_BREAK.finditer(prefix, minimum)), default=len(prefix))


def split_block(block: SourceText) -> Iterator[SourceText]:
    # Bound dense Unicode and markup as well as prose. Prefer row, line and word boundaries.
    start = 0
    while start < len(block.text):
        prefix = block.text[start:start + CHUNK_BYTES].encode("utf-8")[:CHUNK_BYTES].decode("utf-8", errors="ignore")
        if start + len(prefix) == len(block.text):
            yield block.slice(start)
            return
        end = split_boundary(prefix)
        yield block.slice(start, start + len(prefix[:end].rstrip()))
        start += end
        whitespace = WORD_BREAK.match(block.text, start)
        if whitespace:
            start = whitespace.end()


def cell_span(cell, attribute: str) -> int:
    value = cell.get(attribute, "1")
    return min(1000, max(1, int(value[:4]))) if value.isdecimal() else 1


def table_to_markdown(block: SourceText) -> SourceText:
    table = BeautifulSoup(block.text, "html.parser").table
    caption = table.find("caption", recursive=False)
    line_starts = [0, *(match.end() for match in re.finditer("\n", block.text))]

    def tag_start(tag):
        return line_starts[tag.sourceline - 1] + tag.sourcepos

    def tag_range(tag, start, limit):
        closing = re.compile(rf"</{tag.name}\s*>", re.IGNORECASE).search(block.text, start, limit)
        end = closing.end() if closing else limit
        return block.starts[start], block.ends[end - 1]

    row_tags = table.find_all("tr")
    row_starts = [*(tag_start(tag) for tag in row_tags), len(block.text)]
    rows = []
    ranges = []
    headers = 0
    spans = {}
    for index, row in enumerate(row_tags):
        cells = row.find_all(["th", "td"], recursive=False)
        if headers == index and (row.parent.name == "thead" or (cells and all(c.name == "th" for c in cells))):
            headers += 1
        spans = {column: span for column, span in spans.items() if span[1] > index}
        values = {column: span[0] for column, span in spans.items()}
        start, end = tag_range(row, row_starts[index], row_starts[index + 1])
        ranges.append((min((start, *(span[2] for span in spans.values()))), end))
        column = 0
        for cell in cells:
            value = re.sub(r"(?<!\\)\|", r"\\|", " ".join(cell.get_text(" ", strip=True).split()))
            width = cell_span(cell, "colspan")
            height = cell_span(cell, "rowspan")
            while any(position in values for position in range(column, column + width)):
                column += 1
            for position in range(column, column + width):
                values[position] = value
                if height > 1:
                    spans[position] = (value, index + height, start)
            column += width
        rows.append(values)

    width = max((max(row, default=-1) + 1 for row in rows), default=0)
    if not width:
        return SourceText.transformed(table.get_text(" ", strip=True), block.starts[0], block.ends[-1])

    def format_row(values):
        return "| " + " | ".join(values) + " |"

    columns = []
    for column in range(width):
        labels = dict.fromkeys(row[column] for row in rows[:headers] if row.get(column))
        columns.append(" / ".join(labels))
    header_range = (ranges[0][0], ranges[max(headers - 1, 0)][1])
    lines = [
        SourceText.transformed(format_row(columns), *header_range),
        SourceText.transformed(format_row(["---"] * width), *header_range),
    ]
    for row, origin in zip(rows[headers:], ranges[headers:], strict=True):
        lines.append(SourceText.transformed(format_row(row.get(column, "") for column in range(width)), *origin))
    markdown = join_text(lines, "\n")
    if caption:
        title = SourceText.transformed(caption.get_text(" ", strip=True), *tag_range(caption, tag_start(caption), len(block.text)))
        return join_text([title, markdown], "\n\n")
    return markdown


def paragraphs(document: SourceText, start: int, end: int) -> Iterator[SourceText]:
    for match in PARAGRAPH_BREAK.finditer(document.text, start, end):
        yield document.slice(start, match.start())
        start = match.end()
    yield document.slice(start, end)


def markdown_blocks(document: SourceText) -> Iterator[SourceText]:
    start = 0
    for match in MARKDOWN_BLOCK.finditer(document.text):
        yield from paragraphs(document, start, match.start())
        yield document.slice(match.start(), match.end())
        start = match.end()
    yield from paragraphs(document, start, len(document.text))


def split_markdown(document: SourceText) -> Iterator[SourceText]:
    chunk = None
    for block in markdown_blocks(document):
        block = block.strip()
        if not block.text:
            continue
        is_table = block.text.lower().startswith("<table") and MARKDOWN_BLOCK.fullmatch(block.text)
        block_size = len(block.text.encode("utf-8"))
        if chunk is not None and (
            is_table or block.text.startswith("#") or len(chunk.text.encode("utf-8")) + 2 + block_size > CHUNK_BYTES
        ):
            yield chunk
            chunk = None
        if is_table:
            yield from split_block(table_to_markdown(block))
        elif block_size > CHUNK_BYTES:
            yield from split_block(block)
        else:
            chunk = join_text([chunk, block], "\n\n") if chunk is not None else block
    if chunk is not None:
        yield chunk


def fit_token_limit(block: SourceText, tokenizer: Tokenizer, max_tokens: int) -> Iterator[SourceText]:
    while block.text:
        encoding = tokenizer.encode(block.text)
        if len(encoding.ids) <= max_tokens:
            yield block
            return
        if len(block.text) == 1:
            raise ValueError("One character exceeds the embedding token limit")
        # Leave room for the end token. Retokenize both sides because their boundaries change.
        end = encoding.offsets[max_tokens - 1][0]
        if not 0 < end < len(block.text):
            end = len(block.text) // 2
        end = split_boundary(block.text[:end])
        yield from fit_token_limit(block.slice(0, end).strip(), tokenizer, max_tokens)
        block = block.slice(end).strip()


def split_document(document: str, tokenizer: Tokenizer, max_tokens: int) -> Iterator[PaperFragment]:
    for block in split_markdown(normalize_source(document)):
        for fragment in fit_token_limit(block, tokenizer, max_tokens):
            yield PaperFragment(fragment.text, min(fragment.starts), max(fragment.ends))

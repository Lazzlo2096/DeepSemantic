import re
from collections.abc import Iterator

from bs4 import BeautifulSoup
from tokenizers import Tokenizer


CHUNK_BYTES = 1000
MARKDOWN_BLOCK = re.compile(
    r"(^\$\$[ \t]*\n.*?^\$\$[ \t]*$|<table\b.*?</table>)", re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n|\n(?=#{1,6}[ \t])")
WORD_BREAK = re.compile(r"\s+")


def split_boundary(prefix: str) -> int:
    minimum = len(prefix) // 2
    line_end = prefix.rfind("\n", minimum)
    if line_end >= 0:
        return line_end
    return max((match.start() for match in WORD_BREAK.finditer(prefix, minimum)), default=len(prefix))


def split_block(block: str) -> Iterator[str]:
    # Bound dense Unicode and markup as well as prose. Prefer row, line and word boundaries.
    start = 0
    while start < len(block):
        prefix = block[start:start + CHUNK_BYTES].encode("utf-8")[:CHUNK_BYTES].decode("utf-8", errors="ignore")
        if start + len(prefix) == len(block):
            yield prefix
            return
        end = split_boundary(prefix)
        yield prefix[:end].rstrip()
        start += end
        whitespace = WORD_BREAK.match(block, start)
        if whitespace:
            start = whitespace.end()


def cell_span(cell, attribute: str) -> int:
    value = cell.get(attribute, "1")
    return min(1000, max(1, int(value[:4]))) if value.isdecimal() else 1


def table_to_markdown(block: str) -> str:
    table = BeautifulSoup(block, "html.parser").table
    caption = table.find("caption", recursive=False)
    rows = []
    headers = 0
    spans = {}
    for index, row in enumerate(table.find_all("tr")):
        cells = row.find_all(["th", "td"], recursive=False)
        if headers == index and (row.parent.name == "thead" or (cells and all(c.name == "th" for c in cells))):
            headers += 1
        spans = {column: span for column, span in spans.items() if span[1] > index}
        values = {column: value for column, (value, _) in spans.items()}
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
                    spans[position] = (value, index + height)
            column += width
        rows.append(values)

    width = max((max(row, default=-1) + 1 for row in rows), default=0)
    title = caption.get_text(" ", strip=True) if caption else ""
    if not width:
        return table.get_text(" ", strip=True)

    def format_row(values):
        return "| " + " | ".join(values) + " |"

    columns = []
    for column in range(width):
        labels = dict.fromkeys(row[column] for row in rows[:headers] if row.get(column))
        columns.append(" / ".join(labels))
    lines = [format_row(columns), format_row(["---"] * width)]
    lines.extend(format_row(row.get(column, "") for column in range(width)) for row in rows[headers:])
    return "\n\n".join(part for part in (title, "\n".join(lines)) if part)


def split_markdown(document: str) -> Iterator[str]:
    document = document.replace("\x00", "").replace("\r\n", "\n")
    chunk = ""
    for index, part in enumerate(MARKDOWN_BLOCK.split(document)):
        if index % 2 and part.lower().startswith("<table"):
            if chunk:
                yield chunk
                chunk = ""
            table = table_to_markdown(part)
            if table:
                yield from split_block(table)
            continue
        blocks = [part] if index % 2 else PARAGRAPH_BREAK.split(part)
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            joined = f"{chunk}\n\n{block}" if chunk else block
            if chunk and (block.startswith("#") or len(joined.encode("utf-8")) > CHUNK_BYTES):
                yield chunk
                chunk = ""
                joined = block
            if len(joined.encode("utf-8")) > CHUNK_BYTES:
                yield from split_block(block)
            else:
                chunk = joined
    if chunk:
        yield chunk


def fit_token_limit(block: str, tokenizer: Tokenizer, max_tokens: int) -> Iterator[str]:
    while block:
        encoding = tokenizer.encode(block)
        if len(encoding.ids) <= max_tokens:
            yield block
            return
        if len(block) == 1:
            raise ValueError("One character exceeds the embedding token limit")
        # Leave room for the end token. Retokenize both sides because their boundaries change.
        end = encoding.offsets[max_tokens - 1][0]
        if not 0 < end < len(block):
            end = len(block) // 2
        end = split_boundary(block[:end])
        yield from fit_token_limit(block[:end].rstrip(), tokenizer, max_tokens)
        block = block[end:].lstrip()


def split_document(document: str, tokenizer: Tokenizer, max_tokens: int) -> Iterator[str]:
    for block in split_markdown(document):
        yield from fit_token_limit(block, tokenizer, max_tokens)

import re
from collections.abc import Iterator

from tokenizers import Tokenizer


CHUNK_BYTES = 1000
MARKDOWN_BLOCK = re.compile(
    r"(^\$\$[ \t]*\n.*?^\$\$[ \t]*$|^<table\b.*?</table>)", re.MULTILINE | re.DOTALL,
)
PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n|\n(?=#{1,6}[ \t])")
WORD_BREAK = re.compile(r"\s+")


def split_boundary(prefix: str) -> int:
    minimum = len(prefix) // 2
    row_end = prefix.rfind("</tr>", minimum)
    if row_end >= 0:
        return row_end + len("</tr>")
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


def split_markdown(document: str) -> Iterator[str]:
    document = document.replace("\x00", "").replace("\r\n", "\n")
    chunk = ""
    for index, part in enumerate(MARKDOWN_BLOCK.split(document)):
        blocks = [part] if index % 2 else PARAGRAPH_BREAK.split(part)
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            joined = f"{chunk}\n\n{block}" if chunk else block
            if chunk and (block.startswith(("#", "<table>")) or len(joined.encode("utf-8")) > CHUNK_BYTES):
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

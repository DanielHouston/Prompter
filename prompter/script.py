"""Turn a Markdown script into display blocks plus the words a speaker will say.

Headings become sections. Text in [square brackets] is a cue (shown dimmed, never
expected to be spoken), and <!-- comments --> are dropped entirely.
"""

import re
import unicodedata
from dataclasses import dataclass, field

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
QUOTE = re.compile(r"^\s*>\s?")
FENCE = re.compile(r"^\s*(```|~~~)")
COMMENT = re.compile(r"<!--.*?-->", re.S)
LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
EMPHASIS = re.compile(r"\*\*|__|~~|`|(?<!\w)[*_]+|[*_]+(?!\w)")
CUE = re.compile(r"\[[^\]]*\]")
WORD = re.compile(r"[\w'’]+")


def normalize(word):
    """Case-folded letters and digits only (any script), so "Don’t," and "dont" compare equal
    and "Straße" matches "STRASSE"."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", word).casefold() if ch.isalnum())


def words_of(text):
    """Normalized words of free text (e.g. a transcript), hyphenated words split."""
    return [w for w in (normalize(m) for m in WORD.findall(text)) if w]


@dataclass
class Token:
    norm: str
    block: int
    start: int  # UTF-8 byte offsets into the block's text (what Pango indexes by)
    end: int
    optional: bool  # heading words: fine if the speaker skips them


@dataclass
class Block:
    kind: str  # "heading" | "para"
    text: str
    level: int = 0
    cues: list = field(default_factory=list)  # byte ranges of [cues] within text
    first_token: int = 0
    end_token: int = 0


@dataclass
class Section:
    title: str
    block: int
    token: int


class Script:
    def __init__(self, blocks):
        self.blocks = blocks
        self.tokens = []
        self.sections = []

        for b, block in enumerate(blocks):
            block.first_token = len(self.tokens)
            cue_chars = [(m.start(), m.end()) for m in CUE.finditer(block.text)]
            block.cues = [(_bytes(block.text, s), _bytes(block.text, e)) for s, e in cue_chars]
            for m in WORD.finditer(block.text):
                if any(s <= m.start() < e for s, e in cue_chars):
                    continue
                norm = normalize(m.group())
                if norm:
                    self.tokens.append(
                        Token(norm, b, _bytes(block.text, m.start()), _bytes(block.text, m.end()), block.kind == "heading")
                    )
            block.end_token = len(self.tokens)
            if block.kind == "heading":
                self.sections.append(Section(block.text, b, block.first_token))

        if not self.sections or self.sections[0].token > 0:
            self.sections.insert(0, Section("Start", 0, 0))

    def __len__(self):
        return len(self.tokens)

    def section_at(self, token):
        """Index of the section containing token."""
        idx = 0
        for i, s in enumerate(self.sections):
            if s.token <= token:
                idx = i
        return idx

    def context(self, token, before=20, after=50):
        """Original script text around token, used to prime speech recognition."""
        lo, hi = max(0, token - before), min(len(self.tokens), token + after)
        if lo >= hi:
            return ""
        out, block, start = [], None, 0
        for t in self.tokens[lo:hi]:
            if t.block != block:
                if block is not None:
                    out.append(_slice(self.blocks[block].text, start, end))
                block, start = t.block, t.start
            end = t.end
        out.append(_slice(self.blocks[block].text, start, end))
        return " ".join(out)


def parse(markdown):
    markdown = COMMENT.sub("", markdown)
    blocks, para = [], []

    def flush():
        if para:
            text = " ".join(para).strip()
            if text:
                blocks.append(Block("para", text))
            para.clear()

    for raw in markdown.splitlines():
        if FENCE.match(raw):
            flush()
            continue
        line = raw.rstrip()
        heading = HEADING.match(line)
        if heading:
            flush()
            blocks.append(Block("heading", _inline(heading.group(2)), level=len(heading.group(1))))
        elif not line.strip():
            flush()
        elif LIST_ITEM.match(line):
            flush()
            para.append(_inline(LIST_ITEM.sub("", line)))
        else:
            para.append(_inline(QUOTE.sub("", line)))
    flush()
    return Script(blocks)


def load(path):
    """A Markdown script, or a presentation's speaker notes (see decks)."""
    if str(path).lower().endswith((".pptx", ".odp")):
        from . import decks

        return decks.load(path)
    with open(path, encoding="utf-8") as f:
        return parse(f.read())


def _inline(text):
    return EMPHASIS.sub("", LINK.sub(r"\1", text)).strip()


def _bytes(text, char_index):
    return len(text[:char_index].encode())


def _slice(text, start, end):
    return text.encode()[start:end].decode()

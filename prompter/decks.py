"""Speaker notes from presentations: PowerPoint .pptx (also what Google Slides and Keynote
export) and LibreOffice Impress .odp.

Each slide becomes a section of the script, its title the heading and its notes the text, so
a deck reads like any other script. Hidden slides are left out, as the slideshow skips them.
Each slide also says how many clicks its animations take, so following a clicker can tell a
click that plays an animation from one that moves on to the next slide.
"""

import posixpath
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass, field

from .script import CUE, Block, Script, words_of

SUFFIXES = (".pptx", ".odp")

P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"

OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
DRAW = "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}"
PRES = "{urn:oasis:names:tc:opendocument:xmlns:presentation:1.0}"
TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
STYLE = "{urn:oasis:names:tc:opendocument:xmlns:style:1.0}"
ANIM = "{urn:oasis:names:tc:opendocument:xmlns:animation:1.0}"


class DeckError(ValueError):
    pass


@dataclass
class Slide:
    title: str
    notes: list = field(default_factory=list)  # paragraphs
    clicks: int = 0  # animation steps before a click moves on to the next slide


def load(path):
    """A Script with one section per slide, and .slides alongside."""
    try:
        slides = read_odp(path) if str(path).lower().endswith(".odp") else read_pptx(path)
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, KeyError, ET.ParseError) as e:  # damaged, encrypted, unfinished
        raise DeckError(f"not a presentation Prompter can read ({e})") from e
    return to_script(slides)


def to_script(slides):
    blocks = []
    for n, slide in enumerate(slides, 1):
        title = slide.title
        if not words_of(CUE.sub("", title)):  # every slide needs a word to be a section of its own; [cues] aren't said
            title = f"Slide {n}" + (f" · {title}" if title else "")
            slide.title = title
        blocks.append(Block("heading", title, level=1))
        blocks += [Block("para", text) for text in slide.notes]
    script = Script(blocks)
    script.slides = slides
    return script


# --- PowerPoint ---------------------------------------------------------------


def read_pptx(path):
    with zipfile.ZipFile(path) as z:
        presentation = ET.fromstring(z.read("ppt/presentation.xml"))
        targets = _rels(z, "ppt/presentation.xml")
        slides = []
        for slide_id in presentation.iterfind(f"{P}sldIdLst/{P}sldId"):
            part = targets.get(slide_id.get(f"{R}id"), (None, None))[0]
            if part is None:
                continue
            slide = ET.fromstring(z.read(part))
            if slide.get("show") in ("0", "false"):  # hidden
                continue
            notes = []
            for target, kind in _rels(z, part).values():
                if kind.endswith("/notesSlide"):
                    notes = _pptx_notes(ET.fromstring(z.read(target)))
            slides.append(Slide(_pptx_title(slide), notes, _pptx_clicks(slide)))
    return slides


def _rels(z, part):
    """{relationship id: (target part, type)} for a part of the package."""
    folder, name = posixpath.split(part)
    try:
        rels = ET.fromstring(z.read(posixpath.join(folder, "_rels", name + ".rels")))
    except KeyError:
        return {}
    out = {}
    for rel in rels.iter(f"{PKG}Relationship"):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target", "")
        target = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
        out[rel.get("Id")] = (target, rel.get("Type", ""))
    return out


def _placeholder(shape):
    ph = shape.find(f"{P}nvSpPr/{P}nvPr/{P}ph")
    return None if ph is None else ph.get("type", "body")


def _pptx_title(slide):
    for shape in slide.iter(f"{P}sp"):
        if _placeholder(shape) in ("title", "ctrTitle"):
            body = shape.find(f"{P}txBody")
            return " ".join(_drawing_paragraphs(body, bullets=False)) if body is not None else ""
    return ""


def _pptx_notes(notes):
    """The notes placeholder's text; failing that, any text on the notes page that isn't the
    slide picture, number, header, footer or date (some exporters don't mark the notes)."""
    shapes = [(shape, _placeholder(shape)) for shape in notes.iter(f"{P}sp")]
    chosen = [shape for shape, kind in shapes if kind == "body"] or \
             [shape for shape, kind in shapes if kind is None]
    paragraphs = []
    for shape in chosen:
        body = shape.find(f"{P}txBody")
        if body is not None:
            paragraphs += _drawing_paragraphs(body)
    return paragraphs


def _drawing_paragraphs(body, bullets=True):
    """DrawingML paragraphs as text; line breaks within one start a new paragraph."""
    out = []
    for para in body.iterfind(f"{A}p"):
        lines, line = [], []
        for child in para:
            if child.tag in (f"{A}r", f"{A}fld"):
                line.append(child.findtext(f"{A}t") or "")
            elif child.tag == f"{A}br":
                lines.append("".join(line))
                line = []
        lines.append("".join(line))
        props = para.find(f"{A}pPr")
        bullet = bullets and props is not None and (props.find(f"{A}buChar") is not None
                                                    or props.find(f"{A}buAutoNum") is not None)
        indent = " " * int(props.get("lvl", 0)) if props is not None else ""
        for text in lines:
            text = " ".join(text.split())
            if text:
                out.append(indent + ("• " if bullet else "") + text)
    return out


def _pptx_clicks(slide):
    """Clicks the slide's animations wait for: each step of the main sequence that starts on a
    click (steps that start after or with the previous one need none)."""
    for node in slide.iter(f"{P}cTn"):
        if node.get("nodeType") == "mainSeq":
            steps = node.find(f"{P}childTnLst")
            if steps is None:
                return 0
            count = 0
            for step in steps.findall(f"{P}par"):
                first = next((n for n in step.iter(f"{P}cTn") if n.get("nodeType", "").endswith("Effect")), None)
                count += first is not None and first.get("nodeType") == "clickEffect"
            return count
    return 0


# --- LibreOffice Impress --------------------------------------------------------


def read_odp(path):
    with zipfile.ZipFile(path) as z:
        content = ET.fromstring(z.read("content.xml"))
    hidden = {style.get(f"{STYLE}name") for style in content.iter(f"{STYLE}style")
              if any(props.get(f"{PRES}visibility") == "hidden"
                     for props in style.iter(f"{STYLE}drawing-page-properties"))}
    slides = []
    for page in content.iterfind(f"{OFFICE}body/{OFFICE}presentation/{DRAW}page"):
        if page.get(f"{DRAW}style-name") in hidden:
            continue
        title = ""
        for frame in page.findall(f"{DRAW}frame"):
            if frame.get(f"{PRES}class") == "title":
                title = " ".join(_odf_paragraphs(frame, bullets=False))
                break
        notes = []
        for frame in page.iterfind(f"{PRES}notes/{DRAW}frame"):
            if frame.get(f"{PRES}class") == "notes":
                notes += _odf_paragraphs(frame)
        slides.append(Slide(title, notes, _odp_clicks(page)))
    return slides


def _odf_paragraphs(element, bullets=True):
    out = []

    def walk(node, in_list):
        for child in node:
            if child.tag in (f"{TEXT}p", f"{TEXT}h"):
                for text in _odf_text(child).split("\n"):
                    text = " ".join(text.split())
                    if text:
                        out.append(("• " if bullets and in_list else "") + text)
            else:
                walk(child, in_list or child.tag == f"{TEXT}list-item")

    walk(element, False)
    return out


def _odf_text(node):
    parts = [node.text or ""]
    for child in node:
        if child.tag == f"{TEXT}s":
            parts.append(" " * int(child.get(f"{TEXT}c", 1)))
        elif child.tag == f"{TEXT}tab":
            parts.append(" ")
        elif child.tag == f"{TEXT}line-break":
            parts.append("\n")
        else:
            parts.append(_odf_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _odp_clicks(page):
    for seq in page.iter(f"{ANIM}seq"):
        if seq.get(f"{PRES}node-type") == "main-sequence":
            count = 0
            for step in seq.findall(f"{ANIM}par"):
                first = next((n for n in step.iter(f"{ANIM}par") if n.get(f"{PRES}node-type")), None)
                count += first is not None and first.get(f"{PRES}node-type") == "on-click"
            return count
    return 0


# --- moving through the slides --------------------------------------------------


def clicks(script):
    """Animation clicks per section: a deck's slides, or none for a Markdown script."""
    slides = getattr(script, "slides", None)
    counts = [s.clicks for s in slides] if slides else []
    return (counts + [0] * len(script.sections))[: len(script.sections)]


def advance(counts, slide, step, action):
    """(slide, step) after a slide key, the way PowerPoint and Impress move: "next" plays the
    slide's next animation or moves on when none are left; "previous" undoes one, or goes back
    to the previous slide with its animations played; "first" and "last" jump."""
    last = len(counts) - 1
    if action == "next":
        if step < counts[slide]:
            return slide, step + 1
        return (slide + 1, 0) if slide < last else (slide, step)
    if action == "previous":
        if step > 0:
            return slide, step - 1
        return (slide - 1, counts[slide - 1]) if slide > 0 else (0, 0)
    if action == "first":
        return 0, 0
    if action == "last":
        return last, 0
    return slide, step


def follow(script, pos, slide, step, action):
    """A slide key on a followed clicker: (slide, step) the presentation is on now, and where the
    notes go. When the slide changes they go to its start, unless you're already reading it
    (voice follow got there before your click did); animation clicks leave them be."""
    new, step = advance(clicks(script), slide, step, action)
    if new != slide and script.section_at(pos) != new:
        pos = script.sections[new].token
    return new, step, pos

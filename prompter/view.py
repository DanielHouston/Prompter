"""The prompter display: large text that scrolls so the current line sits on the reading line."""

import math
import re

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_foreign("cairo")
from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

SCROLL_TAU = 0.18  # seconds; how softly the text glides to a new position
# The Prompter is a 60 Hz panel, but DisplayLink completes frames instantly, so the compositor
# would happily redraw it ~100 times a second. Every one of those is copied and compressed for
# USB; past what the panel can show, that just smears fast-moving text. Cap redraws instead.
MAX_FPS = 60
PAD = 16  # px between the screen edge and what sits in its corners
GAP = 8  # px between readouts that share a position

DEFAULT_APPEARANCE = {
    "font": "Noto Sans Medium",
    "text_color": "#e6e9ef",
    "background": "#10131a",
    "accent": "#3fb6a8",  # headings and the reading-line marker
    "margin": 5.5,  # % of the width, each side
    "line_spacing": 1.12,
    "reading_line": 36,  # % of the height where the current line sits
    "center_word": False,  # short lines centred on the lens (the crosshair's x), so your eyes barely move
    "line_width": 50,  # % of the width, when centred on the lens
}
DEFAULT_CROSSHAIR = {
    "enabled": False,
    "x": 50,  # % of the screen: put it over the camera lens
    "y": 50,
    "size": 48,  # px
    "color": "#3fb6a8",
    "opacity": 60,  # %
}
DEFAULT_PREVIEW = {
    "enabled": False,
    "device": None,  # V4L2 device path, chosen in Setup
    "corner": "top-right",  # top-right | top-left | bottom-right | bottom-left
    "size": 30,  # % of the screen width
    "mirror": True,
}
_LAYOUT_KEYS = ("font", "margin", "line_spacing", "center_word", "line_width")


def rgba(hex_color):
    c = Gdk.RGBA()
    c.parse(hex_color)
    return c


def _mix(a, b, t):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = (x + (y - x) * t for x, y in zip((a.red, a.green, a.blue, 1), (b.red, b.green, b.blue, 1)))
    return c


def _pango(c):
    return round(c.red * 65535), round(c.green * 65535), round(c.blue * 65535)


class _Block:
    def __init__(self, layout, y):
        self.layout = layout
        self.y = y
        self.height = 0


class PrompterView(Gtk.Widget):
    def __init__(self):
        super().__init__()
        self.script = None
        self.font_size = 52
        self.appearance = dict(DEFAULT_APPEARANCE)
        self.crosshair = dict(DEFAULT_CROSSHAIR)
        self._colors()
        self.blocks = []
        self._left = 0.0  # where the lines start, when centred on the lens
        self.laid_out_width = 0
        self.pos = 0.0  # fractional token index on the reading line
        self.spoken = 0  # tokens before this are dimmed
        self.scroll = 0.0  # content y currently on the reading line
        self.target = 0.0
        self.running = False  # keep animating even when on target (fixed-speed mode)
        self.calibrating = False  # "find the lens": black screen, just the crosshair
        self.preview = dict(DEFAULT_PREVIEW)
        self.preview_frame = None  # latest camera texture, None when there's no signal
        self.readouts = ()  # ((position, text), ...): the timer and progress
        self._readout_layouts = {}
        self.on_frame = None
        self._tick = None
        self._last_frame = None
        self._last_draw = None

    # --- public API -------------------------------------------------------

    def set_script(self, script, pos=0):
        self.script = script
        self.spoken = pos
        self.pos = float(pos)
        self._relayout(jump=True)

    def set_font_size(self, size):
        self.font_size = size
        self._relayout(jump=True)

    def set_style(self, appearance, crosshair):
        relayout = any(appearance.get(k) != self.appearance.get(k) for k in _LAYOUT_KEYS) or (
            appearance.get("center_word") and crosshair.get("x") != self.crosshair["x"])  # lines fit either side of the lens
        self.appearance = {**DEFAULT_APPEARANCE, **appearance}
        self.crosshair = {**DEFAULT_CROSSHAIR, **crosshair}
        self._colors()
        if relayout:
            self._relayout(jump=True)
        else:
            for b in range(len(self.blocks)):
                self._style(b)
            self.queue_draw()

    def set_spoken(self, spoken):
        if not self.script or spoken == self.spoken:
            return
        lo, hi = sorted((self.spoken, spoken))
        self.spoken = spoken
        for b in {t.block for t in self.script.tokens[lo : hi + 1]}:
            self._style(b)
        self.queue_draw()

    def set_position(self, pos, jump=False):
        self.pos = pos
        self.target = self._y_for(pos)
        if jump:
            self.scroll = self.target
            self.queue_draw()
        self._animate()

    def set_preview(self, preview):
        self.preview = {**DEFAULT_PREVIEW, **preview}
        self.queue_draw()

    def set_preview_frame(self, texture):
        self.preview_frame = texture
        if self.preview["enabled"]:
            self.queue_draw()

    def set_readouts(self, readouts):
        """((position, text), ...) such as (("top-left", "4:32"), ("top-right", "37%"))."""
        readouts = tuple(readouts)
        if readouts != self.readouts:
            self.readouts = readouts
            self.queue_draw()

    def set_calibrating(self, on):
        self.calibrating = on
        self.queue_draw()

    def set_running(self, running):
        self.running = running
        self._animate()

    # --- layout -----------------------------------------------------------

    def _colors(self):
        a = self.appearance
        text, bg, accent = rgba(a["text_color"]), rgba(a["background"]), rgba(a["accent"])
        self.c_background, self.c_accent, self.c_text = bg, accent, text
        self.p_text, self.p_heading = _pango(text), _pango(accent)
        self.p_spoken, self.p_cue = _pango(_mix(text, bg, 0.68)), _pango(_mix(text, bg, 0.4))
        self.c_readout = _mix(text, bg, 0.25)
        self.c_readout_bg = _mix(bg, text, 0.08)
        self.c_readout_bg.alpha = 0.92
        self._readout_layouts = {}

    def do_size_allocate(self, width, height, baseline):
        if width != self.laid_out_width:
            self._relayout(jump=True)

    def _relayout(self, jump=False):
        width = self.get_width()
        self.laid_out_width = width
        self.blocks = []
        if not self.script or width <= 0:
            self.queue_draw()
            return

        margin = self._margin()
        column = width - 2 * margin
        if self.centered:  # a short column centred on the lens, so your eyes barely move
            lens_x = width * self.crosshair["x"] / 100
            column = min(width * float(self.appearance["line_width"]) / 100, 2 * (min(lens_x, width - lens_x) - margin))
            column = max(column, self.font_size * 4)
            self._left = round(max(margin, min(lens_x - column / 2, width - margin - column)))  # on screen, near an edge too
        y = 0.0
        for b, block in enumerate(self.script.blocks):
            heading = block.kind == "heading"
            if heading and b > 0:
                y += self.font_size * 0.6
            layout = self.create_pango_layout(block.text)
            desc = Pango.FontDescription.from_string(self.appearance["font"])
            desc.set_absolute_size((self.font_size * (0.5 if heading else 1.0)) * Pango.SCALE)
            if heading:
                desc.set_weight(Pango.Weight.BOLD)
            layout.set_font_description(desc)
            self.blocks.append(_Block(layout, y))
            self._style(b)
            if self.centered:  # measured on one line as styled: headings are spaced-out capitals
                layout.set_text(phrase_lines(block.text, _x_at(layout), column - 1), -1)
                layout.set_alignment(Pango.Alignment.CENTER)
            layout.set_width(int(column * Pango.SCALE))
            layout.set_wrap(Pango.WrapMode.WORD_CHAR)
            layout.set_line_spacing(float(self.appearance["line_spacing"]))
            self.blocks[-1].height = layout.get_pixel_size()[1]
            y += self.blocks[-1].height + self.font_size * (0.25 if heading else 0.55)

        self.set_position(self.pos, jump=jump)

    @property
    def centered(self):
        """Short lines centred on the lens, ending where you'd pause, instead of lines across the
        whole width: your eyes stay close to the camera."""
        return bool(self.appearance["center_word"])

    def _style(self, b):
        """Colour a block: already-read text dim, cues muted and italic, headings accented."""
        if b >= len(self.blocks):
            return
        block, layout = self.script.blocks[b], self.blocks[b].layout
        size = len(block.text.encode())
        if self.spoken >= block.end_token:
            split = size
        elif self.spoken <= block.first_token:
            split = 0
        else:
            split = self.script.tokens[self.spoken].start

        attrs = Pango.AttrList()
        points = sorted({0, size, split, *(p for cue in block.cues for p in cue)})
        for start, end in zip(points, points[1:]):
            in_cue = any(s <= start < e for s, e in block.cues)
            if start < split:
                color = self.p_spoken
            elif in_cue:
                color = self.p_cue
            else:
                color = self.p_heading if block.kind == "heading" else self.p_text
            _insert(attrs, Pango.attr_foreground_new(*color), start, end)
            if in_cue:
                _insert(attrs, Pango.attr_style_new(Pango.Style.ITALIC), start, end)
        if block.kind == "heading":
            _insert(attrs, Pango.attr_letter_spacing_new(int(self.font_size * 0.08 * Pango.SCALE)), 0, size)
            if hasattr(Pango, "attr_text_transform_new"):
                _insert(attrs, Pango.attr_text_transform_new(Pango.TextTransform.UPPERCASE), 0, size)
        layout.set_attributes(attrs)

    def _margin(self):
        return max(16, self.get_width() * float(self.appearance["margin"]) / 100)

    def _token_y(self, i):
        t = self.script.tokens[i]
        blk = self.blocks[t.block]
        rect = blk.layout.index_to_pos(t.start)
        return blk.y + (rect.y + rect.height / 2) / Pango.SCALE

    def _y_for(self, pos):
        if not self.script or not self.blocks or not self.script.tokens:
            return 0.0
        n = len(self.script.tokens)
        if pos >= n:  # finished: last line glides up past the reading line
            return self._token_y(n - 1) + self.font_size * 1.2
        i = int(pos)
        y = self._token_y(i)
        frac = pos - i
        if frac and i + 1 < n:
            y += (self._token_y(i + 1) - y) * frac
        return y

    # --- drawing and animation -------------------------------------------

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        if self.calibrating:
            self._draw_calibration(snapshot, width, height)
            return
        snapshot.append_color(self.c_background, Graphene.Rect().init(0, 0, width, height))
        reading_y = height * float(self.appearance["reading_line"]) / 100

        if self.blocks:
            top = reading_y - self.scroll
            margin = self._margin()
            left = self._left if self.centered else margin
            for blk in self.blocks:
                y = round(top + blk.y)  # whole pixels: crisp glyphs, and plain shifts compress well
                if y > height or y + blk.height < 0:
                    continue
                snapshot.save()
                snapshot.translate(Graphene.Point().init(left, y))
                snapshot.append_layout(blk.layout, self.c_text)
                snapshot.restore()

            marker = self.font_size * 0.9
            snapshot.append_color(self.c_accent, Graphene.Rect().init(left - margin * 0.65, reading_y - marker / 2, 5, marker))

        if self.preview["enabled"]:
            self._draw_preview(snapshot, width, height)
        if self.readouts:
            self._draw_readouts(snapshot, width, height)
        if self.crosshair["enabled"]:
            self._draw_crosshair(snapshot, width, height)

    def _preview_box(self, width, height):
        p = self.preview
        w = width * p["size"] / 100
        h = w * 9 / 16
        x = PAD if p["corner"].endswith("left") else width - w - PAD
        y = PAD if p["corner"].startswith("top") else height - h - PAD
        return x, y, w, h

    def _draw_preview(self, snapshot, width, height):
        """What the camera sees, in a corner, so you can check framing while you read."""
        p = self.preview
        x, y, w, h = self._preview_box(width, height)
        box = Graphene.Rect().init(x, y, w, h)
        outline = Gsk.RoundedRect()
        outline.init_from_rect(box, 10)

        snapshot.push_rounded_clip(outline)
        if self.preview_frame:
            snapshot.save()
            snapshot.translate(Graphene.Point().init(x + w if p["mirror"] else x, y))
            if p["mirror"]:
                snapshot.scale(-1, 1)
            snapshot.append_texture(self.preview_frame, Graphene.Rect().init(0, 0, w, h))
            snapshot.restore()
        else:
            snapshot.append_color(rgba("#1b1f2b"), box)
            label = self.create_pango_layout("camera off")
            desc = Pango.FontDescription.from_string(self.appearance["font"])
            desc.set_absolute_size(max(11, h * 0.09) * Pango.SCALE)
            label.set_font_description(desc)
            lw, lh = label.get_pixel_size()
            snapshot.save()
            snapshot.translate(Graphene.Point().init(x + (w - lw) / 2, y + (h - lh) / 2))
            snapshot.append_layout(label, rgba("#5c6378"))
            snapshot.restore()
        snapshot.pop()
        edge = Gdk.RGBA()
        edge.red, edge.green, edge.blue, edge.alpha = self.c_accent.red, self.c_accent.green, self.c_accent.blue, 0.6
        snapshot.append_border(outline, [1.5] * 4, [edge] * 4)

    def _draw_readouts(self, snapshot, width, height):
        """Timer and progress, each on a pill that keeps it readable over the text scrolling
        underneath."""
        size = max(14.0, height * 0.045)
        pills = []
        for position, text in self.readouts:
            layout = self._readout_layout(text, size)
            text_w, text_h = layout.get_pixel_size()
            pad_x, pad_y = round(text_h * 0.55), round(text_h * 0.18)
            pills.append((position, layout, text_w + 2 * pad_x, text_h + 2 * pad_y, pad_x, pad_y))
        avoid = self._preview_box(width, height) if self.preview["enabled"] else None
        spots = place_readouts(width, height, [(position, w, h) for position, _, w, h, _, _ in pills], avoid)
        for (_, layout, w, h, pad_x, pad_y), (x, y) in zip(pills, spots):
            box = Graphene.Rect().init(x, y, w, h)
            pill = Gsk.RoundedRect()
            pill.init_from_rect(box, h / 2)
            snapshot.push_rounded_clip(pill)
            snapshot.append_color(self.c_readout_bg, box)
            snapshot.pop()
            snapshot.save()
            snapshot.translate(Graphene.Point().init(x + pad_x, y + pad_y))
            snapshot.append_layout(layout, self.c_readout)
            snapshot.restore()

    def _readout_layout(self, text, size):
        layout = self._readout_layouts.get((text, size))
        if layout is None:
            if len(self._readout_layouts) > 16:  # the timer makes a new one every second
                self._readout_layouts.clear()
            layout = self.create_pango_layout(text)
            desc = Pango.FontDescription.from_string(self.appearance["font"])
            desc.set_absolute_size(size * Pango.SCALE)
            layout.set_font_description(desc)
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_font_features_new("tnum"))  # digits all one width: the timer doesn't jiggle
            layout.set_attributes(attrs)
            self._readout_layouts[(text, size)] = layout
        return layout

    def _draw_calibration(self, snapshot, width, height):
        """Black, so the glass turns see-through and the lens shows behind it; the crosshair at
        full strength with faint guide lines across the whole screen to bring it onto the lens."""
        snapshot.append_color(rgba("#000000"), Graphene.Rect().init(0, 0, width, height))
        cx, cy = width * self.crosshair["x"] / 100, height * self.crosshair["y"] / 100
        guide = rgba(self.crosshair["color"])
        guide.alpha = 0.35
        snapshot.append_color(guide, Graphene.Rect().init(0, cy - 0.5, width, 1))
        snapshot.append_color(guide, Graphene.Rect().init(cx - 0.5, 0, 1, height))
        self._draw_crosshair(snapshot, width, height, opacity=1.0)

        hint = self.create_pango_layout("Arrows move the crosshair onto the lens · Shift for fine steps · Enter when done")
        desc = Pango.FontDescription.from_string(self.appearance["font"])
        desc.set_absolute_size(max(12, height * 0.03) * Pango.SCALE)
        hint.set_font_description(desc)
        w, h = hint.get_pixel_size()
        snapshot.save()
        snapshot.translate(Graphene.Point().init((width - w) / 2, height - h * 2))
        snapshot.append_layout(hint, rgba("#5c6378"))
        snapshot.restore()

    def _draw_crosshair(self, snapshot, width, height, opacity=None):
        """An eye-line target over the camera lens: ring, centre dot, four ticks."""
        ch = self.crosshair
        cx, cy, r = width * ch["x"] / 100, height * ch["y"] / 100, ch["size"] / 2
        color = rgba(ch["color"])
        line = max(1.5, r * 0.07)

        snapshot.push_opacity(ch["opacity"] / 100 if opacity is None else opacity)
        cr = snapshot.append_cairo(Graphene.Rect().init(cx - r - line, cy - r - line, 2 * (r + line), 2 * (r + line)))
        cr.set_source_rgba(color.red, color.green, color.blue, 1)
        cr.set_line_width(line)
        cr.arc(cx, cy, r * 0.6, 0, 2 * math.pi)
        cr.stroke()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            cr.move_to(cx + dx * r * 0.75, cy + dy * r * 0.75)
            cr.line_to(cx + dx * r, cy + dy * r)
        cr.stroke()
        cr.arc(cx, cy, line, 0, 2 * math.pi)
        cr.fill()
        snapshot.pop()

    def _animate(self):
        if self._tick is None and (self.running or abs(self.target - self.scroll) > 0.25):
            self._last_frame = None
            self._tick = self.add_tick_callback(self._on_tick)

    def _on_tick(self, _widget, clock):
        now = clock.get_frame_time() / 1e6
        dt = 1 / 60 if self._last_frame is None else min(0.1, now - self._last_frame)
        self._last_frame = now

        if self.on_frame:
            self.on_frame(dt)

        diff = self.target - self.scroll
        if abs(diff) < 0.25 and not self.running:
            self.scroll = self.target
            self.queue_draw()
            self._tick = None
            return GLib.SOURCE_REMOVE  # idle: stop redrawing (each Prompter frame costs a CPU copy)

        self.scroll += diff * (1 - math.exp(-dt / SCROLL_TAU))
        if self._last_draw is None or now - self._last_draw >= 1 / MAX_FPS - 0.002:
            self._last_draw = now
            self.queue_draw()
        return GLib.SOURCE_CONTINUE


def place_readouts(width, height, pills, avoid=None):
    """Where each readout's pill goes, as whole-pixel (x, y). pills: [(position, w, h)] in order,
    position like "top-left" or "bottom-center"; pills sharing a position sit side by side.
    avoid: the self-view's (x, y, w, h), which pills in its way move beside."""
    rows = {}
    for i, (position, _w, _h) in enumerate(pills):
        rows.setdefault(position, []).append(i)
    spots = [None] * len(pills)
    for position, members in rows.items():
        vertical, _, horizontal = position.partition("-")
        total = sum(pills[i][1] for i in members) + GAP * (len(members) - 1)
        tallest = max(pills[i][2] for i in members)
        x = {"left": PAD, "center": (width - total) / 2}.get(horizontal, width - PAD - total)
        y = PAD if vertical == "top" else height - PAD - tallest
        if avoid:
            ax, ay, aw, ah = avoid
            if x < ax + aw and ax < x + total and y < ay + ah and ay < y + tallest:
                x = ax - PAD - total if ax + aw / 2 > width / 2 else ax + aw + PAD
                x = max(PAD, min(x, width - PAD - total))
        for i in members:
            spots[i] = (round(x), round(y + (tallest - pills[i][2]) / 2))
            x += pills[i][1] + GAP
    return spots


def phrase_lines(text, x_at, width):
    """text with a newline in place of each space where a line should break (the same length,
    so every byte offset into it still holds): lines no wider than width that end, where they
    can, where you'd pause anyway. Chosen for the whole paragraph at once, as typesetters do.
    x_at(byte offset) is how far along the text set on one line."""
    data = bytearray(text.encode())
    words = [m.span() for m in re.finditer(rb"\S+", data)]
    n = len(words)
    reach = [(x_at(start), x_at(end)) for start, end in words]
    endings = [_ending(bytes(data[start:end])) for start, end in words]
    cost, back = [0.0] + [math.inf] * n, [0] * (n + 1)  # the best lines for the first k words, and where the last starts
    for k in range(1, n + 1):
        for i in range(k - 1, -1, -1):
            fill = (reach[k - 1][1] - reach[i][0]) / width
            if fill > 1 and i < k - 1:
                break
            if k == n:  # the last line can be short, but not a lone little word
                line = 0.3 if fill < 0.25 else 0.0
            else:  # empty space costs more the more there is; a good ending earns some back
                line = (1 - fill) ** 2 - endings[k - 1]
            if cost[i] + line + 0.1 < cost[k]:
                cost[k], back[k] = cost[i] + line + 0.1, i
    k = n
    while back[k]:
        k = back[k]
        data[words[k - 1][1]] = ord("\n")
    return data.decode()


def _ending(word):
    """How good a place the end of this word is to end a line: the end of a sentence, of a
    clause or a [cue] are good; after a little word like "a", "the", "to" or "der", which belongs
    with the words after it, is poor."""
    word = word.decode()
    if word.endswith("]"):
        return 0.4
    word = word.rstrip("\"'”’»)")
    if word.endswith((".", "!", "?", "…")):
        return 0.8
    if word.endswith((",", ";", ":", "–", "—")):
        return 0.4
    return -0.4 if len(word) <= 3 else 0.0


def _x_at(layout):
    """Byte index -> x (px) on a one-line layout, measured in one pass along it: index_to_pos
    goes back along the line every time, which on a long paragraph adds up to seconds."""
    xs, it = {}, layout.get_iter()
    while True:
        rect = it.get_cluster_extents()[1]
        xs[it.get_index()] = rect.x / Pango.SCALE
        if not it.next_cluster():
            break
    xs.setdefault(len(layout.get_text().encode()), (rect.x + rect.width) / Pango.SCALE)
    return lambda i: xs[i] if i in xs else layout.index_to_pos(i).x / Pango.SCALE


def _insert(attrs, attr, start, end):
    attr.start_index, attr.end_index = start, end
    attrs.insert(attr)

"""The prompter display: large text that scrolls so the current line sits on the reading line."""

import math

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

DEFAULT_APPEARANCE = {
    "font": "Noto Sans Medium",
    "text_color": "#e6e9ef",
    "background": "#10131a",
    "accent": "#3fb6a8",  # headings and the reading-line marker
    "margin": 5.5,  # % of the width, each side
    "line_spacing": 1.12,
    "reading_line": 36,  # % of the height where the current line sits
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
_LAYOUT_KEYS = ("font", "margin", "line_spacing")


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
        self.height = layout.get_pixel_size()[1]


class PrompterView(Gtk.Widget):
    def __init__(self):
        super().__init__()
        self.script = None
        self.font_size = 52
        self.appearance = dict(DEFAULT_APPEARANCE)
        self.crosshair = dict(DEFAULT_CROSSHAIR)
        self._colors()
        self.blocks = []
        self.laid_out_width = 0
        self.pos = 0.0  # fractional token index on the reading line
        self.spoken = 0  # tokens before this are dimmed
        self.scroll = 0.0  # content y currently on the reading line
        self.target = 0.0
        self.running = False  # keep animating even when on target (fixed-speed mode)
        self.calibrating = False  # "find the lens": black screen, just the crosshair
        self.preview = dict(DEFAULT_PREVIEW)
        self.preview_frame = None  # latest camera texture, None when there's no signal
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
        relayout = any(appearance.get(k) != self.appearance.get(k) for k in _LAYOUT_KEYS)
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
            layout.set_width(int((width - 2 * margin) * Pango.SCALE))
            layout.set_wrap(Pango.WrapMode.WORD_CHAR)
            layout.set_line_spacing(float(self.appearance["line_spacing"]))
            self.blocks.append(_Block(layout, y))
            self._style(b)
            y += self.blocks[-1].height + self.font_size * (0.25 if heading else 0.55)

        self.set_position(self.pos, jump=jump)

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
            for blk in self.blocks:
                y = round(top + blk.y)  # whole pixels: crisp glyphs, and plain shifts compress well
                if y > height or y + blk.height < 0:
                    continue
                snapshot.save()
                snapshot.translate(Graphene.Point().init(margin, y))
                snapshot.append_layout(blk.layout, self.c_text)
                snapshot.restore()

            marker = self.font_size * 0.9
            snapshot.append_color(self.c_accent, Graphene.Rect().init(margin * 0.35, reading_y - marker / 2, 5, marker))

        if self.preview["enabled"]:
            self._draw_preview(snapshot, width, height)
        if self.crosshair["enabled"]:
            self._draw_crosshair(snapshot, width, height)

    def _draw_preview(self, snapshot, width, height):
        """What the camera sees, in a corner, so you can check framing while you read."""
        p, pad = self.preview, 16
        w = width * p["size"] / 100
        h = w * 9 / 16
        x = pad if p["corner"].endswith("left") else width - w - pad
        y = pad if p["corner"].startswith("top") else height - h - pad
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


def _insert(attrs, attr, start, end):
    attr.start_index, attr.end_index = start, end
    attrs.insert(attr)

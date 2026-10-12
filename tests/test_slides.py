import os
import struct
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from gi.repository import GLib

from prompter import decks, script, slidekeys

P_NS = ('xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
REL_NS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'
SLIDE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
NOTES_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"


def shape(kind, *paragraphs):
    ph = f'<p:ph type="{kind}"/>' if kind else ""
    paras = "".join(f"<a:p>{p}</a:p>" for p in paragraphs)
    return f"<p:sp><p:nvSpPr><p:cNvPr id='1' name='x'/><p:cNvSpPr/><p:nvPr>{ph}</p:nvPr></p:nvSpPr><p:txBody>{paras}</p:txBody></p:sp>"


def run(text):
    return f"<a:r><a:t>{text}</a:t></a:r>"


def click_steps(*kinds):
    """A slide's animation timing: one main-sequence step per kind (clickEffect, afterEffect...)."""
    steps = "".join(f'<p:par><p:cTn id="{i + 3}" fill="hold"><p:childTnLst><p:par><p:cTn id="{i + 10}"><p:childTnLst>'
                    f'<p:par><p:cTn id="{i + 20}" presetID="1" nodeType="{kind}"/></p:par></p:childTnLst></p:cTn></p:par>'
                    f'</p:childTnLst></p:cTn></p:par>' for i, kind in enumerate(kinds))
    return (f'<p:timing><p:tnLst><p:par><p:cTn id="1" nodeType="tmRoot"><p:childTnLst><p:seq><p:cTn id="2" nodeType="mainSeq">'
            f'<p:childTnLst>{steps}</p:childTnLst></p:cTn></p:seq></p:childTnLst></p:cTn></p:par></p:tnLst></p:timing>')


def make_pptx(path, slides, compression=zipfile.ZIP_STORED):
    """slides: [(title shape or "", notes paragraphs, hidden (True, or show's value), timing xml)]"""
    with zipfile.ZipFile(path, "w", compression) as z:
        ids, rels = [], []
        for n, (title, notes, hidden, timing) in enumerate(slides, 1):
            ids.append(f'<p:sldId id="{255 + n}" r:id="rId{n}"/>')
            rels.append(f'<Relationship Id="rId{n}" Type="{SLIDE_REL}" Target="slides/slide{n}.xml"/>')
            show = f' show="{"0" if hidden is True else hidden}"' if hidden else ""
            z.writestr(f"ppt/slides/slide{n}.xml", f'<p:sld {P_NS}{show}><p:cSld><p:spTree>{title}</p:spTree></p:cSld>{timing}</p:sld>')
            if notes is not None:
                z.writestr(f"ppt/slides/_rels/slide{n}.xml.rels",
                           f'<Relationships {REL_NS}><Relationship Id="rId9" Type="{NOTES_REL}" '
                           f'Target="../notesSlides/notesSlide{n}.xml"/></Relationships>')
                body = shape("sldImg") + shape("body", *notes) + shape("sldNum", run(str(n)))
                z.writestr(f"ppt/notesSlides/notesSlide{n}.xml", f'<p:notes {P_NS}><p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:notes>')
        z.writestr("ppt/presentation.xml", f'<p:presentation {P_NS}><p:sldIdLst>{"".join(ids)}</p:sldIdLst></p:presentation>')
        z.writestr("ppt/_rels/presentation.xml.rels", f'<Relationships {REL_NS}>{"".join(rels)}</Relationships>')


class DecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pptx_notes_titles_hidden_and_clicks(self):
        path = self.dir / "talk.pptx"
        make_pptx(path, [
            (shape("ctrTitle", run("Welcome")), [run("Thank everyone [smile]"), run("Line one") + "<a:br/>" + run("Line two")], False, ""),
            (shape("title", run("Backup")), [run("Only if asked")], True, ""),
            (shape("title", run("Market ") + run("size")), ['<a:pPr lvl="1"><a:buChar char="•"/></a:pPr>' + run("TAM 4.2B")], False,
             click_steps("clickEffect", "afterEffect", "clickEffect")),
            ("", None, False, click_steps("afterEffect")),
        ])
        slides = decks.read_pptx(path)
        self.assertEqual([s.title for s in slides], ["Welcome", "Market size", ""])
        self.assertEqual(slides[0].notes, ["Thank everyone [smile]", "Line one", "Line two"])
        self.assertEqual(slides[1].notes, [" • TAM 4.2B"])
        self.assertEqual([s.clicks for s in slides], [0, 2, 0])  # an animation that starts by itself needs no click

        loaded = script.load(path)
        self.assertEqual([s.title for s in loaded.sections], ["Welcome", "Market size", "Slide 3"])
        self.assertEqual(decks.clicks(loaded), [0, 2, 0])
        self.assertEqual(loaded.blocks[1].cues, [(15, 22)])  # [smile] is still a cue

    def test_odp(self):
        content = """<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"
 xmlns:anim="urn:oasis:names:tc:opendocument:xmlns:animation:1.0">
 <office:automatic-styles><style:style style:name="dp2"><style:drawing-page-properties presentation:visibility="hidden"/></style:style></office:automatic-styles>
 <office:body><office:presentation>
  <draw:page draw:style-name="dp1"><draw:frame presentation:class="title"><draw:text-box><text:p>Hello</text:p></draw:text-box></draw:frame>
   <presentation:notes><draw:frame presentation:class="notes"><draw:text-box><text:p>Say<text:s/>hi<text:line-break/>Then wave</text:p>
    <text:list><text:list-item><text:p>Point</text:p></text:list-item></text:list></draw:text-box></draw:frame></presentation:notes>
   <anim:par presentation:node-type="timing-root"><anim:seq presentation:node-type="main-sequence">
    <anim:par><anim:par><anim:par presentation:node-type="on-click"/></anim:par></anim:par>
    <anim:par><anim:par><anim:par presentation:node-type="after-previous"/></anim:par></anim:par>
   </anim:seq></anim:par></draw:page>
  <draw:page draw:style-name="dp2"><draw:frame presentation:class="title"><draw:text-box><text:p>Hidden</text:p></draw:text-box></draw:frame></draw:page>
 </office:presentation></office:body></office:document-content>"""
        path = self.dir / "talk.odp"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("content.xml", content)
        slides = decks.read_odp(path)
        self.assertEqual([(s.title, s.notes, s.clicks) for s in slides], [("Hello", ["Say hi", "Then wave", "• Point"], 1)])

    def test_not_a_deck(self):
        path = self.dir / "broken.pptx"
        path.write_text("not a zip")
        with self.assertRaises(ValueError):
            script.load(path)

    def test_damaged_deck(self):
        """Damaged inside, as a half-synced file can be: "can't read this file", not a crash."""
        path = self.dir / "damaged.pptx"
        make_pptx(path, [(shape("title", run("Hello")), [run("Some notes " * 200)], False, "")], zipfile.ZIP_DEFLATED)
        with zipfile.ZipFile(path) as z:
            info = z.getinfo("ppt/notesSlides/notesSlide1.xml")
        data = bytearray(path.read_bytes())
        start = info.header_offset + 30 + len(info.filename) + len(info.extra)
        for i in range(start + 5, start + 40):
            data[i] ^= 0x5A
        path.write_bytes(data)
        with self.assertRaises(ValueError):
            script.load(path)

    def test_hidden_either_way(self):
        path = self.dir / "talk.pptx"
        make_pptx(path, [(shape("title", run("Shown")), [], False, ""), (shape("title", run("Zero")), [], "0", ""),
                         (shape("title", run("False")), [], "false", "")])
        self.assertEqual([s.title for s in decks.read_pptx(path)], ["Shown"])

    def test_a_cue_is_not_a_title(self):
        deck = decks.to_script([decks.Slide("Intro", ["hello there"]), decks.Slide("[Demo]"), decks.Slide("Wrap up", ["bye"])])
        self.assertEqual([s.title for s in deck.sections], ["Intro", "Slide 2 · [Demo]", "Wrap up"])
        self.assertEqual(len({s.token for s in deck.sections}), 3)  # each slide a section of its own

    def test_markdown_scripts_have_no_clicks(self):
        self.assertEqual(decks.clicks(script.parse("# One\n\nhi\n\n# Two\n\nbye")), [0, 0])


class AdvanceTest(unittest.TestCase):
    COUNTS = [0, 2, 0]

    def walk(self, *actions, start=(0, 0)):
        slide, step = start
        for action in actions:
            slide, step = decks.advance(self.COUNTS, slide, step, action)
        return slide, step

    def test_next_plays_animations_before_moving_on(self):
        self.assertEqual(self.walk("next"), (1, 0))
        self.assertEqual(self.walk("next", "next"), (1, 1))
        self.assertEqual(self.walk("next", "next", "next", "next"), (2, 0))
        self.assertEqual(self.walk("next", "next", "next", "next", "next"), (2, 0))  # stays on the last

    def test_previous_undoes_then_goes_back_with_animations_played(self):
        self.assertEqual(self.walk("previous", start=(2, 0)), (1, 2))
        self.assertEqual(self.walk("previous", "previous", start=(2, 0)), (1, 1))
        self.assertEqual(self.walk("previous", start=(0, 0)), (0, 0))

    def test_first_and_last(self):
        self.assertEqual(self.walk("last", "first", start=(1, 1)), (0, 0))
        self.assertEqual(self.walk("last"), (2, 0))


class FollowTest(unittest.TestCase):
    """A clicker press moves the notes to the slide on screen, but never out from under voice follow."""

    def setUp(self):
        # sections start at tokens 0, 4 and 8: each heading, then its notes
        self.deck = decks.to_script([decks.Slide("Welcome", ["hello there friends"]),
                                     decks.Slide("Market", ["market is big"], clicks=2),
                                     decks.Slide("Thanks", ["thanks all"])])

    def test_next_slide_brings_its_notes(self):
        self.assertEqual(decks.follow(self.deck, 2, 0, 0, "next"), (1, 0, 4))

    def test_already_reading_the_next_slide(self):
        # voice follow carried you into slide 2's notes before you clicked: stay there
        self.assertEqual(decks.follow(self.deck, 6, 0, 0, "next"), (1, 0, 6))

    def test_animation_clicks_leave_the_notes(self):
        state = (1, 0, 6)
        for expected in [(1, 1, 6), (1, 2, 6), (2, 0, 8)]:
            state = decks.follow(self.deck, state[2], state[0], state[1], "next")
            self.assertEqual(state, expected)

    def test_back_a_slide(self):
        self.assertEqual(decks.follow(self.deck, 9, 2, 0, "previous"), (1, 2, 4))  # its animations played

    def test_notes_follow_the_slides_when_they_disagree(self):
        # reading slide 3's notes while slide 1 is on screen; the slides move to 2, so do the notes
        self.assertEqual(decks.follow(self.deck, 9, 0, 0, "next"), (1, 0, 4))

    def test_markdown_sections(self):
        md = script.parse("# One\n\nhi there\n\n# Two\n\nbye now")
        self.assertEqual(decks.follow(md, 1, 0, 0, "next"), (1, 0, md.sections[1].token))


def event(code, value, kind=slidekeys.EV_KEY):
    return slidekeys.EVENT.pack(0, 0, kind, code, value)


class SlideKeysTest(unittest.TestCase):
    def test_decode(self):
        held = set()
        data = (event(109, 1) + event(109, 2) + event(109, 0)  # Page Down: press, repeat, release
                + event(0, 0, kind=0)  # a sync event
                + event(105, 1)  # ←
                + event(29, 1) + event(106, 1) + event(29, 0)  # Ctrl+→: a shortcut, not a slide key
                + event(102, 1) + event(30, 1))  # Home, then A
        self.assertEqual(slidekeys.decode(data, held), ["next", "previous", "first"])
        self.assertEqual(held, set())

    def test_key_codes(self):
        self.assertEqual(slidekeys.key_codes("0"), set())
        self.assertEqual(slidekeys.key_codes("1 0"), {slidekeys.LONG_BITS})
        self.assertEqual(slidekeys.key_codes("3"), {0, 1})

    def test_devices_from_sysfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            sys_root, dev_root = Path(tmp, "sys"), Path(tmp, "dev")
            dev_root.mkdir()

            def add(event, name, vendor, product, codes, readable=True):
                info = sys_root / event / "device"
                (info / "id").mkdir(parents=True)
                (info / "capabilities").mkdir()
                (info / "name").write_text(name + "\n")
                (info / "id" / "vendor").write_text(vendor)
                (info / "id" / "product").write_text(product)
                bitmap = sum(1 << c for c in codes)
                words = [f"{(bitmap >> (64 * i)) & (2 ** 64 - 1):x}" for i in range(2)]
                (info / "capabilities" / "key").write_text(" ".join(reversed(words)))
                (dev_root / event).write_text("")
                os.chmod(dev_root / event, 0o600 if readable else 0o000)

            add("event3", "Keychron Q10 Keyboard", "3434", "01a1", {30, 44, 106, 109})
            add("event2", "Keychron Q10", "3434", "01a1", {30, 44, 106})
            add("event5", "Logitech G502", "046d", "4099", {30, 44, 106})  # a mouse that types, for macros
            add("event9", "Logitech R400", "046d", "c538", {104, 109, 63, 1}, readable=False)
            add("event10", "Logitech R400 Consumer Control", "046d", "c538", {109, 115})
            add("event11", "2.4G Wireless Presenter", "1d57", "ad03", {30, 44, 104, 109})  # says it's a keyboard
            add("event1", "Power Button", "0000", "0001", {116})
            everything = slidekeys.devices(sys_root, dev_root)
            found = slidekeys.clickers(sys_root, dev_root)
        self.assertEqual([d.name for d in everything],
                         ["Keychron Q10", "Logitech G502", "Logitech R400", "2.4G Wireless Presenter"])
        # never a keyboard: only clickers, including one that claims letters but is named as one
        self.assertEqual([(d.id, d.name) for d in found], [("046d:c538", "Logitech R400"), ("1d57:ad03", "2.4G Wireless Presenter")])
        self.assertEqual(found[0].nodes, [f"{dev_root}/event9", f"{dev_root}/event10"])
        if os.geteuid() != 0:
            self.assertEqual([d.readable for d in found], [False, True])

    def test_rule_names_only_this_device(self):
        device = slidekeys.Device("046d:c538", "Logitech R400", ["/dev/input/event9"])
        commands = slidekeys.rule(device)
        self.assertIn('ATTRS{id/vendor}=="046d", ATTRS{id/product}=="c538", TAG+="uaccess"', commands)
        self.assertIn("/etc/udev/rules.d/71-prompter-046d-c538.rules", commands)
        self.assertIn("udevadm trigger --action=change /dev/input/event9", commands)
        virtual = slidekeys.rule(slidekeys.Device("0001:0001", "Virtual Presenter", ["/dev/input/event9"]))
        self.assertIn('ATTRS{name}=="Virtual Presenter"', virtual)  # virtual devices share ids: the name too

    def test_reads_a_device_without_blocking(self):
        """Events written to the device file reach on_action through the main loop."""
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp, "event0")
            os.mkfifo(fifo)
            actions = []
            keys = slidekeys.SlideKeys(actions.append, lambda: None)
            keys._open(str(fifo))
            writer = os.open(fifo, os.O_WRONLY)
            os.write(writer, event(109, 1) + event(109, 0) + event(104, 1))
            deadline = time.monotonic() + 2
            while len(actions) < 2 and time.monotonic() < deadline:
                GLib.MainContext.default().iteration(False)
            os.close(writer)
            keys._close(str(fifo))
        self.assertEqual(actions, ["next", "previous"])


if __name__ == "__main__":
    unittest.main()

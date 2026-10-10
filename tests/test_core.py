import random
import unittest

from prompter.aligner import Aligner
from prompter.script import parse, words_of

SCRIPT = """# Welcome

Hi everyone, and welcome back to the channel. Today we're looking at how to turn
an Elgato Prompter into a proper voice-following teleprompter on Linux. [smile]

<!-- remember to check the mic -->

## The problem

Camera Hub only runs on **Windows** and *macOS*, so on Linux the prompter is just a
second screen. That's fine for reading notes, but it won't scroll along with you.

## The fix

We listen to the microphone, transcribe the last few seconds with Whisper on the GPU,
and line that up against the script. Wherever the words match is where you are.
If you go off script, nothing moves until you come back to it.

- It runs locally
- It never needs the internet
"""


class ScriptTest(unittest.TestCase):
    def setUp(self):
        self.script = parse(SCRIPT)

    def test_blocks_and_sections(self):
        kinds = [b.kind for b in self.script.blocks]
        self.assertEqual(kinds, ["heading", "para", "heading", "para", "heading", "para", "para", "para"])
        self.assertEqual([s.title for s in self.script.sections], ["Welcome", "The problem", "The fix"])

    def test_markdown_and_cues_are_not_spoken(self):
        words = [t.norm for t in self.script.tokens]
        self.assertNotIn("smile", words)
        self.assertNotIn("remember", words)
        self.assertIn("windows", words)
        self.assertNotIn("*", self.script.blocks[3].text)
        self.assertIn("[smile]", self.script.blocks[1].text)  # cues stay visible
        self.assertEqual(len(self.script.blocks[1].cues), 1)

    def test_byte_offsets_point_at_words(self):
        for t in self.script.tokens:
            text = self.script.blocks[t.block].text.encode()[t.start : t.end].decode()
            self.assertEqual(words_of(text), [t.norm])

    def test_other_languages(self):
        script = parse("Ça va très bien, merci. Wir müssen über Straße reden.")
        self.assertEqual([t.norm for t in script.tokens][:4], ["ça", "va", "très", "bien"])
        self.assertEqual(words_of("STRASSE"), words_of("Straße"))
        self.assertEqual(words_of("Don’t stop"), ["dont", "stop"])

    def test_context(self):
        self.assertTrue(self.script.context(0).startswith("Welcome Hi everyone"))


class AlignerTest(unittest.TestCase):
    def setUp(self):
        self.script = parse(SCRIPT)
        self.words = [t.norm for t in self.script.tokens]
        self.spoken = [i for i, t in enumerate(self.script.tokens) if not t.optional]

    def read(self, aligner, upto, noise=0.0, rng=None, adlib=()):
        """Simulate the rolling transcript after the speaker has said spoken[:upto]."""
        heard = [self.words[i] for i in self.spoken[max(0, upto - 14) : upto]]
        if rng:
            heard = [w if rng.random() > noise else rng.choice(["uh", "the", "and", "um", w[:-1] or w]) for w in heard]
        return aligner.update(heard + list(adlib))

    def test_follows_clean_reading(self):
        a = Aligner(self.script.tokens)
        for upto in range(3, len(self.spoken) + 1):
            self.read(a, upto)
            self.assertLessEqual(abs(a.pos - (self.spoken[upto - 1] + 1)), 1, f"at {upto}")

    def test_follows_noisy_reading(self):
        rng = random.Random(4)
        a = Aligner(self.script.tokens)
        lag = []
        for upto in range(3, len(self.spoken) + 1):
            self.read(a, upto, noise=0.2, rng=rng)
            lag.append(self.spoken[upto - 1] + 1 - a.pos)
        self.assertLessEqual(max(abs(x) for x in lag[-20:]), 4)
        self.assertEqual(a.pos, self.spoken[-1] + 1)

    def test_adlib_does_not_move(self):
        a = Aligner(self.script.tokens)
        self.read(a, 20)
        pos = a.pos
        a.update("so yeah i was going to say something else entirely here".split())
        self.assertEqual(a.pos, pos)

    def test_reread_moves_back(self):
        a = Aligner(self.script.tokens)
        self.read(a, 40)
        self.read(a, 25)
        self.assertEqual(a.pos, self.spoken[24] + 1)

    def test_skips_ahead(self):
        a = Aligner(self.script.tokens)
        self.read(a, 10)
        self.read(a, 30)
        self.assertEqual(a.pos, self.spoken[29] + 1)

    def test_silence_and_hallucination(self):
        a = Aligner(self.script.tokens)
        self.read(a, 10)
        pos = a.pos
        a.update(["thank", "you"])
        a.update([])
        self.assertEqual(a.pos, pos)


if __name__ == "__main__":
    unittest.main()

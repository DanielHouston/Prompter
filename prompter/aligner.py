"""Work out where in the script the speaker is from a rolling speech transcript.

Every recognition tick delivers the words heard in the last few seconds. The tail of
that is locally aligned (Smith-Waterman, fuzzy word matching) against a window of the
script around the current position; where the alignment ends is where the speaker is.
Ad-libs simply don't align, so the position holds until the script resumes.
"""

from difflib import SequenceMatcher
from functools import lru_cache

TAIL = 12  # transcript words considered per tick
BACK = 40  # script words searched behind the current position (re-reads)
AHEAD = 80  # and ahead of it (skipped lines)
MATCH = 0.75  # word similarity that counts as the same word

MATCH_SCORE, MISMATCH, GAP, OPTIONAL_GAP = 2.0, -1.0, -1.0, -0.2


@lru_cache(maxsize=65536)
def similarity(heard, scripted):
    if heard == scripted:
        return 1.0
    if not heard or not scripted:
        return 0.0
    if len(heard) >= 3 and scripted.startswith(heard):
        return 0.8  # word still being spoken when the window was cut
    return SequenceMatcher(None, heard, scripted).ratio()


class Aligner:
    def __init__(self, tokens, pos=0):
        self.words = [t.norm for t in tokens]
        self.optional = [t.optional for t in tokens]
        self.pos = min(pos, len(self.words))
        self.last = {}

    def update(self, heard):
        """Feed normalized transcript words; returns the new position if it moved.

        Why it did or didn't move is left in self.last, for diagnostics.
        """
        hyp = heard[-TAIL:]
        self.last = {"pos": self.pos, "candidate": None, "matches": 0}
        if len(hyp) < 2 or not self.words:
            return self._hold("too few words")

        lo = max(0, self.pos - BACK)
        hi = min(len(self.words), self.pos + AHEAD)
        best = self._align(hyp, lo, hi)
        if best is None:
            return self._hold("no alignment")

        new_pos, matches = best
        self.last.update(candidate=new_pos, matches=matches)
        delta = new_pos - self.pos
        if matches < 3:
            return self._hold("weak match")
        if delta > 30 and matches < 5:
            return self._hold("big jump, weak match")  # skips ahead need strong evidence (repeated phrases)
        if delta < 0 and (delta > -3 or matches < 4):
            return self._hold("small/weak backstep")  # rolling-window jitter, or not clearly a re-read
        if delta == 0:
            return self._hold("same place")

        self.pos = new_pos
        self.last["reason"] = "moved"
        return new_pos

    def _hold(self, reason):
        self.last["reason"] = reason

    def _align(self, hyp, lo, hi):
        script = self.words[lo:hi]
        n, m = len(hyp), len(script)
        # score[i][j], matches[i][j]: best local alignment ending at hyp[i-1], script[j-1]
        score = [[0.0] * (m + 1) for _ in range(n + 1)]
        matches = [[0] * (m + 1) for _ in range(n + 1)]

        for i in range(1, n + 1):
            h = hyp[i - 1]
            for j in range(1, m + 1):
                sim = similarity(h, script[j - 1])
                hit = sim >= MATCH
                diag = score[i - 1][j - 1] + (MATCH_SCORE * sim if hit else MISMATCH)
                skip_script = score[i][j - 1] + (OPTIONAL_GAP if self.optional[lo + j - 1] else GAP)
                extra_word = score[i - 1][j] + GAP

                s, k = diag, matches[i - 1][j - 1] + hit
                if skip_script > s:
                    s, k = skip_script, matches[i][j - 1]
                if extra_word > s:
                    s, k = extra_word, matches[i - 1][j]
                if s > 0:
                    score[i][j], matches[i][j] = s, k

        # The alignment must reach the end of what was heard (allowing one stray
        # trailing word), otherwise it describes the past, not where the speaker is now.
        best = None
        for i in (n, n - 1):
            for j in range(1, m + 1):
                s = score[i][j]
                if s <= 0:
                    continue
                pos = lo + j
                key = (s - (0.5 if i < n else 0), -abs(pos - self.pos))
                if best is None or key > best[0]:
                    best = (key, pos, matches[i][j])
        return None if best is None else (best[1], best[2])

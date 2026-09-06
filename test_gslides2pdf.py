import argparse
import contextlib
import io
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

import gslides2pdf


class ArgumentValidationTests(unittest.TestCase):
    def test_positive_int_accepts_positive_values(self):
        self.assertEqual(gslides2pdf.positive_int("3"), 3)

    def test_positive_int_rejects_non_positive_values(self):
        for value in ("0", "-1", "invalid"):
            with self.assertRaises(argparse.ArgumentTypeError):
                gslides2pdf.positive_int(value)

    def test_positive_float_rejects_non_positive_values(self):
        for value in ("0", "-0.1", "invalid"):
            with self.assertRaises(argparse.ArgumentTypeError):
                gslides2pdf.positive_float(value)

    def test_capture_uses_conservative_animation_limits(self):
        import inspect

        parameters = inspect.signature(gslides2pdf.capture).parameters
        self.assertEqual(parameters["poll"].default, 0.1)
        self.assertEqual(parameters["settle"].default, 0.5)
        self.assertEqual(parameters["max_steps"].default, 25)

    def test_non_negative_int_accepts_zero_but_not_negatives(self):
        self.assertEqual(gslides2pdf.non_negative_int("0"), 0)
        self.assertEqual(gslides2pdf.non_negative_int("3"), 3)
        for value in ("-1", "invalid", "1.5"):
            with self.assertRaises(argparse.ArgumentTypeError):
                gslides2pdf.non_negative_int(value)

    def test_slide_selection_parses_numbers_and_ranges(self):
        self.assertEqual(gslides2pdf.slide_selection("3"), frozenset({3}))
        self.assertEqual(gslides2pdf.slide_selection("2,5-7"),
                         frozenset({2, 5, 6, 7}))
        self.assertEqual(gslides2pdf.slide_selection(" 4 , 1 - 2 "),
                         frozenset({1, 2, 4}))

    def test_slide_selection_rejects_invalid_values(self):
        for value in ("", "0", "7-5", "2..4", "last", "-3", "1,x"):
            with self.assertRaises(argparse.ArgumentTypeError):
                gslides2pdf.slide_selection(value)

    def test_aspect_ratio_parses_valid_values(self):
        self.assertEqual(gslides2pdf.aspect_ratio("16:9"), 16 / 9)

    def test_aspect_ratio_rejects_invalid_values(self):
        for value in ("16", "16:0", "16:9:4", "wide:tall"):
            with self.assertRaises(argparse.ArgumentTypeError):
                gslides2pdf.aspect_ratio(value)


class HelperTests(unittest.TestCase):
    def test_presentation_id_accepts_urls_and_bare_ids(self):
        presentation_id = "abcdefghijklmnopqrst"
        self.assertEqual(
            gslides2pdf.presentation_id(
                f"https://docs.google.com/presentation/d/{presentation_id}/edit"
            ),
            presentation_id,
        )
        self.assertEqual(gslides2pdf.presentation_id(
            presentation_id), presentation_id)

    def test_presentation_id_rejects_invalid_input(self):
        with self.assertRaises(ValueError):
            gslides2pdf.presentation_id("not a slides url or id")

    def test_slide_id_from_url(self):
        self.assertEqual(
            gslides2pdf.slide_id_from_url(
                "https://example.test/present?slide=id.p42&rm=minimal"),
            "id.p42",
        )
        self.assertIsNone(gslides2pdf.slide_id_from_url(
            "https://example.test/present"))

    def test_output_filename_uses_a_safe_slides_title(self):
        self.assertEqual(
            gslides2pdf.output_filename("Quarterly Review - Google Slides"),
            "Quarterly Review.pdf",
        )
        self.assertEqual(
            gslides2pdf.output_filename(
                "Quarterly Review - Draft - Presentaciones de Google"),
            "Quarterly Review - Draft.pdf",
        )
        self.assertEqual(
            gslides2pdf.output_filename("A/B: plan?"), "A_B_ plan_.pdf")
        self.assertEqual(
            gslides2pdf.output_filename(" - Google Slides"), "slides.pdf")

    def test_frame_comparison_and_black_screen_detection(self):
        black = Image.new("RGB", (192, 108), "black")
        almost_black = Image.new("RGB", (192, 108), (5, 5, 5))
        white = Image.new("RGB", (192, 108), "white")

        self.assertTrue(gslides2pdf.frames_equal(black, black.copy()))
        self.assertFalse(gslides2pdf.frames_equal(black, white))
        self.assertTrue(gslides2pdf.is_black_screen(almost_black))
        self.assertFalse(gslides2pdf.is_black_screen(white))


def _png_bytes(frame, size=(16, 9)):
    if not isinstance(frame, Image.Image):
        frame = Image.new("RGB", size, frame)
    buf = io.BytesIO()
    frame.save(buf, format="PNG")
    return buf.getvalue()


def _png_data(png):
    """The raw compressed image payload of a PNG, so a test can prove those
    exact bytes came out the other end rather than being re-encoded."""
    data, i = b"", 8
    while i < len(png):
        length, = struct.unpack(">I", png[i:i + 4])
        if png[i + 4:i + 8] == b"IDAT":
            data += png[i + 8:i + 8 + length]
        i += 12 + length
    return data


class FrameTests(unittest.TestCase):
    def test_identical_bytes_match_without_decoding(self):
        # the settle loop compares the same picture over and over, so the
        # common case must never pay for a decode
        png = _png_bytes("white", size=(64, 64))
        a, b = gslides2pdf.Frame(png), gslides2pdf.Frame(png)
        self.assertTrue(a.matches(b))
        self.assertIsNone(a._image)
        self.assertIsNone(b._image)

    def test_differing_bytes_fall_back_to_the_pixel_comparison(self):
        blank = Image.new("RGB", (100, 100), "white")
        dotted = blank.copy()
        ImageDraw.Draw(dotted).point((10, 10), fill="black")

        a = gslides2pdf.Frame(_png_bytes(blank))
        b = gslides2pdf.Frame(_png_bytes(dotted))
        self.assertNotEqual(a.png, b.png)
        # the default tolerance still sees these as the same picture...
        self.assertTrue(a.matches(b))
        # ...while the strict comparison the state machine uses to mean
        # "nothing happened at all" does not
        self.assertFalse(a.matches(b, tol=1e-6))

    def test_release_drops_the_decode_and_keeps_the_bytes(self):
        frame = gslides2pdf.Frame(_png_bytes("white", size=(64, 64)))
        self.assertEqual(frame.image.size, (64, 64))
        self.assertIsNotNone(frame._image)
        frame.release()
        self.assertIsNone(frame._image)
        self.assertEqual(frame.image.size, (64, 64))  # decodes again on demand


class WritePdfTests(unittest.TestCase):
    def test_pages_are_embedded_without_re_encoding(self):
        frames = [gslides2pdf.Frame(_png_bytes(c, size=(120, 80)))
                  for c in ("white", "gray", "red")]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "deck.pdf"
            gslides2pdf.write_pdf(frames, out)
            pdf = out.read_bytes()

        self.assertTrue(pdf.startswith(b"%PDF"))
        for frame in frames:
            self.assertIn(_png_data(frame.png), pdf)
            # writing the PDF must not have decoded anything either
            self.assertIsNone(frame._image)


class _FakeKeyboard:
    def __init__(self, page):
        self._page = page

    def press(self, key):
        self._page.index = min(self._page.index + 1, len(self._page.script) - 1)


class _FakeMouse:
    def click(self, x, y):
        pass


class _FakePage:
    """A fake Playwright page driven by a script of (slide_id, frame) pairs,
    one per keypress (index 0 is the initial state), where frame is either a
    color or a full PIL.Image. Pressing past the end repeats the last entry,
    mirroring Google's static end-of-slideshow screen."""

    def __init__(self, script):
        self.script = script
        self.index = 0
        self.keyboard = _FakeKeyboard(self)
        self.mouse = _FakeMouse()

    @property
    def url(self):
        slide_id = self.script[self.index][0]
        return f"https://docs.google.com/presentation/d/FAKEID000000000000000/present?slide={slide_id}"

    def screenshot(self, type="png"):
        return _png_bytes(self.script[self.index][1])

    def title(self):
        return "Fake Deck - Google Slides"

    def goto(self, *a, **k):
        pass

    def set_viewport_size(self, *a, **k):
        pass

    def bring_to_front(self):
        pass

    def evaluate(self, *a, **k):
        pass


class _FakeResponse:
    ok = False

    def body(self):
        return b""


class _FakeContext:
    def __init__(self, page):
        self._page = page
        self.request = mock.Mock(get=lambda *a, **k: _FakeResponse())

    def new_page(self):
        return self._page

    def close(self):
        pass


class _FakeBrowser:
    def __init__(self, page):
        self._page = page

    def new_context(self, **kwargs):
        return _FakeContext(self._page)

    def close(self):
        pass


class _FakePlaywright:
    def __init__(self, page):
        self._page = page

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def chromium(self):
        return mock.Mock(launch=lambda **k: _FakeBrowser(self._page))


class CaptureStateMachineTests(unittest.TestCase):
    """Regression tests for capture()'s slide/animation state machine, using
    a fake Playwright stack so no real browser is needed."""

    def _run(self, script, n_slides_info=None, **kwargs):
        page = _FakePage(script)
        pages = []

        def fake_write_pdf(frames, out):
            pages.append(list(frames))

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("gslides2pdf.sync_playwright", lambda: _FakePlaywright(page)))
            stack.enter_context(mock.patch("gslides2pdf.write_pdf", fake_write_pdf))
            stack.enter_context(mock.patch("gslides2pdf.time.sleep", lambda *_: None))
            if n_slides_info is not None:
                stack.enter_context(mock.patch("gslides2pdf.deck_info_from_export", return_value=n_slides_info))
            kwargs.setdefault("verbose", False)
            gslides2pdf.capture(
                "https://docs.google.com/presentation/d/FAKEID000000000000000/edit",
                "ignored.pdf",
                poll=0, max_wait=0, settle=0,
                **kwargs,
            )
        return pages[0] if pages else None

    def _run_logged(self, script, **kwargs):
        """_run, but returning capture()'s stderr log alongside the pages."""
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            pages = self._run(script, verbose=True, **kwargs)
        return pages, log.getvalue()

    @staticmethod
    def _color_at(frame):
        return frame.image.getpixel((0, 0))

    def test_black_slide_mid_deck_is_not_mistaken_for_end_of_show(self):
        # slide 1 ends on a legitimate black content frame (e.g. a dark
        # section divider); slide 2 ends and is genuinely followed by
        # Google's black end-of-slideshow screen. With the slide count
        # unknown, both look like "a black frame appeared" -- only the one
        # that stays black across two presses should end the capture.
        script = [
            ("id.p1", "white"),
            ("id.p1", "black"),
            ("id.p2", "gray"),
            ("id.p2", "black"),
        ]
        pages = self._run(script)
        self.assertEqual(len(pages), 2)
        self.assertEqual(self._color_at(pages[0]), (0, 0, 0))
        self.assertEqual(self._color_at(pages[1]), (128, 128, 128))

    def test_dark_animation_step_survives_the_end_of_show_false_alarm(self):
        # slide 1 has a build step that happens to be black. With the slide
        # count unknown, every black frame is a candidate end-of-slideshow
        # screen, so it is held back pending confirmation -- and the next
        # keypress disproves it by showing more of the same slide. That frame
        # is content, and used to be thrown away instead of recorded, quietly
        # losing a page from --all-steps output.
        script = [
            ("id.p1", "white"),
            ("id.p1", "black"),   # dark build step, mistakable for the end
            ("id.p1", "gray"),    # ...disproved: the slide had more to show
            ("id.p2", "blue"),
            ("id.p2", "black"),   # the real end screen, which does repeat
        ]
        pages = self._run(script, all_steps=True)
        self.assertEqual([self._color_at(p) for p in pages],
                         [(255, 255, 255), (0, 0, 0), (128, 128, 128), (0, 0, 255)])

    def test_unknown_slide_count_does_not_drop_or_abort_on_late_change(self):
        # slide 1 hits the per-slide step cap, then genuinely changes right
        # after giving up, then really does move on to slide 2. The slide
        # count is unknown throughout.
        script = [
            ("id.p1", "white"),
            ("id.p1", "white"),
            ("id.p1", "white"),
            ("id.p1", "gray"),
            ("id.p2", "black"),
        ]
        pages = self._run(script, max_steps=2)
        self.assertEqual(len(pages), 2)
        self.assertEqual(self._color_at(pages[0]), (128, 128, 128))
        self.assertEqual(self._color_at(pages[1]), (0, 0, 0))

    def test_known_slide_count_retries_past_a_stuck_slide(self):
        # slide 1 stops responding to ArrowRight for 10 straight presses
        # (e.g. an embedded video grabbed keyboard focus), then recovers and
        # genuinely changes, then really does move on to slide 2. Because the
        # export probe says there are 2 slides and we aren't on the last one
        # yet, giving up after only 3 failed attempts (the old behavior)
        # would have aborted the whole capture and lost slide 2 entirely.
        script = (
            [("id.p1", "white")] * 13
            + [("id.p1", "gray")]
            + [("id.p2", "black")]
        )
        pages = self._run(script, n_slides_info=(2, 16 / 9), max_steps=2)
        self.assertEqual(len(pages), 2)
        self.assertEqual(self._color_at(pages[0]), (128, 128, 128))
        self.assertEqual(self._color_at(pages[1]), (0, 0, 0))

    def test_known_slide_count_fails_loudly_when_truly_stuck(self):
        # slide 1 never responds to ArrowRight again and the deck never
        # advances. The export probe says there are 5 slides, so silently
        # writing a 1-slide PDF and calling it "ok" would hide real data
        # loss; capture() should fail instead of pretending to succeed.
        script = [("id.p1", "white")]
        with self.assertRaises(SystemExit):
            self._run(script, n_slides_info=(5, 16 / 9), max_steps=2)

    def test_hidden_slides_corrects_the_expected_slide_count(self):
        # the export probe counts 3 slides but one of them is hidden, so
        # present mode only ever walks through 2. Told about the hidden slide
        # up front, capture() expects 2 and finishes cleanly.
        script = [
            ("id.p1", "white"),
            ("id.p2", "gray"),
        ]
        pages = self._run(script, n_slides_info=(3, 16 / 9), hidden_slides=1, max_steps=2)
        self.assertEqual(len(pages), 2)
        self.assertEqual(self._color_at(pages[0]), (255, 255, 255))
        self.assertEqual(self._color_at(pages[1]), (128, 128, 128))

    def test_missing_slide_fails_loudly_without_the_hidden_slides_flag(self):
        # the same deck, but nothing told capture() about the hidden slide.
        # Now that the count is meant to be exact, being short by even one
        # slide is a failure rather than a shrug -- the user is pointed at
        # --hidden-slides instead of silently getting a short PDF.
        script = [
            ("id.p1", "white"),
            ("id.p2", "gray"),
        ]
        with self.assertRaises(SystemExit):
            self._run(script, n_slides_info=(3, 16 / 9), max_steps=2)

    def test_steps_for_rolls_out_only_the_named_slides(self):
        # slide 1 and slide 2 both animate; --steps-for must expand exactly
        # the slides named and flatten the rest to their final state.
        script = [
            ("id.p1", "white"),
            ("id.p1", "gray"),
            ("id.p2", "red"),
            ("id.p2", "blue"),
            ("id.p2", "black"),
        ]

        pages = self._run(script, n_slides_info=(2, 16 / 9), step_slides=frozenset({1}))
        self.assertEqual([self._color_at(p) for p in pages],
                         [(255, 255, 255), (128, 128, 128), (0, 0, 255)])

        pages = self._run(script, n_slides_info=(2, 16 / 9), step_slides=frozenset({2}))
        self.assertEqual([self._color_at(p) for p in pages],
                         [(128, 128, 128), (255, 0, 0), (0, 0, 255)])

        pages = self._run(script, n_slides_info=(2, 16 / 9))
        self.assertEqual([self._color_at(p) for p in pages],
                         [(128, 128, 128), (0, 0, 255)])

    def test_stuck_slide_with_no_real_animations_does_not_duplicate_frames(self):
        # slide 1 has no animations at all, and the next slide is hidden, so
        # present mode can never advance past it: every further press just
        # re-renders the exact same frame forever (no id change, no black
        # end screen -- the deck isn't logically over, it's just stuck
        # behind unreachable hidden content). Recording every one of those
        # identical presses as a distinct "animation step" would pad
        # --all-steps output with dozens of duplicate pages. --hidden-slides
        # accounts for the unreachable slide, so this is a clean finish.
        script = [("id.p1", "white")]
        pages = self._run(script, n_slides_info=(2, 16 / 9), hidden_slides=1, all_steps=True)
        self.assertEqual(len(pages), 1)
        self.assertEqual(self._color_at(pages[0]), (255, 255, 255))

    def test_captured_frames_release_their_decoded_copies(self):
        # a decoded frame costs roughly ten times its PNG, and every frame is
        # decoded at least once on the way through the comparisons -- so a
        # long deck only stays affordable if frames hand that copy back once
        # nothing compares against them any more. Only the very last frame,
        # still live as last_frame when the loop ends, may hold one.
        script = [
            ("id.p1", "white"), ("id.p1", "gray"), ("id.p1", "red"),
            ("id.p2", "blue"), ("id.p2", "green"),
            ("id.p3", "yellow"), ("id.p3", "black"),
        ]
        pages = self._run(script, n_slides_info=(3, 16 / 9), all_steps=True)
        self.assertEqual(len(pages), 6)
        self.assertLessEqual(sum(p._image is not None for p in pages), 1)

    def test_steps_for_warns_about_slides_the_deck_never_reaches(self):
        # a typo'd or stale slide number would otherwise look like --steps-for
        # silently doing nothing
        script = [
            ("id.p1", "white"),
            ("id.p2", "gray"),
        ]
        pages, log = self._run_logged(script, n_slides_info=(2, 16 / 9),
                                      step_slides=frozenset({1, 9, 12}))
        self.assertEqual(len(pages), 2)
        self.assertIn("--steps-for names slide(s) 9, 12", log)

    def test_small_area_animation_steps_are_not_dropped(self):
        # a single small element appearing on an otherwise large, unchanged
        # canvas barely moves the *average* pixel value -- comparing whole
        # frames with a diff threshold (the old approach) treats this as "no
        # visible change" and silently drops real steps like a bullet point
        # fading in one at a time. The slide id not changing is what proves
        # there is more to show, regardless of how little the picture moved.
        blank = Image.new("RGB", (100, 100), "white")
        with_dot_a = blank.copy()
        ImageDraw.Draw(with_dot_a).point((10, 10), fill="black")
        with_dot_b = with_dot_a.copy()
        ImageDraw.Draw(with_dot_b).point((20, 20), fill="black")

        # sanity check: a whole-frame diff threshold really would miss these
        self.assertTrue(gslides2pdf.frames_equal(blank, with_dot_a))
        self.assertTrue(gslides2pdf.frames_equal(with_dot_a, with_dot_b))

        script = [
            ("id.p1", blank),
            ("id.p1", with_dot_a),
            ("id.p1", with_dot_b),
            ("id.p2", "gray"),
            ("id.p2", "black"),
        ]
        pages = self._run(script, n_slides_info=(2, 16 / 9), all_steps=True)
        self.assertEqual(len(pages), 4)
        self.assertEqual(pages[0].image.getpixel((10, 10)), (255, 255, 255))
        self.assertEqual(pages[1].image.getpixel((10, 10)), (0, 0, 0))
        self.assertEqual(pages[2].image.getpixel((20, 20)), (0, 0, 0))
        self.assertEqual(self._color_at(pages[3]), (128, 128, 128))


if __name__ == "__main__":
    unittest.main()

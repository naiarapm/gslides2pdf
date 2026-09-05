import argparse
import unittest

from PIL import Image

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


if __name__ == "__main__":
    unittest.main()

# gslides2pdf

Render a Google Slides presentation to a rasterized PDF using Google Slides present mode. Animations are flattened to their final visible state by default, or can be exported as one PDF page per build step — for the whole deck, or for a chosen subset of slides.

## Requirements

- Python 3.9+
- Chromium installed through Playwright
- On Linux, the system libraries required by Playwright Chromium

## Install

This example installs the project in an existing virtual environment while making `gs2pdf` available from any directory:

```bash
cd /path/to/gslides2pdf
~/.venv/gslides2pdf/bin/pip install -e .
mkdir -p ~/.local/bin
ln -sf ~/.venv/gslides2pdf/bin/gs2pdf ~/.local/bin/gs2pdf
~/.venv/gslides2pdf/bin/playwright install chromium
~/.venv/gslides2pdf/bin/playwright install-deps chromium
```

The final command may request `sudo`. Ensure `~/.local/bin` is included in your `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Add that export to your shell configuration file if needed.

## Usage

Export a publicly shared deck:

```bash
gs2pdf "https://docs.google.com/presentation/d/PRESENTATION_ID/edit"
```

When `-o` is omitted, the output filename is the presentation title with a `.pdf` extension. Specify a path explicitly when needed:

```bash
gs2pdf "https://docs.google.com/presentation/d/PRESENTATION_ID/edit" -o deck.pdf
```

A bare presentation ID also works:

```bash
gs2pdf PRESENTATION_ID
```

## Animation Steps

By default every slide contributes a single page showing its final state. To write one page per visible animation state instead, for every slide in the deck:

```bash
gs2pdf "PRESENTATION_ID" --all-steps
```

To roll out only certain slides and keep the rest flattened, list them with `--steps-for`. It accepts individual numbers and ranges:

```bash
gs2pdf "PRESENTATION_ID" --steps-for 2,5-7
```

That builds out slides 2, 5, 6 and 7 step by step, while every other slide appears once in its final state. Slide numbers are 1-based positions in the presentation *as presented*, which is also the order of the slides in the output PDF; hidden slides are not counted, since present mode never shows them. `--all-steps` and `--steps-for` cannot be combined.

## Hidden Slides

To know how many slides to expect, the tool asks Google for the deck's own PDF export and counts its pages. That count includes hidden slides, but present mode skips them, so a deck with hidden slides ends the export short of the expected count and is reported as an incomplete capture.

State the number of hidden slides to correct the count up front:

```bash
gs2pdf "PRESENTATION_ID" --hidden-slides 3
```

The expected slide count is then exact, so a short capture reliably means content was genuinely missed rather than skipped by design.

## Private Presentations

Sign in once using a persistent browser profile:

```bash
gs2pdf --profile ~/.gslides-profile --login
```

After signing in and closing the browser window, use the same profile for exports:

```bash
gs2pdf --profile ~/.gslides-profile "https://docs.google.com/presentation/d/PRESENTATION_ID/edit"
```

## Useful Options

```text
-o, --output PATH       Set the PDF output path.
--all-steps             Write one page per visible animation state, for every slide.
--steps-for SLIDES      Do that for these slides only, for example 2,5-7.
--hidden-slides N       Number of hidden slides in the deck, excluded from the expected slide count.
--headed                Show the browser while rendering.
--channel chrome        Use an installed browser instead of Playwright Chromium.
--width PIXELS          Set logical slide width (default: 1920).
--aspect WIDTH:HEIGHT   Override the aspect ratio, for example 16:9 or 4:3.
--scale FACTOR          Set device scale factor for sharper output (default: 2).
--max-steps COUNT       Assume something is stuck after this many animation steps on one slide (default: 25).
--debug-dir DIRECTORY   Save captured PNG frames for inspection.
-q, --quiet             Suppress progress output.
```

Run `gs2pdf --help` for every option, including animation timing controls.

## Notes

The output PDF contains raster images rather than editable vector slide content. Google Slides must be able to open the presentation in present mode using the current browser session.

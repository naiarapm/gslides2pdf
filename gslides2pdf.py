#!/usr/bin/env python3
"""
gslides2pdf.py — render a Google Slides deck to PDF with animations flattened
to their FINAL state (i.e. what is on screen right before you leave the slide),
or, with --all-steps / --steps-for, with one page per animation state so the PDF
replays the build sequence.

Works by driving Google's own present mode in headless Chromium, so fonts and
layout are exactly what a viewer sees. Output pages are raster images.

Setup (Linux):
    # install into an existing virtual environment, without activating it
    ~/.venv/gslides2pdf/bin/pip install -e .
    mkdir -p ~/.local/bin
    ln -sf ~/.venv/gslides2pdf/bin/gs2pdf ~/.local/bin/gs2pdf
    ~/.venv/gslides2pdf/bin/playwright install chromium
    ~/.venv/gslides2pdf/bin/playwright install-deps chromium  # system libs, needs sudo

    # ~/.local/bin must be on PATH (it is included by default on many Linux systems)

Usage:
    # public ("anyone with the link") deck
    gs2pdf "https://docs.google.com/presentation/d/<ID>/edit" -o deck.pdf

    # private deck: log in once (opens a real window), then reuse the profile
    gs2pdf --profile ~/.gslides-profile --login
    gs2pdf --profile ~/.gslides-profile "<url>" -o deck.pdf

    # one page per animation step (initial state + every visible change)
    gs2pdf "<url>" -o deck-steps.pdf --all-steps

    # roll out the builds of slides 2 and 5..7 only; every other slide is
    # flattened to its final state as usual
    gs2pdf "<url>" -o deck.pdf --steps-for 2,5-7

    # the deck has 3 hidden slides that present mode never shows, but Google's
    # PDF export still counts them
    gs2pdf "<url>" -o deck.pdf --hidden-slides 3
"""

import argparse
import io
import re
import sys
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

try:
    from playwright.sync_api import sync_playwright, Error as PlaywrightError
except ImportError:
    sys.exit("playwright not installed: pip install playwright && playwright install chromium")

try:
    import img2pdf
except ImportError:
    sys.exit("img2pdf not installed: pip install img2pdf")

try:
    from pypdf import PdfReader
except ImportError:
    sys.exit("pypdf not installed: pip install pypdf")


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def _positive_number(cast, error_message: str, value: str):
    try:
        number = cast(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(error_message) from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def positive_int(value: str) -> int:
    return _positive_number(int, "must be an integer", value)


def positive_float(value: str) -> float:
    return _positive_number(float, "must be a number", value)


def non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return number


def slide_selection(value: str) -> frozenset:
    """Parse "2,5-7" into {2, 5, 6, 7}: 1-based slide numbers as presented."""
    slides = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", part)
        if not m:
            raise argparse.ArgumentTypeError(
                "must be a comma-separated list of slide numbers or ranges, such as 2,5-7")
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        if start < 1:
            raise argparse.ArgumentTypeError("slide numbers start at 1")
        if end < start:
            raise argparse.ArgumentTypeError(f"range {start}-{end} ends before it starts")
        slides.update(range(start, end + 1))
    if not slides:
        raise argparse.ArgumentTypeError("must name at least one slide")
    return frozenset(slides)


def aspect_ratio(value: str) -> float:
    try:
        width, height = value.split(":")
        ratio = positive_float(width) / positive_float(height)
    except (ValueError, argparse.ArgumentTypeError) as exc:
        raise argparse.ArgumentTypeError(
            "must be WIDTH:HEIGHT, such as 16:9") from exc
    return ratio


def presentation_id(url: str) -> str:
    m = re.search(r"/presentation/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        bare_id = re.fullmatch(r"[a-zA-Z0-9_-]{20,}", url.strip())
        if not bare_id:
            raise ValueError(f"Could not find a presentation id in: {url}")
        return bare_id.group(0)
    return m.group(1)


def slide_id_from_url(url: str):
    m = re.search(r"slide=([^&#]+)", url)
    return m.group(1) if m else None


def output_filename(title: str) -> str:
    title = re.sub(r"\s+-\s+[^-]+$", "", title).strip()
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip().rstrip(".")
    return f"{title or 'slides'}.pdf"


def to_image(png_bytes: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png_bytes)).convert("RGB")


def frames_equal(a: Image.Image, b: Image.Image, tol: float = 0.5) -> bool:
    """True if two frames are visually identical (mean channel diff < tol/255)."""
    if a.size != b.size:
        return False
    diff = ImageChops.difference(a, b)
    return max(ImageStat.Stat(diff).mean) < tol


def is_black_screen(img: Image.Image) -> bool:
    """Google's end-of-slideshow screen is (almost) entirely black."""
    small = img.resize((64, 36))
    st = ImageStat.Stat(small)
    return max(st.mean) < 12 and max(st.stddev) < 20


class Frame:
    """One captured screenshot, kept as the PNG bytes the browser produced.

    Those bytes are what eventually goes into the PDF (img2pdf embeds them
    unchanged), so decoding is only ever needed to compare pixels -- and a
    decoded frame costs about ten times the memory of the PNG it came from,
    which adds up fast over a long deck. So the decode is lazy, cached only
    while the frame is still being compared against, and dropped with
    release() once the frame is just waiting to be written out."""

    __slots__ = ("png", "_image")

    def __init__(self, png: bytes):
        self.png = png
        self._image = None

    @property
    def image(self) -> Image.Image:
        if self._image is None:
            self._image = to_image(self.png)
        return self._image

    def release(self):
        """Drop the decoded copy; .image simply decodes again if asked."""
        self._image = None

    def matches(self, other: "Frame", tol: float = 0.5) -> bool:
        """Same picture as `other`? Identical PNG bytes settle it without
        decoding anything -- which is the common case, since waiting for a
        slide to settle means comparing frames that are byte-for-byte the
        same over and over."""
        if self.png == other.png:
            return True
        return frames_equal(self.image, other.image, tol)


def deck_info_from_export(context, pid: str):
    """Use Google's PDF export (same cookies as the browser) to learn the
    number of slides and their aspect ratio. Returns (n_slides, aspect) or None."""
    try:
        r = context.request.get(f"https://docs.google.com/presentation/d/{pid}/export/pdf", timeout=60_000)
        if not r.ok:
            return None
        # every body() call is a fresh round-trip to the browser process, and
        # the export of a big deck runs to several MB -- so fetch it once
        body = r.body()
        if not body.startswith(b"%PDF"):
            return None
        reader = PdfReader(io.BytesIO(body))
        box = reader.pages[0].mediabox
        return len(reader.pages), float(box.width) / float(box.height)
    except Exception:
        return None


# ----------------------------------------------------------------------------
# core
# ----------------------------------------------------------------------------

def wait_settled(page, poll: float, max_wait: float, settle: float) -> Frame:
    """Screenshot repeatedly until the picture has stayed identical for
    `settle` seconds (animation finished, incl. delayed ones) or until
    max_wait elapses. Returns the last frame.

    By definition this loop only ends after several consecutive identical
    frames, and Frame.matches() recognises those from their bytes alone, so
    the wait costs nothing beyond the screenshots themselves."""
    t0 = time.time()
    prev = None
    stable_since = None
    while True:
        cur = Frame(page.screenshot(type="png"))
        now = time.time()
        if prev is not None and cur.matches(prev):
            if stable_since is None:
                stable_since = now
            if now - stable_since >= settle:
                return cur
        else:
            stable_since = None
        prev = cur
        if now - t0 > max_wait:
            return cur
        time.sleep(poll)


def capture(url, out,
            profile=None, channel=None, headless=True,
            width=1920, aspect=None, scale=2,
            poll=0.1, max_wait=15.0, settle=0.5, max_steps=25,
            hidden_slides=0, all_steps=False, step_slides=None,
            debug_dir=None, verbose=True):
    try:
        pid = presentation_id(url)
    except ValueError as exc:
        sys.exit(str(exc))
    log = (lambda *a: print(*a, file=sys.stderr)) if verbose else (lambda *a: None)

    with sync_playwright() as p:
        launch = dict(headless=headless)
        if channel:
            launch["channel"] = channel

        # device_scale_factor is fixed for the life of a context, but viewport
        # size can be changed later once the aspect ratio is known -- so one
        # context (and one browser launch) covers both the export-endpoint
        # probe and the real capture.
        if profile:
            browser = None
            context = p.chromium.launch_persistent_context(str(profile), **launch, device_scale_factor=scale)
        else:
            browser = p.chromium.launch(**launch)
            context = browser.new_context(device_scale_factor=scale)

        try:
            info = deck_info_from_export(context, pid)
            n_slides = None
            if info:
                n_slides, exported_aspect = info
                if aspect is None:
                    aspect = exported_aspect
                log(f"[info] export says {n_slides} slides, aspect {aspect:.4f}")
                if hidden_slides:
                    # The export counts hidden slides, present mode never shows
                    # them. Correcting the target up front keeps every later
                    # decision (on_last, the stuck-slide recovery, the final
                    # completeness check) working from the real number.
                    if hidden_slides >= n_slides:
                        log(f"[warn] --hidden-slides {hidden_slides} is not smaller than the "
                            f"{n_slides} slides the export reports; assuming 1 visible slide")
                    n_slides = max(n_slides - hidden_slides, 1)
                    log(f"[info] minus {hidden_slides} hidden slide(s): expecting {n_slides}")
            else:
                log("[info] could not read PDF export (private deck without profile, or pypdf missing); falling back to heuristics")
                if hidden_slides:
                    log("[info] --hidden-slides has nothing to correct: the slide count is unknown")
            if aspect is None:
                aspect = 16 / 9
            height = round(width / aspect)

            # viewport matches the slide aspect so there is no letterboxing
            page = context.new_page()
            page.set_viewport_size({"width": width, "height": height})

            page.goto(f"https://docs.google.com/presentation/d/{pid}/present?slide=id.p", wait_until="load", timeout=120000)

            # wait until present mode has put a slide id in the url
            for _ in range(100):
                if slide_id_from_url(page.url):
                    break
                time.sleep(0.2)
            if "accounts.google.com" in page.url:
                sys.exit("Redirected to Google sign-in: the deck is private. Run once with --profile DIR --login, then retry with --profile DIR.")
            if not slide_id_from_url(page.url):
                sys.exit("Present mode did not start (no slide id in URL). Is the deck shared, or the link correct?")
            if out is None:
                out = output_filename(page.title())
                log(f"[info] output file: {out}")

            time.sleep(1.0)  # let the initial 'presenting' overlay fade

            def focus_presentation():
                page.bring_to_front()
                page.evaluate("window.focus()")

            finals = []  # one entry per slide: list of frames to emit
            current = slide_id_from_url(page.url)
            last_frame = wait_settled(page, poll, max_wait, settle)
            # every distinct visual state of this slide
            slide_frames = [last_frame]
            steps = 0  # keypresses on the current slide
            duplicate_streak = 0  # consecutive keypresses with a bit-for-bit identical frame
            advance_requested = False
            advance_attempts = 0
            pending_black_frame = None  # unconfirmed candidate end-of-slideshow frame
            log(f"[slide 1] id={current}")

            def dump(tag, frame):
                if debug_dir:
                    path = Path(debug_dir) / f"s{len(finals) + 1:02d}_{tag}.png"
                    path.write_bytes(frame.png)

            def record(frame):
                """Make `frame` this slide's newest state. Whatever it replaces
                stays in slide_frames, but nothing compares against it again,
                so it can drop its decoded copy and keep just the PNG."""
                nonlocal last_frame
                last_frame.release()
                last_frame = frame
                slide_frames.append(frame)

            def unpend_black_frame():
                """The tentative end-of-slideshow frame turned out to be
                content after all, so record it as the animation step it
                always was -- it is a keypress that changed the picture on a
                slide present mode had not finished, which is the whole
                definition of a step here."""
                nonlocal pending_black_frame, steps
                if pending_black_frame is None:
                    return
                steps += 1
                log(f"    keypress {steps}: animation step (dark frame, not the end)")
                dump(f"step{steps:02d}", pending_black_frame)
                record(pending_black_frame)
                pending_black_frame = None

            def rolled_out(number: int) -> bool:
                """Should slide `number` (1-based, as presented) contribute one
                page per animation state instead of only its final state?"""
                return all_steps or (step_slides is not None and number in step_slides)

            def finish_slide(reason=""):
                number = len(finals) + 1
                emitted = slide_frames if rolled_out(number) else [slide_frames[-1]]
                finals.append(emitted)
                log(f"[slide {number}] done after {steps} keypress(es), "
                    f"{len(slide_frames)} state(s) -> {len(emitted)} page(s){reason}")

            dump("initial", last_frame)

            while True:
                focus_presentation()
                page.keyboard.press("ArrowRight")
                time.sleep(poll)
                frame = wait_settled(page, poll, max_wait, settle)
                new_id = slide_id_from_url(page.url)
                on_last = n_slides is not None and len(finals) + 1 >= n_slides

                if new_id != current:
                    # we left the slide: a still-unconfirmed black frame was
                    # real content after all, not the end-of-slideshow screen
                    unpend_black_frame()
                    finish_slide()
                    if n_slides and len(finals) >= n_slides:
                        break
                    last_frame.release()
                    current, last_frame, steps, duplicate_streak = new_id, frame, 0, 0
                    advance_requested = False
                    advance_attempts = 0
                    slide_frames = [frame]
                    log(f"[slide {len(finals) + 1}] id={current}")
                    dump("initial", frame)
                    continue

                # Google's end-of-slideshow screen is a black frame that never
                # changes again. Only n_slides is None or on_last (we can't
                # rule out being at the very end) even considers it; and a
                # black frame only counts once it is *stable* across two
                # presses, so a dark build step that is still changing isn't
                # mistaken for the end (which would otherwise truncate any
                # deck with a dark section-divider slide).
                might_be_end = (on_last or n_slides is None) and is_black_screen(frame.image)

                if pending_black_frame is not None:
                    if might_be_end and frame.matches(pending_black_frame):
                        finish_slide(" (end of slideshow)")
                        break
                    # false alarm: the picture moved on, so the earlier black
                    # frame was itself a real (if brief) animation step. Folding
                    # it in also makes it `last_frame`, so the frame in hand is
                    # compared against the state actually before it.
                    unpend_black_frame()

                if might_be_end and not frame.matches(last_frame):
                    pending_black_frame = frame
                    log("    possible end-of-slideshow screen; confirming...")
                    continue

                # Once a limit is reached, keep pressing until Google reports the
                # next slide. This avoids aborting a deck because a presentation
                # has more builds than expected.
                if advance_requested:
                    if not frame.matches(last_frame, tol=1e-6):
                        # a real change slipped in after we'd already given up
                        # on this slide -- record it instead of dropping it
                        steps += 1
                        log(f"    keypress {steps}: animation step (after giving up)")
                        dump(f"step{steps:02d}", frame)
                        record(frame)
                        advance_attempts = 0
                        continue
                    advance_attempts += 1
                    if on_last:
                        finish_slide(" (limit reached; no next slide)")
                        break
                    # If the export says more slides exist, this isn't a normal
                    # end-of-slide give-up -- ArrowRight is probably not
                    # reaching the page at all (e.g. an embedded video/iframe
                    # grabbed keyboard focus). Try harder to recover instead of
                    # quietly abandoning the rest of the deck: a real mouse
                    # click can reclaim focus in a way window.focus() cannot,
                    # and we allow far more attempts since we know there is
                    # more content to reach.
                    stuck_limit = 3 if n_slides is None else 15
                    if advance_attempts >= stuck_limit:
                        finish_slide(" (advance did not change slide)")
                        break
                    if n_slides is not None:
                        try:
                            page.mouse.click(width // 2, height // 2)
                        except PlaywrightError:
                            pass
                    log(f"    advance attempt {advance_attempts}: slide did not change")
                    continue

                # The slide id didn't change, so present mode still had more
                # to show here -- capture it even if the picture barely
                # moved. A fade-in bullet or a one-pixel color shift is a
                # real, meaningful step just as much as a big visual change,
                # and filtering on "how much" changed is how those got lost.
                # A frame that is *exactly* (bit-for-bit) identical to the
                # last one is different: that means nothing happened at all,
                # e.g. a hidden slide right after this one that present mode
                # can never navigate into, which otherwise repeats the same
                # screenshot forever. Two identical presses in a row (not
                # just one, in case wait_settled returned early during a
                # delayed animation's pause) means it's time to give up on
                # this slide via the same advance/recovery path used for
                # max_steps, rather than recording dozens of fake steps.
                if frame.matches(last_frame, tol=1e-6):
                    duplicate_streak += 1
                    if duplicate_streak >= 2:
                        log(f"    keypress: identical frame {duplicate_streak} times in a row; advancing")
                        advance_requested = True
                    continue
                duplicate_streak = 0

                steps += 1
                log(f"    keypress {steps}: animation step")
                dump(f"step{steps:02d}", frame)
                record(frame)

                if steps >= max_steps:
                    log(f"    keypress {steps}: max steps reached; advancing")
                    advance_requested = True

            if step_slides:
                unreached = sorted(n for n in step_slides if n > len(finals))
                if unreached:
                    log(f"[warn] --steps-for names slide(s) "
                        f"{', '.join(str(n) for n in unreached)}, but the deck ends at "
                        f"slide {len(finals)}; those were ignored")

            # n_slides is now the number of slides present mode should actually
            # walk through (the export count, minus any --hidden-slides), so
            # coming up short at all means capture really did stall partway.
            missing = (n_slides - len(finals)) if n_slides is not None else 0
            truncated = missing > 0
            if truncated:
                log(f"[warn] captured {len(finals)} slides but expected {n_slides}")
        finally:
            context.close()
            if browser:
                browser.close()

    pages = [f for slide in finals for f in slide]
    write_pdf(pages, out)
    if truncated:
        sys.exit(f"[error] wrote {out} but only captured {len(finals)} of {n_slides} slides -- "
                 "output is incomplete. Advancing stopped responding partway through; "
                 "try --headed to see what stalled it, or pass --hidden-slides N if the "
                 "deck has slides present mode never shows.")
    log(f"[ok] wrote {out}: {len(finals)} slides, {len(pages)} pages")


def write_pdf(frames, out):
    """Chromium screenshots are opaque PNGs, which img2pdf embeds as they are,
    so the pages go into the PDF without ever being re-encoded."""
    Path(out).write_bytes(img2pdf.convert([f.png for f in frames]))


def do_login(profile, channel=None):
    with sync_playwright() as p:
        launch = dict(headless=False)
        if channel:
            launch["channel"] = channel
        ctx = p.chromium.launch_persistent_context(str(profile), **launch)
        page = ctx.new_page()
        page.goto("https://accounts.google.com/")
        print("Sign in to Google in the window that opened, then close the window.", file=sys.stderr)
        try:
            page.wait_for_event("close", timeout=0)
        except PlaywrightError:
            pass
        try:
            ctx.close()
        except PlaywrightError:
            pass


# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", help="Google Slides URL (or bare presentation id)")
    ap.add_argument("-o", "--output", help="PDF output path (default: Google Slides title with a .pdf extension)")
    steps_mode = ap.add_mutually_exclusive_group()
    steps_mode.add_argument("--all-steps", action="store_true", help="emit one page per animation state (initial + each visible step) instead of only the final state, for every slide")
    steps_mode.add_argument("--steps-for", type=slide_selection, metavar="SLIDES",
                            help="roll out the animation steps of these slides only, e.g. '2,5-7'; "
                                 "every other slide is flattened to its final state. Numbers are "
                                 "1-based positions in the presentation as presented")
    ap.add_argument("--profile", type=Path, help="persistent browser profile dir (for private decks)")
    ap.add_argument("--login", action="store_true", help="open a window to sign in, store in --profile, exit")
    ap.add_argument("--channel", help="use installed browser instead of bundled Chromium, e.g. 'chrome'")
    ap.add_argument("--headed", action="store_true", help="show the browser while capturing")
    ap.add_argument("--width", type=positive_int, default=1920, help="logical slide width in px (default 1920)")
    ap.add_argument("--aspect", type=aspect_ratio, help="aspect ratio like 16:9 or 4:3 (auto-detected when possible)")
    ap.add_argument("--scale", type=positive_int, default=2, help="render scale factor for sharpness (default 2)")
    ap.add_argument("--poll", type=positive_float, default=0.1, help="seconds between screenshots while waiting (default: 0.1)")
    ap.add_argument("--settle", type=positive_float, default=0.5, help="seconds the picture must stay unchanged to count as finished (default: 0.5)")
    ap.add_argument("--max-wait", type=positive_float, default=15.0, help="max seconds to wait for one animation step")
    ap.add_argument("--max-steps", type=positive_int, default=25, help="max animation steps per slide before assuming something is stuck (default: 25)")
    ap.add_argument("--hidden-slides", type=non_negative_int, default=0, metavar="N",
                    help="number of hidden slides in the deck: Google's PDF export counts them but "
                         "present mode never shows them, so subtracting them keeps the expected "
                         "slide count right from the start (default: 0)")
    ap.add_argument("--debug-dir", type=Path, help="dump every captured frame as PNG here")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args()

    if a.login:
        if not a.profile:
            sys.exit("--login requires --profile DIR")
        do_login(a.profile, a.channel)
        return
    if not a.url:
        ap.error("url is required")
    if a.debug_dir:
        a.debug_dir.mkdir(parents=True, exist_ok=True)

    capture(a.url, a.output, profile=a.profile, channel=a.channel, headless=not a.headed,
            width=a.width, aspect=a.aspect, scale=a.scale, poll=a.poll,
            max_wait=a.max_wait, settle=a.settle, max_steps=a.max_steps,
            hidden_slides=a.hidden_slides, debug_dir=a.debug_dir,
            all_steps=a.all_steps, step_slides=a.steps_for, verbose=not a.quiet)


if __name__ == "__main__":
    main()

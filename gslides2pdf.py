#!/usr/bin/env python3
"""
gslides2pdf.py — render a Google Slides deck to PDF with animations flattened
to their FINAL state (i.e. what is on screen right before you leave the slide),
or, with --all-steps, with one page per animation state so the PDF replays the
build sequence.

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
"""

import argparse
import io
import re
import sys
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright not installed: pip install playwright && playwright install chromium")


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def aspect_ratio(value: str) -> float:
    try:
        width, height = value.split(":")
        ratio = positive_float(width) / positive_float(height)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be WIDTH:HEIGHT, such as 16:9") from exc
    return ratio


def presentation_id(url: str) -> str:
    m = re.search(r"/presentation/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        bare_id = re.fullmatch(r"[a-zA-Z0-9_-]{20,}", url.strip())
        if not bare_id:
            sys.exit(f"Could not find a presentation id in: {url}")
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


def deck_info_from_export(context, pid: str):
    """Use Google's PDF export (same cookies as the browser) to learn the
    number of slides and their aspect ratio. Returns (n_slides, aspect) or None."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        r = context.request.get(f"https://docs.google.com/presentation/d/{pid}/export/pdf", timeout=60_000)
        if not r.ok or not r.body().startswith(b"%PDF"):
            return None
        reader = PdfReader(io.BytesIO(r.body()))
        box = reader.pages[0].mediabox
        return len(reader.pages), float(box.width) / float(box.height)
    except Exception:
        return None


# ----------------------------------------------------------------------------
# core
# ----------------------------------------------------------------------------

def wait_settled(page, poll: float, max_wait: float, settle: float):
    """Screenshot repeatedly until the picture has stayed identical for
    `settle` seconds (animation finished, incl. delayed ones) or until
    max_wait elapses. Returns the last frame."""
    t0 = time.time()
    prev = None
    stable_since = None
    while True:
        cur = to_image(page.screenshot(type="png"))
        now = time.time()
        if prev is not None and frames_equal(cur, prev):
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
            poll=0.3, max_wait=15.0, settle=1.2, max_steps=80,
            debug_dir=None, all_steps=False, verbose=True):
    pid = presentation_id(url)
    log = (lambda *a: print(*a, file=sys.stderr)) if verbose else (lambda *a: None)

    with sync_playwright() as p:
        launch = dict(headless=headless)
        if channel:
            launch["channel"] = channel

        def make_context(ctx_args):
            if profile:
                return None, p.chromium.launch_persistent_context(str(profile), **launch, **ctx_args)
            b = p.chromium.launch(**launch)
            return b, b.new_context(**ctx_args)

        # first, a throwaway context just to ask the export endpoint about the deck
        browser, context = make_context({})
        n_slides = None
        info = deck_info_from_export(context, pid)
        context.close()
        if browser:
            browser.close()

        if info:
            n_slides, exported_aspect = info
            if aspect is None:
                aspect = exported_aspect
            log(f"[info] export says {n_slides} slides, aspect {aspect:.4f}")
        else:
            log("[info] could not read PDF export (private deck without profile, or pypdf missing); falling back to heuristics")
        if aspect is None:
            aspect = 16 / 9
        height = round(width / aspect)

        # real context: viewport matches the slide aspect so there is no letterboxing,
        # device_scale_factor gives hi-dpi pixels for a crisp PDF
        browser, context = make_context({
            "viewport": {"width": width, "height": height},
            "device_scale_factor": scale,
        })
        page = context.new_page()

        page.goto(f"https://docs.google.com/presentation/d/{pid}/present?slide=id.p", wait_until="load", timeout=120000)

        # wait until present mode has put a slide id in the url
        for _ in range(100):
            if slide_id_from_url(page.url):
                break
            time.sleep(0.2)
        if "accounts.google.com" in page.url:
            sys.exit("Redirected to Google sign-in: the deck is private. "
                     "Run once with --profile DIR --login, then retry with --profile DIR.")
        if not slide_id_from_url(page.url):
            sys.exit("Present mode did not start (no slide id in URL). "
                     "Is the deck shared, or the link correct?")
        if out is None:
            out = output_filename(page.title())
            log(f"[info] output file: {out}")

        time.sleep(1.0)  # let the initial 'presenting' overlay fade

        finals = []             # one entry per slide: list of frames to emit
        current = slide_id_from_url(page.url)
        last_frame = wait_settled(page, poll, max_wait, settle)
        # every distinct visual state of this slide
        slide_frames = [last_frame]
        steps = 0               # keypresses on the current slide
        idle = 0                # consecutive keypresses that changed nothing
        log(f"[slide 1] id={current}")

        def dump(tag, img):
            if debug_dir:
                img.save(Path(debug_dir) / f"s{len(finals) + 1:02d}_{tag}.png")

        def finish_slide(reason=""):
            finals.append(slide_frames if all_steps else [slide_frames[-1]])
            log(f"[slide {len(finals)}] done after {steps} keypress(es), "
                f"{len(slide_frames)} state(s){reason}")

        dump("initial", last_frame)

        while True:
            page.keyboard.press("ArrowRight")
            time.sleep(poll)
            frame = wait_settled(page, poll, max_wait, settle)
            new_id = slide_id_from_url(page.url)
            on_last = n_slides is not None and len(finals) + 1 >= n_slides

            if new_id != current:
                # we left the slide: slide_frames[-1] was its final state
                finish_slide()
                if n_slides and len(finals) >= n_slides:
                    break
                current, last_frame, steps, idle = new_id, frame, 0, 0
                slide_frames = [frame]
                log(f"[slide {len(finals) + 1}] id={current}")
                dump("initial", frame)
                continue

            # Same slide id after a keypress. On a slide we KNOW is not the
            # last one this can only be an animation step (possibly one with
            # no visible effect), so keep going. Only on the last slide (or
            # when the slide count is unknown) do we look for the end.
            steps += 1
            if steps > max_steps:
                sys.exit(
                    f"More than {max_steps} keypresses on slide {len(finals) + 1}; aborting (raise --max-steps if this is legitimate).")

            if frames_equal(frame, last_frame):
                idle += 1
                log(f"    keypress {steps}: no visible change ({idle} in a row)")
                if on_last and idle >= 2:
                    finish_slide(" (no further change)")
                    break
                if n_slides is None and idle >= 4:
                    finish_slide(" (no further change, slide count unknown)")
                    break
                continue

            idle = 0
            if (on_last or n_slides is None) and is_black_screen(frame) and not is_black_screen(last_frame):
                # end-of-slideshow screen: previous frame was the real final state
                finish_slide(" (end of slideshow)")
                break

            log(f"    keypress {steps}: animation step")
            dump(f"step{steps:02d}", frame)
            last_frame = frame
            slide_frames.append(frame)

        if n_slides and len(finals) != n_slides:
            log(f"[warn] captured {len(finals)} slides but export has {n_slides}")

        context.close()
        if browser:
            browser.close()

    pages = [f for slide in finals for f in slide]
    write_pdf(pages, out)
    log(f"[ok] wrote {out}: {len(finals)} slides, {len(pages)} pages")


def write_pdf(frames, out):
    out = Path(out)
    try:
        import img2pdf  # lossless
        bufs = []
        for f in frames:
            b = io.BytesIO()
            f.save(b, format="PNG")
            bufs.append(b.getvalue())
        out.write_bytes(img2pdf.convert(bufs))
    except ImportError:
        frames[0].save(out, "PDF", save_all=True, append_images=frames[1:], resolution=150)


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
        except Exception:
            pass
        ctx.close()


# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", help="Google Slides URL (or bare presentation id)")
    ap.add_argument("-o", "--output", help="PDF output path (default: Google Slides title with a .pdf extension)")
    ap.add_argument("--all-steps", action="store_true", help="emit one page per animation state (initial + each visible step) instead of only the final state")
    ap.add_argument("--profile", type=Path, help="persistent browser profile dir (for private decks)")
    ap.add_argument("--login", action="store_true",  help="open a window to sign in, store in --profile, exit")
    ap.add_argument("--channel", help="use installed browser instead of bundled Chromium, e.g. 'chrome'")
    ap.add_argument("--headed", action="store_true", help="show the browser while capturing")
    ap.add_argument("--width", type=positive_int, default=1920, help="logical slide width in px (default 1920)")
    ap.add_argument("--aspect", type=aspect_ratio, help="aspect ratio like 16:9 or 4:3 (auto-detected when possible)")
    ap.add_argument("--scale", type=positive_int, default=2, help="render scale factor for sharpness (default 2)")
    ap.add_argument("--poll", type=positive_float, default=0.3, help="seconds between screenshots while waiting")
    ap.add_argument("--settle", type=positive_float, default=1.2, help="seconds the picture must stay unchanged to count as finished (default 1.2)")
    ap.add_argument("--max-wait", type=positive_float, default=15.0, help="max seconds to wait for one animation step")
    ap.add_argument("--max-steps", type=positive_int, default=80, help="max keypresses per slide")
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
            debug_dir=a.debug_dir, all_steps=a.all_steps, verbose=not a.quiet)


if __name__ == "__main__":
    main()

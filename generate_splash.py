"""
PrimeCare Clinic – Complete Icon & Splash Screen Regenerator
============================================================
STEP 1: Wipes all old splash images, pwa icons, and app icons.
STEP 2: Draws the EXACT web SVG icon (primecare-logo.svg) in pixels:
          • Outer ring  r=46, stroke #0077b6  width 3.2
          • Inner ring  r=38.5, stroke #0077b6 width 2
          • Cross outline M42,17…  stroke #0077b6 width 2.5
          • ECG polyline 17,50…83,50  stroke #0077b6 width 2.6
        Icon background: WHITE (matching the web)
        Icon wrapper: white square with rounded corners (like iOS app icons)
STEP 3: Saves every required PWA icon size.
STEP 4: Generates every Apple splash size + 3 Android splash sizes.
        Splash design:
          • Clean white / very-light-blue background (matches web UI)
          • Centred icon (the exact SVG replica)
          • "PrimeCare Clinic" in brand colour #0077b6 (bold)
          • Tagline in #6b7280 (matches .brand-sub from style.css)
"""

import os
import sys
import math
import shutil
from PIL import Image, ImageDraw, ImageFont

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT   = r"c:\Users\SLIM5\Desktop\doctorbook"
STATIC = os.path.join(ROOT, "static")
PWA    = os.path.join(STATIC, "pwa")
IMAGE  = os.path.join(STATIC, "image")

# ── Brand colours (copy-pasted from style.css) ────────────────────────────────
BLUE        = (0, 119, 182)      # #0077b6   primary
BLUE_DARK   = (2, 62, 138)       # #023e8a   dark
TEAL        = (0, 150, 199)      # #0096c7   secondary
WHITE       = (255, 255, 255)
LIGHT_BLUE  = (230, 244, 255)    # #e6f4ff clean medical light blue
TEXT_MAIN   = (11, 58, 96)       # #0b3a60   heading colour from CSS
TEXT_MUTED  = (107, 114, 128)    # #6b7280   .brand-sub colour

# ── Splash sizes (Apple) ──────────────────────────────────────────────────────
APPLE_SIZES = [
    (2048, 2732), (1668, 2388), (1536, 2048), (1668, 2224), (1620, 2160),
    (1290, 2796), (1179, 2556), (1284, 2778), (1170, 2532), (1125, 2436),
    (1242, 2688), (828,  1792), (750,  1334), (1242, 2208), (640,  1136),
    (1488, 2266), (1640, 2360),
]

# ── PWA icon sizes ────────────────────────────────────────────────────────────
PWA_ICON_SIZES = [48, 72, 96, 128, 144, 152, 192, 384, 512]


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 1 – WIPE EVERYTHING OLD
# ══════════════════════════════════════════════════════════════════════════════

def wipe_old():
    print("\n[1/4] Removing old splash images and app icons...")

    # Delete every file inside static/pwa/
    if os.path.isdir(PWA):
        for fname in os.listdir(PWA):
            fpath = os.path.join(PWA, fname)
            if os.path.isfile(fpath):
                os.remove(fpath)
                print(f"  DEL  pwa/{fname}")
    os.makedirs(PWA, exist_ok=True)

    # Delete root-level icon PNGs
    for fname in [
        "android-chrome-192x192.png",
        "android-chrome-512x512.png",
        "apple-touch-icon.png",
        "favicon-16x16.png",
        "favicon-32x32.png",
    ]:
        p = os.path.join(STATIC, fname)
        if os.path.isfile(p):
            os.remove(p)
            print(f"  DEL  {fname}")

    # Delete image/pwa-icon.png  and  image/pwa_splash.png
    for fname in ["pwa-icon.png", "pwa_splash.png"]:
        p = os.path.join(IMAGE, fname)
        if os.path.isfile(p):
            os.remove(p)
            print(f"  DEL  image/{fname}")

    print("  OK   Wipe complete.")


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 2 – DRAW THE EXACT WEB SVG ICON
# ══════════════════════════════════════════════════════════════════════════════

def _svgS(val, S):
    """Scale a SVG-space value by S to pixel space."""
    return val * S

def draw_web_icon_on(img, cx, cy, icon_px, bg_alpha=0):
    """
    Draw the exact primecare-logo.svg icon centred at (cx, cy).
    icon_px = diameter of the icon in pixels (maps to 100×100 SVG viewBox).
    bg_alpha: alpha for the white fill inside the rings (0=transparent).
    All coordinates match the SVG exactly.
    """
    S   = icon_px / 100.0          # scale factor
    d   = ImageDraw.Draw(img)
    c   = BLUE + (255,)            # solid #0077b6

    # ── Helper: stroke a ring using multiple ellipse draws ──────────────────
    def stroke_ring(r_svg, sw_svg):
        r  = _svgS(r_svg, S)
        sw = max(1, int(_svgS(sw_svg, S)))
        for i in range(sw):
            rr = r - i
            d.ellipse([cx-rr, cy-rr, cx+rr, cy+rr], outline=c, width=1)

    # ── White fill inside outer ring (only if requested) ─────────────────────
    if bg_alpha > 0:
        outer_r = int(_svgS(46, S))
        d.ellipse([cx-outer_r, cy-outer_r, cx+outer_r, cy+outer_r],
                  fill=WHITE + (bg_alpha,))

    # ── Outer ring (r=46, stroke-width=3.2) ──────────────────────────────────
    stroke_ring(46, 3.2)

    # ── Inner ring (r=38.5, stroke-width=2) ──────────────────────────────────
    stroke_ring(38.5, 2)

    # ── Medical cross outline ─────────────────────────────────────────────────
    #   M42 17 L58 17 L58 42 L83 42 L83 58 L58 58 L58 83 L42 83 L42 58 L17 58 L17 42 L42 42 Z
    cross_svg = [
        (42,17),(58,17),(58,42),(83,42),(83,58),
        (58,58),(58,83),(42,83),(42,58),(17,58),(17,42),(42,42)
    ]
    sw_cross = max(1, int(_svgS(2.5, S)))
    scaled_cross = [(cx + int((px-50)*S), cy + int((py-50)*S)) for px,py in cross_svg]
    for i in range(len(scaled_cross)):
        x0, y0 = scaled_cross[i]
        x1, y1 = scaled_cross[(i+1) % len(scaled_cross)]
        d.line([x0, y0, x1, y1], fill=c, width=sw_cross)

    # ── ECG heartbeat polyline ────────────────────────────────────────────────
    #   17,50 29,50 33,40 38,60 42.5,50 57.5,50 62,40 67,60 71,50 83,50
    ecg_svg = [
        (17,50),(29,50),(33,40),(38,60),(42.5,50),
        (57.5,50),(62,40),(67,60),(71,50),(83,50)
    ]
    sw_ecg = max(1, int(_svgS(2.6, S)))
    scaled_ecg = [(cx + int((px-50)*S), cy + int((py-50)*S)) for px,py in ecg_svg]
    for i in range(len(scaled_ecg)-1):
        x0, y0 = scaled_ecg[i]
        x1, y1 = scaled_ecg[i+1]
        d.line([x0, y0, x1, y1], fill=c, width=sw_ecg)


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 3 – GENERATE PWA / APP ICONS
# ══════════════════════════════════════════════════════════════════════════════

def make_icon(size_px, bg_color=LIGHT_BLUE, rounded=False, round_radius_frac=0.22):
    """
    Create a square PNG icon of size_px × size_px.
    bg_color  - background fill (default: LIGHT_BLUE, matching the web)
    rounded   - if True, clip to rounded-rectangle (for maskable/Apple icons)
    """
    img = Image.new("RGBA", (size_px, size_px), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)

    if rounded:
        r = int(size_px * round_radius_frac)
        # Rounded background
        d.rounded_rectangle([0, 0, size_px-1, size_px-1], radius=r,
                             fill=bg_color + (255,))
    else:
        d.rectangle([0, 0, size_px, size_px], fill=bg_color + (255,))

    # Icon occupies 78 % of the canvas, centred
    padding_frac = 0.11
    icon_px = int(size_px * (1 - 2 * padding_frac))
    cx = size_px // 2
    cy = size_px // 2
    draw_web_icon_on(img, cx, cy, icon_px)

    return img


def save_icons():
    print("\n[2/4] Generating app icons...")

    # ── favicon 16 & 32 ──────────────────────────────────────────────────────
    for sz in (16, 32):
        ico = make_icon(sz)
        ico.convert("RGB").save(
            os.path.join(STATIC, f"favicon-{sz}x{sz}.png"), "PNG")
        print(f"  OK   favicon-{sz}x{sz}.png")

    # ── android-chrome icons ──────────────────────────────────────────────────
    for sz in (192, 512):
        ico = make_icon(sz, rounded=False)
        ico.convert("RGB").save(
            os.path.join(STATIC, f"android-chrome-{sz}x{sz}.png"), "PNG")
        print(f"  OK   android-chrome-{sz}x{sz}.png")

    # ── apple-touch-icon (180px, rounded) ────────────────────────────────────
    ico = make_icon(180, rounded=True)
    ico.convert("RGB").save(
        os.path.join(STATIC, "apple-touch-icon.png"), "PNG")
    print("  OK   apple-touch-icon.png")

    # ── pwa/ icon sizes ───────────────────────────────────────────────────────
    for sz in PWA_ICON_SIZES:
        ico = make_icon(sz)
        ico.convert("RGB").save(
            os.path.join(PWA, f"icon-{sz}x{sz}.png"), "PNG")
        print(f"  OK   pwa/icon-{sz}x{sz}.png")

    # ── image/pwa-icon.png  (512 px, the one referenced in manifest) ─────────
    ico = make_icon(512)
    ico.convert("RGB").save(os.path.join(IMAGE, "pwa-icon.png"), "PNG")
    print("  OK   image/pwa-icon.png")


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 4 – GENERATE SPLASH SCREENS
# ══════════════════════════════════════════════════════════════════════════════

def get_font(bold=False, size=40):
    candidates = (
        [r"C:\Windows\Fonts\segoeuib.ttf",
         r"C:\Windows\Fonts\arialbd.ttf",
         r"C:\Windows\Fonts\calibrib.ttf",
         r"C:\Windows\Fonts\verdanab.ttf"]
        if bold else
        [r"C:\Windows\Fonts\segoeui.ttf",
         r"C:\Windows\Fonts\arial.ttf",
         r"C:\Windows\Fonts\calibri.ttf",
         r"C:\Windows\Fonts\verdana.ttf"]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def text_center(draw, text, font, y, color, canvas_w):
    bb  = draw.textbbox((0, 0), text, font=font)
    tw  = bb[2] - bb[0]
    x   = (canvas_w - tw) // 2
    draw.text((x, y), text, font=font, fill=color)
    return bb[3] - bb[1]   # height


def make_splash(w, h):
    """
    Clean light blue splash screen matching the web look:
      • Solid LIGHT_BLUE background
      • The exact SVG icon centred
      • "PrimeCare Clinic" in brand colour #0077b6 (bold)
    """
    img  = Image.new("RGB", (w, h), LIGHT_BLUE)
    draw = ImageDraw.Draw(img)

    # ── Icon: 38 % of the shorter dimension, capped to look professional
    icon_px  = max(160, min(int(min(w, h) * 0.38), 500))

    # ── Typography
    name_size = max(42, min(int(w * 0.085), 120))
    font_name = get_font(bold=True, size=name_size)

    # ── Layout: center the combined block of icon and text vertically
    spacing = int(h * 0.045)
    estimated_text_h = int(name_size * 1.25)

    total_block_h = icon_px + spacing + estimated_text_h
    top_of_block = (h - total_block_h) // 2
    cy = top_of_block + icon_px // 2
    cx = w // 2

    # Draw the logo centered at (cx, cy)
    draw_web_icon_on(img, cx, cy, icon_px)

    # Text placement directly below the logo
    text_y = cy + icon_px // 2 + spacing

    draw_tx = ImageDraw.Draw(img)
    # Draw "PrimeCare Clinic" in brand primary blue
    text_center(draw_tx, "PrimeCare Clinic", font_name, text_y, BLUE, w)

    return img


def save_splashes():
    print("\n[3/4] Generating Apple / iOS splash screens...")
    for w, h in APPLE_SIZES:
        spl  = make_splash(w, h)
        fname = f"apple-splash-{w}-{h}.png"
        spl.save(os.path.join(PWA, fname), "PNG", optimize=True)
        print(f"  OK   {fname}")

    print("\n[4/4] Generating Android splash screens...")
    for w, h, label in [
        (1080, 1920, "android-splash-1080x1920.png"),
        (720,  1280, "android-splash-720x1280.png"),
        (480,  800,  "android-splash-480x800.png"),
    ]:
        spl  = make_splash(w, h)
        spl.save(os.path.join(PWA, label), "PNG", optimize=True)
        print(f"  OK   {label}")

    # Also save image/pwa_splash.png (referenced from some pages)
    spl = make_splash(1080, 1920)
    spl.save(os.path.join(IMAGE, "pwa_splash.png"), "PNG", optimize=True)
    print("  OK   image/pwa_splash.png")


# ==============================================================================
#  MAIN
# ==============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  PrimeCare - Icon & Splash Regenerator")
    print("  Exact web SVG - White background - Brand colours")
    print("=" * 60)

    wipe_old()
    save_icons()
    save_splashes()

    print("\n" + "=" * 60)
    print("  ALL DONE!")
    print(f"  Splash  -> {PWA}")
    print(f"  Icons   -> {STATIC}")
    print("=" * 60)

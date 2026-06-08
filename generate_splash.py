"""
PrimeCare Clinic — Premium PWA Splash Screen Generator
Generates Apple iOS startup splash screens + Android/PWA icons.
Brand:
  Primary blue  : #023e8a  (dark navy)
  Accent blue   : #0077b6
  Highlight teal: #00b4d8 / #20d0ba
  Text white    : #ffffff
  Tagline blue  : #90e0ef
"""

import os
import math
from PIL import Image, ImageDraw, ImageFilter

WORKSPACE = r"c:\Users\SLIM5\Desktop\doctorbook"
SPLASH_DIR = os.path.join(WORKSPACE, "static", "pwa")
STATIC_DIR = os.path.join(WORKSPACE, "static")

# ─── Apple iOS splash sizes (width, height) ───────────────────────────────
SPLASH_SIZES = [
    (2048, 2732), (1668, 2388), (1536, 2048), (1668, 2224), (1620, 2160),
    (1290, 2796), (1179, 2556), (1284, 2778), (1170, 2532), (1125, 2436),
    (1242, 2688), (828, 1792), (750, 1334), (1242, 2208), (640, 1136),
    (1488, 2266), (1640, 2360),
]

# ─── Android / PWA icon sizes ─────────────────────────────────────────────
ICON_SIZES = [48, 72, 96, 128, 144, 152, 192, 384, 512]

# ─── Brand palette ────────────────────────────────────────────────────────
BG_TOP    = (2,  22,  80)    # deep midnight navy
BG_BOT    = (3,  62, 138)    # rich ocean blue
BLUE      = (0, 119, 182)    # brand blue   (#0077b6)
TEAL      = (0, 180, 216)    # highlight    (#00b4d8)
TEAL_ACC  = (20, 208, 186)   # care-teal    (#14d0ba)
WHITE     = (255, 255, 255)
GLOW_CLR  = (0, 140, 210)

# ─── Geometry helpers ─────────────────────────────────────────────────────
def lerp(a, b, t):
    return a + (b - a) * t

def lerp_color(c1, c2, t):
    return tuple(int(lerp(c1[i], c2[i], t)) for i in range(3))

# ─── Font loader (fallback chain) ─────────────────────────────────────────
from PIL import ImageFont

FONT_CANDIDATES_BOLD = [
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\calibrib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\verdanab.ttf",
]
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\calibri.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\verdana.ttf",
]

def get_font(bold=False, size=40):
    for path in (FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()

# ─── Drawing primitives ───────────────────────────────────────────────────
def draw_gradient(img, w, h):
    pix = img.load()
    for y in range(h):
        t = y / (h - 1)
        c = lerp_color(BG_TOP, BG_BOT, t)
        for x in range(w):
            pix[x, y] = c + (255,)


def draw_radial_glow(base: Image.Image, cx, cy, r_max, color_rgb, strength=140):
    """Soft radial glow painted onto base (RGBA)."""
    glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for r in range(r_max, 0, -4):   # step 4 for speed
        t = r / r_max
        alpha = int(strength * (1 - t) ** 1.8)
        gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*color_rgb, alpha))
    base.paste(glow, mask=glow)


def rounded_rect(draw, x0, y0, x1, y1, rad, fill, outline=None, outline_w=0):
    draw.rounded_rectangle([x0, y0, x1, y1], radius=rad, fill=fill,
                            outline=outline, width=outline_w)


def draw_cross_icon(img: Image.Image, cx, cy, size):
    """
    Draw a premium medical cross icon:
      - Two rounded-rectangle arms (vertical blue, horizontal teal)
      - Thin ECG line through the horizontal arm
      - Small white glint dot at center
    Drawn on an RGBA layer then alpha-composited.
    """
    arm_w = int(size * 0.340)   # arm thickness
    arm_l = int(size * 0.880)   # arm full span
    rad   = int(arm_w * 0.42)   # corner radius

    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # ── Subtle shadow under the cross ────────────────────────────────────
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    for offset in range(18, 0, -3):
        a = int(70 * (1 - offset / 18))
        sd.rounded_rectangle(
            [cx - arm_l // 2 + offset, cy - arm_w // 2 + offset,
             cx + arm_l // 2 + offset, cy + arm_w // 2 + offset],
            radius=rad, fill=(0, 0, 0, a))
        sd.rounded_rectangle(
            [cx - arm_w // 2 + offset, cy - arm_l // 2 + offset,
             cx + arm_w // 2 + offset, cy + arm_l // 2 + offset],
            radius=rad, fill=(0, 0, 0, a))
    blurred_shadow = shadow.filter(ImageFilter.GaussianBlur(radius=8))
    img.paste(blurred_shadow, mask=blurred_shadow)

    # ── Horizontal arm — teal gradient look ──────────────────────────────
    d.rounded_rectangle(
        [cx - arm_l // 2, cy - arm_w // 2,
         cx + arm_l // 2, cy + arm_w // 2],
        radius=rad, fill=(*TEAL, 255))

    # ── Vertical arm — brand blue ─────────────────────────────────────────
    d.rounded_rectangle(
        [cx - arm_w // 2, cy - arm_l // 2,
         cx + arm_w // 2, cy + arm_l // 2],
        radius=rad, fill=(*BLUE, 255))

    # ── ECG line through the horizontal bar ──────────────────────────────
    ecg_scale = arm_l / 66.0   # normalise to our arm length
    ecg_amp   = arm_w * 0.28
    ecg_pts_raw = [
        (0,   0), (11,  0), (15, -ecg_amp * 1.0),
        (20,  ecg_amp * 1.6), (25, 0), (41, 0),
        (45, -ecg_amp * 1.0), (50,  ecg_amp * 1.6), (55, 0), (66, 0)
    ]
    ecg_pts = []
    for px, py in ecg_pts_raw:
        ex = cx - arm_l // 2 + int(px * ecg_scale)
        ey = cy + int(py)
        ecg_pts.append((ex, ey))

    d.line(ecg_pts, fill=(255, 255, 255, 210), width=max(3, int(size * 0.026)))

    # ── White glint at center ─────────────────────────────────────────────
    gr = max(5, int(arm_w * 0.14))
    d.ellipse([cx - gr, cy - gr, cx + gr, cy + gr], fill=(255, 255, 255, 220))

    img.alpha_composite(layer)


def text_center(draw, text, font, y, fill, w, shadow=True, shadow_alpha=55):
    bb  = draw.textbbox((0, 0), text, font=font)
    tw  = bb[2] - bb[0]
    th  = bb[3] - bb[1]
    tx  = (w - tw) // 2
    if shadow:
        draw.text((tx + 2, y + 3), text, font=font, fill=(0, 0, 0, shadow_alpha))
    draw.text((tx, y), text, font=font, fill=fill)
    return th


# ─── Shine overlay ────────────────────────────────────────────────────────
def draw_shine(img: Image.Image, w, h):
    """Subtle diagonal top-left shine for depth."""
    shine = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sd    = ImageDraw.Draw(shine)
    # large triangle in top-left corner
    sd.polygon([(0, 0), (w, 0), (0, h // 3)], fill=(255, 255, 255, 12))
    blurred = shine.filter(ImageFilter.GaussianBlur(radius=max(20, w // 30)))
    img.alpha_composite(blurred)


# ─── Main generator ───────────────────────────────────────────────────────
def generate_splash(w: int, h: int) -> Image.Image:
    img = Image.new("RGBA", (w, h))
    draw_gradient(img, w, h)

    # ── Layout constants ─────────────────────────────────────────────────
    cross_size = max(240, min(int(min(w, h) * 0.40), 620))
    cx = w // 2
    cy = int(h * 0.40)

    # ── Glow halo ────────────────────────────────────────────────────────
    glow_r = int(cross_size * 0.78)
    draw_radial_glow(img, cx, cy, glow_r, GLOW_CLR, strength=130)

    # ── Medical cross ────────────────────────────────────────────────────
    draw_cross_icon(img, cx, cy, cross_size)

    # ── Shine ────────────────────────────────────────────────────────────
    draw_shine(img, w, h)

    # ── Typography ───────────────────────────────────────────────────────
    title_size    = max(64, min(int(w * 0.108), 185))
    subtitle_size = max(30, min(int(w * 0.046), 80))
    tagline_size  = max(22, min(int(w * 0.030), 54))
    version_size  = max(18, min(int(w * 0.022), 40))

    f_title    = get_font(bold=True,  size=title_size)
    f_subtitle = get_font(bold=True,  size=subtitle_size)
    f_tagline  = get_font(bold=False, size=tagline_size)
    f_version  = get_font(bold=False, size=version_size)

    draw = ImageDraw.Draw(img)

    # ── "PrimeCare" two-tone wordmark ────────────────────────────────────
    prime_bb = draw.textbbox((0, 0), "Prime", font=f_title)
    care_bb  = draw.textbbox((0, 0), "Care",  font=f_title)
    prime_w  = prime_bb[2] - prime_bb[0]
    care_w   = care_bb[2]  - care_bb[0]
    title_h  = prime_bb[3] - prime_bb[1]
    total_w  = prime_w + care_w
    tx       = (w - total_w) // 2
    text_y   = cy + cross_size // 2 + int(h * 0.044)

    # drop shadow
    draw.text((tx + 3,           text_y + 4), "Prime", font=f_title, fill=(0, 0, 0, 50))
    draw.text((tx + prime_w + 3, text_y + 4), "Care",  font=f_title, fill=(0, 0, 0, 50))
    # text
    draw.text((tx,           text_y), "Prime", font=f_title, fill=(*WHITE, 255))
    draw.text((tx + prime_w, text_y), "Care",  font=f_title, fill=(*TEAL_ACC, 255))

    y = text_y + title_h + int(h * 0.018)

    # ── Subtitle ─────────────────────────────────────────────────────────
    sub_h = text_center(draw, "Healthcare Clinic Portal",
                        f_subtitle, y, (185, 225, 255, 240), w)
    y += sub_h + int(h * 0.020)

    # ── Divider ──────────────────────────────────────────────────────────
    dw = int(w * 0.14)
    dx = (w - dw) // 2
    draw.rounded_rectangle([dx, y, dx + dw, y + 2], radius=1,
                            fill=(255, 255, 255, 70))
    y += int(h * 0.022)

    # ── Tagline ──────────────────────────────────────────────────────────
    tg_h = text_center(draw, "Your health, our priority.",
                       f_tagline, y, (144, 224, 239, 220), w, shadow=False)

    return img


def save_splash(w: int, h: int):
    img   = generate_splash(w, h)
    fname = f"apple-splash-{w}-{h}.png"
    path  = os.path.join(SPLASH_DIR, fname)
    img.convert("RGB").save(path, "PNG", optimize=True)
    print(f"  OK  {fname}")


# --- Icon generator -------------------------------------------------------
def generate_icon(size: int) -> Image.Image:
    """Generate a square app icon on a brand-gradient background."""
    w = h = size
    img = Image.new("RGBA", (w, h))
    draw_gradient(img, w, h)

    cross = int(size * 0.62)
    cx = cy = size // 2

    draw_radial_glow(img, cx, cy, int(cross * 0.75), GLOW_CLR, strength=110)
    draw_cross_icon(img, cx, cy, cross)
    draw_shine(img, w, h)

    return img


def save_icon(size: int, filename: str, out_dir: str):
    img = generate_icon(size)
    path = os.path.join(out_dir, filename)
    img.convert("RGBA").save(path, "PNG", optimize=True)
    print(f"  OK  {filename}")


# --- Entry point ----------------------------------------------------------
if __name__ == "__main__":
    os.makedirs(SPLASH_DIR, exist_ok=True)

    print("\n=== Generating Apple iOS Splash Screens ===")
    for w, h in SPLASH_SIZES:
        save_splash(w, h)

    print("\n=== Generating PWA / Android Icons ===")
    for s in ICON_SIZES:
        save_icon(s, f"icon-{s}x{s}.png", SPLASH_DIR)

    print("\n=== Generating Root Static Icons ===")
    save_icon(192, "android-chrome-192x192.png", STATIC_DIR)
    save_icon(512, "android-chrome-512x512.png", STATIC_DIR)
    save_icon(180, "apple-touch-icon.png",        STATIC_DIR)
    save_icon(512, os.path.join("image", "pwa-icon.png"), STATIC_DIR)

    # favicon 32x32 and 16x16
    for fav_size, fav_name in [(32, "favicon-32x32.png"), (16, "favicon-16x16.png")]:
        save_icon(fav_size, fav_name, STATIC_DIR)

    print("\nDone! PrimeCare splash screens & icons generated.\n")

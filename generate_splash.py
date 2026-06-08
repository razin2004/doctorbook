"""
PrimeCare Clinic – Premium Splash Screen Generator
Matches the exact web SVG icon (double-ring + ECG medical cross).
Colours taken from style.css / manifest.json:
  Primary   #0077b6   rgb(0, 119, 182)
  Secondary #0096c7   rgb(0, 150, 199)
  Deep navy #023e8a   rgb(2, 62, 138)
"""

import os
import math
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ── Paths ────────────────────────────────────────────────────────────────────
workspace_dir = r"c:\Users\SLIM5\Desktop\doctorbook"
output_dir    = os.path.join(workspace_dir, "static", "pwa")
os.makedirs(output_dir, exist_ok=True)

# ── All required splash sizes ─────────────────────────────────────────────────
SIZES = [
    (2048, 2732), (1668, 2388), (1536, 2048), (1668, 2224), (1620, 2160),
    (1290, 2796), (1179, 2556), (1284, 2778), (1170, 2532), (1125, 2436),
    (1242, 2688), (828, 1792),  (750, 1334),  (1242, 2208), (640, 1136),
    (1488, 2266), (1640, 2360),
]

# ── Brand colours (from web CSS) ──────────────────────────────────────────────
C_TOP_GRAD   = (1, 30, 80)          # deep navy top
C_BOT_GRAD   = (2, 62, 138)         # #023e8a bottom
C_PRIMARY    = (0, 119, 182)        # #0077b6  web primary blue
C_SECONDARY  = (0, 150, 199)        # #0096c7  web secondary teal
C_WHITE      = (255, 255, 255)
C_WHITE_SOFT = (220, 235, 255)      # light blue-white for subtitle
C_TAGLINE    = (150, 205, 255)      # muted light-blue tagline


# ── Helpers ──────────────────────────────────────────────────────────────────

def lerp(a, b, t):
    return int(a + (b - a) * t)

def lerp_color(c1, c2, t):
    return tuple(lerp(c1[i], c2[i], t) for i in range(3))


def draw_gradient_bg(img, w, h):
    """Vertical gradient from deep navy to brand blue."""
    px = img.load()
    for y in range(h):
        t = y / max(h - 1, 1)
        c = lerp_color(C_TOP_GRAD, C_BOT_GRAD, t)
        for x in range(w):
            px[x, y] = c + (255,)


def draw_subtle_grid(img, w, h, scale):
    """Faint dot-grid pattern for depth — keeps it looking premium."""
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    step = max(30, int(40 * scale))
    dot_r = max(1, int(1.5 * scale))
    alpha = 18
    for gy in range(0, h, step):
        for gx in range(0, w, step):
            d.ellipse([gx - dot_r, gy - dot_r, gx + dot_r, gy + dot_r],
                      fill=(255, 255, 255, alpha))
    img.alpha_composite(overlay)


def draw_radial_glow(img, cx, cy, radius, color_rgb, strength=140):
    """Soft radial glow halo."""
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd   = ImageDraw.Draw(glow)
    for r in range(radius, 0, -1):
        t     = r / radius
        alpha = int(strength * (1 - t) ** 1.8)
        gd.ellipse([cx - r, cy - r, cx + r, cy + r],
                   fill=(*color_rgb, alpha))
    img.alpha_composite(glow)


# ── Web-icon drawing (matches primecare-logo.svg exactly) ────────────────────

def draw_web_icon(img, cx, cy, icon_size):
    """
    Faithfully reproduce the primecare-logo.svg icon:
      • Outer ring   stroke #0077b6
      • Inner ring   stroke #0077b6
      • Medical cross (outline, not filled)  stroke #0077b6
      • ECG heartbeat line through the horizontal bar  stroke #0077b6
    All strokes scaled proportionally from the 100×100 SVG viewBox.
    """
    # Scale factor: SVG is 100×100, icon_size maps to that
    S       = icon_size / 100.0
    o_w     = int(1.5 * S)          # minimum stroke width for visibility

    d = ImageDraw.Draw(img)

    def stroke_w(base_svg_width):
        return max(o_w, int(base_svg_width * S))

    BLUE = C_PRIMARY + (255,)

    # ── White background circle (gives contrast) ─────────────────────────
    bg_r = int(48 * S)
    d.ellipse([cx - bg_r, cy - bg_r, cx + bg_r, cy + bg_r],
              fill=(255, 255, 255, 30))

    # ── Outer ring (r=46, stroke 3.2) ────────────────────────────────────
    outer_r = int(46 * S)
    sw_outer = stroke_w(3.2)
    for i in range(sw_outer):
        r = outer_r - i
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=BLUE, width=1)

    # ── Inner ring (r=38.5, stroke 2) ────────────────────────────────────
    inner_r = int(38.5 * S)
    sw_inner = stroke_w(2)
    for i in range(sw_inner):
        r = inner_r - i
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=BLUE, width=1)

    # ── Medical cross outline ─────────────────────────────────────────────
    # SVG cross points (17..83 range, centred on 50):
    #   M42,17 L58,17 L58,42 L83,42 L83,58 L58,58 L58,83 L42,83 L42,58 L17,58 L17,42 L42,42 Z
    cross_pts = [
        (42,17),(58,17),(58,42),(83,42),(83,58),
        (58,58),(58,83),(42,83),(42,58),(17,58),(17,42),(42,42)
    ]
    scaled = [(cx + int((px - 50) * S), cy + int((py - 50) * S)) for px, py in cross_pts]
    sw_cross = stroke_w(2.5)
    # Draw as polygon outline by drawing line segments
    for i in range(len(scaled)):
        x0, y0 = scaled[i]
        x1, y1 = scaled[(i + 1) % len(scaled)]
        d.line([x0, y0, x1, y1], fill=BLUE, width=sw_cross)

    # ── ECG heartbeat line ────────────────────────────────────────────────
    # SVG points: 17,50 29,50 33,40 38,60 42.5,50 57.5,50 62,40 67,60 71,50 83,50
    ecg_pts_svg = [
        (17,50),(29,50),(33,40),(38,60),(42.5,50),
        (57.5,50),(62,40),(67,60),(71,50),(83,50)
    ]
    ecg_scaled = [(cx + int((px - 50) * S), cy + int((py - 50) * S)) for px, py in ecg_pts_svg]
    sw_ecg = stroke_w(2.6)
    for i in range(len(ecg_scaled) - 1):
        x0, y0 = ecg_scaled[i]
        x1, y1 = ecg_scaled[i + 1]
        d.line([x0, y0, x1, y1], fill=BLUE, width=sw_ecg)


# ── Decorative thin horizontal line ─────────────────────────────────────────

def draw_divider(draw, w, cy, line_w_frac=0.12, color=(255,255,255,60)):
    dw = int(w * line_w_frac)
    dx = (w - dw) // 2
    draw.rectangle([dx, cy, dx + dw, cy + 2], fill=color)


# ── Font loading ─────────────────────────────────────────────────────────────

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


def draw_text_centered(draw, text, font, y, color, w, shadow_offset=3, shadow_alpha=60):
    bb  = draw.textbbox((0, 0), text, font=font)
    tw  = bb[2] - bb[0]
    th  = bb[3] - bb[1]
    x   = (w - tw) // 2
    if shadow_alpha:
        draw.text((x + shadow_offset, y + shadow_offset), text, font=font,
                  fill=(0, 0, 0, shadow_alpha))
    draw.text((x, y), text, font=font, fill=color)
    return th


# ── Main generation ──────────────────────────────────────────────────────────

def generate(w, h):
    img = Image.new("RGBA", (w, h), (0, 0, 0, 255))

    # 1 – Background gradient
    draw_gradient_bg(img, w, h)

    # 2 – Subtle dot grid
    scale = w / 1080
    draw_subtle_grid(img, w, h, scale)

    # 3 – Geometry: icon centred slightly above middle, text below
    icon_size = max(200, min(int(min(w, h) * 0.40), 600))
    cx        = w // 2
    cy        = int(h * 0.36)

    # 4 – Glow halo behind icon
    glow_r = int(icon_size * 0.80)
    draw_radial_glow(img, cx, cy, glow_r, C_PRIMARY, strength=100)

    # 5 – Web icon (exact SVG replica)
    draw_web_icon(img, cx, cy, icon_size)

    # ── Typography ─────────────────────────────────────────────────────────
    bold_size     = max(60, min(int(w * 0.105), 170))
    subtitle_size = max(28, min(int(w * 0.042), 72))
    tagline_size  = max(20, min(int(w * 0.028), 50))

    font_bold     = get_font(bold=True,  size=bold_size)
    font_sub      = get_font(bold=True,  size=subtitle_size)
    font_tag      = get_font(bold=False, size=tagline_size)

    draw   = ImageDraw.Draw(img)
    text_y = cy + icon_size // 2 + int(h * 0.048)

    # ── "PrimeCare" — two-tone word mark ─────────────────────────────────
    prime_bb = draw.textbbox((0, 0), "Prime", font=font_bold)
    care_bb  = draw.textbbox((0, 0), "Care",  font=font_bold)
    prime_w  = prime_bb[2] - prime_bb[0]
    care_w   = care_bb[2]  - care_bb[0]
    title_h  = prime_bb[3] - prime_bb[1]
    total_tw = prime_w + care_w
    tx       = (w - total_tw) // 2

    # drop shadow
    draw.text((tx + 3,           text_y + 3), "Prime", font=font_bold, fill=(0, 0, 0, 50))
    draw.text((tx + prime_w + 3, text_y + 3), "Care",  font=font_bold, fill=(0, 0, 0, 50))

    # "Prime" → white  |  "Care" → brand secondary teal #0096c7
    draw.text((tx,           text_y), "Prime", font=font_bold, fill=C_WHITE)
    draw.text((tx + prime_w, text_y), "Care",  font=font_bold, fill=C_SECONDARY)

    y = text_y + title_h + int(h * 0.018)

    # ── "Clinic" – secondary word, lighter weight ─────────────────────────
    clinic_size = max(36, min(int(w * 0.058), 100))
    font_clinic = get_font(bold=False, size=clinic_size)
    clinic_bb   = draw.textbbox((0, 0), "Clinic", font=font_clinic)
    clinic_h    = clinic_bb[3] - clinic_bb[1]
    clinic_w    = clinic_bb[2] - clinic_bb[0]
    cx_clinic   = (w - clinic_w) // 2
    draw.text((cx_clinic + 2, y + 2), "Clinic", font=font_clinic, fill=(0, 0, 0, 40))
    draw.text((cx_clinic,     y),     "Clinic", font=font_clinic, fill=C_WHITE_SOFT)

    y += clinic_h + int(h * 0.026)

    # ── Divider ───────────────────────────────────────────────────────────
    draw_divider(draw, w, y, line_w_frac=0.10, color=(255, 255, 255, 55))
    y += int(h * 0.022)

    # ── Tagline ───────────────────────────────────────────────────────────
    draw_text_centered(draw, "Your health, our priority.",
                       font_tag, y, C_TAGLINE, w,
                       shadow_offset=2, shadow_alpha=0)

    # Save
    fname = f"apple-splash-{w}-{h}.png"
    img.convert("RGB").save(os.path.join(output_dir, fname), "PNG", optimize=True)
    print(f"  ✓  {fname}")


# ── Also write a single Android splash (just one canonical size) ─────────────

def generate_android_splash(w, h, name):
    """Same design but saved with a custom filename (for Android splash icons)."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    draw_gradient_bg(img, w, h)
    scale = w / 1080
    draw_subtle_grid(img, w, h, scale)

    icon_size = max(200, min(int(min(w, h) * 0.38), 560))
    cx        = w // 2
    cy        = int(h * 0.36)

    glow_r = int(icon_size * 0.78)
    draw_radial_glow(img, cx, cy, glow_r, C_PRIMARY, strength=100)
    draw_web_icon(img, cx, cy, icon_size)

    bold_size     = max(56, min(int(w * 0.100), 160))
    subtitle_size = max(26, min(int(w * 0.040), 68))

    font_bold   = get_font(bold=True,  size=bold_size)
    font_clinic = get_font(bold=False, size=max(32, min(int(w * 0.054), 90)))
    font_tag    = get_font(bold=False, size=max(18, min(int(w * 0.026), 46)))

    draw   = ImageDraw.Draw(img)
    text_y = cy + icon_size // 2 + int(h * 0.045)

    prime_bb = draw.textbbox((0, 0), "Prime", font=font_bold)
    care_bb  = draw.textbbox((0, 0), "Care",  font=font_bold)
    prime_w  = prime_bb[2] - prime_bb[0]
    care_w   = care_bb[2]  - care_bb[0]
    title_h  = prime_bb[3] - prime_bb[1]
    total_tw = prime_w + care_w
    tx       = (w - total_tw) // 2

    draw.text((tx + 3,           text_y + 3), "Prime", font=font_bold, fill=(0, 0, 0, 50))
    draw.text((tx + prime_w + 3, text_y + 3), "Care",  font=font_bold, fill=(0, 0, 0, 50))
    draw.text((tx,           text_y), "Prime", font=font_bold, fill=C_WHITE)
    draw.text((tx + prime_w, text_y), "Care",  font=font_bold, fill=C_SECONDARY)

    y = text_y + title_h + int(h * 0.016)

    clinic_bb = draw.textbbox((0, 0), "Clinic", font=font_clinic)
    clinic_h  = clinic_bb[3] - clinic_bb[1]
    clinic_w2 = clinic_bb[2] - clinic_bb[0]
    cx2       = (w - clinic_w2) // 2
    draw.text((cx2 + 2, y + 2), "Clinic", font=font_clinic, fill=(0, 0, 0, 40))
    draw.text((cx2,     y),     "Clinic", font=font_clinic, fill=C_WHITE_SOFT)

    y += clinic_h + int(h * 0.024)
    draw_divider(draw, w, y, line_w_frac=0.10, color=(255, 255, 255, 55))
    y += int(h * 0.020)
    draw_text_centered(draw, "Your health, our priority.",
                       font_tag, y, C_TAGLINE, w, shadow_offset=2, shadow_alpha=0)

    img.convert("RGB").save(os.path.join(output_dir, name), "PNG", optimize=True)
    print(f"  ✓  {name}")


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("  PrimeCare – Premium Splash Screen Generator")
    print("  Web-matched icon · Brand colours · ECG cross")
    print("=" * 55)

    print("\n[Apple / iOS splash screens]")
    for w, h in SIZES:
        generate(w, h)

    print("\n[Android splash icons]")
    generate_android_splash(1080, 1920, "android-splash-1080x1920.png")
    generate_android_splash(720,  1280, "android-splash-720x1280.png")
    generate_android_splash(480,  800,  "android-splash-480x800.png")

    print("\n✅  All premium splash screens generated successfully!")
    print(f"   Output → {output_dir}")

import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import math

workspace_dir = r"c:\Users\SLIM5\Desktop\doctorbook"
output_dir = os.path.join(workspace_dir, "static", "pwa")

sizes = [
    (2048, 2732), (1668, 2388), (1536, 2048), (1668, 2224), (1620, 2160),
    (1290, 2796), (1179, 2556), (1284, 2778), (1170, 2532), (1125, 2436),
    (1242, 2688), (828, 1792), (750, 1334), (1242, 2208), (640, 1136),
    (1488, 2266), (1640, 2360)
]

# Deep midnight-to-ocean gradient
TOP_COLOR    = (4, 14, 60)
BOTTOM_COLOR = (5, 60, 140)

def lerp_color(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))

def draw_gradient_bg(img, w, h):
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        color = lerp_color(TOP_COLOR, BOTTOM_COLOR, t)
        draw.line([(0, y), (w, y)], fill=color)

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

def draw_rounded_rect(draw, x0, y0, x1, y1, radius, fill):
    draw.rectangle([x0 + radius, y0, x1 - radius, y1], fill=fill)
    draw.rectangle([x0, y0 + radius, x1, y1 - radius], fill=fill)
    draw.ellipse([x0, y0, x0 + 2*radius, y0 + 2*radius], fill=fill)
    draw.ellipse([x1 - 2*radius, y0, x1, y0 + 2*radius], fill=fill)
    draw.ellipse([x0, y1 - 2*radius, x0 + 2*radius, y1], fill=fill)
    draw.ellipse([x1 - 2*radius, y1 - 2*radius, x1, y1], fill=fill)

def draw_glow(base_img, cx, cy, radius, color_rgb, strength=180):
    """Paint a soft radial glow behind the icon — no image file needed."""
    glow = Image.new("RGBA", base_img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for r in range(radius, 0, -1):
        t = r / radius
        alpha = int(strength * (1 - t) ** 1.6)
        gd.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=(*color_rgb, alpha)
        )
    # blend glow onto base image
    base_img.paste(glow, mask=glow)

def draw_pure_cross(img, cx, cy, cross_size):
    """
    Draw a clean, icon-only medical cross directly on the gradient.
    No box, no background circle, no image file — pure vector shapes.
    Two arms:
      Vertical  = bright blue   (the dominant arm)
      Horizontal = vivid teal   (the accent arm)
    Arms meet cleanly at the center.
    """
    arm_thick = int(cross_size * 0.355)   # arm width
    arm_len   = int(cross_size * 0.90)    # full span of each arm
    r         = int(arm_thick * 0.46)     # corner radius

    BLUE = (30, 110, 230)
    TEAL = (20, 200, 170)

    draw = ImageDraw.Draw(img)

    # ── Horizontal arm (teal) ──────────────────────────────────────────
    draw_rounded_rect(draw,
        cx - arm_len // 2,  cy - arm_thick // 2,
        cx + arm_len // 2,  cy + arm_thick // 2,
        r, TEAL)

    # ── Vertical arm (blue) — drawn on top so blue dominates center ───
    draw_rounded_rect(draw,
        cx - arm_thick // 2,  cy - arm_len // 2,
        cx + arm_thick // 2,  cy + arm_len // 2,
        r, BLUE)

    # ── Clean white highlight dot at center (tiny, polished look) ─────
    dot_r = max(4, int(arm_thick * 0.12))
    draw.ellipse(
        [cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r],
        fill=(255, 255, 255, 200)
    )

def draw_text_centered(draw, text, font, y, color, w, shadow_alpha=70):
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x  = (w - tw) // 2
    if shadow_alpha:
        draw.text((x + 2, y + 3), text, font=font, fill=(0, 0, 0, shadow_alpha))
    draw.text((x, y), text, font=font, fill=color)
    return th

def generate(w, h):
    img = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    draw_gradient_bg(img, w, h)

    # ── Cross geometry ───────────────────────────────────────────────
    cross_size = max(280, min(int(min(w, h) * 0.42), 660))
    cx = w // 2
    cy = int(h * 0.375)

    # Subtle glow halo — NO image, just layered translucent circles
    glow_r = int(cross_size * 0.72)
    draw_glow(img, cx, cy, glow_r, (20, 90, 200), strength=120)

    # The pure cross (no background box, no image)
    draw_pure_cross(img, cx, cy, cross_size)

    # ── Typography ───────────────────────────────────────────────────
    bold_size     = max(68, min(int(w * 0.110), 180))
    subtitle_size = max(32, min(int(w * 0.048), 82))
    tagline_size  = max(24, min(int(w * 0.032), 56))

    font_bold     = get_font(bold=True,  size=bold_size)
    font_subtitle = get_font(bold=True,  size=subtitle_size)
    font_tagline  = get_font(bold=False, size=tagline_size)

    draw = ImageDraw.Draw(img)

    # "PrimeCare" — two-tone word mark
    prime_bb = draw.textbbox((0, 0), "Prime", font=font_bold)
    care_bb  = draw.textbbox((0, 0), "Care",  font=font_bold)
    prime_w  = prime_bb[2] - prime_bb[0]
    care_w   = care_bb[2]  - care_bb[0]
    title_h  = prime_bb[3] - prime_bb[1]
    total_tw = prime_w + care_w
    tx = (w - total_tw) // 2

    text_y = cy + cross_size // 2 + int(h * 0.046)

    # drop shadow
    draw.text((tx + 2,          text_y + 3), "Prime", font=font_bold, fill=(0, 0, 0, 55))
    draw.text((tx + prime_w + 2, text_y + 3), "Care",  font=font_bold, fill=(0, 0, 0, 55))

    draw.text((tx,          text_y), "Prime", font=font_bold, fill=(255, 255, 255))
    draw.text((tx + prime_w, text_y), "Care",  font=font_bold, fill=(30, 220, 185))

    y = text_y + title_h + int(h * 0.020)

    # Subtitle
    sub_h = draw_text_centered(draw, "Healthcare Clinic Portal",
                                font_subtitle, y, (185, 225, 255), w)
    y += sub_h + int(h * 0.018)

    # Divider
    dw = int(w * 0.13)
    dx = (w - dw) // 2
    draw.rectangle([dx, y, dx + dw, y + 2], fill=(255, 255, 255, 80))
    y += int(h * 0.022)

    # Tagline
    draw_text_centered(draw, "Your health, our priority.",
                        font_tagline, y, (150, 200, 255), w, shadow_alpha=0)

    filename = f"apple-splash-{w}-{h}.png"
    img.convert("RGB").save(os.path.join(output_dir, filename), "PNG", optimize=True)
    print(f"  OK  {filename}")

print("Generating premium splash screens (pure icon, no image files)...")
for w, h in sizes:
    generate(w, h)
print("\nAll done!")

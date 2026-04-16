#!/usr/bin/env python3
"""
Generate PrimeCare PWA Icons
Creates white-on-blue versions of the PrimeCare logo for app icons.
Run from the doctorbook directory: python scratch/generate_pwa_icons.py
"""

import os, sys, subprocess

BASE   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(BASE, 'static')

# ── White-on-blue icon SVG ──────────────────────────────────────────────────
ICON_SVG = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" fill="none">
  <!-- Blue background with rounded corners -->
  <rect width="100" height="100" rx="18" fill="#0077b6"/>
  <!-- Outer ring - white -->
  <circle cx="50" cy="50" r="43" stroke="white" stroke-width="3"/>
  <!-- Inner ring - white -->
  <circle cx="50" cy="50" r="36" stroke="white" stroke-width="2"/>
  <!-- Medical cross - white fill -->
  <path d="M42 17 L58 17 L58 42 L83 42 L83 58 L58 58 L58 83 L42 83 L42 58 L17 58 L17 42 L42 42 Z" fill="white"/>
  <!-- ECG / heartbeat line - blue (over the cross) -->
  <polyline points="17,50 29,50 33,40 38,60 42.5,50 57.5,50 62,40 67,60 71,50 83,50"
            stroke="#0077b6" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"/>
</svg>'''

ICONS = [
    (os.path.join(STATIC, 'image', 'pwa-icon.png'),            512),
    (os.path.join(STATIC, 'android-chrome-512x512.png'),       512),
    (os.path.join(STATIC, 'android-chrome-192x192.png'),       192),
    (os.path.join(STATIC, 'apple-touch-icon.png'),             180),
]

# ── Try cairosvg ─────────────────────────────────────────────────────────────
def load_cairosvg():
    try:
        import cairosvg
        return cairosvg
    except ImportError:
        pass
    print("  Installing cairosvg...")
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'cairosvg'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import cairosvg
    return cairosvg

def gen_cairosvg(cs, path, size):
    cs.svg2png(bytestring=ICON_SVG, write_to=path,
               output_width=size, output_height=size)
    print(f"  [OK] {os.path.basename(path)} ({size}x{size})")

# ── Pillow fallback ───────────────────────────────────────────────────────────
def gen_pillow(path, size):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'Pillow'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        from PIL import Image, ImageDraw

    img  = Image.new('RGBA', (size, size), (0, 119, 182, 255))
    draw = ImageDraw.Draw(img)
    s    = size / 100

    # Cross (white)
    cross = [(42*s,17*s),(58*s,17*s),(58*s,42*s),(83*s,42*s),
             (83*s,58*s),(58*s,58*s),(58*s,83*s),(42*s,83*s),
             (42*s,58*s),(17*s,58*s),(17*s,42*s),(42*s,42*s)]
    draw.polygon(cross, fill='white')

    # ECG line (blue over cross)
    ecg = [(17*s,50*s),(29*s,50*s),(33*s,40*s),(38*s,60*s),
           (42.5*s,50*s),(57.5*s,50*s),(62*s,40*s),(67*s,60*s),
           (71*s,50*s),(83*s,50*s)]
    draw.line(ecg, fill='#0077b6', width=max(2, int(2.8*s)))

    # Outer ring
    m1 = int(7*s)
    draw.ellipse([m1, m1, size-m1, size-m1], outline='white', width=max(2, int(3*s)))
    # Inner ring
    m2 = int(14*s)
    draw.ellipse([m2, m2, size-m2, size-m2], outline='white', width=max(1, int(2*s)))

    img.save(path)
    print(f"  [OK] {os.path.basename(path)} ({size}x{size}) [Pillow]")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("\nPrimeCare PWA Icon Generator")
    print("=" * 40)

    cs = None
    try:
        cs = load_cairosvg()
        print("  Using cairosvg renderer")
    except Exception as e:
        print(f"  cairosvg unavailable ({e}), using Pillow")

    for path, size in ICONS:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        if cs:
            gen_cairosvg(cs, path, size)
        else:
            gen_pillow(path, size)

    print("\n[OK] All icons generated!")
    print("   Restart the Flask app (and clear browser PWA cache) to see changes.\n")

if __name__ == '__main__':
    main()

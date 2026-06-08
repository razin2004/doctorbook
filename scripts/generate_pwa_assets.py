import os
from PIL import Image

def generate_assets():
    source_icon_path = 'static/image/pwa-icon.png'
    output_dir = 'static/pwa'
    
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"Created output directory: {output_dir}")
        
    if not os.path.exists(source_icon_path):
        print(f"Error: Source icon not found at {source_icon_path}")
        return
        
    # Load source icon (512x512)
    source_icon = Image.open(source_icon_path)
    
    # 1. Generate standard PWA icons
    icon_sizes = [48, 72, 96, 128, 144, 152, 192, 384, 512]
    for size in icon_sizes:
        resized_icon = source_icon.resize((size, size), Image.Resampling.LANCZOS)
        resized_icon.save(os.path.join(output_dir, f'icon-{size}x{size}.png'))
        print(f"Generated icon: icon-{size}x{size}.png")
        
    # 2. Generate iOS startup splash screens
    # Background color matches the primary PrimeCare brand color
    bg_color = (0, 119, 182) # #0077b6
    
    splash_screens = [
        {"width": 2048, "height": 2732, "name": "apple-splash-2048-2732.png"}, # iPad Pro 12.9"
        {"width": 1668, "height": 2388, "name": "apple-splash-1668-2388.png"}, # iPad Pro 11"
        {"width": 1536, "height": 2048, "name": "apple-splash-1536-2048.png"}, # iPad 9.7"
        {"width": 1668, "height": 2224, "name": "apple-splash-1668-2224.png"}, # iPad Pro 10.5"
        {"width": 1620, "height": 2160, "name": "apple-splash-1620-2160.png"}, # iPad 10.2"
        {"width": 1290, "height": 2796, "name": "apple-splash-1290-2796.png"}, # iPhone 14/15 Pro Max
        {"width": 1179, "height": 2556, "name": "apple-splash-1179-2556.png"}, # iPhone 14/15 Pro
        {"width": 1284, "height": 2778, "name": "apple-splash-1284-2778.png"}, # iPhone 14 Plus / 13 Pro Max
        {"width": 1170, "height": 2532, "name": "apple-splash-1170-2532.png"}, # iPhone 14 / 13 Pro / 13 / 12
        {"width": 1125, "height": 2436, "name": "apple-splash-1125-2436.png"}, # iPhone X / XS / 11 Pro
        {"width": 1242, "height": 2688, "name": "apple-splash-1242-2688.png"}, # iPhone XS Max / 11 Pro Max
        {"width": 828,  "height": 1792, "name": "apple-splash-828-1792.png"},  # iPhone XR / 11
        {"width": 1242, "height": 2208, "name": "apple-splash-1242-2208.png"}, # iPhone 8 Plus / 7 Plus
        {"width": 750,  "height": 1334, "name": "apple-splash-750-1334.png"},  # iPhone 8 / 7 / 6s
        {"width": 640,  "height": 1136, "name": "apple-splash-640-1136.png"},  # iPhone 5 / SE
        {"width": 1640, "height": 2360, "name": "apple-splash-1640-2360.png"}, # iPad Air 10.9"
        {"width": 1488, "height": 2266, "name": "apple-splash-1488-2266.png"}, # iPad mini 8.3"
    ]
    
    for splash in splash_screens:
        w = splash["width"]
        h = splash["height"]
        name = splash["name"]
        
        # Create solid background image
        canvas = Image.new('RGB', (w, h), bg_color)
        
        # Calculate proportional size for the logo icon (around 22% of min dimension, capped)
        icon_dim = int(min(w, h) * 0.22)
        icon_dim = max(120, min(256, icon_dim))
        
        # Resize source icon to centered logo
        logo = source_icon.resize((icon_dim, icon_dim), Image.Resampling.LANCZOS)
        
        # Center the logo on the canvas
        offset_x = (w - icon_dim) // 2
        offset_y = (h - icon_dim) // 2
        
        canvas.paste(logo, (offset_x, offset_y))
        canvas.save(os.path.join(output_dir, name))
        print(f"Generated splash screen: {name} ({w}x{h}, logo: {icon_dim}px)")

    print("PWA asset generation completed successfully!")

if __name__ == '__main__':
    generate_assets()

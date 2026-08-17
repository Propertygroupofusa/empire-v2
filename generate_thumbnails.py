#!/usr/bin/env python3
"""
Generate YouTube thumbnail variations for test video
3 variations with different styles/colors
"""

from PIL import Image, ImageDraw, ImageFont
import os

THUMB_WIDTH = 1280
THUMB_HEIGHT = 720

# Color schemes
COLORS = {
    'green': (34, 197, 94),      # Gig economy green
    'blue': (59, 130, 246),      # Professional blue
    'dark': (20, 20, 25),        # Dark background
    'white': (255, 255, 255),    # White text
    'yellow': (234, 179, 8),     # Accent yellow
    'orange': (249, 115, 22),    # Energy orange
    'red': (239, 68, 68),        # Attention red
}

def get_font(size=60):
    """Get best available font"""
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
    ]
    for path in font_paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except:
                continue
    return ImageFont.load_default()

def create_thumbnail_v1():
    """
    Variation A: Bright green background, clean and bold
    High contrast, maximum readability
    """
    img = Image.new('RGB', (THUMB_WIDTH, THUMB_HEIGHT), COLORS['green'])
    draw = ImageDraw.Draw(img)

    # Add dark overlay bar for text
    overlay_height = 280
    draw.rectangle([(0, THUMB_HEIGHT - overlay_height), (THUMB_WIDTH, THUMB_HEIGHT)],
                   fill=(20, 20, 25))

    # Main text: $1K/WEEK
    font_main = get_font(220)
    text_1k = "$1K/WEEK"
    bbox = draw.textbbox((0, 0), text_1k, font=font_main)
    text_width = bbox[2] - bbox[0]
    x = (THUMB_WIDTH - text_width) // 2
    draw.text((x, 150), text_1k, fill=COLORS['white'], font=font_main)

    # Subtext: On DoorDash
    font_sub = get_font(90)
    text_sub = "On DoorDash?"
    bbox_sub = draw.textbbox((0, 0), text_sub, font=font_sub)
    text_width_sub = bbox_sub[2] - bbox_sub[0]
    x_sub = (THUMB_WIDTH - text_width_sub) // 2
    draw.text((x_sub, THUMB_HEIGHT - 220), text_sub, fill=COLORS['yellow'], font=font_sub)

    # Emoji or accent (optional small text)
    font_small = get_font(50)
    draw.text((100, THUMB_HEIGHT - 100), "💰", font=font_small)
    draw.text((THUMB_WIDTH - 150, THUMB_HEIGHT - 100), "📈", font=font_small)

    img.save('/tmp/thumbnail_v1_green.png')
    return '/tmp/thumbnail_v1_green.png'

def create_thumbnail_v2():
    """
    Variation B: Dark background with bold colored text
    Modern, high contrast approach
    """
    img = Image.new('RGB', (THUMB_WIDTH, THUMB_HEIGHT), COLORS['dark'])
    draw = ImageDraw.Draw(img)

    # Add bright accent bar (top)
    bar_height = 80
    draw.rectangle([(0, 0), (THUMB_WIDTH, bar_height)], fill=COLORS['orange'])

    # Main number: $1K
    font_huge = get_font(250)
    text_amount = "$1K"
    bbox = draw.textbbox((0, 0), text_amount, font=font_huge)
    text_width = bbox[2] - bbox[0]
    x = (THUMB_WIDTH - text_width) // 2
    draw.text((x, 120), text_amount, fill=COLORS['orange'], font=font_huge)

    # Subtext line 1: Per Week
    font_large = get_font(100)
    text_period = "Per Week"
    bbox_period = draw.textbbox((0, 0), text_period, font=font_large)
    text_width_period = bbox_period[2] - bbox_period[0]
    x_period = (THUMB_WIDTH - text_width_period) // 2
    draw.text((x_period, 380), text_period, fill=COLORS['white'], font=font_large)

    # Subtext line 2: On DoorDash
    font_med = get_font(80)
    text_platform = "DoorDash 🚗"
    bbox_platform = draw.textbbox((0, 0), text_platform, font=font_med)
    text_width_platform = bbox_platform[2] - bbox_platform[0]
    x_platform = (THUMB_WIDTH - text_width_platform) // 2
    draw.text((x_platform, 500), text_platform, fill=COLORS['yellow'], font=font_med)

    img.save('/tmp/thumbnail_v2_dark.png')
    return '/tmp/thumbnail_v2_dark.png'

def create_thumbnail_v3():
    """
    Variation C: Split design with earnings callout
    Left side: Amount, Right side: How
    """
    img = Image.new('RGB', (THUMB_WIDTH, THUMB_HEIGHT), COLORS['blue'])
    draw = ImageDraw.Draw(img)

    # Left half accent
    draw.rectangle([(0, 0), (THUMB_WIDTH // 2, THUMB_HEIGHT)], fill=COLORS['green'])

    # Left side: Big money number
    font_huge = get_font(280)
    text_money = "$1K"
    bbox_money = draw.textbbox((0, 0), text_money, font=font_huge)
    text_width_money = bbox_money[2] - bbox_money[0]
    x_money = ((THUMB_WIDTH // 2) - text_width_money) // 2
    draw.text((x_money, 150), text_money, fill=COLORS['white'], font=font_huge)

    # Right side: Platform name (big)
    font_large = get_font(120)
    text_doordash = "DoorDash"
    bbox_dd = draw.textbbox((0, 0), text_doordash, font=font_large)
    text_width_dd = bbox_dd[2] - bbox_dd[0]
    x_dd = (THUMB_WIDTH // 2) + ((THUMB_WIDTH // 2) - text_width_dd) // 2
    draw.text((x_dd, 200), text_doordash, fill=COLORS['white'], font=font_large)

    # Right side: Per week
    font_med = get_font(90)
    text_period = "Per Week"
    bbox_period = draw.textbbox((0, 0), text_period, font=font_med)
    text_width_period = bbox_period[2] - bbox_period[0]
    x_period = (THUMB_WIDTH // 2) + ((THUMB_WIDTH // 2) - text_width_period) // 2
    draw.text((x_period, 360), text_period, fill=COLORS['yellow'], font=font_med)

    # Bottom bar with question
    draw.rectangle([(0, THUMB_HEIGHT - 100), (THUMB_WIDTH, THUMB_HEIGHT)],
                   fill=(0, 0, 0))
    font_question = get_font(70)
    text_q = "Can You? 🤔"
    bbox_q = draw.textbbox((0, 0), text_q, font=font_question)
    text_width_q = bbox_q[2] - bbox_q[0]
    x_q = (THUMB_WIDTH - text_width_q) // 2
    draw.text((x_q, THUMB_HEIGHT - 85), text_q, fill=COLORS['yellow'], font=font_question)

    img.save('/tmp/thumbnail_v3_split.png')
    return '/tmp/thumbnail_v3_split.png'

def main():
    print("=" * 60)
    print("YOUTUBE THUMBNAIL GENERATOR")
    print("Video: Can You Make $1K/Week on DoorDash?")
    print("=" * 60)

    print("\n🎨 Creating thumbnail variations...\n")

    v1 = create_thumbnail_v1()
    print(f"✅ Variation A (Green Bold): {v1}")

    v2 = create_thumbnail_v2()
    print(f"✅ Variation B (Dark Modern): {v2}")

    v3 = create_thumbnail_v3()
    print(f"✅ Variation C (Split Design): {v3}")

    print("\n" + "=" * 60)
    print("THUMBNAIL GUIDE")
    print("=" * 60)

    guide = """
VARIATION A: Green Background
- Most eye-catching for mobile scrolling
- High contrast (green + white + yellow)
- Best for casual audience
- Use if: Want maximum click-through rate
- File: thumbnail_v1_green.png

VARIATION B: Dark Modern
- Professional, clean look
- Orange accent bar adds energy
- High readability on all backgrounds
- Use if: Want to look polished and trustworthy
- File: thumbnail_v2_dark.png

VARIATION C: Split Design
- Bold, divided visual impact
- Blue + Green contrast catches eye
- "Can You?" question creates curiosity
- Use if: Want to test curiosity-driven CTR
- File: thumbnail_v3_split.png

RECOMMENDATION:
Start with Variation A (green) - highest CTR potential for gig economy content.
After upload, you can replace with Variation B or C if needed.

UPLOAD INSTRUCTIONS:
1. Download all 3 PNG files
2. Upload Video 1 with Variation A
3. Monitor performance (views, CTR)
4. After 7 days, can A/B test by changing thumbnail
5. YouTube allows thumbnail changes anytime
"""

    print(guide)

    print("\n" + "=" * 60)
    print("FILES READY FOR YOUTUBE")
    print("=" * 60)
    print("\nAll thumbnails are 1280×720 (YouTube standard)")
    print("Ready to upload immediately")
    print("\n💾 Download all 3 files and pick one for upload")
    print("📤 Can change thumbnail anytime in YouTube Studio")

if __name__ == "__main__":
    main()

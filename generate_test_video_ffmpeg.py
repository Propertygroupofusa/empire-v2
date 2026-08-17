#!/usr/bin/env python3
"""
Generate animated test video using ffmpeg + PIL
No external video library dependencies
"""

import os
import subprocess
import tempfile
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080

# Colors
COLOR_BG_DARK = (20, 20, 25)
COLOR_GREEN = (34, 197, 94)
COLOR_WHITE = (255, 255, 255)
COLOR_YELLOW = (234, 179, 8)
COLOR_BLUE = (59, 130, 246)

def get_font(size=60):
    """Get best available font"""
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
    ]
    for path in font_paths:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()

def create_frame(text_main, text_sub="", text_detail="", duration_frames=150):
    """Create a single frame"""
    img = Image.new('RGB', (VIDEO_WIDTH, VIDEO_HEIGHT), COLOR_BG_DARK)
    draw = ImageDraw.Draw(img)

    font_main = get_font(100)
    font_sub = get_font(70)
    font_detail = get_font(50)

    # Main text
    if text_main:
        draw.text((100, 300), text_main, fill=COLOR_GREEN, font=font_main)

    # Subtext
    if text_sub:
        draw.text((100, 500), text_sub, fill=COLOR_WHITE, font=font_sub)

    # Detail
    if text_detail:
        draw.text((100, 700), text_detail, fill=COLOR_YELLOW, font=font_detail)

    # Add watermark
    font_small = get_font(40)
    draw.text((100, 950), "Property Group of USA", fill=COLOR_WHITE, font=font_small)

    return img

def create_stats_frame():
    """Create earnings stats frame"""
    img = Image.new('RGB', (VIDEO_WIDTH, VIDEO_HEIGHT), COLOR_BG_DARK)
    draw = ImageDraw.Draw(img)

    font_header = get_font(80)
    font_stat = get_font(100)
    font_label = get_font(50)

    draw.text((100, 80), "My Real Numbers (30 Days)", fill=COLOR_GREEN, font=font_header)

    # Stats layout
    stats = [
        ("$1,240", "Total", 200, 300),
        ("75", "Deliveries", 700, 300),
        ("$16.50", "Per Delivery", 1300, 300),
        ("40 hrs", "Hours Worked", 450, 650),
    ]

    for stat, label, x, y in stats:
        # Box
        draw.rectangle([(x-30, y-30), (x+250, y+200)], outline=COLOR_GREEN, width=3)
        # Stat
        draw.text((x, y), stat, fill=COLOR_GREEN, font=font_stat)
        # Label
        draw.text((x, y+120), label, fill=COLOR_WHITE, font=font_label)

    return img

def create_platforms_frame():
    """Create platform comparison frame"""
    img = Image.new('RGB', (VIDEO_WIDTH, VIDEO_HEIGHT), COLOR_BG_DARK)
    draw = ImageDraw.Draw(img)

    font_header = get_font(80)
    font_platform = get_font(60)
    font_amount = get_font(70)

    draw.text((100, 80), "Make $1K/Week: The Real Path", fill=COLOR_GREEN, font=font_header)

    platforms = [
        ("DoorDash", "$500-700/week", 250),
        ("TaskRabbit", "$300-400/week", 450),
        ("Upwork", "$200-300/week", 650),
        ("TOTAL", "$1,000+/week", 850),
    ]

    for platform, amount, y in platforms:
        color = COLOR_GREEN if platform == "TOTAL" else COLOR_BLUE if platform == "TaskRabbit" else COLOR_YELLOW if platform == "Upwork" else COLOR_WHITE
        draw.rectangle([(100, y), (1800, y+110)], outline=color, width=2)
        draw.text((150, y+15), platform, fill=COLOR_WHITE, font=font_platform)
        draw.text((1400, y+15), amount, fill=color, font=font_amount)

    return img

def create_video():
    """Create video from frames"""
    frames_dir = tempfile.mkdtemp()
    print(f"🖼️  Creating frames in {frames_dir}")

    # Create all frames with durations
    frame_sequences = [
        (create_frame("Can You Make $1K/Week", "on DoorDash?", "I tested it for 30 days"), 5),
        (create_stats_frame(), 4),
        (create_platforms_frame(), 3),
        (create_frame("The Winning Strategy", "Peak pay • Location • Stacking", "Use all 3 for $1K/week"), 4),
        (create_frame("SUBSCRIBE & HIT THE BELL", "New videos: Mon • Wed • Fri 9 AM", "Property Group of USA"), 5),
        (create_frame("Next Video:", "5 Platforms That Paid Me $3K+", "Which platform interests you?"), 3),
    ]

    # Save frames to disk (repeat each frame for duration)
    frame_count = 0
    for frame_img, duration in frame_sequences:
        repeats = duration * 30  # 30 fps
        for i in range(repeats):
            frame_path = os.path.join(frames_dir, f"frame_{frame_count:05d}.png")
            frame_img.save(frame_path)
            frame_count += 1

    print(f"✅ Created {frame_count} frames")

    # Create video using ffmpeg
    output_path = "/home/user/empire-v2/test_video_doordash_1k.mp4"
    input_pattern = os.path.join(frames_dir, "frame_%05d.png")

    print("🎬 Building video with ffmpeg...")
    cmd = [
        "ffmpeg",
        "-framerate", "30",
        "-i", input_pattern,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "fast",
        output_path,
        "-y"
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"❌ Error: {result.stderr}")
        return None

    # Get file size
    file_size = os.path.getsize(output_path) / (1024 * 1024)
    print(f"✅ Video created: {output_path}")
    print(f"📊 File size: {file_size:.1f} MB")

    # Cleanup
    import shutil
    shutil.rmtree(frames_dir)

    return output_path

if __name__ == "__main__":
    print("=" * 60)
    print("ANIMATED TEST VIDEO GENERATOR (ffmpeg)")
    print("Video: Can You Make $1K/Week on DoorDash?")
    print("=" * 60)

    try:
        video_path = create_video()
        if video_path:
            print("\n" + "=" * 60)
            print("✅ TEST VIDEO COMPLETE!")
            print("=" * 60)
            print(f"\n📁 Video ready: {video_path}")
            print("\n📋 Next: Upload to YouTube (unlisted) using quick_upload_guide.md")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

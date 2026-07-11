#!/usr/bin/env python3
"""
Quick test: Generate a sample video
This creates a professional trading update video in 60 seconds
"""

import asyncio
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import edge_tts
import sys

print("🎬 EMPIRE VIDEO GENERATOR - TEST")
print("=" * 60)
print()

# Setup
TEMP_DIR = Path("video_temp") / "test_generation"
TEMP_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR = Path("video_exports")
EXPORT_DIR.mkdir(exist_ok=True)

text = "Today we made five thousand dollars on Apple stock. Great market performance. Stay focused on your winners."
output_file = EXPORT_DIR / "test_video.mp4"

print(f"📝 Text: {text}")
print()

# Step 1: Generate audio
print("Step 1️⃣  Generating AI voice...")
audio_file = TEMP_DIR / "audio.mp3"

async def generate_audio():
    communicate = edge_tts.Communicate(text, "en-US-AriaNeural")
    await communicate.save(str(audio_file))
    print(f"   ✅ Voice generated: {audio_file}")

asyncio.run(generate_audio())
print()

# Step 2: Get audio duration
print("Step 2️⃣  Calculating video duration...")
try:
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1:novalue=1', str(audio_file)],
        capture_output=True,
        text=True,
        timeout=30
    )
    duration = float(result.stdout.strip())
    print(f"   ✅ Audio duration: {duration:.1f} seconds")
except Exception as e:
    print(f"   ⚠️  Could not get duration, using default: {e}")
    duration = 10
print()

# Step 3: Create video frames
print("Step 3️⃣  Creating video frames...")
frames_dir = TEMP_DIR / "frames"
frames_dir.mkdir(exist_ok=True)

fps = 24
total_frames = int(duration * fps)

# Template config (TRADING style)
template = {
    'bg_color': (10, 20, 40),  # Dark blue
    'accent_color': (255, 215, 0),  # Gold
    'text_color': (255, 255, 255),  # White
    'font_size': 72,
    'width': 1920,
    'height': 1080
}

# Create frames with animated text
for frame_num in range(total_frames):
    img = Image.new('RGB', (template['width'], template['height']), template['bg_color'])
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                                 template['font_size'])
    except:
        font = ImageFont.load_default()

    # Split text into lines
    words = text.split()
    lines = []
    current_line = []

    for word in words:
        current_line.append(word)
        test_line = ' '.join(current_line)
        bbox = draw.textbbox((0, 0), test_line, font=font)
        line_width = bbox[2] - bbox[0]

        if line_width > template['width'] - 100:
            lines.append(' '.join(current_line[:-1]))
            current_line = [word]

    lines.append(' '.join(current_line))

    # Draw text lines
    total_height = len(lines) * (template['font_size'] + 20)
    start_y = (template['height'] - total_height) // 2

    for i, line in enumerate(lines):
        y = start_y + i * (template['font_size'] + 20)
        bbox = draw.textbbox((0, 0), line, font=font)
        line_width = bbox[2] - bbox[0]
        x = (template['width'] - line_width) // 2
        draw.text((x, y), line, fill=template['text_color'], font=font)

    # Draw accent line
    line_y = start_y - 50
    draw.rectangle([(template['width'] // 4, line_y),
                   (3 * template['width'] // 4, line_y + 4)],
                   fill=template['accent_color'])

    # Draw watermark
    try:
        small_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
    except:
        small_font = font

    draw.text((30, template['height'] - 60), 'EMPIRE',
             fill=template['accent_color'], font=small_font)

    # Save frame
    img.save(frames_dir / f'frame_{frame_num:04d}.png')

    if frame_num % 24 == 0:
        progress = int((frame_num / total_frames) * 100)
        print(f"   ⏳ Creating frames... {progress}%")

print(f"   ✅ Created {total_frames} frames")
print()

# Step 4: Combine frames and audio into video
print("Step 4️⃣  Encoding video (this takes ~10-20 seconds)...")

cmd = [
    'ffmpeg',
    '-framerate', str(fps),
    '-i', str(frames_dir / 'frame_%04d.png'),
    '-i', str(audio_file),
    '-c:v', 'libx264',
    '-c:a', 'aac',
    '-shortest',
    '-pix_fmt', 'yuv420p',
    '-y', str(output_file)
]

result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

if result.returncode == 0:
    print(f"   ✅ Video created: {output_file}")
else:
    print(f"   ❌ FFmpeg error: {result.stderr}")
    sys.exit(1)

print()
print("=" * 60)
print("✅✅✅ VIDEO GENERATED SUCCESSFULLY! ✅✅✅")
print()
print(f"📁 Output: {output_file}")
print(f"📊 Duration: {duration:.1f} seconds")
print(f"📐 Resolution: 1920x1080 (Trading template)")
print()
print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
print()
print("🎯 NEXT STEPS:")
print()
print("1. Start the video generator service:")
print("   python video_generator_bot.py")
print()
print("2. Use the API to generate videos from text:")
print("   curl -X POST http://localhost:5003/api/video-gen/generate \\")
print("     -H 'Content-Type: application/json' \\")
print("     -d '{\"text\":\"Your text here\",\"videoType\":\"trading\",\"autoPublish\":true}'")
print()
print("3. Check status:")
print("   curl http://localhost:5003/api/video-gen/status/job-id")
print()
print("4. Start the web editor:")
print("   cd video-editor && npm start  # Port 3000")
print()
print("5. Deploy everything:")
print("   docker-compose up")
print()
print("=" * 60)
print()
print("💰 YOU'RE SAVING: $6,000/YEAR (vs Synthesia)")
print()

# Cleanup (keep the video, remove temp frames)
import shutil
shutil.rmtree(TEMP_DIR / "frames", ignore_errors=True)
print("✅ Test complete!")

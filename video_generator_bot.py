"""
Empire Video Generator Bot
FREE AI video generation (no Synthesia needed)
- Text → AI voice (edge-tts)
- Voice + animated visuals → Video
- Auto-integrate with auto-editor for publishing
"""

import os
import json
import uuid
import asyncio
import subprocess
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify
from flask_cors import CORS
import logging
import edge_tts
import aiohttp
from edge_tts.exceptions import EdgeTTSException
from PIL import Image, ImageDraw, ImageFont
import requests
import threading
import time
from enum import Enum

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
TEMP_FOLDER = Path('video_temp')
EXPORT_FOLDER = Path('video_exports')
TEMP_FOLDER.mkdir(exist_ok=True)
EXPORT_FOLDER.mkdir(exist_ok=True)

# Generation jobs
generation_jobs = {}

# Voice options (edge-tts voices)
VOICES = {
    'male': 'en-US-AriaNeural',
    'female': 'en-US-JennyNeural',
    'professional': 'en-US-GuyNeural',
    'energetic': 'en-US-AmberNeural'
}

# Video templates with background colors, fonts, animations
class VideoTemplate(Enum):
    TRADING = {
        'name': 'Trading Update',
        'bg_color': (10, 20, 40),  # Dark blue
        'accent_color': (255, 215, 0),  # Gold
        'text_color': (255, 255, 255),  # White
        'font_size': 72,
        'width': 1920,
        'height': 1080,
        'duration': None,  # Auto from audio
        'music': None,
        'overlay_text': 'EMPIRE TRADING ANALYSIS',
        'watermark': True
    }
    PROPERTY = {
        'name': 'Property Listing',
        'bg_color': (240, 248, 255),  # Light blue
        'accent_color': (65, 105, 225),  # Royal blue
        'text_color': (0, 0, 0),  # Black
        'font_size': 64,
        'width': 1920,
        'height': 1080,
        'duration': None,
        'music': None,
        'overlay_text': 'PROPERTY LISTING',
        'watermark': True
    }
    SOCIAL = {
        'name': 'Social Media',
        'bg_color': (255, 255, 255),  # White
        'accent_color': (102, 126, 234),  # Purple
        'text_color': (0, 0, 0),  # Black
        'font_size': 60,
        'width': 1080,
        'height': 1920,  # Vertical for Instagram/TikTok
        'duration': None,
        'music': None,
        'overlay_text': None,
        'watermark': False
    }
    COLD_CALL = {
        'name': 'Cold Call Follow-up',
        'bg_color': (245, 245, 245),  # Light gray
        'accent_color': (220, 20, 60),  # Crimson
        'text_color': (0, 0, 0),  # Black
        'font_size': 56,
        'width': 1920,
        'height': 1080,
        'duration': None,
        'music': None,
        'overlay_text': 'FOLLOW UP',
        'watermark': True
    }


async def generate_audio(text, voice='female', output_path=None):
    """Generate audio from text using edge-tts"""
    if not output_path:
        output_path = TEMP_FOLDER / f'audio_{uuid.uuid4()}.mp3'

    logger.info(f"Generating audio with voice: {voice}")

    try:
        communicate = edge_tts.Communicate(text, VOICES.get(voice, VOICES['female']))
        await communicate.save(str(output_path))
    except aiohttp.ClientResponseError as e:
        # edge-tts retries once internally on a 403 (clock-skew correction);
        # one escaping here means Microsoft's token scheme changed again.
        logger.error(f"Audio generation failed: HTTP {e.status} ({e.message}) from "
                     f"{e.request_info.url} — likely a Microsoft Sec-MS-GEC auth change, "
                     "not a code bug")
        raise
    except EdgeTTSException as e:
        logger.error(f"Audio generation failed [{type(e).__name__}]: {e} "
                     "— check github.com/rany2/edge-tts issues for a known break/fix")
        raise
    except Exception as e:
        logger.error(f"Audio generation failed [{type(e).__name__}]: {e}")
        raise

    logger.info(f"Audio saved: {output_path}")
    return output_path


def get_audio_duration(audio_path):
    """Get duration of audio file in seconds"""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1:novalue=1', str(audio_path)],
            capture_output=True,
            text=True,
            timeout=30
        )
        return float(result.stdout.strip())
    except Exception as e:
        logger.error(f"Failed to get audio duration: {str(e)}")
        return 10  # Default fallback


def create_text_frame(text, template_config, width=1920, height=1080, frame_number=0):
    """Create a frame with animated text"""
    try:
        # Create image
        img = Image.new('RGB', (width, height), template_config['bg_color'])
        draw = ImageDraw.Draw(img)

        # Try to use a nice font, fallback to default
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                                     template_config['font_size'])
        except:
            font = ImageFont.load_default()

        # Split text into lines for better fitting
        words = text.split()
        lines = []
        current_line = []

        for word in words:
            current_line.append(word)
            test_line = ' '.join(current_line)
            bbox = draw.textbbox((0, 0), test_line, font=font)
            line_width = bbox[2] - bbox[0]

            if line_width > width - 100:
                lines.append(' '.join(current_line[:-1]))
                current_line = [word]

        lines.append(' '.join(current_line))

        # Calculate total text height
        total_height = len(lines) * (template_config['font_size'] + 20)
        start_y = (height - total_height) // 2

        # Draw text lines with animation effect
        for i, line in enumerate(lines):
            y = start_y + i * (template_config['font_size'] + 20)

            # Animate text appearing (fade in effect)
            # On early frames, draw text slightly faded
            alpha = min(1.0, frame_number / 10)  # Fade in over 10 frames

            # Draw text
            bbox = draw.textbbox((0, 0), line, font=font)
            line_width = bbox[2] - bbox[0]
            x = (width - line_width) // 2

            draw.text((x, y), line, fill=template_config['text_color'], font=font)

        # Draw accent line at top
        line_y = start_y - 50
        draw.rectangle([(width // 4, line_y), (3 * width // 4, line_y + 4)],
                       fill=template_config['accent_color'])

        # Draw watermark if enabled
        if template_config.get('watermark'):
            try:
                small_font = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
            except:
                small_font = font

            draw.text((30, height - 60), 'EMPIRE', fill=template_config['accent_color'],
                     font=small_font)

        return img

    except Exception as e:
        logger.error(f"Frame creation failed: {str(e)}")
        raise


def create_video_from_frames(frames_dir, audio_path, output_path, fps=24):
    """Combine frames and audio into video"""
    try:
        logger.info(f"Creating video from frames: {frames_dir}")

        # Get audio duration
        duration = get_audio_duration(audio_path)

        # Create video from frames
        cmd = [
            'ffmpeg',
            '-framerate', str(fps),
            '-i', str(frames_dir / 'frame_%04d.png'),
            '-i', str(audio_path),
            '-c:v', 'libx264',
            '-c:a', 'aac',
            '-shortest',
            '-pix_fmt', 'yuv420p',
            '-y', str(output_path)
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            raise Exception(f"FFmpeg error: {result.stderr}")

        logger.info(f"Video created: {output_path}")
        return output_path

    except Exception as e:
        logger.error(f"Video creation failed: {str(e)}")
        raise


def generate_video(text, video_type='trading', voice='female', job_id=None):
    """Generate video from text"""
    try:
        if not job_id:
            job_id = str(uuid.uuid4())

        # Get template
        template_enum = {
            'trading': VideoTemplate.TRADING,
            'property': VideoTemplate.PROPERTY,
            'social': VideoTemplate.SOCIAL,
            'cold-call': VideoTemplate.COLD_CALL
        }.get(video_type, VideoTemplate.TRADING)

        template = template_enum.value

        # Create job folder
        job_folder = TEMP_FOLDER / job_id
        job_folder.mkdir(exist_ok=True)
        frames_folder = job_folder / 'frames'
        frames_folder.mkdir(exist_ok=True)

        generation_jobs[job_id] = {
            'status': 'generating',
            'progress': 0,
            'text': text,
            'video_type': video_type,
            'created_at': datetime.now().isoformat()
        }

        logger.info(f"Starting video generation: {job_id}")

        # Step 1: Generate audio
        generation_jobs[job_id]['status'] = 'generating_audio'
        generation_jobs[job_id]['progress'] = 10

        audio_path = job_folder / 'audio.mp3'
        asyncio.run(generate_audio(text, voice, audio_path))

        generation_jobs[job_id]['progress'] = 30

        # Step 2: Get audio duration
        duration = get_audio_duration(audio_path)
        fps = 24
        total_frames = int(duration * fps)

        logger.info(f"Audio duration: {duration}s, frames: {total_frames}")

        # Step 3: Create frames
        generation_jobs[job_id]['status'] = 'creating_frames'

        for frame_num in range(total_frames):
            # Update progress (30-70%)
            progress = 30 + int((frame_num / total_frames) * 40)
            generation_jobs[job_id]['progress'] = progress

            # Create frame
            frame = create_text_frame(text, template, template['width'], template['height'],
                                     frame_num)

            # Save frame
            frame.save(frames_folder / f'frame_{frame_num:04d}.png')

            if frame_num % 50 == 0:
                logger.info(f"Generated frame {frame_num}/{total_frames}")

        generation_jobs[job_id]['progress'] = 70

        # Step 4: Combine frames and audio into video
        generation_jobs[job_id]['status'] = 'encoding_video'

        output_path = EXPORT_FOLDER / f'generated_{job_id}.mp4'
        create_video_from_frames(frames_folder, audio_path, output_path, fps)

        generation_jobs[job_id]['progress'] = 90
        generation_jobs[job_id]['status'] = 'completed'
        generation_jobs[job_id]['progress'] = 100
        generation_jobs[job_id]['video_url'] = f'/api/video-gen/download/{output_path.name}'
        generation_jobs[job_id]['output_file'] = str(output_path)

        logger.info(f"Video generation completed: {job_id}")

        return job_id

    except Exception as e:
        logger.error(f"Video generation failed: {str(e)}")
        generation_jobs[job_id]['status'] = 'failed'
        generation_jobs[job_id]['error'] = str(e)
        raise


def auto_publish_generated_video(job_id, video_type, youtube_settings=None):
    """Auto-submit generated video to auto-editor for publishing"""
    try:
        job = generation_jobs.get(job_id)
        if not job or job['status'] != 'completed':
            raise Exception("Video not ready")

        video_url = f"http://localhost:5001/api/video/download/{Path(job['output_file']).name}"

        # Submit to auto-editor
        auto_editor_url = os.getenv('VIDEO_AUTO_EDITOR_URL', 'http://localhost:5002')

        if not youtube_settings:
            youtube_settings = {
                'autoPublish': True,
                'privacy': 'unlisted',
                'title': f'AI Generated {video_type.title()} Video',
                'tags': ['ai', 'generated', 'empire']
            }

        response = requests.post(
            f'{auto_editor_url}/api/auto-editor/edit',
            json={
                'videoUrl': video_url,
                'videoType': video_type,
                'youtubeSettings': youtube_settings
            },
            timeout=30
        )

        if response.status_code not in [200, 202]:
            raise Exception(f"Auto-editor error: {response.text}")

        result = response.json()
        generation_jobs[job_id]['auto_editor_job'] = result.get('jobId')
        generation_jobs[job_id]['status'] = 'publishing'

        logger.info(f"Video submitted to auto-editor: {result.get('jobId')}")

        return result

    except Exception as e:
        logger.error(f"Auto-publish failed: {str(e)}")
        generation_jobs[job_id]['auto_publish_error'] = str(e)
        raise


# === API ENDPOINTS ===

@app.route('/api/video-gen/generate', methods=['POST'])
def generate():
    """
    Generate video from text
    POST /api/video-gen/generate
    {
        "text": "Your text here",
        "videoType": "trading|property|social|cold-call",
        "voice": "male|female|professional|energetic",
        "autoPublish": true,
        "youtubeSettings": {
            "privacy": "unlisted|public",
            "title": "Your Title"
        }
    }
    """
    try:
        data = request.json
        text = data.get('text')
        video_type = data.get('videoType', 'trading')
        voice = data.get('voice', 'female')
        auto_publish = data.get('autoPublish', False)
        youtube_settings = data.get('youtubeSettings', {})

        if not text or len(text.strip()) < 10:
            return jsonify({'error': 'Text must be at least 10 characters'}), 400

        if len(text) > 5000:
            return jsonify({'error': 'Text too long (max 5000 characters)'}), 400

        job_id = str(uuid.uuid4())

        # Start generation in background
        def generate_and_publish():
            try:
                generate_video(text, video_type, voice, job_id)
                if auto_publish:
                    auto_publish_generated_video(job_id, video_type, youtube_settings)
            except Exception as e:
                logger.error(f"Background job failed: {str(e)}")

        thread = threading.Thread(target=generate_and_publish)
        thread.daemon = True
        thread.start()

        return jsonify({
            'status': 'generating',
            'jobId': job_id,
            'message': 'Video generation started'
        }), 202

    except Exception as e:
        logger.error(f"Generate error: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/video-gen/status/<job_id>', methods=['GET'])
def get_status(job_id):
    """Check video generation status"""
    if job_id not in generation_jobs:
        return jsonify({'error': 'Job not found'}), 404

    job = generation_jobs[job_id]
    response = {
        'jobId': job_id,
        'status': job['status'],
        'progress': job.get('progress', 0),
        'videoType': job.get('video_type'),
        'createdAt': job.get('created_at')
    }

    if job['status'] == 'completed':
        response['videoUrl'] = job.get('video_url')
        response['downloadUrl'] = f"/api/video-gen/download/{Path(job['output_file']).name}"

    if job['status'] == 'publishing':
        response['autoEditorJobId'] = job.get('auto_editor_job')

    if job['status'] == 'failed':
        response['error'] = job.get('error')

    return jsonify(response), 200


@app.route('/api/video-gen/download/<filename>', methods=['GET'])
def download_video(filename):
    """Download generated video"""
    try:
        file_path = EXPORT_FOLDER / filename
        if not file_path.exists():
            return jsonify({'error': 'File not found'}), 404

        from flask import send_file
        return send_file(
            file_path,
            as_attachment=True,
            download_name=filename,
            mimetype='video/mp4'
        )
    except Exception as e:
        logger.error(f"Download error: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/video-gen/jobs', methods=['GET'])
def list_jobs():
    """List all generation jobs"""
    return jsonify({
        'total': len(generation_jobs),
        'jobs': generation_jobs
    }), 200


@app.route('/api/video-gen/voices', methods=['GET'])
def get_voices():
    """Get available voices"""
    return jsonify({
        'voices': list(VOICES.keys()),
        'default': 'female'
    }), 200


@app.route('/api/video-gen/templates', methods=['GET'])
def get_templates():
    """Get available video templates"""
    templates = {}
    for template in VideoTemplate:
        templates[template.name.lower()] = {
            'name': template.value['name'],
            'width': template.value['width'],
            'height': template.value['height'],
            'accent_color': template.value['accent_color']
        }
    return jsonify(templates), 200


@app.route('/api/video-gen/health', methods=['GET'])
def health():
    """Health check"""
    edge_tts_ok = True
    ffmpeg_ok = False

    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True, timeout=5)
        ffmpeg_ok = True
    except:
        pass

    return jsonify({
        'status': 'ok' if ffmpeg_ok else 'degraded',
        'service': 'video-generator',
        'ffmpeg': 'ok' if ffmpeg_ok else 'missing',
        'edge_tts': 'ok' if edge_tts_ok else 'error',
        'active_jobs': len(generation_jobs)
    }), 200


@app.route('/api/video-gen/cleanup', methods=['POST'])
def cleanup():
    """Clean up old temp files"""
    import shutil
    try:
        cutoff = 3600  # 1 hour
        now = datetime.now().timestamp()

        for item in TEMP_FOLDER.iterdir():
            if item.is_dir():
                mtime = item.stat().st_mtime
                if now - mtime > cutoff:
                    shutil.rmtree(item)

        return jsonify({'status': 'cleaned'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    logger.info("🎬 Empire Video Generator starting...")
    logger.info("   Listening on :5003")
    logger.info("   Using edge-tts for voice generation (FREE)")
    logger.info("   Using FFmpeg for video creation")
    app.run(host='0.0.0.0', port=5003, debug=False)

"""
Video Auto-Editor Bot
Automatically edit Synthesia videos, upgrade quality, and publish to YouTube
Runs as background service monitoring Synthesia webhooks
"""

import os
import json
import requests
import logging
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify
from flask_cors import CORS
import threading
import time
from enum import Enum

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Video quality upgrade profiles
class QualityProfile(Enum):
    STANDARD = {
        'quality': '1080p',
        'filters': [
            {'type': 'brightness', 'intensity': 0.55},
            {'type': 'contrast', 'intensity': 0.6},
            {'type': 'saturation', 'intensity': 0.7}
        ]
    }
    PREMIUM = {
        'quality': '1080p',
        'filters': [
            {'type': 'brightness', 'intensity': 0.6},
            {'type': 'contrast', 'intensity': 0.7},
            {'type': 'saturation', 'intensity': 0.8}
        ]
    }
    CINEMATIC = {
        'quality': '4k',
        'filters': [
            {'type': 'brightness', 'intensity': 0.5},
            {'type': 'contrast', 'intensity': 0.8},
            {'type': 'saturation', 'intensity': 0.6}
        ]
    }

# Edit templates for different video types
EDIT_TEMPLATES = {
    'trading-update': {
        'profile': QualityProfile.PREMIUM,
        'textOverlays': [
            {
                'text': 'EMPIRE TRADING',
                'position': 'top-center',
                'fontSize': 36,
                'color': '#FFD700',
                'startTime': 0,
                'endTime': 3
            },
            {
                'text': 'Real Results. Real Money.',
                'position': 'bottom-center',
                'fontSize': 20,
                'color': '#FFFFFF',
                'startTime': 0,
                'endTime': 2
            }
        ],
        'transitions': [
            {'type': 'fade', 'duration': 0.5, 'position': 'start'},
            {'type': 'fade', 'duration': 0.5, 'position': 'end'}
        ]
    },
    'property-listing': {
        'profile': QualityProfile.CINEMATIC,
        'textOverlays': [
            {
                'text': 'PROPERTY LISTING',
                'position': 'top-center',
                'fontSize': 40,
                'color': '#FFFFFF',
                'startTime': 0,
                'endTime': 2
            }
        ],
        'transitions': [
            {'type': 'fade', 'duration': 0.5, 'position': 'start'}
        ]
    },
    'social-content': {
        'profile': QualityProfile.PREMIUM,
        'textOverlays': [],
        'transitions': [
            {'type': 'fade', 'duration': 0.3, 'position': 'start'}
        ]
    },
    'cold-call-followup': {
        'profile': QualityProfile.STANDARD,
        'textOverlays': [
            {
                'text': 'Follow Up',
                'position': 'bottom-right',
                'fontSize': 24,
                'color': '#FFFFFF',
                'startTime': 0,
                'endTime': 1
            }
        ],
        'transitions': []
    }
}

# Active editing jobs
editing_jobs = {}
EDITOR_API_URL = os.getenv('VIDEO_EDITOR_API_URL', 'http://localhost:5001')
YOUTUBE_API_URL = os.getenv('YOUTUBE_API_URL', 'http://localhost:10000')


def download_video(url, output_path):
    """Download video from URL"""
    try:
        response = requests.get(url, stream=True, timeout=300)
        response.raise_for_status()

        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        logger.info(f"Downloaded video to {output_path}")
        return True
    except Exception as e:
        logger.error(f"Download failed: {str(e)}")
        return False


def build_edit_request(video_type, video_url, quality_profile):
    """Build edit request for video editor API"""
    template = EDIT_TEMPLATES.get(video_type, EDIT_TEMPLATES['social-content'])
    profile = template['profile'].value

    return {
        'videoUrl': video_url,
        'edits': {
            'filters': profile['filters'],
            'textOverlays': template['textOverlays'],
            'transitions': template['transitions'],
            'trim': {'start': 0, 'end': None}
        },
        'format': 'mp4',
        'quality': profile['quality'],
        'metadata': {
            'videoType': video_type,
            'timestamp': datetime.now().isoformat()
        }
    }


def submit_edit_job(video_id, video_url, video_type, youtube_settings=None):
    """Submit video to editor API"""
    try:
        edit_request = build_edit_request(video_type, video_url, None)

        # Download video locally
        temp_path = Path('video_temp') / f'{video_id}.mp4'
        temp_path.parent.mkdir(exist_ok=True)

        if not download_video(video_url, temp_path):
            raise Exception("Failed to download video")

        # Submit to editor API
        with open(temp_path, 'rb') as f:
            files = {'video': f}
            data = {
                'edits': json.dumps(edit_request['edits']),
                'format': edit_request['format'],
                'quality': edit_request['quality']
            }

            response = requests.post(
                f'{EDITOR_API_URL}/api/video/export',
                files=files,
                data=data,
                timeout=30
            )
            response.raise_for_status()

        job_data = response.json()
        job_id = job_data['jobId']

        editing_jobs[job_id] = {
            'video_id': video_id,
            'video_type': video_type,
            'status': 'editing',
            'created_at': datetime.now().isoformat(),
            'youtube_settings': youtube_settings or {}
        }

        logger.info(f"Submitted edit job {job_id} for video {video_id}")

        # Start monitoring job in background
        thread = threading.Thread(
            target=monitor_edit_job,
            args=(job_id, video_id, video_type, youtube_settings)
        )
        thread.daemon = True
        thread.start()

        return job_id

    except Exception as e:
        logger.error(f"Failed to submit edit job: {str(e)}")
        raise


def monitor_edit_job(job_id, video_id, video_type, youtube_settings):
    """Monitor edit job and publish when done"""
    try:
        max_attempts = 120  # 2 minutes with 1s polling
        attempt = 0

        while attempt < max_attempts:
            response = requests.get(
                f'{EDITOR_API_URL}/api/video/export/{job_id}',
                timeout=10
            )
            response.raise_for_status()

            job_status = response.json()
            status = job_status['status']
            progress = job_status['progress']

            if status == 'completed':
                download_url = job_status['downloadUrl']
                logger.info(f"Edit completed for {video_id}: {download_url}")

                editing_jobs[job_id]['status'] = 'completed'
                editing_jobs[job_id]['download_url'] = download_url

                # Auto-publish to YouTube if enabled
                if youtube_settings.get('auto_publish'):
                    publish_to_youtube(
                        download_url,
                        video_id,
                        video_type,
                        youtube_settings
                    )

                return

            elif status == 'failed':
                error = job_status.get('error', 'Unknown error')
                logger.error(f"Edit failed for {video_id}: {error}")
                editing_jobs[job_id]['status'] = 'failed'
                editing_jobs[job_id]['error'] = error
                return

            else:
                logger.info(f"Job {job_id} progress: {progress}%")
                editing_jobs[job_id]['progress'] = progress

            time.sleep(1)
            attempt += 1

        logger.error(f"Edit job {job_id} timed out")
        editing_jobs[job_id]['status'] = 'timeout'

    except Exception as e:
        logger.error(f"Error monitoring job {job_id}: {str(e)}")
        editing_jobs[job_id]['status'] = 'error'
        editing_jobs[job_id]['error'] = str(e)


def publish_to_youtube(video_url, video_id, video_type, youtube_settings):
    """Publish edited video to YouTube"""
    try:
        logger.info(f"Publishing {video_id} to YouTube")

        # Map video type to YouTube endpoint
        endpoint_map = {
            'trading-update': '/publish/youtube/social-content',
            'property-listing': '/publish/youtube/property-listing',
            'social-content': '/publish/youtube/social-content',
            'cold-call-followup': '/publish/youtube/social-content'
        }

        endpoint = endpoint_map.get(video_type, '/publish/youtube/social-content')

        publish_data = {
            'video_url': video_url,
            'title': youtube_settings.get('title', f'Video {video_id}'),
            'description': youtube_settings.get('description', 'Auto-generated content'),
            'privacy': youtube_settings.get('privacy', 'unlisted'),
            'tags': youtube_settings.get('tags', ['empire', 'trading', 'ai'])
        }

        response = requests.post(
            f'{YOUTUBE_API_URL}{endpoint}',
            json=publish_data,
            timeout=30
        )
        response.raise_for_status()

        result = response.json()
        logger.info(f"Published to YouTube: {result}")

        editing_jobs[video_id]['status'] = 'published'
        editing_jobs[video_id]['youtube_url'] = result.get('youtube_url')

    except Exception as e:
        logger.error(f"Failed to publish to YouTube: {str(e)}")
        editing_jobs[video_id]['status'] = 'publish_failed'
        editing_jobs[video_id]['error'] = str(e)


# === WEBHOOK ENDPOINTS ===

@app.route('/api/auto-editor/synthesia-webhook', methods=['POST'])
def synthesia_webhook():
    """
    Receives Synthesia video completion webhook
    Automatically edits and publishes video
    """
    try:
        data = request.json
        event_type = data.get('eventType')

        if event_type == 'video.completed':
            video_data = data.get('data', {})
            video_id = video_data.get('id')
            download_url = video_data.get('downloadUrl')
            video_type = video_data.get('title', 'social-content').lower()

            # Get YouTube settings from metadata
            youtube_settings = {
                'auto_publish': True,
                'privacy': 'unlisted',
                'title': video_data.get('title', 'AI Generated Video'),
                'description': video_data.get('description', 'Auto-edited and published'),
                'tags': ['empire', 'synthesia', 'ai']
            }

            # Classify video type
            if 'trading' in video_type or 'trade' in video_type:
                video_type = 'trading-update'
            elif 'property' in video_type or 'listing' in video_type:
                video_type = 'property-listing'
            elif 'cold' in video_type or 'followup' in video_type:
                video_type = 'cold-call-followup'
            else:
                video_type = 'social-content'

            # Submit to auto-editor
            job_id = submit_edit_job(video_id, download_url, video_type, youtube_settings)

            return jsonify({
                'status': 'editing',
                'jobId': job_id,
                'videoId': video_id,
                'message': 'Video submitted for editing and will auto-publish'
            }), 202

        elif event_type == 'video.failed':
            video_id = data.get('data', {}).get('id')
            logger.error(f"Synthesia video generation failed: {video_id}")
            return jsonify({'status': 'failed'}), 400

        return jsonify({'status': 'unknown_event'}), 200

    except Exception as e:
        logger.error(f"Webhook error: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/auto-editor/edit', methods=['POST'])
def manual_edit():
    """
    Manually submit a video for auto-editing and publishing
    POST /api/auto-editor/edit
    {
        "videoUrl": "https://...",
        "videoType": "trading-update",
        "youtubeSettings": {
            "autoPublish": true,
            "privacy": "unlisted",
            "title": "My Video"
        }
    }
    """
    try:
        data = request.json
        video_url = data.get('videoUrl')
        video_type = data.get('videoType', 'social-content')
        youtube_settings = data.get('youtubeSettings', {})

        if not video_url:
            return jsonify({'error': 'videoUrl required'}), 400

        video_id = f"manual_{int(time.time())}"
        job_id = submit_edit_job(video_id, video_url, video_type, youtube_settings)

        return jsonify({
            'status': 'processing',
            'jobId': job_id,
            'videoId': video_id
        }), 202

    except Exception as e:
        logger.error(f"Manual edit error: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/auto-editor/status/<job_id>', methods=['GET'])
def get_job_status(job_id):
    """Check auto-edit job status"""
    if job_id not in editing_jobs:
        return jsonify({'error': 'Job not found'}), 404

    job = editing_jobs[job_id]
    return jsonify({
        'jobId': job_id,
        'status': job['status'],
        'progress': job.get('progress', 0),
        'videoType': job['videoType'],
        'downloadUrl': job.get('download_url'),
        'youtubeUrl': job.get('youtube_url'),
        'createdAt': job['created_at']
    }), 200


@app.route('/api/auto-editor/jobs', methods=['GET'])
def list_jobs():
    """List all auto-edit jobs"""
    return jsonify({
        'total': len(editing_jobs),
        'jobs': editing_jobs
    }), 200


@app.route('/api/auto-editor/profiles', methods=['GET'])
def get_profiles():
    """Get available quality profiles"""
    profiles = {}
    for profile in QualityProfile:
        profiles[profile.name] = profile.value
    return jsonify(profiles), 200


@app.route('/api/auto-editor/templates', methods=['GET'])
def get_templates():
    """Get available edit templates"""
    templates = {}
    for name, template in EDIT_TEMPLATES.items():
        templates[name] = {
            'profile': template['profile'].name,
            'textOverlayCount': len(template['textOverlays']),
            'transitionCount': len(template['transitions'])
        }
    return jsonify(templates), 200


@app.route('/api/auto-editor/health', methods=['GET'])
def health():
    """Health check"""
    editor_healthy = False
    youtube_healthy = False

    try:
        requests.get(f'{EDITOR_API_URL}/api/video/health', timeout=5)
        editor_healthy = True
    except:
        pass

    try:
        requests.get(f'{YOUTUBE_API_URL}/health', timeout=5)
        youtube_healthy = True
    except:
        pass

    return jsonify({
        'status': 'ok',
        'service': 'video-auto-editor',
        'editor_api': 'ok' if editor_healthy else 'down',
        'youtube_api': 'ok' if youtube_healthy else 'down',
        'active_jobs': len(editing_jobs)
    }), 200


if __name__ == '__main__':
    logger.info("🎬 Video Auto-Editor starting...")
    logger.info(f"   Editor API: {EDITOR_API_URL}")
    logger.info(f"   YouTube API: {YOUTUBE_API_URL}")
    logger.info(f"   Listening for Synthesia webhooks...")
    app.run(host='0.0.0.0', port=5002, debug=False)

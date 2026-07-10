"""
Empire Video Editor API
Handles video processing: trim, filters, text overlays, transitions, export
Integrates with video_revenue_api.py for YouTube publishing
"""

import os
import json
import uuid
import subprocess
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename
import logging

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
UPLOAD_FOLDER = Path('video_uploads')
TEMP_FOLDER = Path('video_temp')
EXPORT_FOLDER = Path('video_exports')
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB
ALLOWED_EXTENSIONS = {'mp4', 'webm', 'mov', 'avi', 'mkv'}

UPLOAD_FOLDER.mkdir(exist_ok=True)
TEMP_FOLDER.mkdir(exist_ok=True)
EXPORT_FOLDER.mkdir(exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE

# Job tracking
jobs = {}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def apply_trim(input_path, output_path, start, end):
    """Trim video from start to end (in seconds)"""
    cmd = [
        'ffmpeg', '-i', input_path,
        '-ss', str(start),
        '-to', str(end),
        '-c:v', 'libx264',
        '-c:a', 'aac',
        '-y', output_path
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def apply_filters(input_path, output_path, filters):
    """Apply FFmpeg filters (brightness, contrast, saturation, etc)"""
    filter_str = ';'.join([
        build_filter(f['type'], f['intensity']) for f in filters
    ])

    if not filter_str:
        # No filters, just copy
        subprocess.run([
            'ffmpeg', '-i', input_path,
            '-c:v', 'copy', '-c:a', 'copy',
            '-y', output_path
        ], check=True, capture_output=True)
        return

    cmd = [
        'ffmpeg', '-i', input_path,
        '-vf', filter_str,
        '-c:a', 'aac',
        '-y', output_path
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def build_filter(filter_type, intensity):
    """Build FFmpeg filter string based on type and intensity"""
    intensity = min(max(intensity, 0), 1)  # Clamp 0-1

    filters = {
        'brightness': f"eq=brightness={0.5 + intensity}",
        'contrast': f"eq=contrast={0.5 + intensity * 1.5}",
        'saturation': f"hue=s={intensity * 2}",
        'grayscale': f"colorchannelmixer=.3:.4:.3:0:.3:.4:.3:0:.3:.4:.3" if intensity > 0.5 else "",
        'blur': f"scale=trunc(iw*{1-intensity*0.5}):trunc(ih*{1-intensity*0.5}),scale=iw:ih" if intensity > 0.1 else ""
    }
    return filters.get(filter_type, "")


def add_text_overlays(input_path, output_path, overlays, duration):
    """Add text overlays to video using FFmpeg drawtext filter"""
    if not overlays:
        subprocess.run([
            'ffmpeg', '-i', input_path,
            '-c:v', 'copy', '-c:a', 'copy',
            '-y', output_path
        ], check=True, capture_output=True)
        return

    # Build drawtext filters for each overlay
    filter_parts = []
    for i, overlay in enumerate(overlays):
        text = overlay['text'].replace("'", "\\'")
        start = overlay['startTime']
        end = overlay['endTime']
        position = overlay['position']
        font_size = overlay.get('fontSize', 24)
        color = overlay.get('color', 'ffffff')

        pos_map = {
            'top-left': '10:10',
            'top-center': '(w-text_w)/2:10',
            'center': '(w-text_w)/2:(h-text_h)/2',
            'bottom-left': '10:h-30',
            'bottom-center': '(w-text_w)/2:h-30',
        }
        xy = pos_map.get(position, '(w-text_w)/2:(h-text_h)/2')

        filter_parts.append(
            f"drawtext=text='{text}':fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
            f"fontsize={font_size}:fontcolor={color}:x={xy}:enable='between(t,{start},{end})'"
        )

    filter_str = ','.join(filter_parts)
    cmd = [
        'ffmpeg', '-i', input_path,
        '-vf', filter_str,
        '-c:a', 'aac',
        '-y', output_path
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def add_transitions(input_path, output_path, transitions):
    """Add fade in/out transitions using FFmpeg"""
    if not transitions:
        subprocess.run([
            'ffmpeg', '-i', input_path,
            '-c:v', 'copy', '-c:a', 'copy',
            '-y', output_path
        ], check=True, capture_output=True)
        return

    # Build fade filter
    filter_parts = []
    for transition in transitions:
        t_type = transition.get('type', 'fade')
        duration = transition.get('duration', 0.5)
        position = transition.get('position', 'start')

        if position == 'start':
            # Fade in at start
            filter_parts.append(f"fade=t=in:st=0:d={duration}")
        elif position == 'end':
            # Fade out at end (would need to know video duration)
            filter_parts.append(f"fade=t=out:st=-{duration}:d={duration}")

    if not filter_parts:
        subprocess.run([
            'ffmpeg', '-i', input_path,
            '-c:v', 'copy', '-c:a', 'copy',
            '-y', output_path
        ], check=True, capture_output=True)
        return

    filter_str = ','.join(filter_parts)
    cmd = [
        'ffmpeg', '-i', input_path,
        '-vf', filter_str,
        '-c:a', 'aac',
        '-y', output_path
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def export_video(input_path, output_path, format_type, quality):
    """Export video with specified format and quality"""
    quality_map = {
        '720p': 'libx264:crf=23',
        '1080p': 'libx264:crf=21',
        '4k': 'libx264:crf=19',
    }

    encoder = quality_map.get(quality, 'libx264:crf=21')

    if format_type == 'webm':
        codec = 'libvpx'
        audio = 'libopus'
    elif format_type == 'mov':
        codec = 'mpeg4'
        audio = 'aac'
    else:  # mp4
        codec = 'libx264'
        audio = 'aac'

    cmd = [
        'ffmpeg', '-i', input_path,
        '-c:v', codec,
        '-c:a', audio,
        '-y', output_path
    ]
    subprocess.run(cmd, check=True, capture_output=True)


@app.route('/api/video/export', methods=['POST'])
def export():
    """Main export endpoint - handles trim, filters, overlays, transitions"""
    try:
        if 'video' not in request.files:
            return jsonify({'error': 'No video file'}), 400

        file = request.files['video']
        if not file or not allowed_file(file.filename):
            return jsonify({'error': 'Invalid file type'}), 400

        # Parse edit instructions
        edits = json.loads(request.form.get('edits', '{}'))
        format_type = request.form.get('format', 'mp4')
        quality = request.form.get('quality', '1080p')

        # Create job
        job_id = str(uuid.uuid4())
        job_path = TEMP_FOLDER / job_id
        job_path.mkdir(exist_ok=True)

        jobs[job_id] = {
            'status': 'processing',
            'progress': 0,
            'error': None,
            'created_at': datetime.now().isoformat()
        }

        # Save uploaded video
        filename = secure_filename(file.filename)
        input_file = job_path / f'original_{filename}'
        file.save(input_file)

        # Process video
        current_file = input_file

        # Apply trim
        if edits.get('trim'):
            trim_data = edits['trim']
            trimmed_file = job_path / 'trimmed.mp4'
            apply_trim(str(current_file), str(trimmed_file),
                      trim_data['start'], trim_data['end'])
            current_file = trimmed_file
            jobs[job_id]['progress'] = 25

        # Apply filters
        if edits.get('filters'):
            filtered_file = job_path / 'filtered.mp4'
            apply_filters(str(current_file), str(filtered_file), edits['filters'])
            current_file = filtered_file
            jobs[job_id]['progress'] = 50

        # Add text overlays
        if edits.get('textOverlays'):
            overlayed_file = job_path / 'overlayed.mp4'
            add_text_overlays(str(current_file), str(overlayed_file),
                            edits['textOverlays'], 0)
            current_file = overlayed_file
            jobs[job_id]['progress'] = 70

        # Add transitions
        if edits.get('transitions'):
            transitioned_file = job_path / 'transitioned.mp4'
            add_transitions(str(current_file), str(transitioned_file),
                          edits['transitions'])
            current_file = transitioned_file
            jobs[job_id]['progress'] = 85

        # Export to final format
        output_filename = f'export_{job_id}.{format_type}'
        output_file = EXPORT_FOLDER / output_filename
        export_video(str(current_file), str(output_file), format_type, quality)

        jobs[job_id]['status'] = 'completed'
        jobs[job_id]['progress'] = 100
        jobs[job_id]['download_url'] = f'/api/video/download/{output_filename}'

        logger.info(f'Export completed: {job_id}')

        return jsonify({
            'jobId': job_id,
            'status': 'processing',
            'progress': 0
        }), 202

    except Exception as e:
        logger.error(f'Export error: {str(e)}')
        if job_id in jobs:
            jobs[job_id]['status'] = 'failed'
            jobs[job_id]['error'] = str(e)
        return jsonify({'error': str(e)}), 500


@app.route('/api/video/export/<job_id>', methods=['GET'])
def export_status(job_id):
    """Check export job status"""
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404

    job = jobs[job_id]
    response = {
        'status': job['status'],
        'progress': job['progress'],
    }

    if job['status'] == 'failed':
        response['error'] = job['error']
    elif job['status'] == 'completed':
        response['downloadUrl'] = job.get('download_url', '')

    return jsonify(response)


@app.route('/api/video/download/<filename>', methods=['GET'])
def download_video(filename):
    """Download processed video"""
    try:
        file_path = EXPORT_FOLDER / secure_filename(filename)
        if not file_path.exists():
            return jsonify({'error': 'File not found'}), 404

        return send_file(
            file_path,
            as_attachment=True,
            download_name=filename,
            mimetype='video/mp4'
        )
    except Exception as e:
        logger.error(f'Download error: {str(e)}')
        return jsonify({'error': str(e)}), 500


@app.route('/api/video/health', methods=['GET'])
def health():
    """Health check"""
    return jsonify({
        'status': 'ok',
        'service': 'video-editor-api',
        'ffmpeg_available': check_ffmpeg()
    })


def check_ffmpeg():
    """Check if FFmpeg is installed"""
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        return True
    except:
        return False


@app.route('/api/video/cleanup', methods=['POST'])
def cleanup():
    """Clean up old temp files (call periodically)"""
    import shutil
    try:
        cutoff = 3600  # 1 hour
        now = datetime.now().timestamp()

        for job_dir in TEMP_FOLDER.iterdir():
            if job_dir.is_dir():
                mtime = job_dir.stat().st_mtime
                if now - mtime > cutoff:
                    shutil.rmtree(job_dir)

        return jsonify({'status': 'cleaned'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False)

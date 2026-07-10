#!/bin/bash

# Empire Video Editor Startup Script
# Run this to start the complete video editing platform

set -e

echo "🎬 Empire Video Editor Platform"
echo "================================"
echo ""

# Check FFmpeg
echo "Checking FFmpeg installation..."
if ! command -v ffmpeg &> /dev/null; then
    echo "❌ FFmpeg is not installed!"
    echo ""
    echo "Install FFmpeg:"
    echo "  macOS: brew install ffmpeg"
    echo "  Ubuntu: sudo apt-get install ffmpeg"
    echo "  Windows: choco install ffmpeg"
    exit 1
fi
echo "✅ FFmpeg found: $(ffmpeg -version | head -n1)"
echo ""

# Check Python
echo "Checking Python installation..."
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is not installed!"
    exit 1
fi
echo "✅ Python: $(python3 --version)"
echo ""

# Check Node
echo "Checking Node.js installation..."
if ! command -v node &> /dev/null; then
    echo "❌ Node.js is not installed!"
    echo "Install from https://nodejs.org/"
    exit 1
fi
echo "✅ Node: $(node --version)"
echo ""

# Install Python dependencies
echo "Installing Python dependencies..."
pip install -q Flask Flask-CORS werkzeug 2>/dev/null || pip3 install -q Flask Flask-CORS werkzeug
echo "✅ Python dependencies installed"
echo ""

# Install Node dependencies
echo "Installing Node dependencies..."
cd video-editor
npm install --silent 2>/dev/null || npm install
cd ..
echo "✅ Node dependencies installed"
echo ""

# Create directories
mkdir -p video_uploads video_temp video_exports
echo "✅ Created processing directories"
echo ""

# Start services
echo "Starting services..."
echo ""
echo "📱 Backend: http://localhost:5001"
echo "🎨 Frontend: http://localhost:3000"
echo ""
echo "Press Ctrl+C to stop"
echo ""

# Start backend in background
python video_editor_api.py &
BACKEND_PID=$!

# Start frontend
cd video-editor
npm start
cd ..

# Cleanup on exit
trap "kill $BACKEND_PID 2>/dev/null" EXIT

import React, { useRef, useEffect, useState } from 'react';
import './VideoEditor.css';

function VideoEditor({ video, edits, onEditChange }) {
  const videoRef = useRef(null);
  const [duration, setDuration] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);
  const [showTextOverlay, setShowTextOverlay] = useState(false);
  const [textOverlayForm, setTextOverlayForm] = useState({
    text: '',
    startTime: 0,
    endTime: 5,
    position: 'center',
    fontSize: 24,
    color: '#ffffff'
  });

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;

    const handleLoadedMetadata = () => {
      setDuration(video.duration);
      onEditChange('trim', { start: 0, end: video.duration });
    };

    const handleTimeUpdate = () => {
      setCurrentTime(video.currentTime);
    };

    video.addEventListener('loadedmetadata', handleLoadedMetadata);
    video.addEventListener('timeupdate', handleTimeUpdate);

    return () => {
      video.removeEventListener('loadedmetadata', handleLoadedMetadata);
      video.removeEventListener('timeupdate', handleTimeUpdate);
    };
  }, [onEditChange]);

  const handleTrimChange = (type, value) => {
    const newTrim = { ...edits.trim, [type]: parseFloat(value) };
    onEditChange('trim', newTrim);
  };

  const handleAddTextOverlay = () => {
    if (!textOverlayForm.text.trim()) {
      alert('Please enter text');
      return;
    }

    const newOverlay = { ...textOverlayForm, id: Date.now() };
    onEditChange('textOverlays', [...edits.textOverlays, newOverlay]);
    setTextOverlayForm({
      text: '',
      startTime: currentTime,
      endTime: currentTime + 5,
      position: 'center',
      fontSize: 24,
      color: '#ffffff'
    });
    setShowTextOverlay(false);
  };

  const handleRemoveTextOverlay = (id) => {
    onEditChange('textOverlays', edits.textOverlays.filter(o => o.id !== id));
  };

  const handleAddFilter = (filterType) => {
    const filter = { id: Date.now(), type: filterType, intensity: 0.5 };
    onEditChange('filters', [...edits.filters, filter]);
  };

  const handleRemoveFilter = (id) => {
    onEditChange('filters', edits.filters.filter(f => f.id !== id));
  };

  const handleUpdateFilter = (id, intensity) => {
    onEditChange('filters', edits.filters.map(f =>
      f.id === id ? { ...f, intensity } : f
    ));
  };

  const handleAddTransition = (position) => {
    const transition = { id: Date.now(), type: 'fade', duration: 0.5, position };
    onEditChange('transitions', [...edits.transitions, transition]);
  };

  return (
    <div className="video-editor">
      <div className="preview-panel">
        <div className="video-container">
          <video ref={videoRef} controls src={URL.createObjectURL(video)} />
        </div>
        <div className="timeline">
          <div className="timeline-label">Timeline</div>
          <div className="timeline-track">
            <input
              type="range"
              min="0"
              max={duration}
              value={currentTime}
              onChange={(e) => videoRef.current.currentTime = parseFloat(e.target.value)}
              className="timeline-slider"
            />
          </div>
          <div className="time-display">
            {formatTime(currentTime)} / {formatTime(duration)}
          </div>
        </div>
      </div>

      <div className="tools-panel">
        <div className="tool-section">
          <h3>✂️ Trim</h3>
          <div className="tool-group">
            <label>
              Start (s)
              <input
                type="number"
                min="0"
                max={duration}
                step="0.1"
                value={edits.trim.start}
                onChange={(e) => handleTrimChange('start', e.target.value)}
              />
            </label>
            <label>
              End (s)
              <input
                type="number"
                min="0"
                max={duration}
                step="0.1"
                value={edits.trim.end}
                onChange={(e) => handleTrimChange('end', e.target.value)}
              />
            </label>
          </div>
        </div>

        <div className="tool-section">
          <h3>✨ Filters</h3>
          <div className="filter-buttons">
            <button onClick={() => handleAddFilter('brightness')}>Brightness</button>
            <button onClick={() => handleAddFilter('contrast')}>Contrast</button>
            <button onClick={() => handleAddFilter('saturation')}>Saturation</button>
            <button onClick={() => handleAddFilter('grayscale')}>Grayscale</button>
            <button onClick={() => handleAddFilter('blur')}>Blur</button>
          </div>
          {edits.filters.length > 0 && (
            <div className="active-filters">
              {edits.filters.map(filter => (
                <div key={filter.id} className="filter-item">
                  <span>{filter.type}</span>
                  <input
                    type="range"
                    min="0"
                    max="1"
                    step="0.1"
                    value={filter.intensity}
                    onChange={(e) => handleUpdateFilter(filter.id, parseFloat(e.target.value))}
                  />
                  <button onClick={() => handleRemoveFilter(filter.id)}>Remove</button>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="tool-section">
          <h3>📝 Text Overlays</h3>
          {!showTextOverlay ? (
            <button onClick={() => setShowTextOverlay(true)} className="add-btn">
              + Add Text
            </button>
          ) : (
            <div className="text-overlay-form">
              <input
                type="text"
                placeholder="Enter text"
                value={textOverlayForm.text}
                onChange={(e) => setTextOverlayForm({ ...textOverlayForm, text: e.target.value })}
              />
              <input
                type="number"
                min="0"
                max={duration}
                step="0.1"
                value={textOverlayForm.startTime}
                onChange={(e) => setTextOverlayForm({ ...textOverlayForm, startTime: parseFloat(e.target.value) })}
                placeholder="Start time (s)"
              />
              <input
                type="number"
                min="0"
                max={duration}
                step="0.1"
                value={textOverlayForm.endTime}
                onChange={(e) => setTextOverlayForm({ ...textOverlayForm, endTime: parseFloat(e.target.value) })}
                placeholder="End time (s)"
              />
              <select
                value={textOverlayForm.position}
                onChange={(e) => setTextOverlayForm({ ...textOverlayForm, position: e.target.value })}
              >
                <option value="top-left">Top Left</option>
                <option value="top-center">Top Center</option>
                <option value="center">Center</option>
                <option value="bottom-left">Bottom Left</option>
                <option value="bottom-center">Bottom Center</option>
              </select>
              <input
                type="color"
                value={textOverlayForm.color}
                onChange={(e) => setTextOverlayForm({ ...textOverlayForm, color: e.target.value })}
              />
              <input
                type="number"
                min="8"
                max="72"
                value={textOverlayForm.fontSize}
                onChange={(e) => setTextOverlayForm({ ...textOverlayForm, fontSize: parseInt(e.target.value) })}
                placeholder="Font size"
              />
              <button onClick={handleAddTextOverlay} className="confirm-btn">Add Overlay</button>
              <button onClick={() => setShowTextOverlay(false)} className="cancel-btn">Cancel</button>
            </div>
          )}
          {edits.textOverlays.length > 0 && (
            <div className="active-overlays">
              {edits.textOverlays.map(overlay => (
                <div key={overlay.id} className="overlay-item">
                  <span>"{overlay.text}"</span>
                  <button onClick={() => handleRemoveTextOverlay(overlay.id)}>Remove</button>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="tool-section">
          <h3>🔄 Transitions</h3>
          <button onClick={() => handleAddTransition('start')} className="transition-btn">
            Add Fade In
          </button>
          <button onClick={() => handleAddTransition('end')} className="transition-btn">
            Add Fade Out
          </button>
        </div>
      </div>
    </div>
  );
}

function formatTime(seconds) {
  if (!seconds) return '0:00';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return `${h > 0 ? h + ':' : ''}${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

export default VideoEditor;

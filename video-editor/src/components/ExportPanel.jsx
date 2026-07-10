import React, { useState } from 'react';
import './ExportPanel.css';

function ExportPanel({ onExport, exporting, exportStatus }) {
  const [format, setFormat] = useState('mp4');
  const [quality, setQuality] = useState('1080p');

  const handleExport = () => {
    if (exporting) return;
    onExport(format, quality);
  };

  return (
    <div className="export-panel">
      <h3>📤 Export Video</h3>

      <div className="export-options">
        <div className="option-group">
          <label>Format</label>
          <select
            value={format}
            onChange={(e) => setFormat(e.target.value)}
            disabled={exporting}
          >
            <option value="mp4">MP4 (H.264)</option>
            <option value="webm">WebM</option>
            <option value="mov">MOV</option>
          </select>
        </div>

        <div className="option-group">
          <label>Quality</label>
          <select
            value={quality}
            onChange={(e) => setQuality(e.target.value)}
            disabled={exporting}
          >
            <option value="720p">720p</option>
            <option value="1080p">1080p (Recommended)</option>
            <option value="4k">4K</option>
          </select>
        </div>
      </div>

      <button
        onClick={handleExport}
        disabled={exporting}
        className={`export-btn ${exporting ? 'loading' : ''}`}
      >
        {exporting ? '⏳ Exporting...' : '🚀 Export & Download'}
      </button>

      {exportStatus && (
        <div className={`export-status ${exporting ? 'active' : 'complete'}`}>
          {exportStatus}
        </div>
      )}

      <div className="export-info">
        <h4>Quick Publish to YouTube</h4>
        <p>After exporting, use <code>/publish/youtube</code> API to auto-upload:</p>
        <code>POST /publish/youtube/social-content</code>
        <p>Your video will be live in seconds!</p>
      </div>
    </div>
  );
}

export default ExportPanel;

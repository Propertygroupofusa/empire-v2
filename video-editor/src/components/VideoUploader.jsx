import React, { useState } from 'react';
import './VideoUploader.css';

function VideoUploader({ onUpload }) {
  const [dragActive, setDragActive] = useState(false);

  const handleDrag = (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.type === 'dragenter' || e.type === 'dragover') {
      setDragActive(true);
    } else if (e.type === 'dragleave') {
      setDragActive(false);
    }
  };

  const handleDrop = (e) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);

    const files = e.dataTransfer.files;
    if (files && files[0]) {
      const file = files[0];
      if (file.type.startsWith('video/')) {
        onUpload(file);
      } else {
        alert('Please drop a video file');
      }
    }
  };

  const handleFileSelect = (e) => {
    const file = e.target.files?.[0];
    if (file && file.type.startsWith('video/')) {
      onUpload(file);
    }
  };

  return (
    <div className="uploader">
      <div
        className={`drop-zone ${dragActive ? 'active' : ''}`}
        onDragEnter={handleDrag}
        onDragLeave={handleDrag}
        onDragOver={handleDrag}
        onDrop={handleDrop}
      >
        <div className="drop-content">
          <div className="upload-icon">📹</div>
          <h2>Upload Your Video</h2>
          <p>Drag and drop or click to select</p>
          <input
            type="file"
            accept="video/*"
            onChange={handleFileSelect}
            className="file-input"
          />
        </div>
      </div>
      <div className="supported-formats">
        <h3>Supported formats:</h3>
        <p>MP4, WebM, MOV, AVI (up to 500MB)</p>
      </div>
    </div>
  );
}

export default VideoUploader;

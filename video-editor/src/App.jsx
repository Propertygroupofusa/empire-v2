import React, { useState } from 'react';
import axios from 'axios';
import VideoUploader from './components/VideoUploader';
import VideoEditor from './components/VideoEditor';
import ExportPanel from './components/ExportPanel';
import './App.css';

function App() {
  const [video, setVideo] = useState(null);
  const [editing, setEditing] = useState(false);
  const [edits, setEdits] = useState({
    trim: { start: 0, end: null },
    textOverlays: [],
    filters: [],
    transitions: []
  });
  const [exporting, setExporting] = useState(false);
  const [exportStatus, setExportStatus] = useState('');

  const handleVideoUpload = (file) => {
    setVideo(file);
    setEditing(true);
    setEdits({
      trim: { start: 0, end: null },
      textOverlays: [],
      filters: [],
      transitions: []
    });
  };

  const handleEditChange = (key, value) => {
    setEdits(prev => ({
      ...prev,
      [key]: value
    }));
  };

  const handleExport = async (format, quality) => {
    setExporting(true);
    setExportStatus('Preparing export...');

    try {
      const formData = new FormData();
      formData.append('video', video);
      formData.append('edits', JSON.stringify(edits));
      formData.append('format', format);
      formData.append('quality', quality);

      setExportStatus('Uploading video...');
      const response = await axios.post('/api/video/export', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
        onUploadProgress: (progressEvent) => {
          const progress = Math.round((progressEvent.loaded / progressEvent.total) * 30);
          setExportStatus(`Uploading... ${progress}%`);
        }
      });

      const { jobId } = response.data;
      setExportStatus('Processing video...');

      // Poll for completion
      let jobComplete = false;
      let attempts = 0;
      const maxAttempts = 120; // 2 minutes with 1s polling

      while (!jobComplete && attempts < maxAttempts) {
        const statusResponse = await axios.get(`/api/video/export/${jobId}`);
        const { status, progress, downloadUrl, error } = statusResponse.data;

        if (status === 'completed') {
          setExportStatus('Export complete! Preparing download...');
          // Trigger download
          const link = document.createElement('a');
          link.href = downloadUrl;
          link.download = `edited-video.${format}`;
          document.body.appendChild(link);
          link.click();
          document.body.removeChild(link);
          jobComplete = true;
        } else if (status === 'failed') {
          throw new Error(error || 'Export failed');
        } else {
          setExportStatus(`Processing... ${progress}%`);
        }

        await new Promise(resolve => setTimeout(resolve, 1000));
        attempts++;
      }

      if (!jobComplete) {
        throw new Error('Export timed out');
      }
    } catch (error) {
      setExportStatus(`Error: ${error.message}`);
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="app">
      <header className="app-header">
        <h1>🎬 Empire Video Editor</h1>
        <p>Create, edit, export — publish to YouTube instantly</p>
      </header>

      <main className="app-main">
        {!editing && <VideoUploader onUpload={handleVideoUpload} />}

        {editing && video && (
          <div className="editor-container">
            <VideoEditor
              video={video}
              edits={edits}
              onEditChange={handleEditChange}
            />
            <ExportPanel
              onExport={handleExport}
              exporting={exporting}
              exportStatus={exportStatus}
            />
          </div>
        )}
      </main>
    </div>
  );
}

export default App;

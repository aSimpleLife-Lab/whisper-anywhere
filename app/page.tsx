"use client";

import { ChangeEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  Clipboard,
  Download,
  FileAudio,
  LoaderCircle,
  Mic,
  RefreshCw,
  Sparkles,
  Square,
  Upload
} from "lucide-react";

const MODELS = [
  {
    value: "gpt-4o-mini-transcribe",
    label: "Fast"
  },
  {
    value: "gpt-4o-transcribe",
    label: "Accurate"
  },
  {
    value: "whisper-1",
    label: "Whisper"
  }
];

function formatTime(totalSeconds: number) {
  const minutes = Math.floor(totalSeconds / 60)
    .toString()
    .padStart(2, "0");
  const seconds = (totalSeconds % 60).toString().padStart(2, "0");
  return `${minutes}:${seconds}`;
}

export default function Home() {
  const [audioBlob, setAudioBlob] = useState<Blob | null>(null);
  const [audioName, setAudioName] = useState("recording.webm");
  const [copied, setCopied] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState("");
  const [isRecording, setIsRecording] = useState(false);
  const [isTranscribing, setIsTranscribing] = useState(false);
  const [language, setLanguage] = useState("");
  const [model, setModel] = useState(MODELS[0].value);
  const [prompt, setPrompt] = useState("");
  const [transcript, setTranscript] = useState("");

  const chunksRef = useRef<Blob[]>([]);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);

  const audioUrl = useMemo(() => {
    return audioBlob ? URL.createObjectURL(audioBlob) : "";
  }, [audioBlob]);

  useEffect(() => {
    return () => {
      if (audioUrl) {
        URL.revokeObjectURL(audioUrl);
      }
    };
  }, [audioUrl]);

  useEffect(() => {
    if (!isRecording) {
      return;
    }

    const timer = window.setInterval(() => {
      setElapsed((value) => value + 1);
    }, 1000);

    return () => window.clearInterval(timer);
  }, [isRecording]);

  async function startRecording() {
    setError("");
    setTranscript("");
    setCopied(false);

    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setError("Microphone recording is not available in this browser.");
      return;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const options = MediaRecorder.isTypeSupported("audio/webm")
        ? { mimeType: "audio/webm" }
        : undefined;
      const recorder = new MediaRecorder(stream, options);

      chunksRef.current = [];
      streamRef.current = stream;
      recorderRef.current = recorder;

      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          chunksRef.current.push(event.data);
        }
      };

      recorder.onstop = () => {
        const blob = new Blob(chunksRef.current, {
          type: recorder.mimeType || "audio/webm"
        });
        setAudioBlob(blob);
        setAudioName(`recording-${Date.now()}.webm`);
        stream.getTracks().forEach((track) => track.stop());
        streamRef.current = null;
      };

      setAudioBlob(null);
      setElapsed(0);
      recorder.start();
      setIsRecording(true);
    } catch {
      setError("Microphone access was blocked or unavailable.");
    }
  }

  function stopRecording() {
    const recorder = recorderRef.current;

    if (recorder && recorder.state !== "inactive") {
      recorder.stop();
    }

    setIsRecording(false);
  }

  function handleUpload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];

    if (!file) {
      return;
    }

    setAudioBlob(file);
    setAudioName(file.name);
    setCopied(false);
    setElapsed(0);
    setError("");
    setTranscript("");
  }

  async function transcribeAudio() {
    if (!audioBlob) {
      setError("Add audio before transcribing.");
      return;
    }

    setCopied(false);
    setError("");
    setIsTranscribing(true);
    setTranscript("");

    const file =
      audioBlob instanceof File
        ? audioBlob
        : new File([audioBlob], audioName, {
            type: audioBlob.type || "audio/webm"
          });
    const formData = new FormData();
    formData.append("file", file);
    formData.append("model", model);

    if (language.trim()) {
      formData.append("language", language.trim());
    }

    if (prompt.trim()) {
      formData.append("prompt", prompt.trim());
    }

    try {
      const response = await fetch("/api/transcribe", {
        method: "POST",
        body: formData
      });
      const payload = (await response.json()) as { error?: string; text?: string };

      if (!response.ok) {
        throw new Error(payload.error || "Transcription failed.");
      }

      setTranscript(payload.text || "");
    } catch (transcriptionError) {
      setError(
        transcriptionError instanceof Error
          ? transcriptionError.message
          : "Transcription failed."
      );
    } finally {
      setIsTranscribing(false);
    }
  }

  async function copyTranscript() {
    if (!transcript) {
      return;
    }

    await navigator.clipboard.writeText(transcript);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  }

  function downloadTranscript() {
    if (!transcript) {
      return;
    }

    const url = URL.createObjectURL(
      new Blob([transcript], { type: "text/plain;charset=utf-8" })
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = `${audioName.replace(/\.[^/.]+$/, "") || "transcript"}.txt`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  function resetWorkspace() {
    if (isRecording) {
      stopRecording();
    }

    streamRef.current?.getTracks().forEach((track) => track.stop());
    setAudioBlob(null);
    setAudioName("recording.webm");
    setCopied(false);
    setElapsed(0);
    setError("");
    setTranscript("");
  }

  return (
    <main className="app-shell">
      <section className="workspace" aria-label="Audio transcription workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">Whisper App</p>
            <h1>Audio to transcript</h1>
          </div>
          <div className="ready-pill">
            <Sparkles size={16} />
            <span>{isRecording ? "Recording" : isTranscribing ? "Working" : "Ready"}</span>
          </div>
        </header>

        <div className="workspace-grid">
          <section className="panel capture-panel" aria-labelledby="capture-title">
            <div className="panel-header">
              <div>
                <p className="eyebrow">Input</p>
                <h2 id="capture-title">Capture audio</h2>
              </div>
              <button
                className="icon-button"
                type="button"
                onClick={resetWorkspace}
                title="Reset workspace"
              >
                <RefreshCw size={18} />
              </button>
            </div>

            <div className={`meter ${isRecording ? "active" : ""}`} aria-hidden="true">
              {Array.from({ length: 22 }).map((_, index) => (
                <span key={index} style={{ animationDelay: `${index * 42}ms` }} />
              ))}
            </div>

            <div className="recording-row">
              <button
                className="primary-button"
                type="button"
                onClick={isRecording ? stopRecording : startRecording}
              >
                {isRecording ? <Square size={18} /> : <Mic size={18} />}
                <span>{isRecording ? "Stop" : "Record"}</span>
              </button>
              <label className="secondary-button upload-button">
                <Upload size={18} />
                <span>Upload</span>
                <input accept="audio/*,video/mp4" type="file" onChange={handleUpload} />
              </label>
              <div className="timer" aria-live="polite">
                {formatTime(elapsed)}
              </div>
            </div>

            <div className="audio-slot">
              {audioUrl ? (
                <>
                  <div className="file-line">
                    <FileAudio size={18} />
                    <span>{audioName}</span>
                  </div>
                  <audio controls src={audioUrl} />
                </>
              ) : (
                <div className="empty-state">
                  <FileAudio size={24} />
                  <span>No audio selected</span>
                </div>
              )}
            </div>
          </section>

          <section className="panel settings-panel" aria-labelledby="settings-title">
            <div className="panel-header">
              <div>
                <p className="eyebrow">Options</p>
                <h2 id="settings-title">Transcription</h2>
              </div>
            </div>

            <label className="field">
              <span>Model</span>
              <select value={model} onChange={(event) => setModel(event.target.value)}>
                {MODELS.map((modelOption) => (
                  <option key={modelOption.value} value={modelOption.value}>
                    {modelOption.label}
                  </option>
                ))}
              </select>
            </label>

            <label className="field">
              <span>Language</span>
              <input
                value={language}
                onChange={(event) => setLanguage(event.target.value)}
                placeholder="Optional, e.g. en"
              />
            </label>

            <label className="field tall-field">
              <span>Prompt</span>
              <textarea
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                placeholder="Names, jargon, or context"
              />
            </label>

            <button
              className="transcribe-button"
              type="button"
              disabled={!audioBlob || isRecording || isTranscribing}
              onClick={transcribeAudio}
            >
              {isTranscribing ? (
                <LoaderCircle className="spin" size={18} />
              ) : (
                <Sparkles size={18} />
              )}
              <span>{isTranscribing ? "Transcribing" : "Transcribe"}</span>
            </button>

            {error ? <p className="error-message" role="alert">{error}</p> : null}
          </section>

          <section className="panel transcript-panel" aria-labelledby="transcript-title">
            <div className="panel-header transcript-header">
              <div>
                <p className="eyebrow">Output</p>
                <h2 id="transcript-title">Transcript</h2>
              </div>
              <div className="result-actions">
                <button
                  className="icon-button"
                  type="button"
                  onClick={copyTranscript}
                  disabled={!transcript}
                  title="Copy transcript"
                >
                  <Clipboard size={18} />
                </button>
                <button
                  className="icon-button"
                  type="button"
                  onClick={downloadTranscript}
                  disabled={!transcript}
                  title="Download transcript"
                >
                  <Download size={18} />
                </button>
              </div>
            </div>

            <textarea
              className="transcript-box"
              value={transcript}
              readOnly
              placeholder="Transcript will appear here."
            />
            <div className="transcript-meta" aria-live="polite">
              <span>{transcript ? `${transcript.length} characters` : "Waiting"}</span>
              <span>{copied ? "Copied" : audioBlob ? "Audio ready" : "No audio"}</span>
            </div>
          </section>
        </div>
      </section>
    </main>
  );
}

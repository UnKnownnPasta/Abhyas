"use strict";

const recordBtn = document.getElementById("recordBtn");
const micIcon = document.getElementById("micIcon");
const stopIcon = document.getElementById("stopIcon");
const statusText = document.getElementById("statusText");
const timerEl = document.getElementById("timer");
const meterCanvas = document.getElementById("meter");
const resultBox = document.getElementById("resultBox");
const errorBox = document.getElementById("errorBox");
const fileDrop = document.getElementById("fileDrop");
const fileInput = document.getElementById("fileInput");

const ctx = meterCanvas.getContext("2d");

let mediaRecorder = null;
let stream = null;
let audioContext = null;
let analyser = null;
let chunks = [];
let recordStart = 0;
let timerInterval = null;
let rafId = null;

function extForMime(mime) {
  if (mime.includes("mp4")) return "mp4";
  if (mime.includes("ogg")) return "ogg";
  if (mime.includes("webm")) return "webm";
  if (mime.includes("mpeg") || mime.includes("mp3")) return "mp3";
  if (mime.includes("aac") || mime.includes("adts") || mime.includes("m4a")) return "m4a";
  return "webm";
}

function showError(msg) {
  errorBox.textContent = msg;
  errorBox.hidden = false;
}

function clearError() {
  errorBox.hidden = true;
  errorBox.textContent = "";
}

function setStatus(text, active) {
  statusText.textContent = text;
  statusText.classList.toggle("active", !!active);
}

function setLoading(on) {
  if (on) {
    resultBox.classList.add("loading");
    resultBox.innerHTML = `<span class="spinner"></span><span>Transcribing&hellip;</span>`;
  } else {
    resultBox.classList.remove("loading");
  }
}

function drawMeter(values) {
  const w = meterCanvas.width;
  const h = meterCanvas.height;
  ctx.clearRect(0, 0, w, h);
  const bars = 40;
  const gap = 4;
  const barW = (w - gap * (bars + 1)) / bars;
  for (let i = 0; i < bars; i++) {
    const level = values[i] !== undefined ? values[i] : 0;
    const bh = Math.max(4, level * (h - 8));
    const x = gap + i * (barW + gap);
    const y = h - bh - 4;
    const hue = 255 - level * 180;
    ctx.fillStyle = `hsl(${hue}, 85%, 60%)`;
    ctx.fillRect(x, y, barW, bh);
  }
}

function meterLoop() {
  if (!analyser) return;
  const data = new Uint8Array(analyser.frequencyBinCount);
  analyser.getByteFrequencyData(data);
  const bars = 40;
  const values = [];
  for (let i = 0; i < bars; i++) {
    const idx = Math.floor((i / bars) * data.length);
    values.push(data[idx] / 255);
  }
  drawMeter(values);
  rafId = requestAnimationFrame(meterLoop);
}

function stopMeter() {
  if (rafId) {
    cancelAnimationFrame(rafId);
    rafId = null;
  }
  drawMeter(new Array(40).fill(0));
}

function startTimer() {
  recordStart = Date.now();
  timerInterval = setInterval(() => {
    const secs = Math.floor((Date.now() - recordStart) / 1000);
    timerEl.textContent = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
  }, 250);
}

function stopTimer() {
  clearInterval(timerInterval);
  timerEl.textContent = "0:00";
}

async function startRecording() {
  clearError();
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    showError(
      "Microphone access was denied or not available. " +
        "Allow microphone access in your browser, or upload an audio file below."
    );
    return;
  }

  const mimeType = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
    ? "audio/webm;codecs=opus"
    : MediaRecorder.isTypeSupported("audio/ogg;codecs=opus")
    ? "audio/ogg;codecs=opus"
    : MediaRecorder.isTypeSupported("audio/mp4")
    ? "audio/mp4"
    : "";

  mediaRecorder = mimeType
    ? new MediaRecorder(stream, { mimeType })
    : new MediaRecorder(stream);

  chunks = [];
  mediaRecorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) chunks.push(e.data);
  };
  mediaRecorder.onstop = handleRecordingStop;

  audioContext = new (window.AudioContext || window.webkitAudioContext)();
  const source = audioContext.createMediaStreamSource(stream);
  analyser = audioContext.createAnalyser();
  analyser.fftSize = 256;
  source.connect(analyser);

  mediaRecorder.start();
  startTimer();
  meterLoop();

  recordBtn.classList.add("recording");
  micIcon.style.display = "none";
  stopIcon.style.display = "block";
  setStatus("Recording&hellip; click to stop", true);
}

async function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== "inactive") {
    mediaRecorder.stop();
  }
}

async function handleRecordingStop() {
  stopTimer();
  stopMeter();
  recordBtn.classList.remove("recording");
  micIcon.style.display = "block";
  stopIcon.style.display = "none";
  setStatus("Processing&hellip;", false);

  if (audioContext) {
    audioContext.close().catch(() => {});
    audioContext = null;
    analyser = null;
  }
  if (stream) {
    stream.getTracks().forEach((t) => t.stop());
    stream = null;
  }

  const blob = new Blob(chunks, { type: mediaRecorder.mimeType || "audio/webm" });
  const filename = "recording." + extForMime(blob.type);
  chunks = [];
  await uploadAudio(blob, filename);
}

async function uploadAudio(blob, filename) {
  clearError();
  setLoading(true);
  setStatus("Transcribing&hellip;", false);

  const form = new FormData();
  form.append("audio", blob, filename);

  try {
    const res = await fetch("/api/transcribe", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Server error (${res.status})`);

    resultBox.classList.remove("loading");
    const timeStr = data.elapsed ? ` (${data.elapsed}s)` : "";
    resultBox.innerHTML =
      `<span class="meta">Transcript${timeStr}</span>` +
      escapeHtml(data.transcript);
    setStatus("Done. Click to record again", false);
  } catch (err) {
    setLoading(false);
    resultBox.innerHTML = `<span class="placeholder">Your transcription will appear here.</span>`;
    setStatus("Click to start recording", false);
    showError(
      err.message.includes("GROQ_API_KEY")
        ? "The server has no GROQ_API_KEY set. " +
            "Restart it with GROQ_API_KEY=your_key in the environment."
        : err.message
    );
  }
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

recordBtn.addEventListener("click", () => {
  if (mediaRecorder && mediaRecorder.state === "recording") {
    stopRecording();
  } else {
    startRecording();
  }
});

fileDrop.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    fileInput.click();
  }
});
fileDrop.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  if (!file) return;
  clearError();
  setStatus("Processing file&hellip;", false);
  uploadAudio(file, file.name);
  fileInput.value = "";
});
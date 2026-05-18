import { NextResponse } from "next/server";

export const runtime = "nodejs";
export const maxDuration = 60;

const DEFAULT_MODEL = "gpt-4o-mini-transcribe";
const SUPPORTED_MODELS = new Set([
  "gpt-4o-mini-transcribe",
  "gpt-4o-transcribe",
  "whisper-1"
]);
const MAX_AUDIO_BYTES = 25 * 1024 * 1024;

type OpenAITranscriptionResponse = {
  text?: string;
  error?: {
    message?: string;
  };
};

function getStringField(formData: FormData, key: string) {
  const value = formData.get(key);
  return typeof value === "string" ? value.trim() : "";
}

function getErrorMessage(payload: OpenAITranscriptionResponse, fallback: string) {
  return payload.error?.message || fallback;
}

export async function POST(request: Request) {
  const apiKey = process.env.OPENAI_API_KEY;

  if (!apiKey) {
    return NextResponse.json(
      { error: "OPENAI_API_KEY is missing on the server." },
      { status: 500 }
    );
  }

  let formData: FormData;

  try {
    formData = await request.formData();
  } catch {
    return NextResponse.json(
      { error: "The request did not include valid audio form data." },
      { status: 400 }
    );
  }

  const audioFile = formData.get("file");

  if (!(audioFile instanceof File)) {
    return NextResponse.json(
      { error: "Add an audio file before transcribing." },
      { status: 400 }
    );
  }

  if (audioFile.size === 0) {
    return NextResponse.json(
      { error: "The audio file is empty." },
      { status: 400 }
    );
  }

  if (audioFile.size > MAX_AUDIO_BYTES) {
    return NextResponse.json(
      { error: "Audio files must be 25 MB or smaller." },
      { status: 400 }
    );
  }

  const requestedModel = getStringField(formData, "model");
  const model = SUPPORTED_MODELS.has(requestedModel)
    ? requestedModel
    : DEFAULT_MODEL;
  const language = getStringField(formData, "language");
  const prompt = getStringField(formData, "prompt");

  const upstreamForm = new FormData();
  upstreamForm.append("file", audioFile, audioFile.name || "recording.webm");
  upstreamForm.append("model", model);
  upstreamForm.append("response_format", "json");

  if (language) {
    upstreamForm.append("language", language);
  }

  if (prompt) {
    upstreamForm.append("prompt", prompt);
  }

  const response = await fetch("https://api.openai.com/v1/audio/transcriptions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`
    },
    body: upstreamForm
  });
  const rawBody = await response.text();
  let payload: OpenAITranscriptionResponse = {};

  try {
    payload = JSON.parse(rawBody) as OpenAITranscriptionResponse;
  } catch {
    payload = { text: rawBody };
  }

  if (!response.ok) {
    return NextResponse.json(
      { error: getErrorMessage(payload, "Transcription failed.") },
      { status: response.status }
    );
  }

  return NextResponse.json({
    model,
    text: payload.text || ""
  });
}

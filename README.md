# Whisper App

A Next.js starter app for recording or uploading audio and transcribing it through a server-side OpenAI transcription endpoint.

## Included

- Browser microphone recording
- Audio file upload
- Model picker for `gpt-4o-mini-transcribe`, `gpt-4o-transcribe`, and `whisper-1`
- Optional language and prompt fields
- Copy and download actions for transcripts
- Server-side API route so the API key stays out of the browser

## Run Locally

1. Install Node.js 20 or newer.
2. Install dependencies:

```bash
npm install
```

3. Create `.env.local` from `.env.example` and add your OpenAI API key.
4. Start the app:

```bash
npm run dev
```

5. Open `http://localhost:3000`.

## Notes

The app sends completed audio recordings or uploaded files to `/api/transcribe`, which forwards them to OpenAI's audio transcription endpoint. OpenAI currently documents a 25 MB upload limit for speech-to-text files.

Useful docs:

- https://platform.openai.com/docs/guides/speech-to-text
- https://platform.openai.com/docs/api-reference/audio/createTranscription

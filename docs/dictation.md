# Live dictation

Dictation uses Soniox `stt-rt-v5` to append Polish and English speech to the current prompt or a user-message edit. It does not send the prompt automatically. AI-message editing does not offer dictation.

## Configuration

Set `SONIOX_API_KEY` in the backend environment. An empty value disables the microphone controls. The key needs speech-to-text and temporary-key creation access. Restart the backend after changing the environment.

The authenticated `POST /api/dictation/session` endpoint requires `X-Brief-Dictation: 1`. It returns a single-use temporary key that can open a connection within 60 seconds. Responses use `Cache-Control: no-store`. The permanent key is never included in browser configuration.

The browser connects directly to `wss://stt-rt.soniox.com/transcribe-websocket` using the temporary key in WebSocket subprotocol authentication. No WebSocket proxy or additional service is needed. The current integration uses Soniox's global endpoint; it does not select an EU processing region.

## Behavior

The microphone requires HTTPS or localhost, browser permission, AudioContext and AudioWorklet support. An AudioWorklet converts microphone audio to mono 16-bit little-endian PCM at 16 kHz. Audio is streamed in approximately 100 ms chunks; the application does not save recordings.

Recognized text appears in the field during capture. Final tokens accumulate while provisional tokens replace the previous provisional tail. Existing text is retained and new speech is appended at the end. The field, Send and Save remain locked during recording and finalization.

Stop releases microphone tracks immediately, flushes the remaining audio and waits for Soniox's final response. After eight seconds without completion, the visible text is retained with an interruption message. A new recording appends to the existing field, including any retained provisional words. There is no automatic reconnect or audio replay.

Switching conversations, leaving Ask or hiding the browser page stops capture. Closing an earlier-message editor discards that edit according to the normal editor behavior. Browser suspension, microphone interruption and connection failures stop capture and preserve the visible prompt text. Resume explicitly opens a new session.

Silence does not trigger an application timeout. Open sessions continue streaming silence and may incur usage charges. There is no application daily usage cap. Provider balance, concurrency and session duration limits still apply. Prompt text is limited to 100,000 characters.

Audio and a fixed vocabulary about programming and strength training are sent to Soniox. Personal context settings, chat history and attachments are not included in speech recognition requests. The resulting prompt text follows the existing browser draft storage behavior.

## Verification

Run `python3 -m pytest backend/tests -q` and `python3 backend/tests/check_dictation_ui.py`. The browser check uses synthetic Chrome microphone input and a fake provider socket, with no paid API requests. It covers real AudioWorklet capture, 44.1/48 kHz resampling, live partial replacement, finalization, interrupted sessions, permission cancellation, resuming, message editing and narrow layouts.

Real microphone recognition and iPhone Safari require manual testing over HTTPS. A narrow Chrome viewport is a layout check, not an iPhone microphone compatibility test.

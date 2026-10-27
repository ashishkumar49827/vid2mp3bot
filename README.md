# Video to MP3 Telegram bot (@vid2mp3_conv_bot)

Small self-contained Telegram bot. Send it a video (or video file/document,
max 20 MB) and it replies with the audio as a real MP3 file
(ffmpeg + libmp3lame). No n8n, no third-party conversion API.

## Files
- `bot.py` - aiohttp service: webhook mode (default) + `--poll` long-poll mode
- `converter.py` - ffmpeg conversion + guards
- `Dockerfile` - python:3.12-slim + ffmpeg
- `requirements.txt` - aiohttp only

## Environment variables
- `BOT_TOKEN` (required) - Telegram bot token
- `WEBHOOK_SECRET` (required in webhook mode) - webhook path + header secret
- `PORT` (default 8080)
- `MAX_PARALLEL` (default 2) - concurrent conversion cap

## Deploy (Render free web service, Docker or Python runtime)
1. Push these files to a public git repo.
2. Render: New Web Service from the public repo URL. Dockerfile detected
   automatically (or: build `pip install -r requirements.txt`, start
   `python bot.py`).
3. Set env vars `BOT_TOKEN` and `WEBHOOK_SECRET`.
4. Register the webhook:
   `curl "https://api.telegram.org/bot$BOT_TOKEN/setWebhook?url=https://<service>.onrender.com/webhook/$WEBHOOK_SECRET&secret_token=$WEBHOOK_SECRET"`

Free-tier notes: service sleeps after ~15 min idle (next message wakes it,
~1 min cold start; Telegram retries the webhook). 750 free instance-hours
per month. Ephemeral disk: conversion files are written to a per-job temp
directory that is deleted right after each job.

## Reliability policy (free hosting)
- At-least-once while the service is up: Telegram retries unanswered webhook
  deliveries; a per-update state machine (processing/done/failed) answers
  503 while a job runs so a retry is never acked prematurely.
- Telegram's retries are finite. If the instance stays down past its retry
  window, that conversion is lost - the user simply gets no reply and will
  resend. No stronger guarantee is made.
- State is in-memory. If the host restarts the instance after an MP3 was
  sent but before Telegram's final ack, the redelivered update runs again
  and the user can receive one duplicate MP3. If the send had not happened
  yet, the retried job just completes once.
- No queue: at most MAX_PARALLEL conversions run at once; anything beyond
  gets an immediate "busy, try again" reply instead of waiting.

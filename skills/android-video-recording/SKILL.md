---
name: android-video-recording
description: Immediately record an attached Android device screen to an MP4 with scrcpy when the user says "开始录制视频", "开始录屏", "停止录制视频", or "停止录屏".
---

# Android Video Recording

Use this skill for exact user intents such as "开始录制视频", "开始录屏", "录一下视频", "停止录制视频", "停止录屏", or "结束录制视频".

## Start Trigger

When the user asks to start recording video, immediately call `android_start_video_recording`.

Do not observe the screen first. Do not call dependency checks, screenshots, UIAutomator, timeline viewers, or natural-language agent loops first. Let the tool choose the configured/default serial unless the user provided one.

Default arguments:

```json
{
  "record_format": "mp4",
  "max_size": 0,
  "bit_rate": "8M",
  "audio": false,
  "start_marker": true
}
```

After the tool returns, keep the reply short and include the `file_path` if useful. The tool returns without a fixed startup wait and includes `timing` plus a best-effort `start_anchor.path` screenshot that is captured in the background. The recording is active until stopped.

## Stop Trigger

When the user asks to stop recording, immediately call `android_stop_video_recording`.

Do not observe the screen first. Do not stop visible scrcpy windows. Do not run extra diagnostics unless stopping fails.

After the tool returns, reply with the MP4 using Markdown video/image syntax and the absolute path returned by `file_path`:

```markdown
![android-video-recording](/absolute/path/to/video.mp4)
```

Also mention the duration or file size only if it is helpful. Keep the response short.

## Distinction

This skill records a real MP4 video through scrcpy. It is different from `android_start_recording`, which records deterministic Android tool actions into a JSON trace for recipe replay.

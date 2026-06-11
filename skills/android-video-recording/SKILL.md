---
name: android-video-recording
description: Handle Android video-recording requests through scrcpy-backed MP4 recording.
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

If the tool returns a `file_path`, include it. If scrcpy is missing or recording fails, keep the reply short and report the concrete tool error.

## Stop Trigger

When the user asks to stop recording, immediately call `android_stop_video_recording`.

Do not observe the screen first. Do not stop visible scrcpy windows. Do not run extra diagnostics unless stopping fails.

After the tool returns with a `file_path`, reply with the MP4 using Markdown video/image syntax and the absolute path:

```markdown
![android-video-recording](/absolute/path/to/video.mp4)
```

Also mention the duration or file size only if it is helpful. Keep the response short.

## Distinction

This skill records a real MP4 video through scrcpy. Use `android_start_recording` separately when the user wants deterministic action traces instead of video.

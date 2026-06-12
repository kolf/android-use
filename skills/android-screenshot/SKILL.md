---
name: android-screenshot
description: Immediately capture and send the current Android device screen when the user says "截图", "截屏", "发截图", "当前屏幕截图", "screenshot", or asks for a quick Android screen capture. Use for Android Use / android-use-plugins sessions where the user expects the image back right away, especially after navigation or device operations.
---

# Android Use: Android Screenshot

## Immediate Workflow

When this skill triggers, capture first and explain later.

1. Call `android_show_screen` immediately. It returns the current Android screen image inline and saves a PNG path.
2. If a serial is already known from context, pass it. If no serial is known and only one device is expected, omit `serial`.
3. If multiple devices may be connected or the first screenshot call fails due to serial ambiguity, call `android_list_devices(include_details=true)`, choose the only authorized device, then retry `android_show_screen`.
4. If `android_show_screen` is unavailable, call `android_screenshot` and return the saved absolute PNG path as a Markdown image.
5. If no authorized device is visible, report that no Android device is currently available and include the `android_list_devices` result.

## Response Contract

Keep the response short. The user asked for a screenshot, so the image is the deliverable.

- Do not start with a plan.
- Do not call `android_observe`, `android_appshot`, VLM tools, or scrcpy before the screenshot unless the user asked for more than a screenshot.
- Do not ask for confirmation for a routine screenshot.
- In the final answer, include the screenshot image if the tool did not already display it inline:

```markdown
![Android screenshot](/absolute/path/to/screenshot.png)
```

## Optional Add-ons

If the user asks for page title or foreground Activity together with the screenshot, run screenshot and a narrow `android_shell` command in parallel:

```bash
dumpsys window | grep -E 'mCurrentFocus|mFocusedApp|topResumedActivity' | head -20
```

Then return the screenshot plus the extracted Activity in one concise response.

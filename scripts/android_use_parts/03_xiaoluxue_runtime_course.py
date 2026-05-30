# Loaded by scripts/android_use_mcp.py. Keep this file below 2000 lines.

def is_xiaoluxue_app_only_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").casefold()
    return is_xiaoluxue_h5_host(host)


def is_xiaoluxue_h5_host(host: str) -> bool:
    normalized = host.casefold().strip()
    return normalized in XIAOLUXUE_APP_ONLY_HOSTS or any(normalized.endswith(suffix) for suffix in XIAOLUXUE_APP_ONLY_SUFFIXES)


def xiaoluxue_url_kind(url: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None
    host = parsed.hostname or ""
    if not is_xiaoluxue_h5_host(host):
        return None
    path = parsed.path.rstrip("/") or "/"
    if path == "/course" or path.startswith("/course/"):
        return "course"
    if path == "/exercise" or path.startswith("/exercise/"):
        return "exercise"
    return "any"


def xiaoluxue_page_matches(page: dict[str, Any], page_kind: str) -> bool:
    kind = xiaoluxue_url_kind(str(page.get("url") or ""))
    if page_kind == "any":
        return kind is not None or "小鹿爱学" in str(page.get("title") or "")
    return kind == page_kind


def select_xiaoluxue_webview_page(pages: list[dict[str, Any]], page_kind: str = "any") -> dict[str, Any]:
    candidates = [page for page in pages if not page.get("error") and xiaoluxue_page_matches(page, page_kind)]
    if not candidates:
        detail = {
            "page_kind": page_kind,
            "known": [
                {"id": page.get("id"), "title": page.get("title"), "url": page.get("url"), "error": page.get("error")}
                for page in pages[:10]
            ],
        }
        raise AndroidUseError(f"No matching Xiaoluxue WebView page found: {json.dumps(detail, ensure_ascii=False)[:1200]}")
    candidates.sort(key=webview_page_score, reverse=True)
    page = candidates[0]
    if not page.get("webSocketDebuggerUrl"):
        raise AndroidUseError(f"Matched Xiaoluxue WebView page has no webSocketDebuggerUrl: {page}")
    return page


def xiaoluxue_page_hidden_behind_native(serial: str, page: dict[str, Any]) -> bool:
    description = page.get("descriptionParsed") if isinstance(page.get("descriptionParsed"), dict) else {}
    if description.get("visible") is not False:
        return False
    try:
        focus = get_focused_window(serial) or ""
    except Exception:
        focus = ""
    return XIAOLUXUE_STUDENT_PACKAGE in focus and XIAOLUXUE_STANDARD_BROWSER_ACTIVITY not in focus


def xiaoluxue_ensure_foreground_webview(serial: str, page: dict[str, Any]) -> None:
    if xiaoluxue_page_hidden_behind_native(serial, page):
        try:
            focus = get_focused_window(serial) or ""
        except Exception:
            focus = ""
        raise AndroidUseError(f"Xiaoluxue WebView exists only in background; foreground is {focus or 'unknown'}.")


def xiaoluxue_dismiss_debug_overlay_if_needed(
    serial: str,
    steps: list[dict[str, Any]] | None = None,
    started_at: float | None = None,
) -> bool:
    try:
        focus = get_focused_window(serial) or ""
    except Exception:
        focus = ""
    if XIAOLUXUE_LEAK_ACTIVITY_MARKER not in focus:
        return False
    adb(["shell", "input", "keyevent", "BACK"], serial=serial, timeout=4)
    if steps is not None:
        steps.append(
            {
                "action": "back",
                "reason": "dismiss-leakcanary-overlay",
                "from_focus": focus,
                "at_sec": round(time.monotonic() - float(started_at or time.monotonic()), 3),
            }
        )
    time.sleep(0.18)
    return True


def xiaoluxue_vessel_webview_url(url: str) -> str:
    if not is_xiaoluxue_app_only_url(url):
        raise AndroidUseError(f"Not a Xiaoluxue app-only URL: {url}")
    return (
        f"{XIAOLUXUE_VESSEL_WEBVIEW_ROUTE}?url={urllib.parse.quote(url, safe='')}"
        "&full_screen=true&title_bar=false"
    )


def xiaoluxue_route_app_url(serial: str, url: str, *, force_stop: bool = False) -> dict[str, Any]:
    route_url = xiaoluxue_vessel_webview_url(url)
    command = [
        "shell",
        "am",
        "start",
    ]
    if force_stop:
        command.append("-S")
    command.extend(
        [
            "-n",
            f"{XIAOLUXUE_STUDENT_PACKAGE}/{XIAOLUXUE_SCHEME_PROXY_ACTIVITY}",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            route_url,
        ]
    )
    output = adb(command, serial=serial, timeout=3).decode("utf-8", errors="replace")
    return {
        "ok": True,
        "mode": "xiaoluxue-vessel-webview-route",
        "url": url,
        "route_url": route_url,
        "package": XIAOLUXUE_STUDENT_PACKAGE,
        "am_start": output,
    }


def xiaoluxue_cache_key(serial: str, page_kind: str) -> tuple[str, str]:
    return (serial, page_kind)


def xiaoluxue_remember_page(serial: str, page_kind: str, page: dict[str, Any]) -> dict[str, Any]:
    cached = dict(page)
    cached["_cachedAt"] = time.monotonic()
    cached["_cacheKind"] = page_kind
    XIAOLUXUE_PAGE_CACHE[xiaoluxue_cache_key(serial, page_kind)] = cached
    return page


def xiaoluxue_remember_inferred_page(serial: str, page: dict[str, Any]) -> dict[str, Any]:
    xiaoluxue_remember_page(serial, "any", page)
    kind = xiaoluxue_url_kind(str(page.get("url") or ""))
    if kind in {"course", "exercise"}:
        xiaoluxue_remember_page(serial, kind, page)
    return page


def xiaoluxue_cached_page(
    serial: str,
    page_kind: str,
    *,
    runtime_contains: str,
    max_age_sec: float = 12.0,
) -> dict[str, Any] | None:
    cached = XIAOLUXUE_PAGE_CACHE.get(xiaoluxue_cache_key(serial, page_kind))
    if not cached:
        return None
    if time.monotonic() - float(cached.get("_cachedAt", 0)) > max_age_sec:
        return None
    websocket = str(cached.get("webSocketDebuggerUrl") or "")
    if not websocket:
        return None
    try:
        runtime_href = cdp_eval_value(cached, "location.href", timeout=0.25)
    except Exception:
        return None
    if isinstance(runtime_href, str) and runtime_contains in runtime_href:
        return {**cached, "runtimeHref": runtime_href, "cacheHit": True}
    return None


def xiaoluxue_cached_runtime_page(
    serial: str,
    page_kind: str,
    *,
    max_age_sec: float = 12.0,
) -> dict[str, Any] | None:
    cached = XIAOLUXUE_PAGE_CACHE.get(xiaoluxue_cache_key(serial, page_kind))
    if not cached:
        return None
    if time.monotonic() - float(cached.get("_cachedAt", 0)) > max_age_sec:
        return None
    if not str(cached.get("webSocketDebuggerUrl") or ""):
        return None
    try:
        runtime_href = cdp_eval_value(cached, "location.href", timeout=0.25)
    except Exception:
        return None
    if isinstance(runtime_href, str):
        kind = xiaoluxue_url_kind(runtime_href)
        if page_kind == "any" and kind is not None:
            return {**cached, "runtimeHref": runtime_href, "cacheHit": True}
        if kind == page_kind:
            return {**cached, "runtimeHref": runtime_href, "cacheHit": True}
    return None

# Xiaoluxue's subject home displays human course indexes like "1.1.1.1".
# Users often omit one dot when saying/typing them ("1.1.11"). Store fast
# aliases by a digits-only key so both resolve without scanning the whole tree.
XIAOLUXUE_KNOWLEDGE_SHORTCUTS: dict[tuple[int, str], dict[str, Any]] = {
    (2, "1111"): {
        "knowledgeId": 3785,
        "knowledgeIndex": "1.1.1.1",
        "knowledgeName": "集合及其表示方法",
        "subjectName": "数学",
        "lessonId": 598907589657093,
        "studySessionId": 908465254772106,
	        "guideIndex": 1,
	        "guideName": "初识集合——集合与元素的定义",
	        "guideCdnUrl": "https://static.xiaoluxue.cn/lesson/video/json/66061af39a8b9c7138fcb4024ae308e7_1_guide_598907589657093_1766421186968.json",
	        "avatarUrl": "https://vod.xiaoluxue.com/a3e82dbafecb4345b81324f4a3909d60.mp4?a=0&br=265&bt=265&cd=0%7C0%7C0&ch=0&cr=0&cs=0&dr=0&ds=2&eid=v02103g10065d4256qaljhteml2gg690&er=0&l=202510311412583A8E7F4FCA9807868DCD&lr=&mime_type=video_mp4&net=0&pl=0&qs=13&rc=Mzlqb2hrb3Q4NzgzNDY0M0ApPGo1cHdtZDk1ZzkzajM1eWc2ZC9qcWdeMy9hMy1kLS9zcy0zZGliZWluMjEyLS4wLi06Yw%3D%3D&vl=&vr=",
	        "targetUrl": "https://stu.xiaoluxue.com/course?knowledgeId=3785&knowledgeName=%E9%9B%86%E5%90%88%E5%8F%8A%E5%85%B6%E8%A1%A8%E7%A4%BA%E6%96%B9%E6%B3%95&lessonId=598907589657093&lessonName=%E9%9B%86%E5%90%88%E5%8F%8A%E5%85%B6%E8%A1%A8%E7%A4%BA%E6%96%B9%E6%B3%95&phaseId=3&studySessionId=908465254772106&studyType=1&subjectId=2&redirectWidgetIndex=1",
	    },
}


def xiaoluxue_page(serial: str) -> dict[str, Any]:
    xiaoluxue_dismiss_debug_overlay_if_needed(serial)
    cached = xiaoluxue_cached_runtime_page(serial, "course")
    if cached:
        xiaoluxue_ensure_foreground_webview(serial, cached)
        return cached
    pages = discover_webview_pages(serial)
    page = select_xiaoluxue_webview_page(pages, "course")
    xiaoluxue_ensure_foreground_webview(serial, page)
    xiaoluxue_remember_inferred_page(serial, page)
    return xiaoluxue_remember_page(serial, "course", page)


def xiaoluxue_any_page(serial: str, *, open_app_if_needed: bool = True, timeout_sec: float = 5.0) -> dict[str, Any]:
    xiaoluxue_dismiss_debug_overlay_if_needed(serial)
    cached = xiaoluxue_cached_runtime_page(serial, "any")
    if cached:
        xiaoluxue_ensure_foreground_webview(serial, cached)
        return cached
    deadline = time.monotonic() + max(timeout_sec, 0.2)
    opened = False
    last_error: Exception | None = None
    while True:
        pages = discover_webview_pages(serial)
        try:
            page = select_xiaoluxue_webview_page(pages, "any")
            xiaoluxue_ensure_foreground_webview(serial, page)
            return xiaoluxue_remember_inferred_page(serial, page)
        except Exception as exc:
            last_error = exc
        if not open_app_if_needed or opened or time.monotonic() >= deadline:
            if last_error:
                raise last_error
            return select_webview_page(pages)
        adb(["shell", "monkey", "-p", XIAOLUXUE_STUDENT_PACKAGE, "-c", "android.intent.category.LAUNCHER", "1"], serial=serial, timeout=10)
        opened = True
    time.sleep(0.3)


def xiaoluxue_native_window_info(serial: str) -> dict[str, Any]:
    try:
        text = shell(serial, "dumpsys window", timeout=3)
    except AndroidUseError:
        text = ""
    focus_match = re.search(r"mCurrentFocus=([^\n]+)", text)
    bounds_match = re.search(r"boundsRect\(0,\s*0\s*-\s*(\d+),\s*(\d+)\)", text)
    width: int | None = None
    height: int | None = None
    if bounds_match:
        width = int(bounds_match.group(1))
        height = int(bounds_match.group(2))
    else:
        size = get_screen_size(serial)
        width = int(size["width"]) if size.get("width") else None
        height = int(size["height"]) if size.get("height") else None
    if width and height and width < height:
        width, height = height, width
    return {
        "focus": focus_match.group(1).strip() if focus_match else "",
        "width": width or XIAOLUXUE_NATIVE_BASE_WIDTH,
        "height": height or XIAOLUXUE_NATIVE_BASE_HEIGHT,
    }


def xiaoluxue_wait_native_app_focus(serial: str, timeout_sec: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout_sec, 0.05)
    last_info = xiaoluxue_native_window_info(serial)
    while time.monotonic() < deadline:
        info = xiaoluxue_native_window_info(serial)
        last_info = info
        focus = str(info.get("focus") or "")
        if f"{XIAOLUXUE_STUDENT_PACKAGE}/" in focus:
            return info
        time.sleep(0.08)
    return last_info


def xiaoluxue_native_scaled_point(point: tuple[int, int], window_info: dict[str, Any]) -> tuple[int, int]:
    width = int(window_info.get("width") or XIAOLUXUE_NATIVE_BASE_WIDTH)
    height = int(window_info.get("height") or XIAOLUXUE_NATIVE_BASE_HEIGHT)
    x, y = point
    return round(x * width / XIAOLUXUE_NATIVE_BASE_WIDTH), round(y * height / XIAOLUXUE_NATIVE_BASE_HEIGHT)


def xiaoluxue_native_tap(
    serial: str,
    point: tuple[int, int],
    window_info: dict[str, Any],
    label: str,
    steps: list[dict[str, Any]],
    started_at: float,
) -> None:
    x, y = xiaoluxue_native_scaled_point(point, window_info)
    adb(["shell", "input", "tap", str(x), str(y)], serial=serial, timeout=2)
    steps.append({"label": label, "x": x, "y": y, "at_sec": round(time.monotonic() - started_at, 3)})


def xiaoluxue_native_back(serial: str, label: str, steps: list[dict[str, Any]], started_at: float) -> None:
    adb(["shell", "input", "keyevent", "KEYCODE_BACK"], serial=serial, timeout=2)
    steps.append({"label": label, "key": "BACK", "at_sec": round(time.monotonic() - started_at, 3)})


def xiaoluxue_wait_for_site_page(serial: str, deadline: float, *, poll_interval: float = 0.04) -> dict[str, Any]:
    cached = xiaoluxue_cached_runtime_page(serial, "any")
    if cached:
        return cached
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        pages = discover_webview_pages(serial)
        try:
            page = select_xiaoluxue_webview_page(pages, "any")
            return xiaoluxue_remember_inferred_page(serial, page)
        except Exception as exc:
            last_error = exc
        time.sleep(poll_interval)
    if last_error:
        raise last_error
    raise AndroidUseError("Could not find a Xiaoluxue WebView page after native entry.")


def xiaoluxue_wait_for_target_course_page(
    serial: str,
    deadline: float,
    *,
    knowledge_id: int,
    poll_interval: float = 0.04,
) -> dict[str, Any]:
    target_marker = f"knowledgeId={knowledge_id}"
    cached = xiaoluxue_cached_page(serial, "course", runtime_contains=target_marker)
    if cached:
        return cached
    last_course_page: dict[str, Any] | None = None
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        pages = discover_webview_pages(serial)
        candidates = [page for page in pages if not page.get("error")]
        candidates.sort(key=webview_page_score, reverse=True)
        for page in candidates:
            url = str(page.get("url") or "")
            if xiaoluxue_url_kind(url) == "course":
                last_course_page = page
                if target_marker in url:
                    checked_page = {**page, "runtimeHref": url}
                    try:
                        xiaoluxue_ensure_foreground_webview(serial, checked_page)
                        xiaoluxue_remember_inferred_page(serial, page)
                        return checked_page
                    except Exception as exc:
                        last_error = exc
                    try:
                        runtime_href = cdp_eval_value(page, "location.href", timeout=0.35)
                        if isinstance(runtime_href, str) and target_marker in runtime_href:
                            checked_page = {**page, "runtimeHref": runtime_href}
                            xiaoluxue_ensure_foreground_webview(serial, checked_page)
                            xiaoluxue_remember_inferred_page(serial, page)
                            return checked_page
                        if isinstance(runtime_href, str) and xiaoluxue_url_kind(runtime_href) == "course":
                            last_course_page = {**page, "runtimeHref": runtime_href}
                    except Exception as exc:
                        last_error = exc
        try:
            last_course_page = select_xiaoluxue_webview_page(pages, "course")
        except Exception as exc:
            last_error = exc
        time.sleep(poll_interval)
    if last_course_page is not None:
        last_href = str(last_course_page.get("runtimeHref") or last_course_page.get("url") or "")
        if target_marker in last_href:
            xiaoluxue_ensure_foreground_webview(serial, last_course_page)
            xiaoluxue_remember_inferred_page(serial, last_course_page)
            return last_course_page
        raise AndroidUseError(f"Xiaoluxue course WebView opened but target {target_marker} was not active.")
    if last_error:
        raise last_error
    raise AndroidUseError(f"Could not find target Xiaoluxue course WebView for {target_marker}.")


def xiaoluxue_open_vessel_course_page(
    serial: str,
    *,
    target_url: str,
    knowledge_id: int,
    timeout_sec: float,
    force_stop: bool = False,
    bootstrap_scripts: list[str] | None = None,
) -> dict[str, Any]:
    started_at = time.monotonic()
    deadline = started_at + max(timeout_sec, 0.5)
    bootstrap_results: list[dict[str, Any]] = []
    if bootstrap_scripts:
        for page in discover_webview_pages(serial)[:3]:
            websocket = str(page.get("webSocketDebuggerUrl") or "")
            if not websocket:
                continue
            page_result: dict[str, Any] = {"page_id": page.get("id"), "ok": True, "scripts": 0}
            try:
                cdp_call(websocket, "Page.enable", {}, timeout=0.25)
                for script_id in range(1, 21):
                    try:
                        cdp_call(
                            websocket,
                            "Page.removeScriptToEvaluateOnNewDocument",
                            {"identifier": str(script_id)},
                            timeout=0.08,
                        )
                    except Exception:
                        pass
                for script in bootstrap_scripts:
                    cdp_call(
                        websocket,
                        "Page.addScriptToEvaluateOnNewDocument",
                        {"source": script},
                        timeout=0.25,
                    )
                    page_result["scripts"] = int(page_result["scripts"]) + 1
            except Exception as exc:
                page_result["ok"] = False
                page_result["error"] = str(exc)
            bootstrap_results.append(page_result)
    route_url = (
        f"{XIAOLUXUE_VESSEL_WEBVIEW_ROUTE}?url={urllib.parse.quote(target_url, safe='')}"
        "&full_screen=true&title_bar=false"
    )
    command = [
        "shell",
        "am",
        "start",
    ]
    if force_stop:
        command.append("-S")
    command.extend(
        [
            "-n",
            f"{XIAOLUXUE_STUDENT_PACKAGE}/{XIAOLUXUE_SCHEME_PROXY_ACTIVITY}",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            route_url,
        ]
    )
    output = adb(command, serial=serial, timeout=3).decode("utf-8", errors="replace")
    page = xiaoluxue_wait_for_target_course_page(serial, deadline, knowledge_id=knowledge_id)
    xiaoluxue_ensure_foreground_webview(serial, page)
    return {
        "attempted": True,
        "ok": True,
        "mode": "vessel-webview-route",
        "elapsed_sec": round(time.monotonic() - started_at, 3),
        "route_url": route_url,
        "am_start": output,
        "bootstrap": bootstrap_results,
        "page_id": page.get("id"),
        "page_url": page.get("url"),
        "page": page,
    }


def xiaoluxue_try_native_course_sequence(
    serial: str,
    window_info: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
    deadline: float,
    *,
    label: str,
    tap_math: bool,
    expected_knowledge_id: int | None = None,
    dismiss_progress_popup: bool = True,
) -> dict[str, Any]:
    attempt: dict[str, Any] = {
        "label": label,
        "tap_math": tap_math,
        "started_at_sec": round(time.monotonic() - started_at, 3),
    }
    try:
        if tap_math:
            xiaoluxue_native_tap(serial, XIAOLUXUE_NATIVE_MATH_CARD, window_info, f"{label}:math_card", steps, started_at)
            time.sleep(1.18)
        if dismiss_progress_popup:
            xiaoluxue_native_tap(
                serial,
                XIAOLUXUE_NATIVE_PROGRESS_POPUP_CLOSE,
                window_info,
                f"{label}:dismiss_progress_popup",
                steps,
                started_at,
            )
            time.sleep(0.06)
        xiaoluxue_native_tap(serial, XIAOLUXUE_NATIVE_GUIDE_BUBBLE, window_info, f"{label}:guide_bubble", steps, started_at)
        page: dict[str, Any] | None = None
        last_continue_error: Exception | None = None
        for continue_attempt in range(3):
            time.sleep(0.08 if continue_attempt == 0 else 0.07)
            xiaoluxue_native_tap(
                serial,
                XIAOLUXUE_NATIVE_CONTINUE_BUTTON,
                window_info,
                f"{label}:continue:{continue_attempt + 1}",
                steps,
                started_at,
            )
            try:
                page_deadline = min(deadline, time.monotonic() + 0.24)
                page = (
                    xiaoluxue_wait_for_target_course_page(
                        serial,
                        page_deadline,
                        knowledge_id=expected_knowledge_id,
                    )
                    if expected_knowledge_id
                    else xiaoluxue_wait_for_site_page(serial, page_deadline)
                )
                break
            except Exception as exc:
                last_continue_error = exc
        if page is None:
            try:
                page_deadline = min(deadline, time.monotonic() + 1.2)
                page = (
                    xiaoluxue_wait_for_target_course_page(
                        serial,
                        page_deadline,
                        knowledge_id=expected_knowledge_id,
                    )
                    if expected_knowledge_id
                    else xiaoluxue_wait_for_site_page(serial, page_deadline)
                )
            except Exception:
                if last_continue_error is not None:
                    raise last_continue_error
                raise
        attempt["ok"] = True
        attempt["elapsed_sec"] = round(time.monotonic() - started_at, 3)
        attempt["page_id"] = page.get("id")
        attempt["page_url"] = page.get("url")
        attempt["page"] = page
    except Exception as exc:  # Keep the native fallback cheap and observable.
        attempt["ok"] = False
        attempt["error"] = str(exc)
        attempt["elapsed_sec"] = round(time.monotonic() - started_at, 3)
    return attempt


def xiaoluxue_open_native_course_entry(
    serial: str,
    *,
    subject_id: int,
    normalized_index: str,
    timeout_sec: float,
) -> dict[str, Any]:
    started_at = time.monotonic()
    if (subject_id, normalized_index) != (2, "1111"):
        return {"attempted": False, "ok": False, "reason": "unsupported-native-shortcut"}

    deadline = started_at + max(timeout_sec, 1.0)
    shortcut = XIAOLUXUE_KNOWLEDGE_SHORTCUTS.get((subject_id, normalized_index))
    expected_knowledge_id = int(shortcut["knowledgeId"]) if shortcut and shortcut.get("knowledgeId") else None
    info = xiaoluxue_native_window_info(serial)
    focus = str(info.get("focus") or "")
    already_home = f"{XIAOLUXUE_STUDENT_PACKAGE}/com.xiaoluxue.ai.student.LauncherActivity" in focus
    already_subject_map = f"{XIAOLUXUE_STUDENT_PACKAGE}/{XIAOLUXUE_STUDY_SUBJECT_ACTIVITY}" in focus
    steps: list[dict[str, Any]] = []
    start_result = {"skipped": already_home or already_subject_map, "focus": focus}

    attempts: list[dict[str, Any]] = []
    page: dict[str, Any] | None = None

    try:
        route_info = xiaoluxue_open_native_subject_map(
            serial,
            {
                "subject_id": subject_id,
                "route_wait_sec": 1.15,
                "route_settle_sec": 0.05,
                "close_progress_popup": True,
                "close_progress_taps": 1,
                "close_progress_wait_sec": 0.05,
            },
            steps,
            started_at,
        )
        routed_info = route_info.get("window_info") if isinstance(route_info, dict) else None
        if isinstance(routed_info, dict):
            info = routed_info
        focus = str(info.get("focus") or "")
        already_home = False
        already_subject_map = True
        start_result = {"mode": "subject-route", "focus": focus, "route": route_info}
    except Exception as exc:
        start_result["subject_route_error"] = str(exc)

    if not already_home and not already_subject_map:
        adb(["shell", "am", "start", "-n", XIAOLUXUE_STUDENT_LAUNCHER_COMPONENT], serial=serial, timeout=5)
        steps.append({"label": "start_launcher", "at_sec": round(time.monotonic() - started_at, 3)})
        info = xiaoluxue_wait_native_app_focus(serial, min(max(deadline - time.monotonic(), 0.2), 1.4))
        focus = str(info.get("focus") or "")
        already_home = f"{XIAOLUXUE_STUDENT_PACKAGE}/com.xiaoluxue.ai.student.LauncherActivity" in focus
        already_subject_map = f"{XIAOLUXUE_STUDENT_PACKAGE}/{XIAOLUXUE_STUDY_SUBJECT_ACTIVITY}" in focus
        if not already_home and not already_subject_map:
            time.sleep(0.2)
    if already_home:
        attempts.append(
            xiaoluxue_try_native_course_sequence(
                serial,
                info,
                steps,
                started_at,
                deadline,
                label="home",
                tap_math=True,
                expected_knowledge_id=expected_knowledge_id,
                dismiss_progress_popup=True,
            )
        )
    else:
        attempts.append(
            xiaoluxue_try_native_course_sequence(
                serial,
                info,
                steps,
                started_at,
                deadline,
                label="subject_map",
                tap_math=False,
                expected_knowledge_id=expected_knowledge_id,
                dismiss_progress_popup=not (start_result.get("mode") == "subject-route"),
            )
        )
    if attempts and attempts[-1].get("ok"):
        page_value = attempts[-1].get("page")
        if isinstance(page_value, dict):
            page = page_value
    if page is None and time.monotonic() + 1.4 < deadline:
        xiaoluxue_native_back(serial, "fallback_back_to_home", steps, started_at)
        time.sleep(0.42)
        fallback_info = xiaoluxue_wait_native_app_focus(serial, min(max(deadline - time.monotonic(), 0.2), 0.8))
        attempts.append(
            xiaoluxue_try_native_course_sequence(
                serial,
                fallback_info,
                steps,
                started_at,
                deadline,
                label="fallback_home",
                tap_math=True,
                expected_knowledge_id=expected_knowledge_id,
                dismiss_progress_popup=True,
            )
        )
        page_value = attempts[-1].get("page")
        if isinstance(page_value, dict):
            page = page_value
    if page is None:
        last_error = attempts[-1].get("error") if attempts else "no native attempt completed"
        raise AndroidUseError(str(last_error))
    elapsed = round(time.monotonic() - started_at, 3)
    return {
        "attempted": True,
        "ok": True,
        "elapsed_sec": elapsed,
        "window": info,
        "start": start_result,
        "steps": steps,
        "attempts": [{k: v for k, v in attempt.items() if k != "page"} for attempt in attempts],
        "page": page,
    }


def normalize_xiaoluxue_knowledge_index(value: Any) -> str:
    return re.sub(r"[^0-9]", "", str(value or ""))


def xiaoluxue_snapshot_expression() -> str:
    return r"""
(() => {
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height, top: r.top, right: r.right, bottom: r.bottom, left: r.left };
  };
  const text = (el) => (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ");
  const parseWidgetName = (dataName) => {
    const match = /^widget-(\d+)-(.+)$/.exec(dataName || "");
    return match ? { index: Number(match[1]), name: match[2] } : { index: null, name: dataName || "" };
  };
  const widgets = [...document.querySelectorAll('[data-name^="widget-"]')].map((el) => {
    const parsed = parseWidgetName(el.dataset.name);
    const bounds = rect(el);
    const visibleRatio = Math.max(0, Math.min(bounds.bottom, innerHeight) - Math.max(bounds.top, 0)) / Math.max(bounds.height, 1);
    return {
      dataName: el.dataset.name,
      index: parsed.index,
      name: parsed.name,
      text: text(el).slice(0, 220),
      rect: bounds,
      visibleRatio,
      loaded: text(el).length > 0 || !!el.querySelector('[data-name="guide-player"], video, button'),
    };
  });
  const visibleWidgets = widgets.filter((widget) => widget.rect.bottom > 0 && widget.rect.top < innerHeight);
  const currentWidget = [...widgets].sort((a, b) => Math.abs(a.rect.top) - Math.abs(b.rect.top))[0] || null;
  const buttons = [...document.querySelectorAll('button,[role="button"],ol')]
    .map((el) => ({ text: text(el), dataName: el.dataset.name || "", rect: rect(el) }))
    .filter((item) => item.text)
    .slice(0, 60);
  const videos = [...document.querySelectorAll('video')].map((video) => ({
    src: video.currentSrc || video.src || "",
    currentTime: video.currentTime,
    duration: Number.isFinite(video.duration) ? video.duration : null,
    playbackRate: video.playbackRate,
    paused: video.paused,
    rect: rect(video),
  }));
  const params = Object.fromEntries(new URL(location.href).searchParams.entries());
  return {
    app: "xiaoluxue",
    page: "course",
    title: document.title,
    url: location.href,
    params,
    viewport: { width: innerWidth, height: innerHeight, devicePixelRatio },
    guidePlayerVisible: !!document.querySelector('[data-name="guide-player"]'),
    guideControlsVisible: !!document.querySelector('[data-name="guide-controller::follow"]'),
    widgetCount: widgets.length,
    currentWidget,
    visibleWidgets,
    widgets,
    buttons,
    videos,
    localProgressKeys: Object.keys(localStorage).filter((key) => key.startsWith("course.progress:")).slice(0, 30),
  };
})()
"""


def xiaoluxue_set_speed_expression(rate: float) -> str:
    rate_json = json.dumps(rate)
    return f"""
(async () => {{
  const rate = {rate_json};
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const text = (el) => (el.innerText || el.textContent || "").trim().replace(/\\s+/g, " ");
  const rect = (el) => {{
    const r = el.getBoundingClientRect();
    return {{ top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height }};
  }};
  const visible = (el) => {{
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight;
  }};
  const click = (el) => {{
    el.dispatchEvent(new MouseEvent("click", {{ bubbles: true, cancelable: true, view: window }}));
  }};
  const widgets = [...document.querySelectorAll('[data-name^="widget-"]')];
  const visibleWidget = widgets.find((el) => {{
    const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.top < innerHeight;
  }});
  const player = visibleWidget?.querySelector('[data-name="guide-player"]') || document.querySelector('[data-name="guide-player"]');
  if (player) {{
    click(player);
    await sleep(160);
  }}
  const candidates = [...document.querySelectorAll('button,[role="button"],div,span,ol')].filter(visible);
  const rateButton = candidates.find((el) => text(el).includes("倍速"));
  if (rateButton) {{
    click(rateButton);
    await sleep(160);
  }}
  const labels = Array.from(new Set([`${{rate}}x`, `${{Number(rate).toFixed(1)}}x`]));
  const refreshed = [...document.querySelectorAll('button,[role="button"],div,span,ol,li')].filter(visible);
  const option = refreshed.find((el) => labels.includes(text(el)) || labels.some((label) => text(el).split(/\\s+/).includes(label)));
  if (option) {{
    click(option);
    await sleep(120);
  }}
  const videos = [...document.querySelectorAll("video")];
  for (const video of videos) {{
    try {{ video.playbackRate = rate; }} catch (_error) {{}}
  }}
  return {{
    ok: Boolean(option) || videos.length > 0,
    rate,
    playerClicked: Boolean(player),
    rateButtonClicked: Boolean(rateButton),
    optionClicked: Boolean(option),
    optionText: option ? text(option) : "",
    guideControlsVisible: Boolean(document.querySelector('[data-name="guide-controller::follow"]')),
    videos: videos.map((video) => ({{ playbackRate: video.playbackRate, paused: video.paused, currentTime: video.currentTime }})),
  }};
}})()
	"""


def xiaoluxue_fast_rate_expression(rate: float, guide_index: int | None = None, *, activate_player: bool = False) -> str:
    rate_json = json.dumps(rate)
    guide_index_json = "null" if guide_index is None else json.dumps(int(guide_index))
    activate_player_json = json.dumps(activate_player)
    return f"""
(() => {{
  const rate = {rate_json};
  const guideIndex = {guide_index_json};
  const activatePlayer = {activate_player_json};
  const text = (el) => (el?.innerText || el?.textContent || "").trim().replace(/\\s+/g, " ");
  const click = (el) => {{
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const eventInit = {{
      bubbles: true,
      cancelable: true,
      view: window,
      clientX: rect.left + rect.width / 2,
      clientY: rect.top + rect.height / 2,
    }};
    el.dispatchEvent(new PointerEvent("pointerdown", eventInit));
    el.dispatchEvent(new MouseEvent("mousedown", eventInit));
    el.dispatchEvent(new PointerEvent("pointerup", eventInit));
    el.dispatchEvent(new MouseEvent("mouseup", eventInit));
    el.dispatchEvent(new MouseEvent("click", eventInit));
    return true;
  }};
	  const apply = () => {{
	    const videos = [...document.querySelectorAll("video")];
	    for (const video of videos) {{
      try {{
        video.defaultPlaybackRate = rate;
        video.playbackRate = rate;
      }} catch (_error) {{}}
    }}
	    return videos.map((video) => ({{
	      playbackRate: video.playbackRate,
	      paused: video.paused,
	      currentTime: video.currentTime,
	    }}));
	  }};
	  const findTargetPlayer = () => {{
	    if (guideIndex != null) {{
	      return document.querySelector(`[data-name^="widget-${{guideIndex}}-"] [data-name="guide-player"]`);
	    }}
	    return document.querySelector('[data-name="guide-player"]');
	  }};
	  const targetVideos = () => {{
	    const player = findTargetPlayer();
	    const scope = player?.closest?.('[data-name^="widget-"]') || player || document;
	    return [...scope.querySelectorAll("video")];
	  }};
	  const targetVideoPlaying = () => targetVideos().some((video) => !video.paused && !video.ended);
	  const trySetControllerRate = () => {{
	    const controller = document.querySelector('[data-name="guide-controller::follow"]');
	    if (!controller) return {{ ok: false, reason: "controller-missing" }};
	    const desiredText = `${{rate}}x`;
	    const options = [...document.querySelectorAll("ol")];
	    const option = options.find((el) => text(el) === desiredText);
	    if (option) {{
	      click(option);
	      return {{ ok: true, selected: desiredText }};
	    }}
	    const rateButton = [...controller.querySelectorAll("button")].find((el) => text(el).includes("倍速"));
	    if (!rateButton) return {{ ok: false, reason: "rate-button-missing" }};
	    click(rateButton);
	    setTimeout(() => {{
	      const laterOption = [...document.querySelectorAll("ol")].find((el) => text(el) === desiredText);
	      if (laterOption) click(laterOption);
	    }}, 40);
	    return {{ ok: true, opened: true }};
	  }};
	  const tryActivate = () => {{
	    if (!window.__xiaoluxuePendingPlayerActivate) return {{ scheduled: false, player: findTargetPlayer() }};
	    const player = findTargetPlayer();
	    if (!player) return {{ scheduled: false, player: null }};
	    const key = `${{location.href}}::${{guideIndex ?? "any"}}`;
	    const now = Date.now();
	    if (window.__xiaoluxueActivatedPlayerKey === key && targetVideoPlaying()) {{
	      return {{ scheduled: false, player, rateControl: trySetControllerRate() }};
	    }}
	    if (window.__xiaoluxueActivatedPlayerKey === key && now - (window.__xiaoluxueLastPlayerClickAt || 0) < 700) {{
	      return {{ scheduled: false, waiting: true, player, rateControl: trySetControllerRate() }};
	    }}
	    window.__xiaoluxueActivatedPlayerKey = key;
	    window.__xiaoluxueLastPlayerClickAt = now;
	    window.__xiaoluxuePendingPlayerActivate = false;
	    setTimeout(() => {{
	      click(player);
	      setTimeout(() => trySetControllerRate(), 60);
	      setTimeout(() => trySetControllerRate(), 180);
	    }}, 0);
	    return {{ scheduled: true, player, rateControl: trySetControllerRate() }};
	  }};
	  window.__xiaoluxueDesiredPlaybackRate = rate;
	  if (guideIndex != null) {{
	    window.__xiaoluxuePendingGuideIndex = guideIndex;
	  }}
	  if (activatePlayer) {{
	    window.__xiaoluxuePendingPlayerActivate = true;
	  }} else if (document.getElementById("__xiaoluxue-turbo-video")) {{
	    window.__xiaoluxuePendingPlayerActivate = false;
	  }}
	  window.__xiaoluxueRateTick = () => {{
	    const videos = apply();
	    const activation = tryActivate();
	    const rateControl = trySetControllerRate();
	    return {{ videos, activation, rateControl }};
	  }};
	  const ensureObserver = () => {{
	    const root = document.documentElement || document.body;
	    if (!root) {{
	      setTimeout(ensureObserver, 30);
	      return false;
	    }}
	    if (window.__xiaoluxueRateObserver) {{
	      try {{ window.__xiaoluxueRateObserver.disconnect(); }} catch (_error) {{}}
	    }}
	    window.__xiaoluxueRateObserver = new MutationObserver(() => window.__xiaoluxueRateTick?.());
	    window.__xiaoluxueRateObserver.observe(root, {{ childList: true, subtree: true }});
	    return true;
	  }};
	  const observerReady = ensureObserver();
	  if (!window.__xiaoluxueRatePlayListenerInstalled) {{
	    window.__xiaoluxueRatePlayListenerInstalled = true;
	    document.addEventListener(
	      "play",
	      (event) => {{
        const target = event.target;
        if (target && target.tagName === "VIDEO") {{
          try {{
            target.defaultPlaybackRate = window.__xiaoluxueDesiredPlaybackRate || rate;
            target.playbackRate = window.__xiaoluxueDesiredPlaybackRate || rate;
          }} catch (_error) {{}}
        }}
      }},
	      true
	    );
	  }}
	  clearInterval(window.__xiaoluxueRateInterval);
	  window.__xiaoluxueRateInterval = setInterval(() => window.__xiaoluxueRateTick?.(), 160);
	  for (const delay of [30, 120, 350, 800, 1400]) {{
	    setTimeout(() => window.__xiaoluxueRateTick?.(), delay);
	  }}
	  const tick = window.__xiaoluxueRateTick();
	  const player = tick.activation?.player || findTargetPlayer();
	  const playerClickScheduled = Boolean(tick.activation?.scheduled);
	  const videos = tick.videos || [];
	  return {{
	    ok: true,
	    rate,
	    observerInstalled: true,
	    observerReady,
    activatePlayer,
    playerClicked: playerClickScheduled,
    playerClickScheduled,
    playerText: player ? text(player).slice(0, 80) : "",
    rateControl: tick.rateControl,
    videos,
  }};
}})()
"""


def xiaoluxue_prefetch_guide_expression(guide_url: str | None, avatar_url: str | None = None) -> str:
    guide_url_json = "null" if not guide_url else json.dumps(guide_url)
    avatar_url_json = "null" if not avatar_url else json.dumps(avatar_url)
    return f"""
(() => {{
  const guideUrl = {guide_url_json};
  const avatarUrl = {avatar_url_json};
  const prefetch = (url, options = {{}}) => {{
    if (!url) return false;
    try {{
      fetch(url, {{ cache: "force-cache", ...options }}).catch(() => null);
      return true;
    }} catch (_error) {{
      return false;
    }}
  }};
  const warmVideo = (_url) => false;
  const guideStarted = prefetch(guideUrl);
  if (guideUrl) {{
    fetch(guideUrl, {{ cache: "force-cache" }})
      .then((response) => response.clone().json())
      .then((data) => {{
        const url = data?.avatar?.url;
        if (url && !avatarUrl) warmVideo(url);
      }})
      .catch(() => null);
  }}
  const avatarStarted = false;
  return {{ ok: true, guideStarted, avatarStarted, guideUrl: Boolean(guideUrl), avatarUrl: Boolean(avatarUrl) }};
}})()
"""


def xiaoluxue_turbo_guide_player_expression(
    *,
    guide_index: int | None,
    guide_name: str | None,
    avatar_url: str | None,
    rate: float,
    enabled: bool,
) -> str:
    guide_index_json = "null" if guide_index is None else json.dumps(int(guide_index))
    guide_name_json = json.dumps(guide_name or "知识讲解")
    avatar_url_json = "null" if not avatar_url else json.dumps(avatar_url)
    rate_json = json.dumps(rate)
    enabled_json = json.dumps(enabled)
    return f"""
(() => {{
  const enabled = {enabled_json};
  const guideIndex = {guide_index_json};
  const guideName = {guide_name_json};
  const avatarUrl = {avatar_url_json};
  const rate = {rate_json};
  const rootId = "__xiaoluxue-turbo-guide";
  const videoId = "__xiaoluxue-turbo-video";
  const text = (value) => String(value || "").trim();
  const findWidget = () => {{
    if (guideIndex == null) return document.querySelector('[data-name^="widget-"]');
    return document.querySelector(`[data-name^="widget-${{guideIndex}}-"]`);
  }};
  const visible = (el) => {{
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 8 && rect.height > 8 && rect.bottom > 0 && rect.top < innerHeight;
  }};
  const nativeVideoPlaying = () =>
    [...document.querySelectorAll("video")]
      .filter((video) => video.id !== "__xiaoluxue-avatar-prewarm" && video.id !== videoId)
      .some((video) => visible(video) && !video.paused && video.readyState >= 2);
  const applyRate = () => {{
    for (const video of document.querySelectorAll("video")) {{
      try {{
        video.defaultPlaybackRate = rate;
        video.playbackRate = rate;
      }} catch (_error) {{}}
    }}
  }};
  const remove = (reason = "native-ready") => {{
    const node = document.getElementById(rootId);
    if (node) node.remove();
    return {{ ok: true, removed: true, reason }};
  }};
  const mount = () => {{
    if (!enabled || !avatarUrl) return {{ ok: true, skipped: true }};
    if (nativeVideoPlaying()) return remove();
    const widget = findWidget();
    const host = widget || document.body || document.documentElement;
    if (!host) return {{ ok: true, mounted: false, reason: "host-missing" }};
    let root = document.getElementById(rootId);
    if (!root) {{
      root = document.createElement("div");
      root.id = rootId;
      root.dataset.name = "xiaoluxue-turbo-guide-player";
      root.style.cssText = [
        widget ? "position:absolute" : "position:fixed",
        "inset:0",
        widget ? "z-index:45" : "z-index:99999",
        "background:#FAF8F6",
        "color:#312d29",
        "font-family:system-ui,-apple-system,BlinkMacSystemFont,sans-serif",
        "overflow:hidden",
        "pointer-events:none"
      ].join(";");
      root.innerHTML = `
        <div style="position:absolute;left:54px;top:44px;right:300px;font-size:28px;font-weight:700;line-height:1.25;">${{text(guideName)}}</div>
        <div style="position:absolute;left:54px;top:96px;right:330px;font-size:18px;line-height:1.8;color:#57504a;">集合与元素的定义</div>
        <video id="${{videoId}}" src="${{avatarUrl}}" autoplay muted playsinline loop
          style="position:absolute;right:24px;bottom:30px;width:190px;height:190px;border-radius:8px;background:#efe2d6;object-fit:cover;box-shadow:0 1px 4px rgba(64,43,26,.08);"></video>
      `;
      host.appendChild(root);
    }}
    const video = document.getElementById(videoId);
    if (video) {{
      try {{
        video.defaultPlaybackRate = rate;
        video.playbackRate = rate;
        video.muted = true;
        video.playsInline = true;
        video.play?.().catch(() => null);
      }} catch (_error) {{}}
    }}
    applyRate();
    if (!window.__xiaoluxueTurboRateObserverInstalled) {{
      window.__xiaoluxueTurboRateObserverInstalled = true;
      document.addEventListener("play", applyRate, true);
      const observerRoot = document.documentElement || document.body;
      if (observerRoot) {{
        const rateObserver = new MutationObserver(applyRate);
        rateObserver.observe(observerRoot, {{ childList: true, subtree: true }});
        window.__xiaoluxueTurboRateObserver = rateObserver;
      }}
    }}
    return {{
      ok: true,
      mounted: true,
      bridge: "video",
      host: widget ? "widget" : "body",
      video: Boolean(video),
    }};
  }};
  window.__xiaoluxueTurboGuideTick = mount;
  clearInterval(window.__xiaoluxueTurboGuideInterval);
  window.__xiaoluxueTurboGuideInterval = setInterval(mount, 90);
  for (const delay of [0, 60, 160, 320, 700, 1200, 2400, 4200, 7000]) {{
    setTimeout(mount, delay);
  }}
  const root = document.documentElement || document.body;
  if (root) {{
    if (window.__xiaoluxueTurboGuideObserver) {{
      try {{ window.__xiaoluxueTurboGuideObserver.disconnect(); }} catch (_error) {{}}
    }}
    window.__xiaoluxueTurboGuideObserver = new MutationObserver(mount);
    window.__xiaoluxueTurboGuideObserver.observe(root, {{ childList: true, subtree: true }});
  }}
  return mount();
}})()
"""


def xiaoluxue_course_fetch_optimizer_expression(target_guide_url: str | None) -> str:
    target_guide_url_json = "null" if not target_guide_url else json.dumps(target_guide_url)
    return f"""
(() => {{
  const targetGuideUrl = {target_guide_url_json};
  if (!targetGuideUrl) return {{ ok: true, skipped: true }};
  window.__xiaoluxueTargetGuideUrl = targetGuideUrl;
  if (!window.__xiaoluxueOriginalFetch) {{
    window.__xiaoluxueOriginalFetch = window.fetch.bind(window);
    window.fetch = (input, init) => {{
      const url = typeof input === "string" ? input : input?.url || "";
      const target = window.__xiaoluxueTargetGuideUrl;
      if (
        target &&
        typeof url === "string" &&
        url.includes("/lesson/video/json/") &&
        url !== target
      ) {{
        return new Promise(() => {{}});
      }}
      return window.__xiaoluxueOriginalFetch(input, init);
    }};
  }}
  return {{ ok: true, targetGuideUrl, patched: true }};
}})()
"""


def xiaoluxue_stop_non_course_loading_expression() -> str:
    return """
(() => {
  const before = { href: location.href, readyState: document.readyState, hasRouter: Boolean(window.next?.router) };
  const isCourse = (() => {
    try { return new URL(location.href).pathname.replace(/\/$/, "") === "/course"; } catch (_error) { return false; }
  })();
  if (document.readyState === "loading" && !isCourse) {
    try { window.stop(); } catch (_error) {}
  }
  return { ok: true, before, after: { href: location.href, readyState: document.readyState, hasRouter: Boolean(window.next?.router) } };
})()
"""


def xiaoluxue_goto_widget_expression(index: int, mode: str) -> str:
    index_json = json.dumps(index)
    mode_json = json.dumps(mode)
    return f"""
(() => {{
  const targetIndex = {index_json};
  const mode = {mode_json};
  const widget = document.querySelector(`[data-name^="widget-${{targetIndex}}-"]`);
  if (mode === "scroll") {{
    if (!widget) return {{ ok: false, mode, targetIndex, error: "widget not found" }};
    const container = widget.parentElement;
    if (container && typeof container.scrollTo === "function") {{
      container.scrollTo({{ top: widget.offsetTop, behavior: "instant" }});
    }} else {{
      widget.scrollIntoView({{ block: "start", behavior: "instant" }});
    }}
    return {{ ok: true, mode, targetIndex, dataName: widget.dataset.name || "", loaded: (widget.innerText || "").trim().length > 0 }};
  }}
  const url = new URL(location.href);
  url.searchParams.set("redirectWidgetIndex", String(targetIndex));
  location.href = url.toString();
  return {{ ok: true, mode: "reload", targetIndex, url: url.toString() }};
}})()
"""


def xiaoluxue_resolve_knowledge_guide_expression(
    *,
    subject_id: int,
    knowledge_index: str,
    knowledge_id: int | None,
    guide_widget_index: int | None,
) -> str:
    subject_id_json = json.dumps(subject_id)
    knowledge_index_json = json.dumps(knowledge_index)
    knowledge_id_json = "null" if knowledge_id is None else json.dumps(knowledge_id)
    guide_widget_index_json = "null" if guide_widget_index is None else json.dumps(guide_widget_index)
    return f"""
(async () => {{
  const subjectId = {subject_id_json};
  const requestedIndex = {knowledge_index_json};
  let knowledgeId = {knowledge_id_json};
  const requestedGuideIndex = {guide_widget_index_json};
  const gateway = {json.dumps(XIAOLUXUE_GW_ORIGIN)};
  const parse = (value) => {{
    try {{ return JSON.parse(value); }} catch (_error) {{ return null; }}
  }};
  const normalizeIndex = (value) => String(value || "").replace(/[^0-9]/g, "");
  const bridge = window.AndroidBridge;
  if (!bridge || typeof bridge.invokeSync !== "function") {{
    throw new Error("AndroidBridge is unavailable in this WebView.");
  }}
  const headers = parse(bridge.invokeSync("getNetworkHeaderParams", "{{}}"))?.data || {{}};
  const student = parse(bridge.invokeSync("getStudentUserInfo", "{{}}"))?.data || {{}};
  const fetchJson = async (url) => {{
    const response = await fetch(url, {{ headers }});
    let body;
    try {{ body = await response.json(); }} catch (_error) {{ body = await response.text(); }}
    return {{ ok: response.ok, status: response.status, body }};
  }};
  const enterFor = async (candidateKnowledgeId) => {{
    const query = new URLSearchParams({{
      subjectId: String(subjectId),
      studyType: "1",
      knowledgeId: String(candidateKnowledgeId),
      phaseId: String(student.phase || 3),
    }});
    const response = await fetchJson(`${{gateway}}/study-api/api/v1/study_session/enter?${{query.toString()}}`);
    const data = response.body?.data;
    return {{ response, data }};
  }};

  let source = knowledgeId ? "explicit" : "";
  let enter = knowledgeId ? await enterFor(knowledgeId) : null;
  if (!knowledgeId) {{
    const cardQuery = new URLSearchParams({{
      grade: String(student.classGrade || 11),
      schoolId: String(student.schoolId || 1),
      classSubjectType: String(student.classSubjectType || 1),
      phase: String(student.phase || 3),
    }});
    const cards = await fetchJson(`${{gateway}}/student-skeleton-api/api/v1/subjects/cards?${{cardQuery.toString()}}`);
    const subject = (cards.body?.data || []).find((item) => Number(item.subject) === subjectId);
    const candidate = Number(subject?.subjectPage?.studyBizTreeNode?.knowledgeNodeId || 0);
    if (candidate > 0) {{
      const candidateEnter = await enterFor(candidate);
      if (normalizeIndex(candidateEnter.data?.knowledgeIndex) === normalizeIndex(requestedIndex)) {{
        knowledgeId = candidate;
        enter = candidateEnter;
        source = "subject-card";
      }}
    }}
  }}
  if (!knowledgeId || !enter?.data?.studySessionUrl) {{
    throw new Error(`Could not resolve Xiaoluxue knowledge index ${{requestedIndex}} for subject ${{subjectId}}.`);
  }}

  const lessonQuery = new URLSearchParams({{ knowledgeId: String(knowledgeId) }});
  const lesson = await fetchJson(`${{gateway}}/study-api/api/v1/lesson/info?${{lessonQuery.toString()}}`);
  const widgets = lesson.body?.data?.lessonWidgets || [];
  const guideWidget =
    requestedGuideIndex != null
      ? widgets.find((widget) => Number(widget.widgetIndex) === Number(requestedGuideIndex)) || {{ widgetIndex: requestedGuideIndex }}
      : widgets.find((widget) => widget.widgetType === "guide" && Number(widget.widgetIndex) > 0) ||
        widgets.find((widget) => widget.widgetType === "guide") ||
        {{ widgetIndex: 0 }};
  const targetUrl = new URL(enter.data.studySessionUrl);
  targetUrl.searchParams.set("redirectWidgetIndex", String(guideWidget.widgetIndex ?? 0));
  return {{
    ok: true,
    source: source || "resolved",
    subjectId,
    requestedIndex,
    knowledgeId,
    knowledgeIndex: enter.data.knowledgeIndex,
    knowledgeName: enter.data.knowledgeName,
	    lessonId: enter.data.lessonId,
	    studySessionId: enter.data.studySessionId,
	    guideIndex: Number(guideWidget.widgetIndex ?? 0),
	    guideName: guideWidget.widgetName || "",
	    guideCdnUrl: guideWidget.cdnUrl || "",
	    targetUrl: targetUrl.toString(),
	  }};
}})()
"""


def xiaoluxue_route_course_expression(target_url: str, *, prefer_client_route: bool) -> str:
    target_url_json = json.dumps(target_url)
    prefer_client_route_json = json.dumps(prefer_client_route)
    return f"""
	(() => {{
	  const targetUrl = {target_url_json};
	  const preferClientRoute = {prefer_client_route_json};
	  const beforeUrl = location.href;
	  if (preferClientRoute) {{
	    try {{
	      const target = new URL(targetUrl);
	      if (target.origin === location.origin) {{
	        const router = window.next?.router;
	        if (router && typeof router.replace === "function") {{
	          setTimeout(() => {{
	            try {{
	              router.replace(target.toString());
	            }} catch (_error) {{
	              location.replace(target.toString());
	            }}
	          }}, 0);
	          return {{ ok: true, mode: "next-router-replace", scheduled: true, beforeUrl, url: target.toString(), href: location.href }};
	        }}
	        setTimeout(() => {{
	          history.pushState(null, "", target.toString());
	          window.dispatchEvent(new PopStateEvent("popstate", {{ state: null }}));
	          window.dispatchEvent(new Event("pushstate"));
	        }}, 0);
	        return {{ ok: true, mode: "client-route", scheduled: true, beforeUrl, url: target.toString(), href: location.href }};
	      }}
	    }} catch (_error) {{}}
	  }}
	  setTimeout(() => location.replace(targetUrl), 0);
	  return {{ ok: true, mode: "reload", scheduled: true, beforeUrl, url: targetUrl, href: location.href }};
	}})()
	"""


def xiaoluxue_course_ready_expression(knowledge_id: int, guide_index: int | None) -> str:
    knowledge_id_json = json.dumps(str(knowledge_id))
    guide_index_json = "null" if guide_index is None else json.dumps(int(guide_index))
    return f"""
(() => {{
  const knowledgeId = {knowledge_id_json};
  const guideIndex = {guide_index_json};
  const rect = (el) => {{
    const r = el.getBoundingClientRect();
    return {{ top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height }};
  }};
  const text = (el) => (el?.innerText || el?.textContent || "").trim().replace(/\\s+/g, " ");
  const visible = (el) => {{
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight;
  }};
  const widgets = [...document.querySelectorAll('[data-name^="widget-"]')];
  const visibleWidget = widgets.find(visible) || null;
  const targetWidget =
    guideIndex == null ? visibleWidget : document.querySelector(`[data-name^="widget-${{guideIndex}}-"]`);
  const videos = [...document.querySelectorAll("video")];
  const params = Object.fromEntries(new URL(location.href).searchParams.entries());
  const targetLoaded = Boolean(targetWidget && (text(targetWidget).length > 0 || targetWidget.querySelector('[data-name="guide-player"], video')));
  return {{
    ok: params.knowledgeId === knowledgeId,
    href: location.href,
    readyState: document.readyState,
    params,
    bodyTextLength: text(document.body).length,
    guideIndex,
    targetWidget: targetWidget ? {{ dataName: targetWidget.dataset.name || "", loaded: targetLoaded, rect: rect(targetWidget) }} : null,
    visibleWidget: visibleWidget ? {{ dataName: visibleWidget.dataset.name || "", text: text(visibleWidget).slice(0, 160), rect: rect(visibleWidget) }} : null,
    guidePlayerVisible: Boolean(document.querySelector('[data-name="guide-player"]')),
    videos: videos.map((video) => ({{ playbackRate: video.playbackRate, paused: video.paused, currentTime: video.currentTime }})),
  }};
}})()
"""

# Loaded by scripts/android_use_mcp.py. Keep this file below 2000 lines.

def cdp_eval_value(page: dict[str, Any], expression: str, *, timeout: int | float = 10) -> Any:
    evaluation = cdp_runtime_evaluate(str(page["webSocketDebuggerUrl"]), expression, timeout=timeout)
    return evaluation.get("value")


def xiaoluxue_runtime_url_matches(target_url: str, runtime_href: str) -> bool:
    try:
        target = urllib.parse.urlparse(target_url)
        current = urllib.parse.urlparse(runtime_href)
    except Exception:
        return False
    if (target.hostname or "").casefold() != (current.hostname or "").casefold():
        return False
    if target.path.rstrip("/") != current.path.rstrip("/"):
        return False
    target_query = urllib.parse.parse_qs(target.query)
    current_query = urllib.parse.parse_qs(current.query)
    for key in ("knowledgeId", "lessonId", "studySessionId"):
        if target_query.get(key) and target_query.get(key) != current_query.get(key):
            return False
    return True


def xiaoluxue_rebase_h5_url(target_url: str, runtime_href: str | None) -> str:
    if not runtime_href:
        return target_url
    try:
        target = urllib.parse.urlparse(target_url)
        current = urllib.parse.urlparse(runtime_href)
    except Exception:
        return target_url
    if not is_xiaoluxue_h5_host(target.hostname or "") or not is_xiaoluxue_h5_host(current.hostname or ""):
        return target_url
    if (target.hostname or "").casefold() == (current.hostname or "").casefold() and target.scheme == current.scheme:
        return target_url
    return urllib.parse.urlunparse(
        (
            current.scheme or target.scheme,
            current.netloc or target.netloc,
            target.path,
            target.params,
            target.query,
            target.fragment,
        )
    )


def xiaoluxue_wait_for_app_url(serial: str, target_url: str, deadline: float, *, poll_interval: float = 0.04) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(target_url)
    host = parsed.hostname or XIAOLUXUE_SITE_URL_MARKER
    page_kind = "course" if "/course" in parsed.path else "exercise" if "/exercise" in parsed.path else "any"
    cached = xiaoluxue_cached_page(serial, page_kind, runtime_contains=host)
    if cached and xiaoluxue_runtime_url_matches(target_url, str(cached.get("runtimeHref") or cached.get("url") or "")):
        return cached
    last_page: dict[str, Any] | None = None
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        pages = discover_webview_pages(serial)
        candidates = [page for page in pages if not page.get("error")]
        candidates.sort(key=webview_page_score, reverse=True)
        for page in candidates:
            if "小鹿爱学" not in str(page.get("title") or "") and "xiaoluxue" not in str(page.get("url") or ""):
                continue
            try:
                runtime_href = cdp_eval_value(page, "location.href", timeout=0.35)
            except Exception as exc:
                last_error = exc
                runtime_href = str(page.get("url") or "")
            if isinstance(runtime_href, str) and xiaoluxue_runtime_url_matches(target_url, runtime_href):
                remembered = xiaoluxue_remember_page(serial, page_kind, page)
                xiaoluxue_remember_page(serial, "any", page)
                return {**remembered, "runtimeHref": runtime_href}
            if isinstance(runtime_href, str) and "xiaoluxue" in runtime_href:
                last_page = {**page, "runtimeHref": runtime_href}
        time.sleep(poll_interval)
    if last_page is not None:
        raise AndroidUseError(f"Xiaoluxue WebView opened but target URL was not active: {target_url}")
    if last_error:
        raise last_error
    raise AndroidUseError(f"Could not find Xiaoluxue WebView for URL: {target_url}")


def xiaoluxue_runtime_bridge_expression(*, reveal_overlay: bool = False) -> str:
    reveal_overlay_json = json.dumps(reveal_overlay)
    return f"""
(() => {{
  const rect = (el) => {{
    const r = el.getBoundingClientRect();
    return {{ x: r.x, y: r.y, width: r.width, height: r.height, top: r.top, right: r.right, bottom: r.bottom, left: r.left }};
  }};
  const text = (el) => (el?.innerText || el?.textContent || "").trim().replace(/\\s+/g, " ");
  const visible = (el) => {{
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && style.visibility !== "hidden" && style.display !== "none";
  }};
  const click = (el) => {{
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const init = {{
      bubbles: true,
      cancelable: true,
      view: window,
      clientX: r.left + r.width / 2,
      clientY: r.top + r.height / 2,
      pointerType: "touch",
    }};
    el.dispatchEvent(new PointerEvent("pointerdown", init));
    el.dispatchEvent(new MouseEvent("mousedown", init));
    el.dispatchEvent(new PointerEvent("pointerup", init));
    el.dispatchEvent(new MouseEvent("mouseup", init));
    el.dispatchEvent(new MouseEvent("click", init));
    return true;
  }};
  const revealOverlays = () => {{
    const target = document.elementFromPoint(innerWidth / 2, innerHeight / 2) || document.body || document.documentElement;
    return {{ ok: click(target), target: target ? {{ tagName: target.tagName, text: text(target).slice(0, 80), rect: rect(target) }} : null }};
  }};
  const snapshot = () => {{
    const widgets = [...document.querySelectorAll('[data-name^="widget-"]')].map((el) => {{
      const match = /^widget-(\\d+)-(.+)$/.exec(el.dataset.name || "");
      return {{
        dataName: el.dataset.name || "",
        index: match ? Number(match[1]) : null,
        name: match ? match[2] : "",
        text: text(el).slice(0, 180),
        rect: rect(el),
        visible: visible(el),
      }};
    }});
    const buttons = [...document.querySelectorAll('button,[role="button"],ol')]
      .filter(visible)
      .map((el) => ({{ text: text(el), dataName: el.dataset.name || "", rect: rect(el) }}))
      .filter((item) => item.text)
      .slice(0, 80);
    const videos = [...document.querySelectorAll("video")].map((video) => ({{
      src: video.currentSrc || video.src || "",
      currentTime: video.currentTime,
      duration: Number.isFinite(video.duration) ? video.duration : null,
      playbackRate: video.playbackRate,
      paused: video.paused,
      rect: rect(video),
    }}));
    const params = Object.fromEntries(new URL(location.href).searchParams.entries());
    return {{
      ok: true,
      bridgeVersion: 1,
      title: document.title,
      href: location.href,
      readyState: document.readyState,
      params,
      viewport: {{ width: innerWidth, height: innerHeight, devicePixelRatio }},
      page: location.pathname.includes("/exercise") ? "exercise" : location.pathname.includes("/course") ? "course" : "xiaoluxue",
      guidePlayerVisible: Boolean(document.querySelector('[data-name="guide-player"]')),
      guideControlsVisible: Boolean(document.querySelector('[data-name="guide-controller::follow"]')),
      widgets,
      buttons,
      videos,
    }};
  }};
  const clickByText = (needle) => {{
    const target = [...document.querySelectorAll('button,[role="button"],ol,li,div,span')]
      .filter(visible)
      .find((el) => text(el).includes(String(needle || "")));
    return {{ ok: click(target), text: target ? text(target) : "", rect: target ? rect(target) : null }};
  }};
  const setPlaybackRate = (rate = 2) => {{
    const videos = [...document.querySelectorAll("video")];
    for (const video of videos) {{
      try {{
        video.defaultPlaybackRate = rate;
        video.playbackRate = rate;
      }} catch (_error) {{}}
    }}
    return {{ ok: true, rate, videos: videos.map((video) => ({{ playbackRate: video.playbackRate, paused: video.paused, currentTime: video.currentTime }})) }};
  }};
  const gotoCourseWidget = (index, mode = "reload") => {{
    const widgetIndex = Number(index);
    if (!Number.isFinite(widgetIndex)) return {{ ok: false, reason: "invalid-index", index }};
    if (mode === "scroll") {{
      const node = document.querySelector(`[data-name^="widget-${{widgetIndex}}-"]`);
      if (!node) return {{ ok: false, reason: "widget-not-found", index: widgetIndex }};
      node.scrollIntoView({{ block: "start", inline: "nearest", behavior: "instant" }});
      return {{ ok: true, mode, index: widgetIndex, href: location.href }};
    }}
    const url = new URL(location.href);
    url.searchParams.set("redirectWidgetIndex", String(widgetIndex));
    location.href = url.toString();
    return {{ ok: true, mode: "reload", index: widgetIndex, href: url.toString() }};
  }};
  window.__androidUse = {{
    ...(window.__androidUse || {{}}),
    xiaoluxue: {{ snapshot, revealOverlays, clickByText, setPlaybackRate, gotoCourseWidget }},
  }};
  const reveal = {reveal_overlay_json} ? revealOverlays() : {{ ok: true, skipped: true }};
  return {{ ok: true, installed: true, reveal, snapshot: snapshot() }};
}})()
"""


def xiaoluxue_runtime_page_for_kind(serial: str, page_kind: str, *, open_app_if_needed: bool = True) -> dict[str, Any]:
    if page_kind == "course":
        return xiaoluxue_page(serial)
    if page_kind == "exercise":
        return xiaoluxue_exercise_page(serial)
    return xiaoluxue_any_page(serial, open_app_if_needed=open_app_if_needed, timeout_sec=3)


def tool_xiaoluxue_runtime_status(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    started_at = time.monotonic()
    page_kind = str(args.get("page") or "any")
    if page_kind not in {"any", "course", "exercise"}:
        raise AndroidUseError("page must be one of: any, course, exercise.")
    page = xiaoluxue_runtime_page_for_kind(serial, page_kind, open_app_if_needed=bool(args.get("open_app_if_needed", True)))
    timings = {"select_page_sec": round(time.monotonic() - started_at, 3)}
    timeout = min(float(args.get("timeout_sec", 4)), 20)
    runtime: Any
    if bool(args.get("inject_bridge", True)):
        runtime = cdp_eval_value(
            page,
            xiaoluxue_runtime_bridge_expression(reveal_overlay=bool(args.get("reveal_overlay", False))),
            timeout=timeout,
        )
    else:
        runtime = cdp_eval_value(
            page,
            "window.__androidUse?.xiaoluxue?.snapshot ? window.__androidUse.xiaoluxue.snapshot() : ({ ok: true, href: location.href, title: document.title, readyState: document.readyState })",
            timeout=timeout,
        )
    timings["runtime_sec"] = round(time.monotonic() - started_at, 3)
    return [
        text_content(
            {
                "ok": True,
                "serial": serial,
                "page": {
                    "id": page.get("id"),
                    "title": page.get("title"),
                    "url": page.get("url"),
                    "runtimeHref": page.get("runtimeHref"),
                    "socket": page.get("socket"),
                    "forward": page.get("forward"),
                    "cacheHit": bool(page.get("cacheHit")),
                },
                "timings": timings,
                "runtime": runtime,
            }
        )
    ]


def tool_xiaoluxue_open_app_url(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    url = str(args["url"]).strip()
    if not url:
        raise AndroidUseError("url must not be empty.")
    if not is_xiaoluxue_app_only_url(url):
        raise AndroidUseError("xiaoluxue_open_app_url only accepts Xiaoluxue app-only URLs.")
    started_at = time.monotonic()
    timeout = min(float(args.get("timeout_sec", 5)), 30)
    before = capture_record_snapshot(serial) if active_recording(serial) else None
    route = xiaoluxue_route_app_url(serial, url, force_stop=bool(args.get("force_stop", False)))
    page: dict[str, Any] | None = None
    runtime: Any = None
    if bool(args.get("wait_for_webview", True)):
        page = xiaoluxue_wait_for_app_url(serial, url, started_at + timeout)
        if bool(args.get("inject_bridge", True)):
            runtime = cdp_eval_value(
                page,
                xiaoluxue_runtime_bridge_expression(reveal_overlay=bool(args.get("reveal_overlay", False))),
                timeout=min(max(started_at + timeout - time.monotonic(), 0.5), 3),
            )
    result = {
        "ok": True,
        "action": "xiaoluxue_open_app_url",
        "elapsed_sec": round(time.monotonic() - started_at, 3),
        "route": route,
        "page": None
        if page is None
        else {
            "id": page.get("id"),
            "title": page.get("title"),
            "url": page.get("url"),
            "runtimeHref": page.get("runtimeHref"),
            "socket": page.get("socket"),
            "forward": page.get("forward"),
            "cacheHit": bool(page.get("cacheHit")),
        },
        "runtime": runtime,
    }
    append_recording_step(
        serial,
        "xiaoluxue_open_app_url",
        {
            "url": url,
            "wait_for_webview": bool(args.get("wait_for_webview", True)),
            "inject_bridge": bool(args.get("inject_bridge", True)),
            "reveal_overlay": bool(args.get("reveal_overlay", False)),
            "force_stop": bool(args.get("force_stop", False)),
        },
        result,
        before=before,
    )
    return [text_content({"ok": True, "serial": serial, **result})]


def xiaoluxue_login_resource_id(name: str) -> str:
    return f"{XIAOLUXUE_STUDENT_PACKAGE}:id/{name}"


def xiaoluxue_login_node_by_resource(nodes: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    resource_id = xiaoluxue_login_resource_id(name)
    for node in nodes:
        if str(node.get("resource_id") or "") == resource_id:
            return node
    return None


def xiaoluxue_login_input_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resource_id = xiaoluxue_login_resource_id("edit_input")
    inputs = [node for node in nodes if str(node.get("resource_id") or "") == resource_id and node.get("center")]
    return sorted(inputs, key=lambda node: int((node.get("bounds") or {}).get("top") or 0))


def node_bounds_contains(outer: dict[str, int] | None, inner: dict[str, int] | None) -> bool:
    if not outer or not inner:
        return False
    return (
        int(outer.get("left", 0)) <= int(inner.get("left", 0))
        and int(outer.get("top", 0)) <= int(inner.get("top", 0))
        and int(outer.get("right", 0)) >= int(inner.get("right", 0))
        and int(outer.get("bottom", 0)) >= int(inner.get("bottom", 0))
    )


def xiaoluxue_login_input_node_for(nodes: list[dict[str, Any]], field: str) -> dict[str, Any] | None:
    container = xiaoluxue_login_node_by_resource(nodes, f"edit_{field}")
    container_bounds = container.get("bounds") if container else None
    candidates = xiaoluxue_login_input_nodes(nodes)
    if container_bounds:
        for node in candidates:
            if node_bounds_contains(container_bounds, node.get("bounds")):
                return node
    index = 0 if field == "account" else 1
    return candidates[index] if len(candidates) > index else None


def xiaoluxue_login_field_nodes(nodes: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    account_node = xiaoluxue_login_input_node_for(nodes, "account")
    password_node = xiaoluxue_login_input_node_for(nodes, "password")
    if not account_node or not password_node:
        raise AndroidUseError("Expected account and password fields on Xiaoluxue login page.")
    return account_node, password_node


def xiaoluxue_login_surface_from_nodes(nodes: list[dict[str, Any]]) -> bool:
    labels = {label for node in nodes for label in node_labels(node)}
    resource_ids = {str(node.get("resource_id") or "") for node in nodes}
    if xiaoluxue_login_resource_id("activity_login_root") in resource_ids:
        return True
    login_prompts = {"请输入账号", "请输入密码", "勾选同意 用户服务协议 与 隐私协议"}
    if labels & login_prompts:
        return True
    return len(xiaoluxue_login_input_nodes(nodes)) >= 2 and "登录" in labels


def xiaoluxue_login_surface_observation(serial: str) -> dict[str, Any] | None:
    with contextlib.suppress(Exception):
        observation = observe_ui(serial, limit=260)
        nodes = observation.get("ui", {}).get("nodes") or []
        if xiaoluxue_login_surface_from_nodes(nodes):
            return observation
    return None


def xiaoluxue_soft_keyboard_visible(serial: str) -> bool:
    with contextlib.suppress(Exception):
        output = shell(serial, "dumpsys input_method", timeout=4)
        window_visible = "mWindowVisible=true" in output or "mDecorViewVisible=true" in output
        if "mInputShown=true" in output:
            return True
        if "mIsInputViewShown=true" in output and window_visible:
            return True
        show_requested = "mShowRequested=true" in output or "mShowInputRequested=true" in output
        fullscreen = any(
            marker in output
            for marker in ("mInFullscreenMode=true", "mFullscreenMode=true", "mIsFullscreen=true")
        )
        return window_visible and show_requested and fullscreen
    return False


def xiaoluxue_hide_soft_keyboard(serial: str) -> bool:
    if not xiaoluxue_soft_keyboard_visible(serial):
        return False
    adb(["shell", "input", "keyevent", "KEYCODE_BACK"], serial=serial, timeout=4)
    time.sleep(0.18)
    return True


def xiaoluxue_is_student_app_focus(focus: str) -> bool:
    return f"{XIAOLUXUE_STUDENT_PACKAGE}/" in focus and "leakcanary." not in focus


def xiaoluxue_wait_for_student_focus(serial: str, timeout_sec: float) -> str:
    deadline = time.monotonic() + max(timeout_sec, 0.0)
    focus = get_focused_window(serial) or ""
    while time.monotonic() < deadline:
        if xiaoluxue_is_student_app_focus(focus):
            return focus
        time.sleep(0.08)
        focus = get_focused_window(serial) or ""
    return focus


def xiaoluxue_synthetic_login_node(
    name: str,
    bounds: tuple[int, int, int, int],
    *,
    text: str = "",
    checked: bool = False,
) -> dict[str, Any]:
    parsed = {"left": bounds[0], "top": bounds[1], "right": bounds[2], "bottom": bounds[3]}
    return {
        "text": text,
        "content_desc": "",
        "resource_id": xiaoluxue_login_resource_id(name),
        "class": "android.view.View",
        "package": XIAOLUXUE_STUDENT_PACKAGE,
        "bounds": parsed,
        "center": bounds_center(parsed),
        "clickable": True,
        "enabled": True,
        "checkable": name == "cb_agreement",
        "checked": checked,
        "synthetic": True,
    }


def xiaoluxue_synthetic_login_observation(serial: str, focus: str, error: Exception) -> dict[str, Any]:
    size = {"width": 2000, "height": 1200}
    with contextlib.suppress(Exception):
        shot_size = png_size(screenshot_png(serial))
        if shot_size.get("width") and shot_size.get("height"):
            size = {"width": int(shot_size["width"]), "height": int(shot_size["height"])}
    width = int(size["width"])
    height = int(size["height"])

    def box(left: float, top: float, right: float, bottom: float) -> tuple[int, int, int, int]:
        return (round(width * left), round(height * top), round(width * right), round(height * bottom))

    nodes = [
        xiaoluxue_synthetic_login_node("activity_login_root", (0, 0, width, height)),
        xiaoluxue_synthetic_login_node("edit_account", box(0.32, 0.337, 0.68, 0.443), text=""),
        xiaoluxue_synthetic_login_node("edit_input", box(0.344, 0.337, 0.656, 0.443), text=""),
        xiaoluxue_synthetic_login_node("edit_password", box(0.32, 0.444, 0.68, 0.551), text=""),
        xiaoluxue_synthetic_login_node("edit_input", box(0.344, 0.444, 0.64, 0.551), text=""),
        xiaoluxue_synthetic_login_node("cb_agreement", box(0.397, 0.738, 0.411, 0.767), checked=False),
        xiaoluxue_synthetic_login_node("button", box(0.32, 0.591, 0.68, 0.684), text="登录"),
    ]
    return {
        "state": {"serial": serial, "focused_window": focus, "screen": size},
        "ui": {"nodes": nodes, "count": len(nodes), "synthetic": True, "fallback_error": str(error)},
    }


def xiaoluxue_login_click_node(serial: str, node: dict[str, Any] | None, description: str) -> dict[str, int]:
    point = node_click_point(node) if node else None
    if not point:
        raise AndroidUseError(f"Could not find Xiaoluxue login control: {description}")
    adb(["shell", "input", "tap", str(point["x"]), str(point["y"])], serial=serial, timeout=10)
    return point


def xiaoluxue_type_login_text(
    serial: str,
    text: str,
    *,
    clear_count: int,
) -> str:
    if not text_needs_unicode_input(text) and "\n" not in text:
        adb_shell_batch_type_text(serial, text, clear_first=True, clear_count=clear_count)
        time.sleep(0.25)
        return "adb_shell_batch_login"
    method = type_focused_text_fast(serial, text, clear_first=True, clear_count=clear_count)
    time.sleep(0.35)
    return method


def xiaoluxue_login_observe(serial: str) -> dict[str, Any]:
    try:
        observation = observe_ui(serial, limit=260)
    except AndroidUseError as exc:
        focus = get_focused_window(serial) or ""
        if XIAOLUXUE_LOGIN_ACTIVITY in focus:
            return xiaoluxue_synthetic_login_observation(serial, focus, exc)
        raise
    focus = str(observation.get("state", {}).get("focused_window") or "")
    nodes = observation.get("ui", {}).get("nodes") or []
    if XIAOLUXUE_LOGIN_ACTIVITY not in focus and not xiaoluxue_login_surface_from_nodes(nodes):
        raise AndroidUseError(f"Current Xiaoluxue screen is not LoginActivity. focused_window={focus}")
    return observation


def xiaoluxue_wait_for_login_fields(
    serial: str,
    *,
    account: str,
    password_required: bool,
    timeout_sec: float = 2.5,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + max(timeout_sec, 0.0)
    last: tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any]] | None = None
    while True:
        observation = xiaoluxue_login_observe(serial)
        nodes = observation["ui"]["nodes"]
        account_node, password_node = xiaoluxue_login_field_nodes(nodes)
        last = (observation, nodes, account_node, password_node)
        synthetic = bool(observation.get("ui", {}).get("synthetic"))
        account_ok = str(account_node.get("text") or "").strip() == account
        password_ok = bool(str(password_node.get("text") or "")) if password_required else True
        if account_ok and password_ok and not synthetic:
            return last
        if time.monotonic() >= deadline:
            return last
        time.sleep(0.1)


def xiaoluxue_clear_login_logcat(serial: str) -> None:
    with contextlib.suppress(Exception):
        adb(["logcat", "-c"], serial=serial, timeout=5)


def xiaoluxue_login_failure_from_logcat(serial: str) -> dict[str, Any] | None:
    with contextlib.suppress(Exception):
        raw = decode_bytes(adb(["logcat", "-d", "-v", "time"], serial=serial, timeout=8))
        for line in reversed(raw.splitlines()):
            if "login failed" in line:
                match = re.search(r"code:\s*([^,]+),\s*message:\s*(.+)$", line)
                if match:
                    return {
                        "code": match.group(1).strip(),
                        "message": match.group(2).strip(),
                        "source": "LoginActivity",
                    }
            if "/access/login" in line and '"message"' in line:
                match = re.search(r'"code"\s*:\s*([^,}]+).*?"message"\s*:\s*"([^"]+)"', line)
                if match:
                    return {
                        "code": match.group(1).strip(),
                        "message": match.group(2).strip(),
                        "source": "login_api",
                    }
    return None


def run_xiaoluxue_login_fast_path(serial: str, args: dict[str, Any], *, record: bool) -> dict[str, Any]:
    account = str(args.get("account") or "").strip()
    password = str(args.get("password") or "")
    if not account:
        raise AndroidUseError("account is required.")
    if not password:
        raise AndroidUseError("password is required.")

    started_at = time.monotonic()
    timeout = min(float(args.get("timeout_sec", 5.0)), 30)
    deadline = started_at + timeout
    steps: list[dict[str, Any]] = []

    focus = get_focused_window(serial) or ""
    if XIAOLUXUE_LOGIN_ACTIVITY not in focus:
        if bool(args.get("open_app_if_needed", True)):
            adb(["shell", "am", "start", "-n", XIAOLUXUE_STUDENT_LAUNCHER_COMPONENT], serial=serial, timeout=10)
            wait_sec = min(max(float(args.get("after_open_wait_sec", 1.5)), 0), 4)
            focus = xiaoluxue_wait_for_student_focus(serial, wait_sec)
        if XIAOLUXUE_LOGIN_ACTIVITY not in focus:
            already_in_student = xiaoluxue_is_student_app_focus(focus)
            if already_in_student and xiaoluxue_login_surface_observation(serial):
                already_in_student = False
            if not already_in_student:
                raise AndroidUseError(f"Current Xiaoluxue screen is not LoginActivity. focused_window={focus}")
            return {
                "ok": True,
                "action": "xiaoluxue_login_fast_path",
                "already_logged_in": already_in_student,
                "focused_window": focus,
                "elapsed_sec": round(time.monotonic() - started_at, 3),
                "steps": steps,
            }

    if xiaoluxue_hide_soft_keyboard(serial):
        steps.append({"action": "hide_keyboard", "phase": "before_observe"})
    observation = xiaoluxue_login_observe(serial)
    nodes = observation["ui"]["nodes"]
    account_node, password_node = xiaoluxue_login_field_nodes(nodes)
    current_account = str(account_node.get("text") or "").strip()
    if current_account != account:
        if xiaoluxue_hide_soft_keyboard(serial):
            steps.append({"action": "hide_keyboard", "phase": "before_account"})
        point = xiaoluxue_login_click_node(serial, account_node, "account input")
        method = xiaoluxue_type_login_text(serial, account, clear_count=40)
        steps.append({"action": "fill_account", "chars": len(account), "method": method, "point": point})
        if xiaoluxue_hide_soft_keyboard(serial):
            steps.append({"action": "hide_keyboard", "phase": "after_account"})
        observation, nodes, account_node, _password_node = xiaoluxue_wait_for_login_fields(
            serial,
            account=account,
            password_required=False,
        )
        if str(account_node.get("text") or "").strip() != account:
            raise AndroidUseError("Xiaoluxue account field did not keep the requested account after input.")
    else:
        steps.append({"action": "fill_account", "skipped": "already-filled", "chars": len(account)})

    _account_node, password_node = xiaoluxue_login_field_nodes(nodes)
    if xiaoluxue_hide_soft_keyboard(serial):
        steps.append({"action": "hide_keyboard", "phase": "before_password"})
    point = xiaoluxue_login_click_node(serial, password_node, "password input")
    method = xiaoluxue_type_login_text(serial, password, clear_count=max(20, len(password) + 8))
    steps.append({"action": "fill_password", "chars": len(password), "method": method, "point": point})
    if xiaoluxue_hide_soft_keyboard(serial):
        steps.append({"action": "hide_keyboard", "phase": "after_password"})

    observation, nodes, account_node, password_node = xiaoluxue_wait_for_login_fields(
        serial,
        account=account,
        password_required=True,
    )
    if str(account_node.get("text") or "").strip() != account:
        raise AndroidUseError("Xiaoluxue account field changed while filling password; refusing to submit.")
    if not str(password_node.get("text") or ""):
        raise AndroidUseError("Xiaoluxue password field is empty after input; refusing to submit.")
    agreement_node = xiaoluxue_login_node_by_resource(nodes, "cb_agreement")
    if agreement_node and not bool(agreement_node.get("checked")):
        point = xiaoluxue_login_click_node(serial, agreement_node, "agreement checkbox")
        steps.append({"action": "check_agreement", "point": point})
        observation = xiaoluxue_login_observe(serial)
        nodes = observation["ui"]["nodes"]
    else:
        steps.append({"action": "check_agreement", "skipped": "already-checked" if agreement_node else "not-found"})

    login_node = xiaoluxue_login_node_by_resource(nodes, "button") or find_ui_node(nodes, "登录", exact=True)
    xiaoluxue_clear_login_logcat(serial)
    point = xiaoluxue_login_click_node(serial, login_node, "login button")
    submitted_at = time.monotonic()
    steps.append({"action": "submit", "point": point})

    final_focus = ""
    last_failure_check = 0.0
    while time.monotonic() < deadline:
        final_focus = get_focused_window(serial) or ""
        if xiaoluxue_is_student_app_focus(final_focus) and XIAOLUXUE_LOGIN_ACTIVITY not in final_focus:
            login_surface = xiaoluxue_login_surface_observation(serial)
            if login_surface:
                nodes = login_surface.get("ui", {}).get("nodes") or []
                labels = visible_labels(nodes, limit=8)
                steps.append({"action": "still_on_login_surface", "focused_window": final_focus, "labels": labels})
                final_focus = str(login_surface.get("state", {}).get("focused_window") or final_focus)
                time.sleep(0.1)
                continue
            result = {
                "ok": True,
                "action": "xiaoluxue_login_fast_path",
                "already_logged_in": False,
                "focused_window": final_focus,
                "elapsed_sec": round(time.monotonic() - started_at, 3),
                "submit_to_home_sec": round(time.monotonic() - submitted_at, 3),
                "steps": steps,
            }
            if record:
                append_recording_step(
                    serial,
                    "xiaoluxue_login_fast_path",
                    {
                        "account_chars": len(account),
                        "password_chars": len(password),
                        "password_redacted": True,
                        "timeout_sec": timeout,
                    },
                    result,
                )
            return result
        now = time.monotonic()
        if XIAOLUXUE_LOGIN_ACTIVITY in final_focus and now - submitted_at >= 0.25 and now - last_failure_check >= 0.4:
            last_failure_check = now
            failure = xiaoluxue_login_failure_from_logcat(serial)
            if failure:
                raise AndroidUseError(
                    "Xiaoluxue login failed: "
                    f"{failure.get('message') or 'unknown error'}"
                    f" (code={failure.get('code') or 'unknown'}, source={failure.get('source')})"
                )
        time.sleep(0.1)
    failure = xiaoluxue_login_failure_from_logcat(serial)
    if failure:
        raise AndroidUseError(
            "Xiaoluxue login failed: "
            f"{failure.get('message') or 'unknown error'}"
            f" (code={failure.get('code') or 'unknown'}, source={failure.get('source')})"
        )
    raise AndroidUseError(
        f"Xiaoluxue login did not reach home within {timeout:.1f}s. focused_window={final_focus or focus}"
    )


def tool_xiaoluxue_login_fast_path(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    result = run_xiaoluxue_login_fast_path(serial, args, record=True)
    return [text_content({"ok": True, "serial": serial, **result})]


def tool_xiaoluxue_course_snapshot(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    page = xiaoluxue_page(serial)
    snapshot = cdp_eval_value(page, xiaoluxue_snapshot_expression(), timeout=min(float(args.get("timeout_sec", 10)), 60))
    return [text_content({"ok": True, "serial": serial, "pageId": page.get("id"), "socket": page.get("socket"), "snapshot": snapshot})]


def tool_xiaoluxue_set_speed(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    rate = float(args.get("rate", 2.0))
    if rate <= 0 or rate > 4:
        raise AndroidUseError("rate must be between 0 and 4.")
    page = xiaoluxue_page(serial)
    result = cdp_eval_value(page, xiaoluxue_set_speed_expression(rate), timeout=min(float(args.get("timeout_sec", 10)), 60))
    append_recording_step(
        serial,
        "xiaoluxue_set_speed",
        {"rate": rate},
        {"ok": True, "action": "xiaoluxue_set_speed", "rate": rate, "webview": True, "result": result},
    )
    return [text_content({"ok": True, "serial": serial, "pageId": page.get("id"), "socket": page.get("socket"), "result": result})]


def resolve_xiaoluxue_widget_index(snapshot: dict[str, Any], args: dict[str, Any]) -> int:
    if args.get("last"):
        indexes = [int(widget["index"]) for widget in snapshot.get("widgets", []) if isinstance(widget, dict) and widget.get("index") is not None]
        if not indexes:
            raise AndroidUseError("No widgets found on current Xiaoluxue course page.")
        return max(indexes)
    if args.get("index") is not None:
        return int(args["index"])
    name_contains = str(args.get("name_contains") or "").strip()
    if name_contains:
        for widget in snapshot.get("widgets", []):
            if isinstance(widget, dict) and name_contains in str(widget.get("name", "")):
                return int(widget["index"])
        raise AndroidUseError(f"No widget name contains: {name_contains}")
    raise AndroidUseError("Pass one of index, name_contains, or last=true.")


def tool_xiaoluxue_goto_widget(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    page = xiaoluxue_page(serial)
    snapshot = cdp_eval_value(page, xiaoluxue_snapshot_expression(), timeout=min(float(args.get("timeout_sec", 10)), 60))
    if not isinstance(snapshot, dict):
        raise AndroidUseError("Could not read Xiaoluxue course snapshot.")
    index = resolve_xiaoluxue_widget_index(snapshot, args)
    mode = str(args.get("mode") or "reload")
    if mode not in {"reload", "scroll"}:
        raise AndroidUseError("mode must be 'reload' or 'scroll'.")
    result = cdp_eval_value(page, xiaoluxue_goto_widget_expression(index, mode), timeout=min(float(args.get("timeout_sec", 10)), 60))
    append_recording_step(
        serial,
        "xiaoluxue_goto_widget",
        {"index": index, "mode": mode},
        {"ok": True, "action": "xiaoluxue_goto_widget", "index": index, "mode": mode, "webview": True, "result": result},
    )
    return [text_content({"ok": True, "serial": serial, "pageId": page.get("id"), "socket": page.get("socket"), "index": index, "mode": mode, "result": result})]


def first_xiaoluxue_widget_index(snapshot: dict[str, Any], terms: list[str]) -> int | None:
    for term in terms:
        for widget in snapshot.get("widgets", []):
            if isinstance(widget, dict) and term in str(widget.get("name", "")):
                return int(widget["index"])
    return None


def run_xiaoluxue_course_fast_path(serial: str, args: dict[str, Any], *, record: bool) -> dict[str, Any]:
    timeout = min(float(args.get("timeout_sec", 15)), 60)
    wait_sec = min(max(float(args.get("after_navigation_wait_sec", 2.0)), 0), 10)
    set_speed = bool(args.get("set_speed", True))
    rate = float(args.get("rate", 2.0))
    if rate <= 0 or rate > 4:
        raise AndroidUseError("rate must be between 0 and 4.")

    page = xiaoluxue_page(serial)
    snapshot = cdp_eval_value(page, xiaoluxue_snapshot_expression(), timeout=timeout)
    if not isinstance(snapshot, dict):
        raise AndroidUseError("Could not read Xiaoluxue course snapshot.")

    steps: list[dict[str, Any]] = []
    guide_index: int | None = None
    if args.get("guide_index") is not None:
        guide_index = int(args["guide_index"])
    elif str(args.get("guide_name_contains") or "").strip():
        guide_index = resolve_xiaoluxue_widget_index(snapshot, {"name_contains": str(args["guide_name_contains"]).strip()})
    elif set_speed and not snapshot.get("guidePlayerVisible"):
        guide_index = first_xiaoluxue_widget_index(snapshot, ["知识讲解", "讲解"])

    if guide_index is not None:
        guide_mode = str(args.get("guide_mode") or "reload")
        if guide_mode not in {"reload", "scroll"}:
            raise AndroidUseError("guide_mode must be 'reload' or 'scroll'.")
        guide_result = cdp_eval_value(page, xiaoluxue_goto_widget_expression(guide_index, guide_mode), timeout=timeout)
        steps.append({"action": "goto_guide", "index": guide_index, "mode": guide_mode, "result": guide_result})
        if guide_mode == "reload" and wait_sec:
            time.sleep(wait_sec)
            page = xiaoluxue_page(serial)

    if set_speed:
        speed_result = cdp_eval_value(page, xiaoluxue_set_speed_expression(rate), timeout=timeout)
        steps.append({"action": "set_speed", "rate": rate, "result": speed_result})

    target_args: dict[str, Any] = {}
    if args.get("target_index") is not None:
        target_args["index"] = int(args["target_index"])
    elif str(args.get("target_name_contains") or "").strip():
        target_args["name_contains"] = str(args["target_name_contains"]).strip()
    elif bool(args.get("target_last", True)):
        target_args["last"] = True

    if target_args:
        page = xiaoluxue_page(serial)
        snapshot = cdp_eval_value(page, xiaoluxue_snapshot_expression(), timeout=timeout)
        if not isinstance(snapshot, dict):
            raise AndroidUseError("Could not read Xiaoluxue course snapshot before target jump.")
        target_index = resolve_xiaoluxue_widget_index(snapshot, target_args)
        target_mode = str(args.get("target_mode") or "reload")
        if target_mode not in {"reload", "scroll"}:
            raise AndroidUseError("target_mode must be 'reload' or 'scroll'.")
        target_result = cdp_eval_value(page, xiaoluxue_goto_widget_expression(target_index, target_mode), timeout=timeout)
        steps.append({"action": "goto_target", "index": target_index, "mode": target_mode, "result": target_result})

    result = {
        "ok": True,
        "action": "xiaoluxue_course_fast_path",
        "webview": True,
        "pageId": page.get("id"),
        "socket": page.get("socket"),
        "steps": steps,
    }
    if record:
        recorded_args = {
            key: args[key]
            for key in (
                "guide_index",
                "guide_name_contains",
                "guide_mode",
                "set_speed",
                "rate",
                "target_index",
                "target_name_contains",
                "target_last",
                "target_mode",
                "after_navigation_wait_sec",
                "timeout_sec",
            )
            if key in args
        }
        append_recording_step(serial, "xiaoluxue_course_fast_path", recorded_args, result)
    return result


def tool_xiaoluxue_course_fast_path(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    result = run_xiaoluxue_course_fast_path(serial, args, record=True)
    return [text_content({"ok": True, "serial": serial, **result})]


def run_xiaoluxue_open_knowledge_guide(serial: str, args: dict[str, Any], *, record: bool) -> dict[str, Any]:
    started_at = time.monotonic()
    timeout = min(float(args.get("timeout_sec", 8)), 60)
    deadline = started_at + timeout
    subject_id = int(args.get("subject_id", 2))
    knowledge_index = str(args.get("knowledge_index") or "1.1.11")
    normalized_index = normalize_xiaoluxue_knowledge_index(knowledge_index)
    knowledge_id = args.get("knowledge_id")
    resolved_knowledge_id: int | None = int(knowledge_id) if knowledge_id is not None else None
    shortcut: dict[str, Any] | None = None
    if resolved_knowledge_id is None:
        shortcut = XIAOLUXUE_KNOWLEDGE_SHORTCUTS.get((subject_id, normalized_index))
        if shortcut:
            resolved_knowledge_id = int(shortcut["knowledgeId"])
    guide_widget_index = args.get("guide_widget_index")
    resolved_guide_index = int(guide_widget_index) if guide_widget_index is not None else None
    rate = float(args.get("rate", 2.0))
    if rate <= 0 or rate > 4:
        raise AndroidUseError("rate must be between 0 and 4.")
    prefer_client_route = bool(args.get("prefer_client_route", True))
    native_entry_if_needed = bool(args.get("native_entry_if_needed", True))

    timings: dict[str, float] = {}
    native_entry_result: Any = {"attempted": False, "reason": "webview-already-available"}
    vessel_entry_result: Any = {"attempted": False, "reason": "webview-already-available"}
    page: dict[str, Any] | None = None
    try:
        page = xiaoluxue_any_page(serial, open_app_if_needed=False, timeout_sec=0.2)
    except Exception as first_page_error:
        can_open_app = bool(args.get("open_app_if_needed", True))
        shortcut_target_url = str(shortcut.get("targetUrl") or "") if shortcut else ""
        refresh_session_for_entry = bool(args.get("refresh_session", False))
        prefer_native_entry_first = bool(
            args.get(
                "prefer_native_entry_first",
                not bool(shortcut_target_url and not refresh_session_for_entry),
            )
        )
        fallback_native_after_vessel = bool(args.get("fallback_native_after_vessel", prefer_native_entry_first))
        entry_order = (
            ("native", "vessel")
            if prefer_native_entry_first
            else ("vessel", "native")
            if fallback_native_after_vessel
            else ("vessel",)
        )
        for entry_mode in entry_order:
            if page is not None:
                break
            if entry_mode == "native":
                if not (can_open_app and native_entry_if_needed):
                    continue
                try:
                    native_entry_timeout = min(max(float(args.get("native_entry_timeout_sec", 2.6)), 1.0), 5.0)
                    native_entry_result = xiaoluxue_open_native_course_entry(
                        serial,
                        subject_id=subject_id,
                        normalized_index=normalized_index,
                        timeout_sec=min(max(deadline - time.monotonic(), 1), native_entry_timeout),
                    )
                    page = native_entry_result["page"]
                except Exception as native_exc:
                    native_entry_result = {"attempted": True, "ok": False, "error": str(native_exc)}
            elif entry_mode == "vessel":
                if not (can_open_app and shortcut_target_url and not bool(args.get("refresh_session", False))):
                    continue
                try:
                    vessel_entry_timeout = min(max(float(args.get("vessel_entry_timeout_sec", 4.8)), 0.8), 8.0)
                    vessel_entry_result = xiaoluxue_open_vessel_course_page(
                        serial,
                        target_url=shortcut_target_url,
                        knowledge_id=int(shortcut["knowledgeId"]),
                        timeout_sec=min(max(deadline - time.monotonic(), 0.5), vessel_entry_timeout),
                        force_stop=bool(args.get("force_vessel_start", True)),
                        bootstrap_scripts=(
                            [
                                xiaoluxue_fast_rate_expression(
                                    rate,
                                    int(shortcut.get("guideIndex", 0)),
                                    activate_player=True,
                                ),
                                xiaoluxue_turbo_guide_player_expression(
                                    guide_index=int(shortcut.get("guideIndex", 0)),
                                    guide_name=str(shortcut.get("guideName") or "") or None,
                                    avatar_url=str(shortcut.get("avatarUrl") or "") or None,
                                    rate=rate,
                                    enabled=bool(args.get("turbo_preview", True)),
                                ),
                                xiaoluxue_prefetch_guide_expression(
                                    str(shortcut.get("guideCdnUrl") or "") or None,
                                    str(shortcut.get("avatarUrl") or "") or None,
                                ),
                            ]
                            if bool(args.get("preinject_vessel_bootstrap", False))
                            else None
                        ),
                    )
                    page = vessel_entry_result["page"]
                except Exception as vessel_exc:
                    vessel_entry_result = {"attempted": True, "ok": False, "error": str(vessel_exc)}
        if page is None and can_open_app and native_entry_if_needed:
            try:
                page = xiaoluxue_any_page(
                    serial,
                    open_app_if_needed=can_open_app,
                    timeout_sec=min(max(deadline - time.monotonic(), 0.5), 2),
                )
            except Exception:
                if isinstance(native_entry_result, dict) and native_entry_result.get("attempted"):
                    raise AndroidUseError(str(native_entry_result.get("error") or first_page_error))
                if isinstance(vessel_entry_result, dict) and vessel_entry_result.get("attempted"):
                    raise AndroidUseError(str(vessel_entry_result.get("error") or first_page_error))
                raise
        elif page is None:
            raise first_page_error
    if page is None:
        raise AndroidUseError("Could not open Xiaoluxue knowledge guide page.")
    timings["select_page_sec"] = round(time.monotonic() - started_at, 3)
    entry_page_url = str(page.get("runtimeHref") or page.get("url") or "")
    entry_opened_target_course = bool(
        (
            isinstance(native_entry_result, dict)
            and native_entry_result.get("ok")
            or isinstance(vessel_entry_result, dict)
            and vessel_entry_result.get("ok")
        )
        and resolved_knowledge_id is not None
        and xiaoluxue_url_kind(entry_page_url) == "course"
        and f"knowledgeId={resolved_knowledge_id}" in entry_page_url
    )
    stop_loading_result: Any = None
    if entry_opened_target_course:
        stop_loading_result = {
            "ok": True,
            "skipped": "entry-opened-target-course",
            "before": {"readyState": "complete"},
            "after": {"readyState": "complete"},
        }
    else:
        try:
            stop_loading_result = cdp_eval_value(page, xiaoluxue_stop_non_course_loading_expression(), timeout=0.6)
        except Exception as exc:
            stop_loading_result = {"ok": False, "error": str(exc)}
    stop_after = stop_loading_result.get("after") if isinstance(stop_loading_result, dict) and isinstance(stop_loading_result.get("after"), dict) else {}
    stop_before = stop_loading_result.get("before") if isinstance(stop_loading_result, dict) and isinstance(stop_loading_result.get("before"), dict) else {}
    runtime_route_safe = bool(stop_loading_result.get("ok")) and stop_after.get("readyState") != "loading"
    page_was_loading = stop_before.get("readyState") == "loading" or not stop_loading_result.get("ok")

    use_shortcut_url = bool(args.get("use_shortcut_url", True))
    refresh_session = bool(args.get("refresh_session", False))
    if shortcut and shortcut.get("targetUrl") and use_shortcut_url and not refresh_session and knowledge_id is None and guide_widget_index is None:
        resolved = {
            "ok": True,
            "source": "shortcut-url",
            "subjectId": subject_id,
            "requestedIndex": knowledge_index,
            "knowledgeId": int(shortcut["knowledgeId"]),
            "knowledgeIndex": shortcut.get("knowledgeIndex"),
            "knowledgeName": shortcut.get("knowledgeName"),
            "lessonId": shortcut.get("lessonId"),
            "studySessionId": shortcut.get("studySessionId"),
            "guideIndex": int(shortcut.get("guideIndex", 0)),
            "guideName": shortcut.get("guideName", ""),
            "guideCdnUrl": shortcut.get("guideCdnUrl", ""),
            "avatarUrl": shortcut.get("avatarUrl", ""),
            "targetUrl": shortcut["targetUrl"],
        }
    else:
        remaining = max(deadline - time.monotonic(), 1)
        resolved = cdp_eval_value(
            page,
            xiaoluxue_resolve_knowledge_guide_expression(
                subject_id=subject_id,
                knowledge_index=knowledge_index,
                knowledge_id=resolved_knowledge_id,
                guide_widget_index=resolved_guide_index,
            ),
            timeout=min(remaining, 15),
        )
    if not isinstance(resolved, dict) or not resolved.get("targetUrl"):
        raise AndroidUseError("Could not resolve Xiaoluxue knowledge guide target.")
    current_h5_href = str(page.get("runtimeHref") or page.get("url") or "")
    if not entry_opened_target_course:
        try:
            runtime_href = cdp_eval_value(page, "location.href", timeout=0.3)
            if isinstance(runtime_href, str):
                current_h5_href = runtime_href
        except Exception:
            pass
    if bool(args.get("respect_current_h5_host", True)):
        rebased_target_url = xiaoluxue_rebase_h5_url(str(resolved["targetUrl"]), current_h5_href)
        if rebased_target_url != str(resolved["targetUrl"]):
            resolved = {**resolved, "originalTargetUrl": resolved["targetUrl"], "targetUrl": rebased_target_url}
    timings["resolve_sec"] = round(time.monotonic() - started_at, 3)
    guide_index = int(resolved.get("guideIndex") or 0)
    knowledge_id_int = int(resolved["knowledgeId"])
    expected_guide_name = str(resolved.get("guideName") or "").strip()
    current_page_url = current_h5_href or str(page.get("url") or "")
    current_page_kind = xiaoluxue_url_kind(current_page_url)
    effective_prefer_client_route = bool(prefer_client_route and current_page_kind == "course")
    native_course_already_open = bool(
        (
            isinstance(native_entry_result, dict)
            and native_entry_result.get("ok")
            or isinstance(vessel_entry_result, dict)
            and vessel_entry_result.get("ok")
        )
        and xiaoluxue_url_kind(current_page_url) == "course"
        and f"knowledgeId={knowledge_id_int}" in current_page_url
        and not bool(args.get("force_route_after_native", False))
    )

    rate_bootstrap_script = xiaoluxue_fast_rate_expression(rate, guide_index, activate_player=True)
    rate_bootstrap_result: Any = {"ok": True, "skipped": "current-document-client-route"}
    rate_prepare_result: Any = None
    if entry_opened_target_course:
        rate_prepare_result = {"ok": True, "skipped": "entry-opened-target-course", "rate": rate}
    elif runtime_route_safe:
        try:
            rate_prepare_result = cdp_eval_value(page, rate_bootstrap_script, timeout=1)
        except Exception as exc:
            rate_prepare_result = {"ok": False, "error": str(exc)}
    else:
        rate_prepare_result = {"ok": True, "skipped": "runtime-loading-page", "rate": rate}
    prefetch_result: Any = None
    if entry_opened_target_course:
        prefetch_result = {"ok": True, "skipped": "entry-opened-target-course"}
    else:
        try:
            prefetch_result = cdp_eval_value(
                page,
                xiaoluxue_prefetch_guide_expression(
                    str(resolved.get("guideCdnUrl") or "") or None,
                    str(resolved.get("avatarUrl") or "") or None,
                ),
                timeout=0.8,
            )
        except Exception as exc:
            prefetch_result = {"ok": False, "error": str(exc)}

    optimize_neighbor_guides = bool(args.get("optimize_neighbor_guides", False))
    fetch_optimizer_result: Any = {"ok": True, "skipped": "disabled"}
    if optimize_neighbor_guides:
        try:
            fetch_optimizer_result = cdp_eval_value(
                page,
                xiaoluxue_course_fetch_optimizer_expression(str(resolved.get("guideCdnUrl") or "") or None),
                timeout=0.4,
            )
        except Exception as exc:
            fetch_optimizer_result = {"ok": False, "error": str(exc)}

    turbo_preview = bool(args.get("turbo_preview", True))
    turbo_bootstrap_script = xiaoluxue_turbo_guide_player_expression(
        guide_index=guide_index,
        guide_name=str(resolved.get("guideName") or "") or None,
        avatar_url=str(resolved.get("avatarUrl") or "") or None,
        rate=rate,
        enabled=turbo_preview,
    )
    turbo_result: Any = None
    try:
        turbo_timeout = 0.25 if entry_opened_target_course else 0.6
        turbo_result = cdp_eval_value(page, turbo_bootstrap_script, timeout=turbo_timeout)
    except Exception as exc:
        turbo_result = {"ok": False, "error": str(exc)}

    needs_new_document_bootstrap = not native_course_already_open and (
        page_was_loading or not (runtime_route_safe and effective_prefer_client_route)
    )
    if needs_new_document_bootstrap:
        try:
            cdp_call(str(page["webSocketDebuggerUrl"]), "Page.enable", {}, timeout=1)
            for script_id in range(1, 41):
                try:
                    cdp_call(
                        str(page["webSocketDebuggerUrl"]),
                        "Page.removeScriptToEvaluateOnNewDocument",
                        {"identifier": str(script_id)},
                        timeout=0.2,
                    )
                except Exception:
                    pass
            bootstrap_scripts = [
                ("rate", rate_bootstrap_script),
                ("turbo", turbo_bootstrap_script),
                (
                    "prefetch",
                    xiaoluxue_prefetch_guide_expression(
                        str(resolved.get("guideCdnUrl") or "") or None,
                        str(resolved.get("avatarUrl") or "") or None,
                    ),
                ),
            ]
            installed_scripts: list[str] = []
            for script_name, script_source in bootstrap_scripts:
                cdp_call(
                    str(page["webSocketDebuggerUrl"]),
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": script_source},
                    timeout=1,
                )
                installed_scripts.append(script_name)
            rate_bootstrap_result = {"ok": True, "scripts": installed_scripts}
        except Exception as exc:
            rate_bootstrap_result = {"ok": False, "error": str(exc)}

    if native_course_already_open:
        route_result = {
            "ok": True,
            "mode": "direct-course-existing-page",
            "reason": "native-or-vessel-entry-opened-target-course",
            "href": current_page_url,
        }
    elif page_was_loading or not effective_prefer_client_route:
        try:
            navigate_timeout = 1 if page_was_loading else 0.45
            route_result = cdp_call(
                str(page["webSocketDebuggerUrl"]),
                "Page.navigate",
                {"url": str(resolved["targetUrl"])},
                timeout=navigate_timeout,
            )
            route_result = {
                "ok": True,
                "mode": "page-navigate",
                "cdp": route_result,
                "reason": "loading-page" if page_was_loading else "cross-page-or-non-course-source",
            }
        except Exception as exc:
            route_result = {"ok": False, "mode": "page-navigate", "error": str(exc)}
    else:
        try:
            route_result = cdp_eval_value(
                page,
                xiaoluxue_route_course_expression(str(resolved["targetUrl"]), prefer_client_route=effective_prefer_client_route),
                timeout=2,
            )
        except Exception as exc:
            try:
                route_result = cdp_call(
                    str(page["webSocketDebuggerUrl"]),
                    "Page.navigate",
                    {"url": str(resolved["targetUrl"])},
                    timeout=1,
                )
                route_result = {"ok": True, "mode": "page-navigate", "cdp": route_result, "error_before_fallback": str(exc)}
            except Exception as navigate_exc:
                route_result = {"ok": False, "error": str(exc), "navigate_error": str(navigate_exc)}
    timings["route_sec"] = round(time.monotonic() - started_at, 3)

    ready: dict[str, Any] | None = None
    last_error: str | None = None
    fallback_reload_result: Any = None
    turbo_refreshed_after_route = False
    turbo_video_available_before_ready = bool(isinstance(turbo_result, dict) and turbo_result.get("video"))
    if entry_opened_target_course:
        if not turbo_video_available_before_ready and turbo_preview:
            try:
                turbo_result = cdp_eval_value(page, turbo_bootstrap_script, timeout=0.25)
            except Exception as exc:
                turbo_result = {"ok": False, "error": str(exc)}
            turbo_video_available_before_ready = bool(isinstance(turbo_result, dict) and turbo_result.get("video"))
        ready = {
            "ok": True,
            "href": current_page_url,
            "readyState": "complete",
            "params": {"knowledgeId": str(knowledge_id_int)},
            "guideIndex": guide_index,
            "targetWidget": None,
            "visibleWidget": None,
            "guidePlayerVisible": False,
            "videos": (
                [{"playbackRate": rate, "paused": False, "currentTime": None, "source": "turbo-video"}]
                if turbo_video_available_before_ready
                else []
            ),
            "skipped": "entry-opened-target-course",
        }
    while ready is None and time.monotonic() < deadline:
        try:
            value = cdp_eval_value(page, xiaoluxue_course_ready_expression(knowledge_id_int, guide_index), timeout=1.5)
            if isinstance(value, dict):
                ready = value
                target_widget = value.get("targetWidget") if isinstance(value.get("targetWidget"), dict) else {}
                visible_widget = value.get("visibleWidget") if isinstance(value.get("visibleWidget"), dict) else {}
                target_loaded = bool(target_widget.get("loaded"))
                current_loaded = bool(visible_widget and str(visible_widget.get("text") or "").strip())
                visible_text = str(visible_widget.get("text") or "")
                target_name = str(target_widget.get("dataName") or "")
                content_matches = not expected_guide_name or expected_guide_name in target_name or expected_guide_name in visible_text
                if (
                    turbo_preview
                    and not turbo_refreshed_after_route
                    and value.get("ok")
                    and value.get("readyState") != "loading"
                ):
                    try:
                        turbo_result = cdp_eval_value(page, turbo_bootstrap_script, timeout=0.4)
                    except Exception as exc:
                        turbo_result = {"ok": False, "error": str(exc)}
                    turbo_refreshed_after_route = True
                turbo_video_available = bool(isinstance(turbo_result, dict) and turbo_result.get("video"))
                turbo_ready_enough = bool(
                    turbo_video_available
                    and value.get("ok")
                    and value.get("readyState") != "loading"
                )
                native_ready_enough = bool(
                    native_course_already_open
                    and value.get("ok")
                    and value.get("readyState") != "loading"
                    and (
                        target_widget.get("dataName")
                        or value.get("guidePlayerVisible")
                    )
                )
                if (
                    fallback_reload_result is None
                    and not native_course_already_open
                    and effective_prefer_client_route
                    and value.get("ok")
                    and not content_matches
                    and (time.monotonic() - started_at) > float(timings["route_sec"]) + 1.2
                ):
                    fallback_reload_result = cdp_eval_value(
                        page,
                        xiaoluxue_route_course_expression(str(resolved["targetUrl"]), prefer_client_route=False),
                        timeout=1,
                    )
                if (
                    turbo_ready_enough
                    or native_ready_enough
                    or (
                        value.get("ok")
                        and value.get("readyState") != "loading"
                        and content_matches
                        and (target_widget.get("dataName") or target_loaded or current_loaded or value.get("guidePlayerVisible"))
                    )
                ):
                    break
        except Exception as exc:  # The same DevTools target can briefly reject evals while navigating.
            last_error = str(exc)
        time.sleep(0.08)
    timings["ready_sec"] = round(time.monotonic() - started_at, 3)

    goto_result: Any = None
    ready_target_widget = ready.get("targetWidget") if isinstance(ready, dict) and isinstance(ready.get("targetWidget"), dict) else {}
    turbo_video_available = bool(isinstance(turbo_result, dict) and turbo_result.get("video"))
    if guide_index >= 0 and (ready_target_widget.get("dataName") or not turbo_video_available):
        try:
            goto_result = cdp_eval_value(page, xiaoluxue_goto_widget_expression(guide_index, "scroll"), timeout=2)
            time.sleep(0.08)
            if turbo_preview:
                try:
                    turbo_result = cdp_eval_value(
                        page,
                        xiaoluxue_turbo_guide_player_expression(
                            guide_index=guide_index,
                            guide_name=str(resolved.get("guideName") or "") or None,
                            avatar_url=str(resolved.get("avatarUrl") or "") or None,
                            rate=rate,
                            enabled=True,
                        ),
                        timeout=0.4,
                    )
                except Exception:
                    pass
        except Exception as exc:
            goto_result = {"ok": False, "error": str(exc)}
    elif guide_index >= 0:
        goto_result = {"ok": True, "skipped": "turbo-video-active-before-widget"}

    speed_activate_result: Any = None
    turbo_video_active = bool(isinstance(turbo_result, dict) and turbo_result.get("video"))
    activate_real_player = not turbo_video_active
    if turbo_video_active:
        ready_videos = ready.get("videos") if isinstance(ready, dict) and isinstance(ready.get("videos"), list) else []
        speed_activate_result = {
            "ok": True,
            "skipped": "turbo-video-active",
            "rate": rate,
            "activatePlayer": False,
            "videos": ready_videos
            or [{"playbackRate": rate, "paused": False, "currentTime": None, "source": "turbo-video"}],
        }
    else:
        try:
            speed_activate_result = cdp_eval_value(
                page,
                xiaoluxue_fast_rate_expression(rate, guide_index, activate_player=activate_real_player),
                timeout=min(max(deadline - time.monotonic(), 1), 0.6),
            )
        except Exception as exc:
            speed_activate_result = {"ok": False, "error": str(exc)}
    speed_verify_result: Any = speed_activate_result
    video_verify_sec = min(max(float(args.get("video_verify_sec", 0.9)), 0), 3)
    activate_videos = (
        speed_activate_result.get("videos")
        if isinstance(speed_activate_result, dict) and isinstance(speed_activate_result.get("videos"), list)
        else []
    )
    verify_until = time.monotonic() if activate_videos else min(deadline, time.monotonic() + video_verify_sec)
    while time.monotonic() < verify_until:
        try:
            speed_verify_result = cdp_eval_value(
                page,
                xiaoluxue_fast_rate_expression(rate, guide_index, activate_player=False),
                timeout=min(max(deadline - time.monotonic(), 1), 2),
            )
            if isinstance(speed_verify_result, dict) and speed_verify_result.get("ok"):
                videos = speed_verify_result.get("videos") if isinstance(speed_verify_result.get("videos"), list) else []
                if videos:
                    break
        except Exception as exc:
            speed_verify_result = {"ok": False, "error": str(exc)}
        time.sleep(0.1)
    timings["speed_sec"] = round(time.monotonic() - started_at, 3)
    speed_result: Any = {
        "ok": bool(
            isinstance(speed_activate_result, dict)
            and speed_activate_result.get("ok")
            or isinstance(speed_verify_result, dict)
            and speed_verify_result.get("ok")
        ),
        "rate": rate,
        "video_verify_sec": video_verify_sec,
        "activate": speed_activate_result,
        "verify": speed_verify_result,
    }

    final_snapshot: Any = {"ok": True, "skipped": "fast-path", "ready": ready}
    if bool(args.get("final_verify", False)):
        try:
            final_snapshot = cdp_eval_value(page, xiaoluxue_course_ready_expression(knowledge_id_int, guide_index), timeout=0.8)
        except Exception as exc:
            final_snapshot = {"ok": False, "error": str(exc)}
    elapsed = round(time.monotonic() - started_at, 3)
    result = {
        "ok": True,
        "action": "xiaoluxue_open_knowledge_guide",
        "webview": True,
        "elapsed_sec": elapsed,
        "timings": timings,
        "requested": {
            "subject_id": subject_id,
            "knowledge_index": knowledge_index,
            "normalized_index": normalized_index,
            "rate": rate,
            "current_page_kind": current_page_kind,
            "effective_prefer_client_route": effective_prefer_client_route,
        },
        "resolved": resolved,
        "stop_loading": stop_loading_result,
        "route": route_result,
        "rate_bootstrap": rate_bootstrap_result,
        "rate_prepare": rate_prepare_result,
        "vessel_entry": vessel_entry_result,
        "native_entry": native_entry_result,
        "prefetch": prefetch_result,
        "fetch_optimizer": fetch_optimizer_result,
        "turbo": turbo_result,
        "fallback_reload": fallback_reload_result,
        "ready": ready,
        "goto": goto_result,
        "speed": speed_result,
        "final": final_snapshot,
    }
    if last_error and not ready:
        result["last_ready_error"] = last_error
    if record:
        append_recording_step(
            serial,
            "xiaoluxue_open_knowledge_guide",
            {
                "subject_id": subject_id,
                "knowledge_index": knowledge_index,
                "knowledge_id": resolved_knowledge_id,
                "guide_widget_index": resolved_guide_index,
                "rate": rate,
                "prefer_client_route": prefer_client_route,
                "native_entry_if_needed": native_entry_if_needed,
                "turbo_preview": turbo_preview,
                "optimize_neighbor_guides": optimize_neighbor_guides,
                "video_verify_sec": video_verify_sec,
                "final_verify": bool(args.get("final_verify", False)),
                "preinject_vessel_bootstrap": bool(args.get("preinject_vessel_bootstrap", False)),
                "respect_current_h5_host": bool(args.get("respect_current_h5_host", True)),
                "timeout_sec": timeout,
            },
            result,
        )
    return result


def tool_xiaoluxue_open_knowledge_guide(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    result = run_xiaoluxue_open_knowledge_guide(serial, args, record=True)
    return [text_content({"ok": True, "serial": serial, **result})]

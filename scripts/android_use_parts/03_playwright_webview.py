# Loaded by scripts/android_use_mcp.py. Keep this file below 2000 lines.


def node_binary() -> str:
    return os.environ.get("ANDROID_USE_NODE", "node")


def playwright_android_helper_path() -> Path:
    return PLUGIN_ROOT / "scripts" / "playwright_android_webview.js"


def playwright_android_status() -> dict[str, Any]:
    node_path = shutil.which(node_binary()) or node_binary()
    helper = playwright_android_helper_path()
    status: dict[str, Any] = {
        "backend": "playwright-android",
        "node_command": node_binary(),
        "node_path": node_path,
        "node_available": shutil.which(node_binary()) is not None or Path(node_binary()).exists(),
        "helper": str(helper),
        "helper_exists": helper.exists(),
        "package_installed": False,
    }
    if not status["node_available"] or not status["helper_exists"]:
        return status
    script = (
        "for (const name of ['playwright-core','playwright']) {"
        "try { const mod = require(name);"
        "if (mod && mod._android) { console.log(JSON.stringify({ok:true, package:name})); process.exit(0); }"
        "} catch (e) {}"
        "}"
        "console.log(JSON.stringify({ok:false})); process.exit(1);"
    )
    try:
        result = subprocess.run(
            [node_binary(), "-e", script],
            cwd=str(PLUGIN_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        status["error"] = str(exc)
        return status
    try:
        payload = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError:
        payload = {}
    status["package_installed"] = result.returncode == 0 and bool(payload.get("ok"))
    if payload.get("package"):
        status["package"] = payload["package"]
    if result.returncode != 0 and result.stderr.strip():
        status["error"] = result.stderr.strip()[-500:]
    return status


def run_playwright_android_webview(payload: dict[str, Any], timeout: int | float = 10) -> dict[str, Any]:
    helper = playwright_android_helper_path()
    if not helper.exists():
        raise AndroidUseError(f"Playwright Android helper is missing: {helper}")
    command = [node_binary(), str(helper)]
    try:
        result = subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(PLUGIN_ROOT),
            env=tool_env(),
            timeout=max(float(timeout), 1.0) + 5,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AndroidUseError("Node.js is required for Playwright Android WebView support.") from exc
    except subprocess.TimeoutExpired as exc:
        raise AndroidUseError(f"Playwright Android helper timed out after {timeout}s.") from exc
    stdout = decode_bytes(result.stdout)
    stderr = decode_bytes(result.stderr)
    try:
        response = json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        raise AndroidUseError(f"Playwright Android helper returned invalid JSON.\nstdout={stdout[:800]}\nstderr={stderr[:800]}") from exc
    if result.returncode != 0 or not response.get("ok"):
        detail = response.get("error") or stderr or stdout or f"exit code {result.returncode}"
        raise AndroidUseError(f"Playwright Android WebView operation failed: {detail}")
    return response


def playwright_android_webview_pages(serial: str, *, timeout: int | float = 10) -> list[dict[str, Any]]:
    response = run_playwright_android_webview(
        {
            "action": "list",
            "serial": serial,
            "timeout_sec": float(timeout),
        },
        timeout=timeout,
    )
    pages = response.get("pages")
    return pages if isinstance(pages, list) else []


def playwright_android_webview_eval(
    serial: str,
    expression: str,
    *,
    page_id: str | None = None,
    url_contains: str | None = None,
    title_contains: str | None = None,
    package: str | None = None,
    socket_name: str | None = None,
    await_promise: bool = True,
    return_by_value: bool = True,
    timeout: int | float = 10,
) -> dict[str, Any]:
    return run_playwright_android_webview(
        {
            "action": "eval",
            "serial": serial,
            "page_id": page_id,
            "url_contains": url_contains,
            "title_contains": title_contains,
            "package": package,
            "socket_name": socket_name,
            "expression": expression,
            "await_promise": await_promise,
            "return_by_value": return_by_value,
            "timeout_sec": float(timeout),
        },
        timeout=timeout,
    )


def webview_first_enabled() -> bool:
    return env_flag("ANDROID_USE_WEBVIEW_FIRST", True)


def webview_fast_timeout() -> float:
    return max(0.5, min(env_float("ANDROID_USE_WEBVIEW_FAST_TIMEOUT", 3.0), 10.0))


def webview_dom_snapshot_expression(limit: int = 80, max_text: int = 4000) -> str:
    config_json = json.dumps({"limit": limit, "maxText": max_text}, ensure_ascii=False)
    return r"""
(() => {
  const config = __CONFIG__;
  const norm = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const visible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && rect.bottom >= 0 && rect.right >= 0
      && rect.top <= innerHeight && rect.left <= innerWidth
      && style.visibility !== "hidden" && style.display !== "none";
  };
  const elementText = (el) => norm(el.innerText || el.textContent || el.getAttribute?.("aria-label")
    || el.getAttribute?.("title") || el.getAttribute?.("placeholder") || el.value || "");
  const isInteractive = (el) => {
    const tag = String(el.tagName || "").toLowerCase();
    const role = String(el.getAttribute?.("role") || "").toLowerCase();
    const style = getComputedStyle(el);
    return ["button", "a", "input", "textarea", "select", "summary"].includes(tag)
      || ["button", "link", "menuitem", "tab", "checkbox", "radio", "option"].includes(role)
      || typeof el.onclick === "function"
      || style.cursor === "pointer"
      || el.hasAttribute?.("tabindex")
      || el.isContentEditable
      || el.getAttribute?.("contenteditable") === "true";
  };
  const all = [...document.querySelectorAll("body *")];
  const items = [];
  for (const el of all) {
    if (!visible(el)) continue;
    const text = elementText(el);
    if (!text || text.length > 240) continue;
    const rect = el.getBoundingClientRect();
    const interactive = isInteractive(el);
    const area = rect.width * rect.height;
    if (!interactive && area > innerWidth * innerHeight * 0.45) continue;
    items.push({
      tag: String(el.tagName || "").toLowerCase(),
      text,
      aria: norm(el.getAttribute?.("aria-label")),
      role: norm(el.getAttribute?.("role")),
      id: norm(el.id),
      className: norm(el.className),
      interactive,
      rect: {
        left: Math.round(rect.left),
        top: Math.round(rect.top),
        right: Math.round(rect.right),
        bottom: Math.round(rect.bottom),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        x: Math.round(rect.left + rect.width / 2),
        y: Math.round(rect.top + rect.height / 2),
      },
    });
  }
  items.sort((a, b) => Number(b.interactive) - Number(a.interactive)
    || a.text.length - b.text.length
    || (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));
  return {
    ok: true,
    url: location.href,
    title: document.title,
    viewport: { width: innerWidth, height: innerHeight, devicePixelRatio },
    text: norm(document.body?.innerText || document.body?.textContent || "").slice(0, config.maxText),
    elements: items.slice(0, config.limit),
  };
})()
""".replace("__CONFIG__", config_json)


def webview_text_click_expression(query: str, *, exact: bool = True) -> str:
    config_json = json.dumps({"query": query, "exact": exact}, ensure_ascii=False)
    return r"""
(async () => {
  const config = __CONFIG__;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const norm = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const needle = norm(config.query).toLocaleLowerCase();
  const visible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && rect.bottom >= 0 && rect.right >= 0
      && rect.top <= innerHeight && rect.left <= innerWidth
      && style.visibility !== "hidden" && style.display !== "none";
  };
  const isInteractive = (el) => {
    const tag = String(el.tagName || "").toLowerCase();
    const role = String(el.getAttribute?.("role") || "").toLowerCase();
    const style = getComputedStyle(el);
    return ["button", "a", "input", "textarea", "select", "summary"].includes(tag)
      || ["button", "link", "menuitem", "tab", "checkbox", "radio", "option"].includes(role)
      || typeof el.onclick === "function"
      || style.cursor === "pointer"
      || el.hasAttribute?.("tabindex")
      || el.isContentEditable
      || el.getAttribute?.("contenteditable") === "true";
  };
  const elementText = (el) => norm(el.innerText || el.textContent || el.getAttribute?.("aria-label")
    || el.getAttribute?.("title") || el.getAttribute?.("placeholder") || el.value || "");
  const textMatches = (text) => {
    const haystack = norm(text).toLocaleLowerCase();
    if (!haystack || !needle) return false;
    return config.exact ? haystack === needle : haystack.includes(needle);
  };
  const targetFor = (el) => {
    let current = el;
    let depth = 0;
    while (current && depth < 6) {
      if (isInteractive(current)) return current;
      current = current.parentElement;
      depth += 1;
    }
    return el;
  };
  const candidates = [];
  for (const el of document.querySelectorAll("body *")) {
    if (!visible(el)) continue;
    const text = elementText(el);
    if (!textMatches(text)) continue;
    const target = targetFor(el);
    if (!visible(target)) continue;
    const rect = target.getBoundingClientRect();
    const targetText = elementText(target);
    if (targetText.length > 500 && !isInteractive(target)) continue;
    candidates.push({
      element: el,
      target,
      text,
      targetText,
      interactive: isInteractive(target),
      area: rect.width * rect.height,
      rect,
    });
  }
  candidates.sort((a, b) => Number(b.interactive) - Number(a.interactive)
    || a.text.length - b.text.length
    || a.area - b.area);
  const chosen = candidates[0];
  if (!chosen) {
    return { ok: false, reason: "no visible DOM text match", query: config.query, exact: config.exact };
  }
  chosen.target.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
  await sleep(80);
  const rect = chosen.target.getBoundingClientRect();
  const clientX = rect.left + rect.width / 2;
  const clientY = rect.top + rect.height / 2;
  for (const type of ["mouseover", "mousedown", "mouseup"]) {
    chosen.target.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, clientX, clientY }));
  }
  if (typeof chosen.target.click === "function") {
    chosen.target.click();
  } else {
    chosen.target.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, clientX, clientY }));
  }
  await sleep(120);
  return {
    ok: true,
    method: "dom_click",
    query: config.query,
    exact: config.exact,
    text: chosen.text,
    targetText: chosen.targetText.slice(0, 300),
    tag: String(chosen.target.tagName || "").toLowerCase(),
    role: chosen.target.getAttribute?.("role") || "",
    id: chosen.target.id || "",
    className: String(chosen.target.className || "").slice(0, 160),
    rect: {
      left: Math.round(rect.left),
      top: Math.round(rect.top),
      right: Math.round(rect.right),
      bottom: Math.round(rect.bottom),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
      x: Math.round(clientX),
      y: Math.round(clientY),
    },
  };
})()
""".replace("__CONFIG__", config_json)


def webview_snapshot_fast(serial: str, *, limit: int = 80, timeout: int | float | None = None) -> dict[str, Any] | None:
    if not webview_first_enabled():
        return None
    deadline = webview_fast_timeout() if timeout is None else float(timeout)
    try:
        pages = playwright_android_webview_pages(serial, timeout=deadline)
    except AndroidUseError:
        return None
    for page in pages:
        if not page.get("ok"):
            continue
        url = str(page.get("url") or "")
        if url == "about:blank":
            continue
        try:
            response = playwright_android_webview_eval(
                serial,
                webview_dom_snapshot_expression(limit=limit),
                page_id=str(page.get("id") or "") or None,
                timeout=deadline,
            )
        except AndroidUseError:
            continue
        value = response.get("result", {}).get("value") if isinstance(response.get("result"), dict) else None
        if isinstance(value, dict) and value.get("ok"):
            return {
                "backend": response.get("backend", "playwright-android"),
                "page": response.get("page") or page,
                **value,
            }
    return None


def tap_webview_text_fast(
    serial: str,
    query: str,
    *,
    exact: bool = True,
    timeout: int | float | None = None,
) -> dict[str, Any] | None:
    if not webview_first_enabled() or not query.strip():
        return None
    deadline = webview_fast_timeout() if timeout is None else float(timeout)
    try:
        pages = playwright_android_webview_pages(serial, timeout=deadline)
    except AndroidUseError:
        return None
    for page in pages:
        if not page.get("ok"):
            continue
        url = str(page.get("url") or "")
        if url == "about:blank":
            continue
        try:
            response = playwright_android_webview_eval(
                serial,
                webview_text_click_expression(query, exact=exact),
                page_id=str(page.get("id") or "") or None,
                timeout=deadline,
            )
        except AndroidUseError:
            continue
        value = response.get("result", {}).get("value") if isinstance(response.get("result"), dict) else None
        if isinstance(value, dict) and value.get("ok"):
            return {
                "source": "webview_dom",
                "backend": response.get("backend", "playwright-android"),
                "page": response.get("page") or page,
                "match": value,
            }
    return None


def webview_observation_from_snapshot(serial: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    page = snapshot.get("page") if isinstance(snapshot.get("page"), dict) else {}
    elements = snapshot.get("elements") if isinstance(snapshot.get("elements"), list) else []
    nodes: list[dict[str, Any]] = []
    for index, element in enumerate(elements):
        rect = element.get("rect") if isinstance(element, dict) else {}
        bounds = None
        center = None
        if isinstance(rect, dict):
            bounds = {
                "left": int(rect.get("left") or 0),
                "top": int(rect.get("top") or 0),
                "right": int(rect.get("right") or 0),
                "bottom": int(rect.get("bottom") or 0),
            }
            center = {"x": int(rect.get("x") or 0), "y": int(rect.get("y") or 0)}
        interactive = bool(element.get("interactive")) if isinstance(element, dict) else False
        node: dict[str, Any] = {
            "index": index,
            "text": str(element.get("text") or "") if isinstance(element, dict) else "",
            "content_desc": str(element.get("aria") or "") if isinstance(element, dict) else "",
            "resource_id": str(element.get("id") or "") if isinstance(element, dict) else "",
            "class": f"webview.dom.{element.get('tag') or 'element'}" if isinstance(element, dict) else "webview.dom.element",
            "package": str(page.get("pkg") or page.get("package") or ""),
            "bounds": bounds,
            "center": center,
            "clickable": interactive,
            "enabled": True,
            "checkable": False,
            "checked": False,
            "selected": False,
            "focused": False,
            "long_clickable": False,
            "depth": 0,
            "source": "webview_dom",
        }
        nodes.append(node)
    return {
        "state": {
            **device_state(serial),
            "webview_url": snapshot.get("url"),
            "webview_title": snapshot.get("title"),
        },
        "ui": {
            "nodes": nodes,
            "count": len(nodes),
            "source": "webview_dom",
            "coordinate_space": "css_pixels",
        },
        "webview": snapshot,
        "source": "webview_dom",
    }


def fast_webview_action_from_instruction(serial: str, instruction: str) -> dict[str, Any] | None:
    snapshot = webview_snapshot_fast(serial, limit=120)
    if not snapshot:
        return None
    elements = snapshot.get("elements") if isinstance(snapshot.get("elements"), list) else []
    labels: list[str] = []
    for element in elements:
        if not isinstance(element, dict) or not element.get("interactive"):
            continue
        text = str(element.get("text") or element.get("aria") or "").strip()
        if text and text not in labels:
            labels.append(text)
    candidates = quoted_phrases(instruction)
    candidates.extend(
        match.strip()
        for match in re.findall(r"(?:切换到|点击|点|进入|打开|选择|去|切到)\s*([^\s，。,.!！?？]+)", instruction)
        if match.strip()
    )
    for label in sorted(labels, key=len, reverse=True):
        if len(label) >= 2 and label in instruction:
            candidates.append(label)
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        for exact in (True, False):
            for element in elements:
                if not isinstance(element, dict) or not element.get("interactive"):
                    continue
                text = str(element.get("text") or element.get("aria") or "").strip()
                if not text:
                    continue
                matched = text == candidate if exact else candidate in text
                if matched:
                    return {
                        "action": "tap_text",
                        "text": candidate,
                        "exact": exact,
                        "source": "webview_dom",
                        "matched_webview": {
                            key: element.get(key)
                            for key in ("tag", "text", "role", "id", "className", "rect")
                        },
                    }
    return None


def cdp_eval_value(page: dict[str, Any], expression: str, timeout: int | float = 10) -> Any:
    serial = str(page.get("serial") or configured_serial() or "")
    if not serial:
        raise AndroidUseError("WebView page does not include a serial.")
    result = playwright_android_webview_eval(
        serial,
        expression,
        url_contains=str(page.get("url") or "") or None,
        title_contains=str(page.get("title") or "") or None,
        package=str(page.get("pkg") or page.get("package") or "") or None,
        socket_name=str(page.get("socket") or page.get("socketName") or "") or None,
        timeout=timeout,
    )
    remote = result.get("result") if isinstance(result.get("result"), dict) else {}
    return remote.get("value")

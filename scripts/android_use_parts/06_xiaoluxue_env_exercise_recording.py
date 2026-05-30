# Loaded by scripts/android_use_mcp.py. Keep this file below 2000 lines.

def normalize_xiaoluxue_env(env: Any) -> tuple[str, str, str]:
    raw = str(env or "test").strip()
    lowered = raw.casefold().replace("_", "-").replace(" ", "")
    aliases = {
        "": "test",
        "测试": "test",
        "测试环境": "test",
        "test环境": "test",
        "开发": "dev",
        "开发环境": "dev",
        "dev环境": "dev",
        "生产": "prod",
        "正式": "prod",
        "生产环境": "prod",
        "生产环境-com": "prod-com",
        "prodcom": "prod-com",
        "production-com": "prod-com",
    }
    key = aliases.get(lowered, lowered)
    if key.endswith("环境"):
        key = key[: -len("环境")]
    if key in XIAOLUXUE_ENV_CHOICES:
        choice = XIAOLUXUE_ENV_CHOICES[key]
        return key, choice["url"], choice["label"]
    match = XIAOLUXUE_CONFIG_URL_PATTERN.search(raw)
    if match:
        url = match.group(0)
        normalized_url = url.rstrip("/")
        for known_key, choice in XIAOLUXUE_ENV_CHOICES.items():
            if choice["url"].rstrip("/") == normalized_url:
                return known_key, choice["url"], choice["label"]
    valid = ", ".join(sorted({key for key in XIAOLUXUE_ENV_CHOICES if key != "production"}))
    raise AndroidUseError(f"Unsupported Xiaoluxue environment: {raw!r}. Expected one of: {valid}")


def xiaoluxue_config_observe(serial: str, *, limit: int = 500) -> dict[str, Any]:
    xml_text = dump_ui_xml(serial)
    nodes = parse_ui_nodes(xml_text, limit=limit)
    return {
        "state": {"focused_window": get_focused_window(serial)},
        "ui": {"nodes": nodes, "count": len(nodes)},
    }


def xiaoluxue_config_labels(nodes: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    for node in nodes:
        for label in node_labels(node)[:2]:
            value = label.strip()
            if value and value not in labels:
                labels.append(value)
    return labels


def xiaoluxue_config_current_url(nodes: list[dict[str, Any]]) -> str | None:
    marker_index: int | None = None
    for node in sorted(nodes, key=lambda item: int(item.get("index", 0))):
        labels = node_labels(node)[:2]
        for label in labels:
            match = XIAOLUXUE_CONFIG_URL_PATTERN.search(label)
            if "当前配置" in label and match:
                return match.group(0)
            if "当前配置" in label:
                marker_index = int(node.get("index", 0))
                continue
            if marker_index is not None and int(node.get("index", 0)) >= marker_index:
                if match:
                    return match.group(0)
    return None


def xiaoluxue_wait_config_screen(serial: str, timeout_sec: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout_sec, 0.2)
    last_observation: dict[str, Any] | None = None
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            observation = xiaoluxue_config_observe(serial)
            last_observation = observation
            focus = str(observation.get("state", {}).get("focused_window") or "")
            nodes = observation.get("ui", {}).get("nodes", [])
            labels = xiaoluxue_config_labels(nodes) if isinstance(nodes, list) else []
            has_config_labels = any(label in labels for label in ("API 环境", "提交配置", "当前配置：", "学生端", "网络相关"))
            if has_config_labels or ("API 环境" in labels or "提交配置" in labels) or (XIAOLUXUE_CONFIG_PACKAGE in focus and labels):
                return observation
        except Exception as exc:  # noqa: PERF203 - retry until the config activity draws.
            last_error = exc
        time.sleep(0.12)
    if last_observation:
        return last_observation
    if last_error:
        raise AndroidUseError(f"Could not observe Xiaoluxue config screen: {last_error}") from last_error
    raise AndroidUseError("Could not observe Xiaoluxue config screen.")


def xiaoluxue_tap_ui_node(
    serial: str,
    node: dict[str, Any] | None,
    label: str,
    steps: list[dict[str, Any]],
    started_at: float,
) -> dict[str, int]:
    point = node_click_point(node) if node else None
    if not point:
        raise AndroidUseError(f"Could not tap Xiaoluxue config UI node: {label}")
    adb(["shell", "input", "tap", str(point["x"]), str(point["y"])], serial=serial, timeout=4)
    steps.append(
        {
            "action": "tap",
            "label": label,
            "x": point["x"],
            "y": point["y"],
            "matched_node": compact_node(node),
            "at_sec": round(time.monotonic() - started_at, 3),
        }
    )
    return point


def xiaoluxue_config_swipe(serial: str, direction: str, steps: list[dict[str, Any]], started_at: float) -> None:
    size = get_screen_size(serial)
    width = int(size.get("width") or XIAOLUXUE_NATIVE_BASE_WIDTH)
    height = int(size.get("height") or XIAOLUXUE_NATIVE_BASE_HEIGHT)
    x = round(width * 0.5)
    if direction == "down":
        start_y, end_y = round(height * 0.35), round(height * 0.75)
    else:
        start_y, end_y = round(height * 0.75), round(height * 0.35)
    adb(["shell", "input", "swipe", str(x), str(start_y), str(x), str(end_y), "180"], serial=serial, timeout=5)
    steps.append(
        {
            "action": "swipe",
            "direction": direction,
            "x": x,
            "start_y": start_y,
            "end_y": end_y,
            "at_sec": round(time.monotonic() - started_at, 3),
        }
    )


def xiaoluxue_find_config_node(
    serial: str,
    queries: list[str],
    *,
    timeout_sec: float,
    steps: list[dict[str, Any]],
    started_at: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + max(timeout_sec, 0.3)
    directions = ["down", "up", "up", "up", "up", "down"]
    direction_index = 0
    last_observation = xiaoluxue_wait_config_screen(serial, min(timeout_sec, 2))
    while True:
        nodes = last_observation.get("ui", {}).get("nodes", [])
        if isinstance(nodes, list):
            for query in queries:
                node = find_ui_node(nodes, query, exact=True) or find_ui_node(nodes, query, exact=False)
                if node:
                    return last_observation, node
        if time.monotonic() >= deadline or direction_index >= len(directions):
            break
        xiaoluxue_config_swipe(serial, directions[direction_index], steps, started_at)
        direction_index += 1
        time.sleep(0.15)
        last_observation = xiaoluxue_wait_config_screen(serial, min(max(deadline - time.monotonic(), 0.2), 1.2))
    raise AndroidUseError(f"Could not find Xiaoluxue config option: {queries}")


def xiaoluxue_wait_env_apply(serial: str, target_url: str, timeout_sec: float, steps: list[dict[str, Any]], started_at: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout_sec, 0.3)
    last_payload: dict[str, Any] = {}
    confirmed_once = False
    while time.monotonic() < deadline:
        observation = xiaoluxue_config_observe(serial)
        nodes = observation.get("ui", {}).get("nodes", [])
        labels = xiaoluxue_config_labels(nodes) if isinstance(nodes, list) else []
        focus = str(observation.get("state", {}).get("focused_window") or "")
        current_url = xiaoluxue_config_current_url(nodes) if isinstance(nodes, list) else None
        last_payload = {"focus": focus, "current_url": current_url}
        if current_url == target_url:
            return {"ok": True, "reason": "current-config-updated", **last_payload}
        if XIAOLUXUE_STUDENT_PACKAGE in focus:
            return {"ok": True, "reason": "student-opened", **last_payload}
        if not confirmed_once and XIAOLUXUE_CONFIG_PACKAGE in focus and isinstance(nodes, list):
            confirm = find_ui_node(nodes, "确定", exact=True) or find_ui_node(nodes, "OK", exact=True)
            if confirm and any(label in labels for label in ("API 环境", "提交配置", "当前配置：")):
                xiaoluxue_tap_ui_node(serial, confirm, "confirm_config_dialog", steps, started_at)
                confirmed_once = True
        time.sleep(0.15)
    return {"ok": False, "reason": "timeout", **last_payload}


def xiaoluxue_launch_student_after_env(serial: str, *, force_stop: bool, timeout_sec: float) -> dict[str, Any]:
    if force_stop:
        adb(["shell", "am", "force-stop", XIAOLUXUE_STUDENT_PACKAGE], serial=serial, timeout=8)
    adb(["shell", "monkey", "-p", XIAOLUXUE_STUDENT_PACKAGE, "-c", "android.intent.category.LAUNCHER", "1"], serial=serial, timeout=10)
    return xiaoluxue_wait_native_app_focus(serial, min(max(timeout_sec, 0.5), 4))


def run_xiaoluxue_switch_env(serial: str, args: dict[str, Any], *, record: bool) -> dict[str, Any]:
    started_at = time.monotonic()
    timeout = min(float(args.get("timeout_sec", 10)), 60)
    env_key, target_url, option_label = normalize_xiaoluxue_env(args.get("env", "test"))
    force_submit = bool(args.get("force_submit", False))
    open_student = bool(args.get("open_student", True))
    steps: list[dict[str, Any]] = []

    adb(["shell", "am", "start", "-n", XIAOLUXUE_CONFIG_LAUNCHER_COMPONENT], serial=serial, timeout=10)
    steps.append({"action": "open_app", "package": XIAOLUXUE_CONFIG_PACKAGE, "at_sec": round(time.monotonic() - started_at, 3)})
    observation = xiaoluxue_wait_config_screen(serial, min(timeout, 5))
    nodes = observation.get("ui", {}).get("nodes", [])

    labels = xiaoluxue_config_labels(nodes) if isinstance(nodes, list) else []
    if isinstance(nodes, list) and "API 环境" not in labels:
        student_tab = find_ui_node(nodes, "学生端", exact=True)
        if student_tab:
            xiaoluxue_tap_ui_node(serial, student_tab, "学生端", steps, started_at)
            time.sleep(0.15)
            observation = xiaoluxue_wait_config_screen(serial, min(timeout, 3))
            nodes = observation.get("ui", {}).get("nodes", [])

    labels = xiaoluxue_config_labels(nodes) if isinstance(nodes, list) else []
    if isinstance(nodes, list) and "API 环境" not in labels:
        network_tab = find_ui_node(nodes, "网络相关", exact=True)
        if network_tab:
            xiaoluxue_tap_ui_node(serial, network_tab, "网络相关", steps, started_at)
            time.sleep(0.15)
            observation = xiaoluxue_wait_config_screen(serial, min(timeout, 3))
            nodes = observation.get("ui", {}).get("nodes", [])

    current_before = xiaoluxue_config_current_url(nodes) if isinstance(nodes, list) else None
    already_current = current_before == target_url
    submit_result: dict[str, Any]
    if already_current and not force_submit:
        submit_result = {"ok": True, "skipped": "already-current"}
    else:
        observation, target_node = xiaoluxue_find_config_node(
            serial,
            [option_label, target_url],
            timeout_sec=max(timeout - (time.monotonic() - started_at), 1),
            steps=steps,
            started_at=started_at,
        )
        xiaoluxue_tap_ui_node(serial, target_node, option_label, steps, started_at)
        time.sleep(0.15)
        _submit_observation, submit_node = xiaoluxue_find_config_node(
            serial,
            ["提交配置"],
            timeout_sec=max(timeout - (time.monotonic() - started_at), 1),
            steps=steps,
            started_at=started_at,
        )
        xiaoluxue_tap_ui_node(serial, submit_node, "提交配置", steps, started_at)
        submit_result = xiaoluxue_wait_env_apply(
            serial,
            target_url,
            timeout_sec=max(timeout - (time.monotonic() - started_at), 1),
            steps=steps,
            started_at=started_at,
        )

    if already_current and not force_submit:
        current_after = current_before
    else:
        current_after = submit_result.get("current_url")
    student_result: dict[str, Any] | None = None
    apply_ok = bool(already_current or submit_result.get("ok"))
    if open_student and apply_ok:
        force_stop_student = bool(args.get("force_stop_student", not already_current or force_submit))
        student_result = xiaoluxue_launch_student_after_env(
            serial,
            force_stop=force_stop_student,
            timeout_sec=max(timeout - (time.monotonic() - started_at), 1),
        )
        steps.append(
            {
                "action": "open_student",
                "force_stop": force_stop_student,
                "focus": student_result.get("focus"),
                "at_sec": round(time.monotonic() - started_at, 3),
            }
        )

    result = {
        "ok": apply_ok,
        "action": "xiaoluxue_switch_env",
        "elapsed_sec": round(time.monotonic() - started_at, 3),
        "env": env_key,
        "target_url": target_url,
        "option_label": option_label,
        "current_before": current_before,
        "current_after": current_after,
        "changed": bool(not already_current or force_submit),
        "submit": submit_result,
        "student": student_result,
        "steps": steps,
    }
    if record:
        recorded_args = {
            key: args[key]
            for key in ("env", "open_student", "force_submit", "force_stop_student", "timeout_sec")
            if key in args
        }
        append_recording_step(serial, "xiaoluxue_switch_env", recorded_args, result)
    return result


def tool_xiaoluxue_switch_env(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    result = run_xiaoluxue_switch_env(serial, args, record=True)
    return [text_content({"ok": True, "serial": serial, **result})]


XIAOLUXUE_EXERCISE_URL_MARKER = "stu.xiaoluxue.com/exercise"


def xiaoluxue_exercise_page(serial: str) -> dict[str, Any]:
    cached = xiaoluxue_cached_runtime_page(serial, "exercise")
    if cached:
        return cached
    pages = discover_webview_pages(serial)
    page = select_xiaoluxue_webview_page(pages, "exercise")
    xiaoluxue_remember_inferred_page(serial, page)
    return xiaoluxue_remember_page(serial, "exercise", page)


def xiaoluxue_exercise_snapshot_expression() -> str:
    return r"""
(() => {
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height, top: r.top, right: r.right, bottom: r.bottom, left: r.left };
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && style.visibility !== "hidden" && style.display !== "none";
  };
  const text = (el) => (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ");
  const disabled = (el) => {
    const anyEl = el;
    return Boolean(anyEl.disabled) || el.getAttribute("aria-disabled") === "true" || el.getAttribute("data-disabled") === "true";
  };
  const optionKey = (raw) => {
    const trimmed = (raw || "").trim();
    const alpha = /^([A-Z])(?:\s|[.．、)])/.exec(trimmed);
    if (alpha) return alpha[1];
    const judge = /^(TRUE|FALSE|正确|错误)\b/i.exec(trimmed);
    return judge ? judge[1].toUpperCase() : "";
  };
  const buttons = [...document.querySelectorAll("button,[role='button']")]
    .filter(visible)
    .map((el, index) => ({
      index,
      text: text(el),
      disabled: disabled(el),
      className: String(el.className || ""),
      rect: rect(el),
    }))
    .filter((item) => item.text)
    .slice(0, 80);
  const options = [...document.querySelectorAll(".option-button")]
    .filter(visible)
    .map((el, index) => {
      const itemText = text(el);
      return {
        index: index + 1,
        key: optionKey(itemText),
        text: itemText.slice(0, 500),
        disabled: disabled(el),
        selected: /selected|#EAF8FF|rgb\(234,\s*248,\s*255\)/i.test(String(el.className || "") + " " + String(el.getAttribute("style") || "")),
        rect: rect(el),
      };
    });
  const questionSelectors = [
    ".question-main-content",
    ".question-content-left",
    ".question-content-right",
    ".fill-blank-content",
    ".choice-question-view",
    ".question-content-view",
  ];
  const questionBlocks = questionSelectors
    .flatMap((selector) => [...document.querySelectorAll(selector)])
    .filter(visible)
    .map((el) => ({ selector: questionSelectors.find((selector) => el.matches(selector)) || "", text: text(el).slice(0, 1000), rect: rect(el) }))
    .filter((item, index, arr) => item.text && arr.findIndex((other) => other.text === item.text) === index)
    .slice(0, 20);
  const audios = [...document.querySelectorAll("audio")].map((audio) => ({
    src: audio.currentSrc || audio.src || "",
    currentTime: audio.currentTime,
    duration: Number.isFinite(audio.duration) ? audio.duration : null,
    playbackRate: audio.playbackRate,
    paused: audio.paused,
    rect: rect(audio),
  }));
  const progressTexts = [...document.querySelectorAll("body *")]
    .filter((el) => visible(el) && el.children.length === 0)
    .map(text)
    .filter((value) => value && /(\d+\s*\/\s*\d+|第\s*\d+|进度|已答|未答)/.test(value))
    .slice(0, 40);
  const answerBoxes = [...document.querySelectorAll(".math-field-answer-box,[class*='answer-box']")]
    .filter(visible)
    .map((el, index) => ({
      index: index + 1,
      text: text(el).slice(0, 500),
      className: String(el.className || ""),
      rect: rect(el),
    }))
    .slice(0, 20);
  const findAction = (labels) => buttons.find((button) => !button.disabled && labels.some((label) => button.text === label || button.text.includes(label)));
  const params = Object.fromEntries(new URL(location.href).searchParams.entries());
  return {
    app: "xiaoluxue",
    page: "exercise",
    title: document.title,
    url: location.href,
    params,
    viewport: { width: innerWidth, height: innerHeight, devicePixelRatio },
    ready: options.length > 0 || buttons.length > 0 || questionBlocks.length > 0,
    questionText: questionBlocks[0]?.text || "",
    questionBlocks,
    options,
    answerBoxes,
    answerText: answerBoxes[0]?.text || "",
    buttons,
    actions: {
      canSubmit: Boolean(findAction(["提交"])),
      canContinue: Boolean(findAction(["继续", "下一题"])),
      canNextPart: Boolean(findAction(["下一空", "下一问"])),
      canGiveUp: Boolean(findAction(["放弃作答"])),
      canUncertain: Boolean(findAction(["不确定"])),
    },
    audios,
    progressTexts,
    localExerciseKeys: Object.keys(localStorage).filter((key) => /exercise|question|answer|study/i.test(key)).slice(0, 40),
  };
})()
"""


def xiaoluxue_exercise_action_expression(args: dict[str, Any]) -> str:
    config = {
        "actionName": str(args.get("action_name") or args.get("action") or "next"),
        "optionKey": str(args.get("option_key") or "").strip(),
        "optionIndex": int(args["option_index"]) if args.get("option_index") is not None else None,
        "optionText": str(args.get("option_text") or "").strip(),
        "answerText": str(args["answer_text"]) if args.get("answer_text") is not None else None,
        "buttonText": str(args.get("button_text") or "").strip(),
    }
    config_json = json.dumps(config, ensure_ascii=False)
    return f"""
(async () => {{
  const config = {config_json};
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const rect = (el) => {{
    const r = el.getBoundingClientRect();
    return {{ x: r.x, y: r.y, width: r.width, height: r.height, top: r.top, right: r.right, bottom: r.bottom, left: r.left }};
  }};
  const visible = (el) => {{
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && style.visibility !== "hidden" && style.display !== "none";
  }};
  const text = (el) => (el.innerText || el.textContent || "").trim().replace(/\\s+/g, " ");
  const disabled = (el) => {{
    return Boolean(el.disabled) || el.getAttribute("aria-disabled") === "true" || el.getAttribute("data-disabled") === "true";
  }};
  const click = (el) => {{
    el.scrollIntoView({{ block: "center", inline: "center", behavior: "instant" }});
    el.dispatchEvent(new PointerEvent("pointerdown", {{ bubbles: true, cancelable: true, pointerType: "touch" }}));
    el.dispatchEvent(new MouseEvent("mousedown", {{ bubbles: true, cancelable: true, view: window }}));
    el.dispatchEvent(new PointerEvent("pointerup", {{ bubbles: true, cancelable: true, pointerType: "touch" }}));
    el.dispatchEvent(new MouseEvent("mouseup", {{ bubbles: true, cancelable: true, view: window }}));
    el.dispatchEvent(new MouseEvent("click", {{ bubbles: true, cancelable: true, view: window }}));
  }};
  const optionKey = (raw) => {{
    const trimmed = (raw || "").trim();
    const alpha = /^([A-Z])(?:\\s|[.．、)])/.exec(trimmed);
    if (alpha) return alpha[1];
    const judge = /^(TRUE|FALSE|正确|错误)\\b/i.exec(trimmed);
    return judge ? judge[1].toUpperCase() : "";
  }};
  const pack = (el, extra = {{}}) => el ? {{ text: text(el), rect: rect(el), className: String(el.className || ""), ...extra }} : null;
  const buttons = () => [...document.querySelectorAll("button,[role='button']")].filter((el) => visible(el) && !disabled(el));
  const reactFiber = (el) => {{
    if (!el) return null;
    const key = Object.keys(el).find((key) => key.startsWith("__reactFiber$") || key.startsWith("__reactInternalInstance$"));
    return key ? el[key] : null;
  }};
  const findReactFunctionProp = (root, propName) => {{
    const seen = new Set();
    const elements = [];
    if (root) elements.push(root);
    if (root?.querySelectorAll) elements.push(...root.querySelectorAll("*"));
    elements.push(document.activeElement);
    for (const startEl of elements.filter(Boolean)) {{
      let fiber = reactFiber(startEl);
      let depth = 0;
      while (fiber && depth < 40) {{
        if (!seen.has(fiber)) {{
          seen.add(fiber);
          const props = fiber.memoizedProps || fiber.pendingProps || {{}};
          if (typeof props[propName] === "function") return {{ fiber, props, element: startEl }};
        }}
        fiber = fiber.return;
        depth += 1;
      }}
    }}
    return null;
  }};
  const unique = (items) => {{
    const seen = new Set();
    const result = [];
    for (const item of items.filter(Boolean)) {{
      if (!seen.has(item)) {{
        seen.add(item);
        result.push(item);
      }}
    }}
    return result;
  }};
  const isTextField = (el) => {{
    if (!el) return false;
    if (el instanceof HTMLTextAreaElement) return true;
    if (el instanceof HTMLInputElement) return el.type !== "hidden";
    return el.getAttribute?.("contenteditable") === "true" || el.getAttribute?.("role") === "textbox";
  }};
  const fieldValue = (el) => ("value" in el ? el.value : (el.textContent || ""));
  const textFieldCandidates = () => unique([
    document.activeElement,
    ...document.querySelectorAll("textarea.keyboard-input-textarea, textarea, input:not([type='hidden']), [contenteditable='true'], [role='textbox']"),
  ]).filter((el) => isTextField(el) && visible(el));
  const setNativeValue = (el, value) => {{
    if ("value" in el) {{
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
      if (descriptor?.set) descriptor.set.call(el, value);
      else el.value = value;
    }} else {{
      el.textContent = value;
    }}
    const inputEvent = typeof InputEvent === "function"
      ? new InputEvent("input", {{ bubbles: true, cancelable: true, data: value, inputType: "insertText" }})
      : new Event("input", {{ bubbles: true, cancelable: true }});
    el.dispatchEvent(inputEvent);
    el.dispatchEvent(new Event("change", {{ bubbles: true, cancelable: true }}));
  }};
  const reactPropsChain = (el) => {{
    const seen = new Set();
    const propsList = [];
    for (let startEl = el; startEl; startEl = startEl.parentElement) {{
      let fiber = reactFiber(startEl);
      let depth = 0;
      while (fiber && depth < 40) {{
        if (!seen.has(fiber)) {{
          seen.add(fiber);
          const props = fiber.memoizedProps || fiber.pendingProps || {{}};
          propsList.push({{ props, element: startEl }});
        }}
        fiber = fiber.return;
        depth += 1;
      }}
    }}
    return propsList;
  }};
  const callReactTextHandlers = (el, value) => {{
    const called = [];
    const valueHandlerNames = ["onValueChange", "onAnswerChange", "onTextChange", "onLatexChange"];
    const eventHandlerNames = ["onChange", "onInput"];
    const event = {{
      target: el,
      currentTarget: el,
      type: "change",
      bubbles: true,
      cancelable: true,
      defaultPrevented: false,
      nativeEvent: new Event("input", {{ bubbles: true, cancelable: true }}),
      preventDefault: () => {{}},
      stopPropagation: () => {{}},
    }};
    for (const {{ props }} of reactPropsChain(el)) {{
      for (const name of valueHandlerNames) {{
        if (typeof props[name] === "function") {{
          try {{
            props[name](value);
            called.push(name);
          }} catch (_err) {{}}
        }}
      }}
      for (const name of eventHandlerNames) {{
        if (typeof props[name] === "function") {{
          try {{
            props[name](event);
            called.push(name);
          }} catch (_err) {{}}
        }}
      }}
    }}
    return [...new Set(called)];
  }};
  const atomValue = (value) => {{
    if (!value) return value;
    if (value.v !== undefined) return value.v;
    if (value.value !== undefined) return value.value;
    try {{
      if (typeof value.peek === "function") return value.peek();
    }} catch (_err) {{}}
    return value;
  }};
  const findQuestionStore = () => {{
    const roots = [
      document.activeElement,
      ...document.querySelectorAll("#right-answer-area,.question-main-content,button,[data-container-id],textarea,input,[contenteditable='true'],[role='textbox']"),
    ].filter(Boolean);
    const seen = new Set();
    for (const root of roots) {{
      let fiber = reactFiber(root);
      let depth = 0;
      while (fiber && depth < 70) {{
        if (!seen.has(fiber)) {{
          seen.add(fiber);
          const props = fiber.memoizedProps || fiber.pendingProps || {{}};
          if (props.questionStore) return props.questionStore;
        }}
        fiber = fiber.return;
        depth += 1;
      }}
    }}
    return null;
  }};
  const fillQuestionStoreAnswer = async (answerText) => {{
    const store = findQuestionStore();
    if (!store || typeof store.updateUserAnswer !== "function") return null;
    const currentQuestion = atomValue(store.currentQuestion) || {{}};
    const questionId = currentQuestion.questionId || currentQuestion.questionContent?.questionId || currentQuestion.id;
    if (!questionId) return {{ ok: false, reason: "question id not found" }};
    const currentAnswers = atomValue(store.currentQuestionAnswers);
    const answerCount = Array.isArray(currentAnswers) && currentAnswers.length ? currentAnswers.length : 1;
    const activeBlankIndex = Number(atomValue(store.activeBlankIndex));
    const blankIndex = Number.isFinite(activeBlankIndex) && activeBlankIndex >= 0
      ? Math.min(activeBlankIndex, answerCount - 1)
      : 0;
    const currentInputMode = Number(atomValue(store.currentInputMode));
    const type = Number.isFinite(currentInputMode) && currentInputMode > 0 ? currentInputMode : 1;
    store.updateUserAnswer({{ questionId, blankIndex, type, content: answerText }});
    if (typeof store.updateUserAnswerType === "function") {{
      try {{ store.updateUserAnswerType(questionId, type); }} catch (_err) {{}}
    }}
    await sleep(120);
    const updatedAnswers = atomValue(store.currentQuestionAnswers);
    const userAnswerDataMap = atomValue(store.userAnswerDataMap);
    const storedAnswers = userAnswerDataMap instanceof Map ? userAnswerDataMap.get(questionId) : null;
    return {{
      ok: true,
      method: "react_question_store",
      questionId,
      blankIndex,
      type,
      chars: answerText.length,
      answerText,
      currentAnswers: Array.isArray(updatedAnswers) ? updatedAnswers : null,
      storedAnswers: Array.isArray(storedAnswers) ? storedAnswers : null,
    }};
  }};
  const ensurePlausibleAnswerDuration = (minMs = 6500) => {{
    const store = findQuestionStore();
    const timerState = store?.timerState;
    if (!timerState || !timerState.value) return null;
    const current = Number(timerState.value.currentTime || 0);
    const total = Number(timerState.value.totalTime || 0);
    const currentQuestionTotal = Number(store?.homeworkProgress?.currentQuestionTotal || 0);
    const nextCurrent = Math.max(current, currentQuestionTotal + minMs, minMs);
    timerState.value = {{
      ...timerState.value,
      currentTime: nextCurrent,
      totalTime: Math.max(total, nextCurrent),
    }};
    return {{ before: current, after: nextCurrent, currentQuestionTotal }};
  }};
  const fillAnswer = async () => {{
    if (config.answerText === null || config.answerText === undefined) {{
      return {{ ok: false, reason: "answer_text is required" }};
    }}
    const answerText = String(config.answerText);
    const directFields = textFieldCandidates();
    const directField = directFields.find((el) => String(el.className || "").includes("keyboard-input-textarea"))
      || directFields.find((el) => el === document.activeElement)
      || directFields[0];
    if (directField) {{
      const before = fieldValue(directField);
      setNativeValue(directField, answerText);
      const reactHandlers = callReactTextHandlers(directField, answerText);
      directField.blur?.();
      await sleep(80);
      return {{
        ok: true,
        method: "dom_text_field",
        chars: answerText.length,
        answerText,
        before,
        after: fieldValue(directField),
        reactHandlers,
        renderedText: text(directField.closest("[class*='answer'],[class*='question'],body") || directField).slice(0, 500),
        rect: rect(directField),
      }};
    }}
    const roots = [
      ...document.querySelectorAll(".math-field-answer-box"),
      ...document.querySelectorAll("[class*='math-field'][class*='answer']"),
      ...document.querySelectorAll("[class*='answer-box']"),
      document.activeElement,
    ].filter(Boolean);
    const root = roots.find((el) => el === document.activeElement || visible(el)) || roots[0] || document.body;
    const found = findReactFunctionProp(root, "onLatexChange");
    if (found) {{
      found.props.onLatexChange(answerText);
      await sleep(100);
      const box = document.querySelector(".math-field-answer-box") || root;
      return {{
        ok: true,
        method: "react_onLatexChange",
        chars: answerText.length,
        answerText,
        renderedText: box ? text(box).slice(0, 500) : "",
        rect: box ? rect(box) : null,
      }};
    }}
    const field = textFieldCandidates()[0] || roots.find((el) => el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement);
    if (field) {{
      setNativeValue(field, answerText);
      const reactHandlers = callReactTextHandlers(field, answerText);
      field.blur?.();
      await sleep(80);
      return {{
        ok: true,
        method: "dom_text_field_fallback",
        chars: answerText.length,
        answerText,
        reactHandlers,
        renderedText: text(root).slice(0, 500),
        rect: root ? rect(root) : null,
      }};
    }}
    const storeResult = await fillQuestionStoreAnswer(answerText);
    if (storeResult) return storeResult;
    return {{ ok: false, reason: "answer input not found" }};
  }};
  const clickButton = (labels, mode = "contains") => {{
    const candidates = buttons();
    const exact = candidates.find((el) => labels.some((label) => text(el) === label));
    const included = candidates.find((el) => labels.some((label) => text(el).includes(label)));
    const target = mode === "exact" ? exact : (exact || included);
    if (!target) return {{ ok: false, reason: "button not found", labels, visibleButtons: candidates.map((el) => text(el)).slice(0, 30) }};
    const timer =
      labels.some((label) => ["提交", "继续提交", "提交自评"].includes(label))
        ? ensurePlausibleAnswerDuration()
        : null;
    click(target);
    return {{ ok: true, clicked: pack(target), labels, timer }};
  }};
  const clickOption = () => {{
    const optionButtons = [...document.querySelectorAll(".option-button")].filter((el) => visible(el) && !disabled(el));
    let target = null;
    if (config.optionIndex != null) target = optionButtons[Number(config.optionIndex) - 1] || null;
    if (!target && config.optionKey) {{
      const key = config.optionKey.toUpperCase();
      target = optionButtons.find((el) => optionKey(text(el)).toUpperCase() === key) || null;
    }}
    if (!target && config.optionText) {{
      target = optionButtons.find((el) => text(el).includes(config.optionText)) || null;
    }}
    if (!target) return {{
      ok: false,
      reason: "option not found",
      optionKey: config.optionKey,
      optionIndex: config.optionIndex,
      optionText: config.optionText,
      visibleOptions: optionButtons.map((el, index) => ({{ index: index + 1, key: optionKey(text(el)), text: text(el).slice(0, 160) }})),
    }};
    click(target);
    return {{ ok: true, clicked: pack(target, {{ index: optionButtons.indexOf(target) + 1, key: optionKey(text(target)) }}) }};
  }};
  const actionName = String(config.actionName || "next");
  let result;
  if (actionName === "fill_answer" || actionName === "input_answer") {{
    result = {{ actionName, ...(await fillAnswer()) }};
  }} else if (actionName === "select_option" || actionName === "answer") {{
    result = {{ actionName, ...(clickOption()) }};
  }} else if (actionName === "submit") {{
    result = {{ actionName, ...(clickButton(["提交"])) }};
  }} else if (actionName === "next") {{
    result = {{ actionName, ...(clickButton(["下一空", "下一问", "下一题", "继续"])) }};
  }} else if (actionName === "continue") {{
    result = {{ actionName, ...(clickButton(["继续", "下一题"])) }};
  }} else if (actionName === "uncertain") {{
    result = {{ actionName, ...(clickButton(["不确定"])) }};
  }} else if (actionName === "give_up") {{
    result = {{ actionName, ...(clickButton(["放弃作答"])) }};
  }} else if (actionName === "button_text") {{
    result = {{ actionName, ...(clickButton([config.buttonText], "contains")) }};
  }} else {{
    result = {{ ok: false, actionName, reason: "unsupported action" }};
  }}
  await sleep(120);
  return result;
}})()
"""


def xiaoluxue_exercise_auto_answer_expression(args: dict[str, Any]) -> str:
    config = {
        "maxSteps": min(max(int(args.get("max_steps", 24)), 1), 80),
        "stepWaitMs": int(min(max(float(args.get("step_wait_sec", 0.45)), 0.2), 3.0) * 1000),
        "clickReport": bool(args.get("click_report", True)),
        "openAnswerText": str(
            args.get("open_answer_text")
            or "I can answer this question clearly. I will make a plan, review what I have learned, and ask my teacher for help when needed."
        ),
    }
    config_json = json.dumps(config, ensure_ascii=False)
    return f"""
(async () => {{
  const config = {config_json};
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const logs = [];
  const text = (el) => (el.innerText || el.textContent || "").trim().replace(/\\s+/g, " ");
  const visible = (el) => {{
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight
      && style.visibility !== "hidden" && style.display !== "none";
  }};
  const disabled = (el) => {{
    return Boolean(el.disabled) || el.getAttribute("aria-disabled") === "true" || el.getAttribute("data-disabled") === "true";
  }};
  const click = (el) => {{
    el.scrollIntoView({{ block: "center", inline: "center", behavior: "instant" }});
    el.dispatchEvent(new PointerEvent("pointerdown", {{ bubbles: true, cancelable: true, pointerType: "touch" }}));
    el.dispatchEvent(new MouseEvent("mousedown", {{ bubbles: true, cancelable: true, view: window }}));
    el.dispatchEvent(new PointerEvent("pointerup", {{ bubbles: true, cancelable: true, pointerType: "touch" }}));
    el.dispatchEvent(new MouseEvent("mouseup", {{ bubbles: true, cancelable: true, view: window }}));
    el.dispatchEvent(new MouseEvent("click", {{ bubbles: true, cancelable: true, view: window }}));
  }};
  const clickAtRatio = (xRatio, yRatio) => {{
    const x = Math.max(1, Math.min(innerWidth - 1, Math.round(innerWidth * xRatio)));
    const y = Math.max(1, Math.min(innerHeight - 1, Math.round(innerHeight * yRatio)));
    const target = document.elementFromPoint(x, y);
    if (!target) return "";
    click(target);
    return `point(${{x}},${{y}}):${{text(target).slice(0, 40)}}`;
  }};
  const buttons = () => [...document.querySelectorAll("button,[role='button']")].filter((el) => visible(el) && !disabled(el));
  const clickExact = (labels) => {{
    const target = buttons().find((el) => labels.includes(text(el)));
    if (!target) return "";
    click(target);
    return text(target);
  }};
  const clickContains = (labels) => {{
    const target = buttons().find((el) => labels.some((label) => text(el).includes(label)));
    if (!target) return "";
    click(target);
    return text(target);
  }};
  const clickByOptionKey = (key) => {{
    const normalized = String(key || "").trim().toUpperCase();
    if (!normalized) return "";
    const candidates = buttons().filter((el) => String(el.className || "").includes("option-button"));
    const target = candidates.find((el) => {{
      const optionText = text(el).toUpperCase();
      return optionText === normalized
        || optionText.startsWith(`${{normalized}} `)
        || optionText.startsWith(`${{normalized}}.`)
        || optionText.startsWith(`${{normalized}}．`)
        || optionText.startsWith(`${{normalized}}、`)
        || optionText.startsWith(`${{normalized}})`);
    }});
    if (!target) return "";
    click(target);
    return text(target);
  }};
  const reactFiber = (el) => {{
    if (!el) return null;
    const key = Object.keys(el).find((item) => item.startsWith("__reactFiber$") || item.startsWith("__reactInternalInstance$"));
    return key ? el[key] : null;
  }};
  const atomValue = (value) => {{
    if (!value) return value;
    if (value.v !== undefined) return value.v;
    if (value.value !== undefined) return value.value;
    try {{
      if (typeof value.peek === "function") return value.peek();
    }} catch (_err) {{}}
    return value;
  }};
  const findQuestionStore = () => {{
    const roots = [
      document.activeElement,
      ...document.querySelectorAll("#right-answer-area,.question-main-content,.choice-question-view,button,[data-container-id],textarea,input,[contenteditable='true'],[role='textbox']"),
    ].filter(Boolean);
    const seen = new Set();
    for (const root of roots) {{
      let fiber = reactFiber(root);
      let depth = 0;
      while (fiber && depth < 90) {{
        if (!seen.has(fiber)) {{
          seen.add(fiber);
          const props = fiber.memoizedProps || fiber.pendingProps || {{}};
          if (props.questionStore) return props.questionStore;
        }}
        fiber = fiber.return;
        depth += 1;
      }}
    }}
    return null;
  }};
  const answerRows = (answer) => {{
    const rows = answer?.answerOptionMatrix || (answer?.answerOptionList ? [answer.answerOptionList] : []);
    return rows.map((row) => Array.isArray(row) ? row[0] : row).filter(Boolean);
  }};
  const answerValue = (option) => String(option?.optionVal || option?.optionKey || "").replace(/&nbsp;/g, " ").trim();
  const answerKey = (option) => String(option?.optionKey || "").trim();
  const clickNext = () => clickExact(["继续提交", "下一空", "下一问", "下一题", "提交", "提交自评", "查看报告", "继续"]);
  const ensurePlausibleAnswerDuration = (minMs = 6500) => {{
    const store = findQuestionStore();
    const timerState = store?.timerState;
    if (!timerState || !timerState.value) return null;
    const current = Number(timerState.value.currentTime || 0);
    const total = Number(timerState.value.totalTime || 0);
    const currentQuestionTotal = Number(store?.homeworkProgress?.currentQuestionTotal || 0);
    const nextCurrent = Math.max(current, currentQuestionTotal + minMs, minMs);
    timerState.value = {{
      ...timerState.value,
      currentTime: nextCurrent,
      totalTime: Math.max(total, nextCurrent),
    }};
    return {{ before: current, after: nextCurrent, currentQuestionTotal }};
  }};
  const textFields = () => [
    ...document.querySelectorAll("textarea.keyboard-input-textarea, textarea, input:not([type='hidden']), [contenteditable='true'], [role='textbox']")
  ].filter((el) => visible(el) && !disabled(el));
  const setFieldValue = (el, value) => {{
    if ("value" in el) {{
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
      if (descriptor?.set) descriptor.set.call(el, value);
      else el.value = value;
    }} else {{
      el.textContent = value;
    }}
    const inputEvent = typeof InputEvent === "function"
      ? new InputEvent("input", {{ bubbles: true, cancelable: true, data: value, inputType: "insertText" }})
      : new Event("input", {{ bubbles: true, cancelable: true }});
    el.dispatchEvent(inputEvent);
    el.dispatchEvent(new Event("change", {{ bubbles: true, cancelable: true }}));
  }};
  const selfEvaluateAsCorrect = () => {{
    const body = document.body.innerText || "";
    const hasSelfEval = body.includes("提交自评") || buttons().some((el) => text(el).includes("提交自评"));
    if (!hasSelfEval) return "";
    const clickedChoice = clickExact(["我答对了"]) || clickContains(["我答对了"]) || clickAtRatio(0.585, 0.565);
    const timer = ensurePlausibleAnswerDuration();
    const clickedSubmit = clickExact(["提交自评", "查看报告", "继续"]) || clickContains(["提交自评", "查看报告", "继续"]);
    return clickedChoice || clickedSubmit ? `${{clickedChoice}} -> ${{clickedSubmit}} timer=${{timer?.after || ""}}` : "";
  }};
  for (let i = 0; i < config.maxSteps; i += 1) {{
    const guard = clickExact(["我知道了", "知道了"]);
    if (guard) {{
      logs.push({{ i, action: "dismiss", clicked: guard }});
      await sleep(config.stepWaitMs);
      continue;
    }}
    const confirmSubmit = clickExact(["继续提交"]);
    if (confirmSubmit) {{
      const timer = ensurePlausibleAnswerDuration();
      logs.push({{ i, action: "confirm-submit", clicked: confirmSubmit, timer }});
      await sleep(config.stepWaitMs);
      continue;
    }}
    const selfEval = selfEvaluateAsCorrect();
    if (selfEval) {{
      logs.push({{ i, action: "self-evaluate", clicked: selfEval }});
      await sleep(config.stepWaitMs);
      continue;
    }}
    const reportButton = buttons().find((el) => text(el) === "查看报告");
    if (reportButton) {{
      logs.push({{ i, action: "report", clicked: text(reportButton) }});
      if (config.clickReport) setTimeout(() => click(reportButton), 0);
      return {{
        ok: true,
        completed: true,
        reportScheduled: Boolean(config.clickReport),
        logs,
        text: document.body.innerText.slice(0, 800),
      }};
    }}
    const continued = clickExact(["继续"]);
    if (continued) {{
      logs.push({{ i, action: "continue", clicked: continued }});
      await sleep(config.stepWaitMs);
      continue;
    }}
    const store = findQuestionStore();
    if (!store) {{
      logs.push({{ i, action: "no-store", text: document.body.innerText.slice(0, 300) }});
      break;
    }}
    const question = atomValue(store.currentQuestion) || {{}};
    const questionId = question.questionId || question.questionContent?.questionId || question.id;
    const status = atomValue(store.questionStatus);
    if (status === "submitted") {{
      logs.push({{ i, questionId, action: "submitted" }});
      await sleep(Math.max(300, Math.floor(config.stepWaitMs / 2)));
      continue;
    }}
    const answers = answerRows(question.questionAnswer).map((option) => ({{
      key: answerKey(option),
      value: answerValue(option),
    }}));
    const currentAnswers = atomValue(store.currentQuestionAnswers);
    const activeBlank = Number(atomValue(store.activeBlankIndex));
    const answerIndex = Number.isFinite(activeBlank) && activeBlank >= 0
      ? Math.min(activeBlank, Math.max(answers.length - 1, 0))
      : 0;
    const currentAnswer = answers[answerIndex] || answers[0] || {{}};
    let selected = "";
    if (currentAnswer.key) {{
      selected = clickByOptionKey(currentAnswer.key);
    }} else if (currentAnswer.value === "true") {{
      selected = clickByOptionKey("TRUE") || clickExact(["正确"]);
    }} else if (currentAnswer.value === "false") {{
      selected = clickByOptionKey("FALSE") || clickExact(["错误"]);
    }} else {{
      const checked = buttons().find((el) => String(el.className || "").includes("option-button") && text(el).includes("✅"));
      if (checked) {{
        click(checked);
        selected = text(checked);
      }}
    }}
    if (selected) {{
      await sleep(Math.max(220, Math.floor(config.stepWaitMs * 0.55)));
      const timer = ensurePlausibleAnswerDuration();
      const next = clickNext();
      logs.push({{ i, questionId, action: "choice", answer: currentAnswer, activeBlank: answerIndex, selected, next, timer }});
      await sleep(config.stepWaitMs);
      continue;
    }}
    const field = textFields()[0];
    if (field && !answers.length) {{
      setFieldValue(field, config.openAnswerText);
      field.blur?.();
      await sleep(Math.max(160, Math.floor(config.stepWaitMs / 2)));
      const timer = ensurePlausibleAnswerDuration();
      const submit = clickExact(["提交"]);
      logs.push({{ i, questionId, action: "open-answer", chars: config.openAnswerText.length, submit, timer }});
      await sleep(config.stepWaitMs);
      continue;
    }}
    if (questionId && answers.length && typeof store.updateUserAnswer === "function") {{
      const count = Math.max(answers.length, Array.isArray(currentAnswers) ? currentAnswers.length : 1);
      for (let blankIndex = 0; blankIndex < count; blankIndex += 1) {{
        const item = answers[Math.min(blankIndex, answers.length - 1)] || {{}};
        const content = item.value || item.key || "";
        try {{
          store.updateUserAnswer({{ questionId, blankIndex, type: 1, content, selfEvaluation: 1 }});
        }} catch (_err) {{}}
        if (typeof store.updateUserAnswerType === "function") {{
          try {{ store.updateUserAnswerType(questionId, 1); }} catch (_err) {{}}
        }}
      }}
      await sleep(Math.max(250, Math.floor(config.stepWaitMs / 3)));
      const clicks = [];
      for (let blankIndex = 0; blankIndex < Math.max(1, count); blankIndex += 1) {{
        const timer = ensurePlausibleAnswerDuration();
        const next = clickNext();
        if (!next) break;
        clicks.push({{ next, timer }});
        await sleep(config.stepWaitMs);
        if (["提交", "下一问", "下一题"].includes(next)) break;
      }}
      logs.push({{ i, questionId, action: "fill", answers, clicks }});
      continue;
    }}
    const fallback = clickContains(["提交", "下一空", "下一问", "下一题", "继续"]);
    logs.push({{ i, questionId, action: "fallback", fallback, buttons: buttons().map(text).slice(0, 20) }});
    if (!fallback) break;
    await sleep(config.stepWaitMs);
  }}
  return {{
    ok: true,
    completed: false,
    logs,
    text: document.body.innerText.slice(0, 800),
    buttons: buttons().map((el) => ({{ text: text(el), disabled: disabled(el) }})).slice(0, 20),
  }};
}})()
"""


def tool_xiaoluxue_exercise_snapshot(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    page = xiaoluxue_exercise_page(serial)
    snapshot = cdp_eval_value(page, xiaoluxue_exercise_snapshot_expression(), timeout=min(float(args.get("timeout_sec", 10)), 60))
    return [text_content({"ok": True, "serial": serial, "pageId": page.get("id"), "socket": page.get("socket"), "snapshot": snapshot})]


def tool_xiaoluxue_exercise_action(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    page = xiaoluxue_exercise_page(serial)
    result = cdp_eval_value(page, xiaoluxue_exercise_action_expression(args), timeout=min(float(args.get("timeout_sec", 10)), 60))
    recorded_args = {
        key: args[key]
        for key in ("action_name", "option_key", "option_index", "option_text", "answer_text", "button_text")
        if key in args
    }
    append_recording_step(
        serial,
        "xiaoluxue_exercise_action",
        recorded_args,
        {"ok": True, "action": "xiaoluxue_exercise_action", "webview": True, "result": result},
    )
    return [text_content({"ok": True, "serial": serial, "pageId": page.get("id"), "socket": page.get("socket"), "result": result})]


def run_xiaoluxue_exercise_fast_path(serial: str, args: dict[str, Any], *, record: bool) -> dict[str, Any]:
    timeout = min(float(args.get("timeout_sec", 15)), 60)
    wait_sec = min(max(float(args.get("after_action_wait_sec", 0.4)), 0), 10)
    page = xiaoluxue_exercise_page(serial)
    steps: list[dict[str, Any]] = []
    action_name = str(args.get("action_name") or ("button_text" if args.get("button_text") else "next"))

    has_answer_text = args.get("answer_text") is not None
    has_option = any(args.get(key) is not None and str(args.get(key)).strip() for key in ("option_key", "option_index", "option_text"))
    if action_name == "auto_answer":
        auto_result = cdp_eval_value(page, xiaoluxue_exercise_auto_answer_expression(args), timeout=timeout)
        steps.append({"action": "auto_answer", "result": auto_result})
        if wait_sec:
            time.sleep(wait_sec)
    elif has_answer_text:
        answer_args = {**args, "action_name": "fill_answer"}
        answer_result = cdp_eval_value(page, xiaoluxue_exercise_action_expression(answer_args), timeout=timeout)
        steps.append({"action": "fill_answer", "result": answer_result})
        if wait_sec:
            time.sleep(wait_sec)

    if has_option:
        page = xiaoluxue_exercise_page(serial)
        select_args = {**args, "action_name": "select_option"}
        select_result = cdp_eval_value(page, xiaoluxue_exercise_action_expression(select_args), timeout=timeout)
        steps.append({"action": "select_option", "result": select_result})
        if wait_sec:
            time.sleep(wait_sec)

    if bool(args.get("submit", False)):
        page = xiaoluxue_exercise_page(serial)
        submit_result = cdp_eval_value(page, xiaoluxue_exercise_action_expression({"action_name": "submit"}), timeout=timeout)
        steps.append({"action": "submit", "result": submit_result})
        if wait_sec:
            time.sleep(wait_sec)
        if bool(args.get("continue_after_submit", False)):
            page = xiaoluxue_exercise_page(serial)
            continue_result = cdp_eval_value(page, xiaoluxue_exercise_action_expression({"action_name": "continue"}), timeout=timeout)
            steps.append({"action": "continue", "result": continue_result})
    elif not has_option and not has_answer_text:
        action_args = {**args, "action_name": action_name}
        action_result_value = cdp_eval_value(page, xiaoluxue_exercise_action_expression(action_args), timeout=timeout)
        steps.append({"action": action_name, "result": action_result_value})

    snapshot: dict[str, Any] | None = None
    page_error: str | None = None
    try:
        page = xiaoluxue_exercise_page(serial)
        snapshot = cdp_eval_value(page, xiaoluxue_exercise_snapshot_expression(), timeout=timeout)
    except AndroidUseError as exc:
        page_error = str(exc)
    result = {
        "ok": True,
        "action": "xiaoluxue_exercise_fast_path",
        "webview": True,
        "pageId": page.get("id") if snapshot is not None else None,
        "socket": page.get("socket") if snapshot is not None else None,
        "steps": steps,
        "snapshot": snapshot,
    }
    if page_error:
        result["pageError"] = page_error
    if record:
        recorded_args = {
            key: args[key]
            for key in (
                "option_key",
                "option_index",
                "option_text",
                "answer_text",
                "submit",
                "continue_after_submit",
                "action_name",
                "button_text",
                "after_action_wait_sec",
                "timeout_sec",
                "max_steps",
                "step_wait_sec",
                "click_report",
            )
            if key in args
        }
        append_recording_step(serial, "xiaoluxue_exercise_fast_path", recorded_args, result)
    return result


def tool_xiaoluxue_exercise_fast_path(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    result = run_xiaoluxue_exercise_fast_path(serial, args, record=True)
    return [text_content({"ok": True, "serial": serial, **result})]


def tool_start_recording(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    if active_recording(serial):
        raise AndroidUseError(f"Recording is already active for device {serial}. Stop it first.")
    name = str(args.get("name") or f"recording-{serial}").strip()
    recording_id = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime())}-{slugify(name, 'recording')}"
    recording_dir = RECORDINGS_DIR / recording_id
    recording_dir.mkdir(parents=True, exist_ok=True)
    recording = {
        "schema_version": 1,
        "id": recording_id,
        "name": name,
        "serial": serial,
        "created_at": timestamp_iso(),
        "dir": str(recording_dir),
        "include_screenshots": bool(args.get("include_screenshots", False)),
        "redact_text": bool(args.get("redact_text", False)),
        "after_delay_sec": float(args.get("after_delay_sec", 0.25)),
        "steps": [],
        "errors": [],
        "initial_snapshot": capture_record_snapshot(
            serial,
            include_screenshot=bool(args.get("include_screenshots", False)),
            base_dir=recording_dir,
            name="000-initial",
        ),
    }
    ACTIVE_RECORDINGS[serial] = recording
    trace_path = recording_dir / "trace.json"
    return [
        text_content(
            {
                "ok": True,
                "serial": serial,
                "recording_id": recording_id,
                "trace_path": str(trace_path),
                "message": "Recording started. Deterministic Android actions issued through this plugin will be captured.",
            }
        )
    ]


def tool_record_checkpoint(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    label = str(args.get("label") or "checkpoint").strip()
    step = append_recording_checkpoint(serial, label)
    return [
        text_content(
            {
                "ok": True,
                "serial": serial,
                "index": step["index"],
                "label": label,
                "fingerprint": step["snapshot"].get("fingerprint"),
            }
        )
    ]


def tool_stop_recording(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    recording = ACTIVE_RECORDINGS.pop(serial, None)
    if not recording:
        raise AndroidUseError(f"No active recording for device {serial}.")
    recording["ended_at"] = timestamp_iso()
    recording["final_snapshot"] = capture_record_snapshot(
        serial,
        include_screenshot=bool(recording.get("include_screenshots")),
        base_dir=Path(recording["dir"]),
        name="999-final",
    )
    trace_path = Path(recording["dir"]) / "trace.json"
    write_json(trace_path, recording)
    return [
        text_content(
            {
                "ok": True,
                "serial": serial,
                "recording_id": recording["id"],
                "trace_path": str(trace_path),
                "steps": len(recording.get("steps", [])),
                "errors": recording.get("errors", []),
            }
        )
    ]


def resolve_trace_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_dir():
        trace_path = path / "trace.json"
        if trace_path.exists():
            return trace_path
    if path.exists():
        return path
    candidates = [
        RECORDINGS_DIR / value / "trace.json",
        RECORDINGS_DIR / f"{value}.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise AndroidUseError(f"Recording trace not found: {value}")


def resolve_recipe_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.exists():
        return path
    candidates = [RECIPES_DIR / value, RECIPES_DIR / f"{value}.json"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise AndroidUseError(f"Recipe not found: {value}")


def tool_create_recipe(args: dict[str, Any]) -> list[dict[str, Any]]:
    trace_path = resolve_trace_path(str(args["trace"]))
    trace = json.loads(trace_path.read_text())
    recipe_name = str(args.get("name") or trace.get("name") or trace_path.parent.name)
    recipe = recipe_from_trace(trace, recipe_name)
    output_path = Path(args["output_path"]).expanduser() if args.get("output_path") else RECIPES_DIR / f"{slugify(recipe_name, 'recipe')}.json"
    write_json(output_path, recipe)
    return [
        text_content(
            {
                "ok": True,
                "trace_path": str(trace_path),
                "recipe_path": str(output_path),
                "steps": len(recipe.get("steps", [])),
                "name": recipe.get("name"),
            }
        )
    ]


def tool_replay_recipe(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    recipe_path = resolve_recipe_path(str(args["recipe"]))
    recipe = json.loads(recipe_path.read_text())
    dry_run = bool(args.get("dry_run", False))
    strict_verify = bool(args.get("strict_verify", False))
    step_delay_sec = min(float(args.get("step_delay_sec", 0.25)), 5.0)
    results: list[dict[str, Any]] = []
    for index, step in enumerate(recipe.get("steps", []), start=1):
        if not isinstance(step, dict):
            continue
        step_result = execute_recipe_step(serial, step, dry_run=dry_run)
        verification = {"checked": False, "ok": True}
        if not dry_run:
            if step_delay_sec > 0:
                time.sleep(step_delay_sec)
            verification = verify_recipe_step(serial, step)
            if strict_verify and not verification.get("ok", True):
                results.append({"index": index, "result": step_result, "verification": verification})
                break
        results.append({"index": index, "result": step_result, "verification": verification})
    return [
        text_content(
            {
                "ok": all(item["verification"].get("ok", True) for item in results),
                "serial": serial,
                "recipe_path": str(recipe_path),
                "dry_run": dry_run,
                "steps": results,
            }
        )
    ]

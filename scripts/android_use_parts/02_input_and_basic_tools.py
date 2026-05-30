# Loaded by scripts/android_use_mcp.py. Keep this file below 2000 lines.

def keycode(value: str | int) -> str:
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    if text.isdigit():
        return text
    upper = text.upper().replace("KEYCODE_", "")
    return KEY_ALIASES.get(upper, f"KEYCODE_{upper}")


def escape_input_text(text: str) -> str:
    escaped = text.replace("%", "%25").replace(" ", "%s")
    for char in ['"', "'", "\\", "&", "<", ">", "(", ")", "|", ";", "*", "~", "`"]:
        escaped = escaped.replace(char, "\\" + char)
    return escaped


def current_input_method(serial: str) -> str:
    try:
        return shell(serial, "settings get secure default_input_method", timeout=3).strip()
    except AndroidUseError:
        return ""


def input_method_looks_like_adb_keyboard(value: str) -> bool:
    normalized = value.casefold()
    return "adbkeyboard" in normalized or "adb_keyboard" in normalized or "adbime" in normalized


def fast_input_ime_enabled() -> bool:
    return env_flag("ANDROID_USE_FAST_INPUT_IME", True)


def restore_ime_after_fast_input() -> bool:
    return env_flag("ANDROID_USE_RESTORE_IME_AFTER_TYPE", False)


def fast_input_min_chars() -> int:
    raw = os.environ.get("ANDROID_USE_FAST_INPUT_MIN_CHARS", "24")
    try:
        return max(0, int(raw))
    except ValueError:
        return 24


def text_needs_unicode_input(text: str) -> bool:
    return any(ord(char) > 0x7F for char in text)


def should_try_fast_ime(text: str, *, clear_first: bool = False) -> bool:
    if not fast_input_ime_enabled():
        return False
    if not text and not clear_first:
        return False
    return text_needs_unicode_input(text) or "\n" in text or len(text) >= fast_input_min_chars()


def webview_direct_input_enabled() -> bool:
    return env_flag("ANDROID_USE_WEBVIEW_DIRECT_INPUT", True)


def webview_direct_input_expression(
    text: str,
    *,
    clear_first: bool = False,
    enter: bool = False,
    prefer_answer_box: bool = False,
) -> str:
    config_json = json.dumps(
        {
            "text": text,
            "clearFirst": clear_first,
            "enter": enter,
            "preferAnswerBox": prefer_answer_box,
        },
        ensure_ascii=False,
    )
    return r"""
(async () => {
  const config = __CONFIG__;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height, top: r.top, right: r.right, bottom: r.bottom, left: r.left };
  };
  const visible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && style.visibility !== "hidden" && style.display !== "none";
  };
  const textOf = (el) => (el?.innerText || el?.textContent || "").trim().replace(/\s+/g, " ");
  const editable = (el) => {
    if (!el) return false;
    const tag = String(el.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || el.isContentEditable || el.getAttribute?.("contenteditable") === "true";
  };
  const reactFiber = (el) => {
    if (!el) return null;
    const key = Object.keys(el).find((key) => key.startsWith("__reactFiber$") || key.startsWith("__reactInternalInstance$"));
    return key ? el[key] : null;
  };
  const findReactFunctionProp = (root, propName) => {
    const seen = new Set();
    const elements = [];
    if (root) elements.push(root);
    if (root?.querySelectorAll) elements.push(...root.querySelectorAll("*"));
    elements.push(document.activeElement);
    for (const startEl of elements.filter(Boolean)) {
      let fiber = reactFiber(startEl);
      let depth = 0;
      while (fiber && depth < 50) {
        if (!seen.has(fiber)) {
          seen.add(fiber);
          const props = fiber.memoizedProps || fiber.pendingProps || {};
          if (typeof props[propName] === "function") return { fiber, props, element: startEl };
        }
        fiber = fiber.return;
        depth += 1;
      }
    }
    return null;
  };
  const setNativeValue = (el, value) => {
    if (el.isContentEditable || el.getAttribute?.("contenteditable") === "true") {
      el.focus();
      el.textContent = value;
      el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
    el.focus();
    if (descriptor?.set) descriptor.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  };
  const dispatchEnter = (el) => {
    if (!config.enter || !el) return;
    for (const type of ["keydown", "keypress", "keyup"]) {
      el.dispatchEvent(new KeyboardEvent(type, { bubbles: true, cancelable: true, key: "Enter", code: "Enter", keyCode: 13, which: 13 }));
    }
  };
  const answerRoots = [
    ...document.querySelectorAll(".math-field-answer-box"),
    ...document.querySelectorAll("[class*='math-field'][class*='answer']"),
    ...document.querySelectorAll("[class*='answer-box']"),
  ].filter(visible);
  const active = document.activeElement;
  const activeEditable = editable(active) ? active : null;
  const activeRoot = active?.closest?.(".math-field-answer-box,[class*='math-field'][class*='answer'],[class*='answer-box']") || null;
  const answerRoot = (activeRoot && visible(activeRoot)) ? activeRoot : answerRoots[0];
  const root = config.preferAnswerBox ? (answerRoot || activeEditable) : (activeEditable || answerRoot);
  if (!root) {
    return { ok: false, reason: "no focused DOM input or visible answer box", activeTag: active?.tagName || "" };
  }

  const latexTarget = findReactFunctionProp(root, "onLatexChange");
  if (latexTarget) {
    const current = typeof latexTarget.props.content === "string" ? latexTarget.props.content : "";
    const value = config.clearFirst ? String(config.text) : current + String(config.text);
    latexTarget.props.onLatexChange(value);
    dispatchEnter(root);
    await sleep(80);
    const box = answerRoot || root;
    return { ok: true, method: "react_onLatexChange", target: "math_answer_box", chars: String(config.text).length, value, renderedText: textOf(box).slice(0, 500), rect: box ? rect(box) : null };
  }

  const field = editable(root) ? root : root.querySelector?.("textarea,input,[contenteditable='true']");
  if (field) {
    const current = field.isContentEditable ? textOf(field) : String(field.value || "");
    const value = config.clearFirst ? String(config.text) : current + String(config.text);
    setNativeValue(field, value);
    dispatchEnter(field);
    await sleep(80);
    return { ok: true, method: "dom_value", target: String(field.tagName || "").toLowerCase(), chars: String(config.text).length, value, renderedText: textOf(root).slice(0, 500), rect: rect(field) };
  }

  const changeTarget = findReactFunctionProp(root, "onChange");
  if (changeTarget) {
    const value = String(config.text);
    changeTarget.props.onChange({ target: { value }, currentTarget: { value } });
    dispatchEnter(root);
    await sleep(80);
    return { ok: true, method: "react_onChange", target: "react_input", chars: value.length, value, renderedText: textOf(root).slice(0, 500), rect: rect(root) };
  }
  return { ok: false, reason: "DOM input target not writable", activeTag: active?.tagName || "", rootText: textOf(root).slice(0, 120) };
})()
""".replace("__CONFIG__", config_json)


def candidate_webview_pages_for_input(serial: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in (
        XIAOLUXUE_PAGE_CACHE.get((serial, "exercise")),
        XIAOLUXUE_PAGE_CACHE.get((serial, "course")),
        XIAOLUXUE_PAGE_CACHE.get((serial, "any")),
    ):
        if page and page.get("webSocketDebuggerUrl"):
            key = str(page.get("id") or page.get("webSocketDebuggerUrl") or "")
            if key not in seen:
                candidates.append(page)
                seen.add(key)
    try:
        pages = discover_webview_pages(serial)
        page = select_webview_page(pages)
        key = str(page.get("id") or page.get("webSocketDebuggerUrl") or "")
        if key not in seen:
            candidates.append(page)
    except AndroidUseError:
        pass
    return candidates


def type_webview_text_fast(
    serial: str,
    text: str,
    *,
    clear_first: bool = False,
    enter: bool = False,
    prefer_answer_box: bool = False,
) -> str | None:
    for page in candidate_webview_pages_for_input(serial):
        expression = webview_direct_input_expression(
            text,
            clear_first=clear_first,
            enter=enter,
            prefer_answer_box=prefer_answer_box or xiaoluxue_url_kind(str(page.get("url") or "")) == "exercise",
        )
        try:
            result = cdp_eval_value(page, expression, timeout=3)
        except AndroidUseError:
            continue
        if isinstance(result, dict) and result.get("ok"):
            return f"webview_dom_{result.get('method') or 'direct'}"
    return None


def list_input_methods(serial: str) -> list[str]:
    try:
        output = shell(serial, "ime list -s", timeout=5)
    except AndroidUseError:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def find_adb_keyboard_ime(serial: str) -> str | None:
    configured = os.environ.get("ANDROID_USE_ADB_KEYBOARD_IME")
    if configured:
        return configured
    for ime in list_input_methods(serial):
        if input_method_looks_like_adb_keyboard(ime):
            return ime
    return None


def set_input_method(serial: str, ime: str) -> None:
    shell(serial, f"ime set {shlex.quote(ime)} >/dev/null", timeout=5)


def adb_keyboard_broadcast_text(
    serial: str,
    text: str,
    *,
    clear_first: bool = False,
    enter: bool = False,
) -> bool:
    if not input_method_looks_like_adb_keyboard(current_input_method(serial)):
        return False
    commands: list[str] = []
    if clear_first:
        commands.append("am broadcast -a ADB_CLEAR_TEXT >/dev/null")
    if text:
        commands.append(f"am broadcast -a ADB_INPUT_TEXT --es msg {shlex.quote(text)} >/dev/null")
    if enter:
        commands.append("am broadcast -a ADB_INPUT_KEYCODE --ei code 66 >/dev/null")
    if commands:
        shell(serial, " && ".join(commands), timeout=8)
    return True


def switch_to_adb_keyboard_and_broadcast_text(
    serial: str,
    text: str,
    *,
    clear_first: bool = False,
    enter: bool = False,
) -> str | None:
    original_ime = current_input_method(serial)
    if input_method_looks_like_adb_keyboard(original_ime):
        if adb_keyboard_broadcast_text(serial, text, clear_first=clear_first, enter=enter):
            return "adb_keyboard_broadcast"
        return None

    ime = find_adb_keyboard_ime(serial)
    if not ime:
        return None

    set_input_method(serial, ime)
    try:
        if not adb_keyboard_broadcast_text(serial, text, clear_first=clear_first, enter=enter):
            return None
    finally:
        if original_ime and restore_ime_after_fast_input() and original_ime != ime:
            with contextlib.suppress(AndroidUseError):
                set_input_method(serial, original_ime)
    return "adb_keyboard_switch_restore" if restore_ime_after_fast_input() else "adb_keyboard_switch"


def input_keyevent_list(command: str, count: int) -> str | None:
    if count <= 0:
        return None
    return f"input keyevent {' '.join([command] * count)}"


def adb_shell_batch_type_text(
    serial: str,
    text: str,
    *,
    clear_first: bool = False,
    clear_count: int = 80,
    enter: bool = False,
) -> None:
    commands: list[str] = []
    if clear_first:
        commands.append("input keyevent KEYCODE_MOVE_END")
        delete_command = input_keyevent_list("KEYCODE_DEL", max(0, clear_count))
        if delete_command:
            commands.append(delete_command)
    if text:
        commands.append(f"input text {shlex.quote(escape_input_text(text))}")
    if enter:
        commands.append("input keyevent KEYCODE_ENTER")
    if commands:
        shell(serial, " && ".join(commands), timeout=15)


def type_focused_text_fast(
    serial: str,
    text: str,
    *,
    clear_first: bool = False,
    clear_count: int = 80,
    enter: bool = False,
) -> str:
    if webview_direct_input_enabled():
        with contextlib.suppress(AndroidUseError):
            method = type_webview_text_fast(
                serial,
                text,
                clear_first=clear_first,
                enter=enter,
            )
            if method:
                return method
    if should_try_fast_ime(text, clear_first=clear_first):
        with contextlib.suppress(AndroidUseError):
            method = switch_to_adb_keyboard_and_broadcast_text(
                serial,
                text,
                clear_first=clear_first,
                enter=enter,
            )
            if method:
                return method
    else:
        with contextlib.suppress(AndroidUseError):
            if adb_keyboard_broadcast_text(serial, text, clear_first=clear_first, enter=enter):
                return "adb_keyboard_broadcast"
    adb_shell_batch_type_text(
        serial,
        text,
        clear_first=clear_first,
        clear_count=clear_count,
        enter=enter,
    )
    return "adb_shell_batch"


def action_result(action: str, serial: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True, "action": action, "serial": serial}
    if extra:
        payload.update(extra)
    return payload


def text_content(payload: Any) -> dict[str, str]:
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, indent=2, ensure_ascii=False)
    return {"type": "text", "text": text}


def image_content(png: bytes) -> dict[str, str]:
    return {
        "type": "image",
        "data": base64.b64encode(png).decode("ascii"),
        "mimeType": "image/png",
    }


def check_dependencies(_args: dict[str, Any]) -> list[dict[str, Any]]:
    adb_path = shutil.which(adb_binary()) or adb_binary()
    scrcpy_path = shutil.which(scrcpy_binary()) or scrcpy_binary()
    adb_available = shutil.which(adb_binary()) is not None or Path(adb_binary()).exists()
    scrcpy_available = shutil.which(scrcpy_binary()) is not None or Path(scrcpy_binary()).exists()
    wireless_host, wireless_port, wireless_serial = wireless_config_from_env()
    wireless_configs = wireless_configs_from_env()
    payload: dict[str, Any] = {
        "ok": adb_available and scrcpy_available,
        "adb": {
            "command": adb_binary(),
            "path": adb_path,
            "available": adb_available,
            "required": True,
        },
        "scrcpy": {
            "command": scrcpy_binary(),
            "path": scrcpy_path,
            "available": scrcpy_available,
            "required": True,
            "install_hint": "brew install scrcpy",
        },
        "vlm": {
            "provider": os.environ.get("ANDROID_USE_AGENT_PROVIDER", "openai-compatible"),
            "base_url_configured": bool(os.environ.get("ANDROID_USE_VLM_BASE_URL")),
            "api_key_configured": bool(os.environ.get("ANDROID_USE_VLM_API_KEY")),
            "model": os.environ.get("ANDROID_USE_VLM_MODEL"),
            "coordinate_mode": infer_coordinate_mode(os.environ.get("ANDROID_USE_VLM_MODEL")),
            "timeout_sec": float(os.environ.get("ANDROID_USE_VLM_TIMEOUT", "45")),
        },
        "wireless": {
            "auto_connect": env_flag("ANDROID_USE_WIRELESS_AUTO_CONNECT", True),
            "host": wireless_host,
            "port": wireless_port,
            "serial": wireless_serial,
            "devices": wireless_configs,
            "configured_count": len(wireless_configs),
            "env_file": str(USER_ENV_FILE),
            "configured": bool(wireless_configs),
        },
        "openai": {
            "api_key_configured": bool(os.environ.get("ANDROID_USE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")),
            "base_url": os.environ.get("ANDROID_USE_OPENAI_BASE_URL", OPENAI_BASE_URL),
            "computer_model": os.environ.get("ANDROID_USE_OPENAI_COMPUTER_MODEL", "gpt-5.5"),
            "vision_model": os.environ.get("ANDROID_USE_OPENAI_VISION_MODEL", os.environ.get("ANDROID_USE_OPENAI_MODEL", "gpt-5.5")),
        },
    }
    try:
        stdout, stderr = run_command([adb_binary(), "version"], timeout=5)
        version_text = decode_bytes(stdout or stderr)
        payload["adb"]["version"] = version_text.splitlines()[0] if version_text else "unknown"
    except AndroidUseError as exc:
        payload["adb"]["error"] = str(exc)
    try:
        stdout, stderr = run_command([scrcpy_binary(), "--version"], timeout=5)
        version_text = decode_bytes(stdout or stderr)
        payload["scrcpy"]["version"] = version_text.splitlines()[0] if version_text else "unknown"
    except AndroidUseError as exc:
        payload["scrcpy"]["error"] = str(exc)
    if adb_available:
        try:
            devices = list_devices()
            connected = [device for device in devices if device.get("state") == "device"]
            payload["devices"] = {
                "items": devices,
                "connected_count": len(connected),
                "authorized": bool(connected),
            }
            if not connected:
                payload["connection_help"] = android_connection_help(devices)
        except AndroidUseError as exc:
            payload["devices"] = {"error": str(exc)}
    return [text_content(payload)]


def tool_list_devices(args: dict[str, Any]) -> list[dict[str, Any]]:
    include_details = bool(args.get("include_details", True))
    devices = list_devices()
    if include_details:
        for device in devices:
            if device.get("state") == "device":
                try:
                    device["state_details"] = device_state(str(device["serial"]))
                except AndroidUseError as exc:
                    device["state_error"] = str(exc)
    payload: dict[str, Any] = {"devices": devices}
    if not [device for device in devices if device.get("state") == "device"]:
        payload["connection_help"] = android_connection_help(devices)
    return [text_content(payload)]


def tool_wireless_pair(args: dict[str, Any]) -> list[dict[str, Any]]:
    host = str(args.get("host") or "").strip()
    pair_port = int(args.get("pair_port") or 0)
    code = str(args.get("code") or "").strip()
    connect_port = int(args.get("connect_port") or 0) or None
    save = bool(args.get("save", True))
    start_scrcpy = bool(args.get("start_scrcpy", True))
    if not host:
        raise AndroidUseError("host is required, for example 172.27.31.51")
    if pair_port <= 0:
        raise AndroidUseError("pair_port is required. Use the port shown beside the pairing code.")
    if not code:
        raise AndroidUseError("code is required. It is the temporary Wireless debugging pairing code.")

    target = f"{host}:{pair_port}"
    stdout, stderr = run_command([adb_binary(), "pair", target, code], timeout=30)
    pair_output = "\n".join(part for part in [decode_bytes(stdout), decode_bytes(stderr)] if part)
    reconnect_result = wireless_reconnect(
        host=host,
        port=connect_port,
        save=save,
        start_scrcpy=start_scrcpy,
    )
    return [
        text_content(
            {
                "ok": True,
                "paired": True,
                "pair_target": target,
                "pair_output": pair_output,
                "reconnect": reconnect_result,
                "env_file": str(USER_ENV_FILE) if save else None,
            }
        )
    ]


def tool_wireless_reconnect(args: dict[str, Any]) -> list[dict[str, Any]]:
    host = str(args.get("host") or "").strip() or None
    port = int(args.get("port") or 0) or None
    serial = str(args.get("serial") or "").strip() or None
    save = bool(args.get("save", True))
    start_scrcpy = bool(args.get("start_scrcpy", True))
    if bool(args.get("all", False)):
        result = wireless_reconnect_all(save=save, start_scrcpy=start_scrcpy)
        return [text_content({"ok": True, **result, "env_file": str(USER_ENV_FILE) if save else None})]
    result = wireless_reconnect(host=host, port=port, serial=serial, save=save, start_scrcpy=start_scrcpy)
    return [text_content({"ok": True, **result, "env_file": str(USER_ENV_FILE) if save else None})]


def tool_get_state(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    payload = device_state(serial)
    include_screenshot = bool(args.get("include_screenshot", False))
    content = [text_content(payload)]
    if include_screenshot:
        png = screenshot_png(serial)
        content.append(image_content(png))
    return content


def tool_screenshot(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    png = screenshot_png(serial)
    size = png_size(png)
    save_path = save_png(serial, png, args.get("save_path") if args.get("save_path") else None)
    return [
        text_content(
            {
                "serial": serial,
                "path": save_path,
                "mime_type": "image/png",
                "bytes": len(png),
                "screen": size,
            }
        ),
        image_content(png),
    ]


def tool_show_screen(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    png = screenshot_png(serial)
    size = png_size(png)
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    save_path = save_png(serial, png, str(SCREEN_DIR / "latest.png"))
    return [
        text_content(
            {
                "serial": serial,
                "path": save_path,
                "mime_type": "image/png",
                "bytes": len(png),
                "screen": size,
                "display": "Current Android screen image is attached in this tool result.",
            }
        ),
        image_content(png),
    ]


def appshot_save_paths(serial: str, save_dir: str | None = None) -> tuple[Path, Path]:
    if save_dir:
        directory = Path(save_dir).expanduser()
    else:
        directory = SCREEN_DIR / "appshots"
    safe_serial = slugify(serial, default="device")
    stem = f"appshot-{safe_serial}-{int(time.time() * 1000)}"
    return directory / f"{stem}.json", directory / f"{stem}.png"


def tool_appshot(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    include_xml = bool(args.get("include_xml", False))
    include_image = bool(args.get("include_image", True))
    strict_ui = bool(args.get("strict_ui", False))
    save = bool(args.get("save", True))
    limit = max(20, min(int(args.get("limit", 220)), 500))

    captured_at = timestamp_iso()
    png = screenshot_png(serial)
    screenshot = {
        "mime_type": "image/png",
        "bytes": len(png),
        "screen": png_size(png),
    }

    try:
        observation = observe_ui(serial, include_xml=include_xml, limit=limit)
        state = observation.get("state") or device_state(serial)
        ui = observation.get("ui", {})
        ui_error = None
    except Exception as exc:
        if strict_ui:
            raise
        try:
            state = device_state(serial)
        except Exception as state_exc:
            state = {"serial": serial, "error": str(state_exc)}
        ui = {"nodes": [], "count": 0}
        ui_error = str(exc)

    payload: dict[str, Any] = {
        "ok": True,
        "kind": "android_appshot",
        "version": 1,
        "serial": serial,
        "captured_at": captured_at,
        "state": state,
        "screenshot": screenshot,
        "ui": ui,
        "image_attached": include_image,
        "display": "Android appshot includes a screenshot plus device state and UI tree for Codex evidence.",
    }
    if ui_error:
        payload["ui_error"] = ui_error

    if save:
        json_path, png_path = appshot_save_paths(serial, args.get("save_dir") if args.get("save_dir") else None)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        png_path.write_bytes(png)
        payload["paths"] = {
            "json": str(json_path),
            "png": str(png_path),
        }
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    content = [text_content(payload)]
    if include_image:
        content.append(image_content(png))
    return content


def tool_observe(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    include_screenshot = bool(args.get("include_screenshot", False))
    include_xml = bool(args.get("include_xml", False))
    limit = max(20, min(int(args.get("limit", 160)), 500))
    observation = observe_ui(serial, include_xml=include_xml, limit=limit)
    content = [text_content(observation)]
    if include_screenshot:
        png = screenshot_png(serial)
        content.append(image_content(png))
    return content


def tool_tap_text(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    query = str(args["text"]).strip()
    exact = bool(args.get("exact", True))
    include_resource_id = bool(args.get("include_resource_id", False))
    observation = observe_ui(serial, limit=300)
    before = snapshot_from_observation(observation) if active_recording(serial) else None
    nodes = observation["ui"]["nodes"]
    node = find_ui_node(nodes, query, exact=exact, include_resource_id=include_resource_id)
    if not node and exact:
        node = find_ui_node(nodes, query, exact=False, include_resource_id=include_resource_id)
    point = node_click_point(node) if node else None
    if not point:
        raise AndroidUseError(f"Could not find a tappable UI node matching text: {query!r}")
    adb(["shell", "input", "tap", str(point["x"]), str(point["y"])], serial=serial, timeout=10)
    payload = action_result(
        "tap_text",
        serial,
        {"text": query, "x": point["x"], "y": point["y"], "matched_node": compact_node(node)},
    )
    append_recording_step(
        serial,
        "tap_text",
        {"text": query, "exact": exact, "include_resource_id": include_resource_id},
        payload,
        before=before,
    )
    return [text_content(payload)]


def tool_tap(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    x = int(args["x"])
    y = int(args["y"])
    before = capture_record_snapshot(serial) if active_recording(serial) else None
    adb(["shell", "input", "tap", str(x), str(y)], serial=serial, timeout=10)
    payload = action_result("tap", serial, {"x": x, "y": y})
    append_recording_step(serial, "tap", {"x": x, "y": y}, payload, before=before)
    return [text_content(payload)]


def tool_swipe(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    start_x = int(args["start_x"])
    start_y = int(args["start_y"])
    end_x = int(args["end_x"])
    end_y = int(args["end_y"])
    duration_ms = int(args.get("duration_ms", 300))
    before = capture_record_snapshot(serial) if active_recording(serial) else None
    adb(
        [
            "shell",
            "input",
            "swipe",
            str(start_x),
            str(start_y),
            str(end_x),
            str(end_y),
            str(duration_ms),
        ],
        serial=serial,
        timeout=10,
    )
    action_args = {
        "start_x": start_x,
        "start_y": start_y,
        "end_x": end_x,
        "end_y": end_y,
        "duration_ms": duration_ms,
    }
    payload = action_result("swipe", serial, action_args)
    append_recording_step(serial, "swipe", action_args, payload, before=before)
    return [text_content(payload)]


def tool_type_text(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    text = str(args["text"])
    before = capture_record_snapshot(serial) if active_recording(serial) else None
    clear_first = bool(args.get("clear_first"))
    clear_count = int(args.get("clear_count", 80))
    enter = bool(args.get("enter"))
    method = type_focused_text_fast(
        serial,
        text,
        clear_first=clear_first,
        clear_count=clear_count,
        enter=enter,
    )
    action_args = {
        "text": text,
        "clear_first": clear_first,
        "clear_count": clear_count,
        "enter": enter,
    }
    payload = action_result(
        "type_text",
        serial,
        {
            "chars": len(text),
            "clear_first": action_args["clear_first"],
            "enter": action_args["enter"],
            "method": method,
        },
    )
    append_recording_step(serial, "type_text", action_args, payload, before=before)
    return [text_content(payload)]


def tool_press_key(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    key = keycode(args["key"])
    before = capture_record_snapshot(serial) if active_recording(serial) else None
    adb(["shell", "input", "keyevent", key], serial=serial, timeout=10)
    payload = action_result("press_key", serial, {"key": key})
    append_recording_step(serial, "press_key", {"key": key}, payload, before=before)
    return [text_content(payload)]


def tool_wake_unlock(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    before = capture_record_snapshot(serial) if active_recording(serial) else None
    adb(["shell", "input", "keyevent", "KEYCODE_WAKEUP"], serial=serial, timeout=10)
    if args.get("dismiss_keyguard", True):
        adb(["shell", "wm", "dismiss-keyguard"], serial=serial, timeout=10)
    action_args = {"dismiss_keyguard": bool(args.get("dismiss_keyguard", True))}
    payload = action_result("wake_unlock", serial, action_args)
    append_recording_step(serial, "wake_unlock", action_args, payload, before=before)
    return [text_content(payload)]


def tool_open_url(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    url = str(args["url"]).strip()
    if not url:
        raise AndroidUseError("url must not be empty.")
    before = capture_record_snapshot(serial) if active_recording(serial) else None
    if is_xiaoluxue_app_only_url(url):
        route_result = xiaoluxue_route_app_url(serial, url)
        payload = action_result("open_url", serial, {"url": url, "routed": route_result})
        append_recording_step(serial, "open_url", {"url": url, "xiaoluxue_app_route": True}, payload, before=before)
        return [text_content(payload)]
    adb(
        ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url],
        serial=serial,
        timeout=15,
    )
    payload = action_result("open_url", serial, {"url": url})
    append_recording_step(serial, "open_url", {"url": url}, payload, before=before)
    return [text_content(payload)]


def tool_open_app(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    package = str(args["package"]).strip()
    activity = str(args.get("activity", "")).strip()
    if not package:
        raise AndroidUseError("package must not be empty.")
    before = capture_record_snapshot(serial) if active_recording(serial) else None
    if activity:
        component = activity if "/" in activity else f"{package}/{activity}"
        adb(["shell", "am", "start", "-n", component], serial=serial, timeout=15)
        payload = action_result("open_app", serial, {"package": package, "activity": activity})
        append_recording_step(serial, "open_app", {"package": package, "activity": activity}, payload, before=before)
        return [text_content(payload)]

    adb(
        ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
        serial=serial,
        timeout=15,
    )
    payload = action_result("open_app", serial, {"package": package})
    append_recording_step(serial, "open_app", {"package": package}, payload, before=before)
    return [text_content(payload)]


def tool_shell(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    timeout = min(float(args.get("timeout_sec", 20)), 120)
    command = str(args["command"])
    stdout = shell(serial, command, timeout=timeout)
    return [text_content({"serial": serial, "command": command, "stdout": stdout})]


def tool_webview_pages(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    port = int(args["port"]) if args.get("port") is not None else None
    pages = discover_webview_pages(serial, port=port)
    return [text_content({"ok": True, "serial": serial, "pages": pages})]


def tool_webview_eval(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    pages = discover_webview_pages(serial)
    page = select_webview_page(
        pages,
        page_id=str(args.get("page_id") or "") or None,
        url_contains=str(args.get("url_contains") or "") or None,
        title_contains=str(args.get("title_contains") or "") or None,
    )
    timeout = min(float(args.get("timeout_sec", 10)), 60)
    evaluation = cdp_runtime_evaluate(
        str(page["webSocketDebuggerUrl"]),
        str(args["expression"]),
        await_promise=bool(args.get("await_promise", True)),
        return_by_value=bool(args.get("return_by_value", True)),
        timeout=timeout,
    )
    return [
        text_content(
            {
                "ok": True,
                "serial": serial,
                "page": {
                    "id": page.get("id"),
                    "title": page.get("title"),
                    "url": page.get("url"),
                    "socket": page.get("socket"),
                    "forward": page.get("forward"),
                },
                "result": evaluation,
            }
        )
    ]


XIAOLUXUE_SITE_URL_MARKER = "stu.xiaoluxue.com"
XIAOLUXUE_COURSE_URL_MARKER = "stu.xiaoluxue.com/course"
XIAOLUXUE_STUDENT_PACKAGE = "com.xiaoluxue.ai.student"
XIAOLUXUE_STUDENT_LAUNCHER_COMPONENT = "com.xiaoluxue.ai.student/com.xiaoluxue.ai.student.LauncherActivity"
XIAOLUXUE_CONFIG_PACKAGE = "com.xiaoluxue.ai.config"
XIAOLUXUE_CONFIG_LAUNCHER_COMPONENT = "com.xiaoluxue.ai.config/com.xiaoluxue.ai.config.LauncherActivity"
XIAOLUXUE_LOGIN_ACTIVITY = "com.xiaoluxue.ai.business.account.ui.LoginActivity"
XIAOLUXUE_STUDY_SUBJECT_ACTIVITY = "com.xiaoluxue.ai.business.launcher.study.subject.StudySubjectActivity"
XIAOLUXUE_LESSON_ACTIVITY = "com.xiaoluxue.ai.business.lesson.LessonActivity"
XIAOLUXUE_SCHEME_PROXY_ACTIVITY = "com.xiaoluxue.ai.infra.framework.router.SchemeProxyActivity"
XIAOLUXUE_STANDARD_BROWSER_ACTIVITY = "com.xiaoluxue.ai.infra.browser.vessel.StandardBrowserActivity"
XIAOLUXUE_LEAK_ACTIVITY_MARKER = "leakcanary.internal.activity"
XIAOLUXUE_VESSEL_WEBVIEW_ROUTE = "xlx://router/vessel/webview"
XIAOLUXUE_STUDY_SUBJECT_ROUTE = "xlx://router/study/subject"
XIAOLUXUE_GW_ORIGIN = "https://gw-stu.xiaoluxue.com"
XIAOLUXUE_NATIVE_BASE_WIDTH = 2000
XIAOLUXUE_NATIVE_BASE_HEIGHT = 1200
XIAOLUXUE_NATIVE_MATH_CARD = (690, 280)
XIAOLUXUE_NATIVE_GUIDE_BUBBLE = (770, 505)
XIAOLUXUE_NATIVE_CONTINUE_BUTTON = (780, 815)
XIAOLUXUE_NATIVE_PROGRESS_POPUP_CLOSE = (1515, 422)
XIAOLUXUE_NATIVE_MAP_CACHE_PATH = SOURCE_MAP_DIR / "xiaoluxue-native-map-cache.json"
XIAOLUXUE_NATIVE_MAP_CACHE: dict[str, Any] = {}
XIAOLUXUE_SUBJECT_ALIASES: dict[str, int] = {
    "语文": 1,
    "中文": 1,
    "数学": 2,
    "英语": 3,
    "英文": 3,
    "物理": 4,
    "化学": 5,
    "生物": 6,
}
XIAOLUXUE_NATIVE_MAP_ROUTE_PRESETS: dict[int, dict[str, dict[str, tuple[int, int]]]] = {
    1: {
        "1.5": {
            "index": (1508, 251),
            "practise": (1116, 401),
            "expand": (1314, 593),
            "wrong": (926, 820),
            "notebook": (1074, 820),
            "report": (1000, 708),
        }
    }
}
XIAOLUXUE_MAP_MODULE_ENTRY_ACTIONS = {"practise", "expand"}
XIAOLUXUE_NATIVE_SELECTED_MODULE_POINTS: dict[str, tuple[int, int]] = {
    "practise": (1000, 392),
    "expand": (1198, 501),
}
XIAOLUXUE_NATIVE_MODULE_CARD_ENTER_OFFSET_Y = 273
XIAOLUXUE_NATIVE_EXPAND_CONFIRM_ENTER = (1312, 854)
XIAOLUXUE_NATIVE_DIRECT_PRACTICE_ENTER = (468, 936)
XIAOLUXUE_NATIVE_CURRENT_CARD_DIRECT_PRACTICE_ENTER = (955, 936)
XIAOLUXUE_NATIVE_RIGHT_CARD_DIRECT_PRACTICE_ENTER = (1432, 936)
XIAOLUXUE_NATIVE_ANSWER_CONTINUE = (1845, 1108)
XIAOLUXUE_NATIVE_RESULT_FINISH = (1160, 856)
XIAOLUXUE_NATIVE_TRANSITION_START = (1000, 1055)
ANDROID_ANIMATION_SCALE_SETTINGS = (
    "window_animation_scale",
    "transition_animation_scale",
    "animator_duration_scale",
)
XIAOLUXUE_MAP_FAST_KEYWORDS = (
    "地图",
    "题型突破",
    "题型",
    "突破",
    "专属精练",
    "专属练习",
    "专属",
    "精练",
    "专练",
    "巩固练习",
    "巩固",
    "错题",
    "笔记",
    "笔记本",
    "学习任务",
    "任务",
    "薄弱知识",
    "薄弱",
    "看报告",
    "报告",
)
XIAOLUXUE_CONFIG_URL_PATTERN = re.compile(r"https://gw-stu[^\s，,;]+")
XIAOLUXUE_APP_ONLY_HOSTS = {"stu.xiaoluxue.com"}
XIAOLUXUE_APP_ONLY_SUFFIXES = (".xiaoluxue.cn",)
XIAOLUXUE_ENV_CHOICES: dict[str, dict[str, str]] = {
    "prod": {"label": "生产环境-com", "url": "https://gw-stu.xiaoluxue.com"},
    "prod-com": {"label": "生产环境-com", "url": "https://gw-stu.xiaoluxue.com"},
    "production": {"label": "生产环境-com", "url": "https://gw-stu.xiaoluxue.com"},
    "dev": {"label": "Dev环境", "url": "https://gw-stu.dev.xiaoluxue.cn/"},
    "test": {"label": "Test环境", "url": "https://gw-stu.test.xiaoluxue.cn/"},
    "test2": {"label": "Test2环境", "url": "https://gw-stu.test2.xiaoluxue.cn/"},
    "test3": {"label": "Test3环境", "url": "https://gw-stu.test3.xiaoluxue.cn/"},
    "test4": {"label": "Test4环境", "url": "https://gw-stu.test4.xiaoluxue.cn/"},
    "test5": {"label": "Test5环境", "url": "https://gw-stu.test5.xiaoluxue.cn/"},
    "test6": {"label": "Test6环境", "url": "https://gw-stu.test6.xiaoluxue.cn/"},
    "kmtest": {"label": "Kmtest环境", "url": "https://gw-stu.kmtest.xiaoluxue.cn/"},
}

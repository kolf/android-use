# Loaded by scripts/android_use_mcp.py. Keep this file below 2000 lines.

def get_prop(serial: str, prop: str) -> str | None:
    try:
        value = shell(serial, f"getprop {prop}", timeout=5).strip()
        return value or None
    except AndroidUseError:
        return None


def parse_screen_size(text: str) -> dict[str, int | None]:
    matches = re.findall(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", text)
    if not matches:
        return {"width": None, "height": None}
    width, height = matches[-1]
    return {"width": int(width), "height": int(height)}


def get_screen_size(serial: str) -> dict[str, int | None]:
    try:
        return parse_screen_size(shell(serial, "wm size", timeout=5))
    except AndroidUseError:
        return {"width": None, "height": None}


def parse_bounds(bounds: str | None) -> dict[str, int] | None:
    if not bounds:
        return None
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds.strip())
    if not match:
        return None
    left, top, right, bottom = (int(part) for part in match.groups())
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def bounds_center(bounds: dict[str, int] | None) -> dict[str, int] | None:
    if not bounds:
        return None
    return {
        "x": round((bounds["left"] + bounds["right"]) / 2),
        "y": round((bounds["top"] + bounds["bottom"]) / 2),
    }


def bool_attr(value: str | None) -> bool:
    return str(value or "").lower() == "true"


def extract_hierarchy_xml(raw: str) -> str:
    start = raw.find("<hierarchy")
    end = raw.rfind("</hierarchy>")
    if start < 0 or end < 0:
        raise AndroidUseError(f"uiautomator did not return hierarchy XML: {raw[:500]}")
    return raw[start : end + len("</hierarchy>")]


def dump_ui_xml(serial: str) -> str:
    try:
        adb(["shell", "uiautomator", "dump", REMOTE_UI_DUMP_PATH], serial=serial, timeout=12)
        raw = decode_bytes(adb(["exec-out", "cat", REMOTE_UI_DUMP_PATH], serial=serial, timeout=8))
        return extract_hierarchy_xml(raw)
    except AndroidUseError:
        # Some builds can write the hierarchy directly to stdout via /dev/tty.
        raw = shell(serial, "uiautomator dump /dev/tty", timeout=12)
        return extract_hierarchy_xml(raw)


def parse_ui_nodes(xml_text: str, *, limit: int = 300) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise AndroidUseError(f"Failed to parse uiautomator XML: {exc}") from exc

    nodes: list[dict[str, Any]] = []

    def walk(element: ET.Element, parent_click_target: dict[str, Any] | None = None, depth: int = 0) -> None:
        if len(nodes) >= limit:
            return
        attrs = element.attrib
        bounds = parse_bounds(attrs.get("bounds"))
        clickable = bool_attr(attrs.get("clickable"))
        long_clickable = bool_attr(attrs.get("long-clickable"))
        label_parts = [
            attrs.get("text") or "",
            attrs.get("content-desc") or "",
            attrs.get("resource-id") or "",
        ]
        click_target = None
        if bounds and (clickable or long_clickable):
            click_target = {"bounds": bounds, "center": bounds_center(bounds)}
        elif parent_click_target:
            click_target = parent_click_target
        node = {
            "index": len(nodes),
            "text": attrs.get("text") or "",
            "content_desc": attrs.get("content-desc") or "",
            "resource_id": attrs.get("resource-id") or "",
            "class": attrs.get("class") or "",
            "package": attrs.get("package") or "",
            "bounds": bounds,
            "center": bounds_center(bounds),
            "clickable": clickable,
            "enabled": bool_attr(attrs.get("enabled")),
            "checkable": bool_attr(attrs.get("checkable")),
            "checked": bool_attr(attrs.get("checked")),
            "selected": bool_attr(attrs.get("selected")),
            "focused": bool_attr(attrs.get("focused")),
            "long_clickable": long_clickable,
            "depth": depth,
        }
        if click_target:
            node["click_target"] = click_target
        if any(part.strip() for part in label_parts) or clickable or long_clickable:
            nodes.append(node)
        next_click_target = click_target or parent_click_target
        for child in element:
            walk(child, next_click_target, depth + 1)

    walk(root)
    return nodes


def observe_ui(serial: str, *, include_xml: bool = False, limit: int = 160) -> dict[str, Any]:
    xml_text = dump_ui_xml(serial)
    nodes = parse_ui_nodes(xml_text, limit=limit)
    observation: dict[str, Any] = {
        "state": device_state(serial),
        "ui": {
            "nodes": nodes,
            "count": len(nodes),
        },
    }
    if include_xml:
        observation["ui"]["xml"] = xml_text
    return observation


def node_labels(node: dict[str, Any]) -> list[str]:
    labels = []
    for key in ("text", "content_desc", "resource_id"):
        value = str(node.get(key) or "").strip()
        if value:
            labels.append(value)
    return labels


def node_click_point(node: dict[str, Any]) -> dict[str, int] | None:
    click_target = node.get("click_target") or {}
    center = click_target.get("center") if isinstance(click_target, dict) else None
    if center:
        return {"x": int(center["x"]), "y": int(center["y"])}
    center = node.get("center")
    if center:
        return {"x": int(center["x"]), "y": int(center["y"])}
    return None


def find_ui_node(
    nodes: list[dict[str, Any]],
    query: str,
    *,
    exact: bool = True,
    include_resource_id: bool = False,
) -> dict[str, Any] | None:
    needle = query.strip().casefold()
    if not needle:
        return None
    candidates: list[tuple[int, dict[str, Any]]] = []
    for node in nodes:
        labels = node_labels(node)
        if not include_resource_id:
            labels = labels[:2]
        for label in labels:
            haystack = label.strip().casefold()
            if not haystack:
                continue
            if haystack == needle:
                candidates.append((0, node))
                break
            if not exact and needle in haystack:
                candidates.append((1, node))
                break
            if not exact and haystack in needle and len(haystack) >= 2:
                candidates.append((2, node))
                break
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1].get("depth", 0)))
    return candidates[0][1]


def quoted_phrases(text: str) -> list[str]:
    phrases = re.findall(r"[“\"'‘「『《](.*?)[”\"'’」』》]", text)
    phrases.extend(re.findall(r"‘(.*?)’", text))
    return [phrase.strip() for phrase in phrases if phrase.strip()]


def fast_ui_action_from_instruction(serial: str, instruction: str) -> dict[str, Any] | None:
    observation = observe_ui(serial, limit=220)
    nodes = observation["ui"]["nodes"]
    labels: list[str] = []
    for node in nodes:
        for label in node_labels(node)[:2]:
            label = label.strip()
            if label and len(label) >= 1 and label not in labels:
                labels.append(label)

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
        node = find_ui_node(nodes, candidate, exact=True) or find_ui_node(nodes, candidate, exact=False)
        point = node_click_point(node) if node else None
        if point:
            return {
                "action": "tap",
                "x": point["x"],
                "y": point["y"],
                "source": "uiautomator",
                "target": candidate,
                "matched_node": {
                    key: node.get(key)
                    for key in ("index", "text", "content_desc", "resource_id", "class", "bounds", "clickable")
                },
            }
    return None


def get_focused_window(serial: str) -> str | None:
    try:
        dump = shell(serial, "dumpsys window", timeout=8)
    except AndroidUseError:
        return None
    patterns = [
        r"mCurrentFocus=Window\{[^ ]+ [^ ]+ ([^}]+)\}",
        r"mFocusedApp=ActivityRecord\{[^ ]+ [^ ]+ ([^}]+)\}",
    ]
    for pattern in patterns:
        match = re.search(pattern, dump)
        if match:
            return match.group(1)
    return None


def device_state(serial: str) -> dict[str, Any]:
    return {
        "serial": serial,
        "model": get_prop(serial, "ro.product.model"),
        "manufacturer": get_prop(serial, "ro.product.manufacturer"),
        "android_version": get_prop(serial, "ro.build.version.release"),
        "sdk": get_prop(serial, "ro.build.version.sdk"),
        "screen": get_screen_size(serial),
        "focused_window": get_focused_window(serial),
    }


def screenshot_png(serial: str) -> bytes:
    png = adb(["exec-out", "screencap", "-p"], serial=serial, timeout=20)
    return normalize_png_bytes(png)


def normalize_png_bytes(png: bytes) -> bytes:
    if not png.startswith(b"\x89PNG"):
        fixed = png.replace(b"\r\n", b"\n")
        if fixed.startswith(b"\x89PNG"):
            png = fixed
    if not png.startswith(b"\x89PNG"):
        raise AndroidUseError("adb screencap did not return a PNG image.")
    return png


def png_size(png: bytes) -> dict[str, int | None]:
    if len(png) >= 24 and png.startswith(b"\x89PNG"):
        width, height = struct.unpack(">II", png[16:24])
        return {"width": int(width), "height": int(height)}
    return {"width": None, "height": None}


def screenshot_raw(serial: str) -> bytes:
    return adb(["exec-out", "screencap"], serial=serial, timeout=12)


def raw_screenshot_frame(raw: bytes) -> dict[str, Any]:
    if len(raw) < 16:
        return {"ok": False, "error": "raw screenshot too small"}
    width, height, pixel_format = struct.unpack("<III", raw[:12])
    if width <= 0 or height <= 0:
        return {"ok": False, "error": "invalid raw screenshot size"}
    expected_bytes = int(width) * int(height) * 4
    header_len = 16 if len(raw) >= 16 + expected_bytes else 12
    if len(raw) < header_len + expected_bytes:
        return {
            "ok": False,
            "width": int(width),
            "height": int(height),
            "format": int(pixel_format),
            "error": "raw screenshot payload truncated",
        }
    return {
        "ok": True,
        "width": int(width),
        "height": int(height),
        "format": int(pixel_format),
        "data_offset": header_len,
        "data": raw,
    }


def raw_screenshot_content_stats(raw: bytes) -> dict[str, Any]:
    frame = raw_screenshot_frame(raw)
    if not frame.get("ok"):
        return {"ready": False, **{key: value for key, value in frame.items() if key not in {"ok", "data"}}}
    width = int(frame["width"])
    height = int(frame["height"])
    pixel_format = int(frame["format"])
    data = frame["data"]
    data_offset = int(frame["data_offset"])
    x_step = max(int(width) // 80, 1)
    y_step = max(int(height) // 48, 1)
    y_start = min(max(int(height * 0.10), 0), int(height) - 1)
    y_end = min(max(int(height * 0.96), y_start + 1), int(height))
    sample_count = 0
    non_white_count = 0
    blue_count = 0
    dark_count = 0
    for y in range(y_start, y_end, y_step):
        row_offset = data_offset + y * int(width) * 4
        for x in range(0, int(width), x_step):
            offset = row_offset + x * 4
            r, g, b = data[offset], data[offset + 1], data[offset + 2]
            sample_count += 1
            if min(r, g, b) < 240:
                non_white_count += 1
            if b > 150 and g > 90 and r < 210:
                blue_count += 1
            if max(r, g, b) < 96:
                dark_count += 1

    if not sample_count:
        return {"ready": False, "error": "no screenshot samples"}
    non_white_ratio = non_white_count / sample_count
    blue_ratio = blue_count / sample_count
    dark_ratio = dark_count / sample_count
    ready = non_white_ratio >= 0.035 or blue_ratio >= 0.006 or dark_ratio >= 0.010
    return {
        "ready": bool(ready),
        "width": int(width),
        "height": int(height),
        "format": int(pixel_format),
        "samples": sample_count,
        "non_white_ratio": round(non_white_ratio, 4),
        "blue_ratio": round(blue_ratio, 4),
        "dark_ratio": round(dark_ratio, 4),
    }


def save_png(serial: str, png: bytes, save_path: str | None = None) -> str:
    if save_path:
        path = Path(save_path).expanduser()
    else:
        safe_serial = re.sub(r"[^A-Za-z0-9_.-]+", "-", serial)
        path = TMP_DIR / f"{safe_serial}-{int(time.time() * 1000)}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return str(path)


def timestamp_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slugify(value: str, default: str = "android") -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._")
    return slug[:80] or default


def lock_path(name: str) -> Path:
    return SCREEN_DIR / f"{slugify(name, 'lock')}.lock"


def scrcpy_user_closed_path(serial: str) -> Path:
    return SCREEN_DIR / f"scrcpy-user-closed-{slugify(serial)}.json"


def read_scrcpy_user_closed(serial: str) -> dict[str, Any] | None:
    path = scrcpy_user_closed_path(serial)
    if not path.exists():
        return None
    payload = read_json_file(path)
    return payload or {"path": str(path)}


def clear_scrcpy_user_closed(serial: str) -> None:
    with contextlib.suppress(OSError):
        scrcpy_user_closed_path(serial).unlink()


@contextlib.contextmanager
def exclusive_file_lock(path: Path, *, blocking: bool = True) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    locked = False
    try:
        try:
            fcntl.flock(handle.fileno(), flags)
            locked = True
            yield True
        except BlockingIOError:
            yield False
    finally:
        if locked:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    tmp_path.replace(path)


def visible_labels(nodes: list[dict[str, Any]], limit: int = 24) -> list[str]:
    labels: list[str] = []
    for node in nodes:
        for key in ("text", "content_desc", "resource_id"):
            label = str(node.get(key) or "").strip()
            if label and label not in labels and len(label) <= 120:
                labels.append(label)
            if len(labels) >= limit:
                return labels
    return labels


def snapshot_fingerprint(snapshot: dict[str, Any]) -> dict[str, Any]:
    nodes = snapshot.get("ui", {}).get("nodes", [])
    return {
        "focused_window": snapshot.get("state", {}).get("focused_window"),
        "labels": visible_labels(nodes, limit=16) if isinstance(nodes, list) else [],
    }


def snapshot_from_observation(observation: dict[str, Any]) -> dict[str, Any]:
    snapshot = {
        "timestamp": timestamp_iso(),
        "state": observation.get("state", {}),
        "ui": observation.get("ui", {}),
    }
    snapshot["fingerprint"] = snapshot_fingerprint(snapshot)
    return snapshot


def capture_record_snapshot(
    serial: str,
    *,
    include_screenshot: bool = False,
    base_dir: Path | None = None,
    name: str = "snapshot",
) -> dict[str, Any]:
    try:
        snapshot = snapshot_from_observation(observe_ui(serial, include_xml=False, limit=320))
    except Exception as exc:
        snapshot = {"timestamp": timestamp_iso(), "error": str(exc)}
        try:
            snapshot["state"] = device_state(serial)
        except Exception:
            pass
        snapshot["fingerprint"] = snapshot_fingerprint(snapshot)
    if include_screenshot and base_dir is not None:
        try:
            png = screenshot_png(serial)
            screen_dir = base_dir / "screens"
            screen_dir.mkdir(parents=True, exist_ok=True)
            path = screen_dir / f"{slugify(name)}.png"
            path.write_bytes(png)
            snapshot["screenshot_path"] = str(path)
            snapshot["screenshot_size"] = png_size(png)
        except Exception as exc:
            snapshot["screenshot_error"] = str(exc)
    return snapshot


def compact_node(node: dict[str, Any] | None) -> dict[str, Any] | None:
    if not node:
        return None
    return {
        key: node.get(key)
        for key in ("index", "text", "content_desc", "resource_id", "class", "bounds", "center", "clickable")
        if node.get(key) not in (None, "")
    }


def selector_candidates_for_node(node: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not node:
        return []
    selectors: list[dict[str, Any]] = []
    resource_id = str(node.get("resource_id") or "").strip()
    content_desc = str(node.get("content_desc") or "").strip()
    text = str(node.get("text") or "").strip()
    if resource_id:
        selectors.append({"strategy": "resource_id", "value": resource_id})
    if content_desc:
        selectors.append({"strategy": "content_desc", "value": content_desc})
    if text:
        selectors.append({"strategy": "text", "value": text})
    return selectors


def point_in_bounds(bounds: dict[str, int] | None, x: int, y: int) -> bool:
    if not bounds:
        return False
    return bounds["left"] <= x <= bounds["right"] and bounds["top"] <= y <= bounds["bottom"]


def node_at_point(nodes: list[dict[str, Any]], x: int, y: int) -> dict[str, Any] | None:
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for node in nodes:
        bounds = node.get("bounds")
        if isinstance(bounds, dict) and point_in_bounds(bounds, x, y):
            area = max(1, (bounds["right"] - bounds["left"]) * (bounds["bottom"] - bounds["top"]))
            candidates.append((area, -int(node.get("depth", 0)), node))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def find_node_by_selector(nodes: list[dict[str, Any]], selector: dict[str, Any]) -> dict[str, Any] | None:
    strategy = str(selector.get("strategy", "")).strip()
    value = str(selector.get("value", "")).strip()
    if not strategy or not value:
        return None
    if strategy == "resource_id":
        for node in nodes:
            if str(node.get("resource_id") or "").strip() == value:
                return node
        return None
    if strategy == "content_desc":
        for node in nodes:
            if str(node.get("content_desc") or "").strip() == value:
                return node
        return None
    if strategy == "text":
        for node in nodes:
            if str(node.get("text") or "").strip() == value:
                return node
        return None
    if strategy == "text_contains":
        return find_ui_node(nodes, value, exact=False)
    return None


def coordinate_from_target(serial: str, target: dict[str, Any]) -> dict[str, int] | None:
    coordinate = target.get("coordinate")
    if not isinstance(coordinate, dict):
        return None
    x = coordinate.get("x")
    y = coordinate.get("y")
    if x is None or y is None:
        return None
    source_screen = coordinate.get("screen") if isinstance(coordinate.get("screen"), dict) else {}
    source_width = int(source_screen.get("width") or 0)
    source_height = int(source_screen.get("height") or 0)
    current = get_screen_size(serial)
    current_width = int(current.get("width") or 0)
    current_height = int(current.get("height") or 0)
    if source_width > 0 and source_height > 0 and current_width > 0 and current_height > 0:
        return {
            "x": round(int(x) * current_width / source_width),
            "y": round(int(y) * current_height / source_height),
        }
    return {"x": int(x), "y": int(y)}


def resolve_target_point(serial: str, target: dict[str, Any]) -> tuple[dict[str, int], dict[str, Any] | None]:
    observation = observe_ui(serial, limit=320)
    nodes = observation["ui"]["nodes"]
    selectors = target.get("selectors") if isinstance(target.get("selectors"), list) else []
    for selector in selectors:
        if isinstance(selector, dict):
            node = find_node_by_selector(nodes, selector)
            point = node_click_point(node) if node else None
            if point:
                return point, node
    point = coordinate_from_target(serial, target)
    if point:
        return point, None
    raise AndroidUseError(f"Could not resolve recipe target: {target}")


def action_target_from_record(record: dict[str, Any]) -> dict[str, Any]:
    args = record.get("arguments", {}) if isinstance(record.get("arguments"), dict) else {}
    result = record.get("result", {}) if isinstance(record.get("result"), dict) else {}
    before = record.get("before", {}) if isinstance(record.get("before"), dict) else {}
    nodes = before.get("ui", {}).get("nodes", [])
    matched_node = result.get("matched_node") if isinstance(result.get("matched_node"), dict) else None
    x = result.get("x", args.get("x"))
    y = result.get("y", args.get("y"))
    if matched_node is None and isinstance(nodes, list) and x is not None and y is not None:
        matched_node = node_at_point(nodes, int(x), int(y))
    screen = before.get("state", {}).get("screen") if isinstance(before.get("state"), dict) else {}
    target: dict[str, Any] = {
        "selectors": selector_candidates_for_node(matched_node),
        "matched_node": compact_node(matched_node),
    }
    if x is not None and y is not None:
        target["coordinate"] = {"x": int(x), "y": int(y), "screen": screen or {}}
    return target


def verify_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    fingerprint = snapshot.get("fingerprint") if isinstance(snapshot.get("fingerprint"), dict) else {}
    focused_window = fingerprint.get("focused_window")
    labels = fingerprint.get("labels") or []
    human_labels = [
        str(label)
        for label in labels
        if label and ":id/" not in str(label) and "/" not in str(label) and len(str(label)) <= 80
    ]
    return {
        "focused_window": focused_window,
        "labels_any": [] if focused_window else human_labels[:6],
    }


def recipe_from_trace(trace: dict[str, Any], recipe_name: str | None = None) -> dict[str, Any]:
    name = recipe_name or str(trace.get("name") or "android-recipe")
    recipe: dict[str, Any] = {
        "schema_version": 1,
        "name": name,
        "created_at": timestamp_iso(),
        "source_trace_id": trace.get("id"),
        "serial": trace.get("serial"),
        "steps": [],
    }
    for record in trace.get("steps", []):
        if not isinstance(record, dict) or record.get("kind") != "action":
            continue
        action = str(record.get("action", "")).strip()
        args = record.get("arguments", {}) if isinstance(record.get("arguments"), dict) else {}
        step: dict[str, Any] = {"action": action}
        if action in {"tap", "tap_text"}:
            step["target"] = action_target_from_record(record)
            if action == "tap_text" and args.get("text"):
                step["text"] = args.get("text")
        elif action == "swipe":
            step.update({key: args[key] for key in ("start_x", "start_y", "end_x", "end_y") if key in args})
            step["duration_ms"] = int(args.get("duration_ms", 300))
            before = record.get("before", {}) if isinstance(record.get("before"), dict) else {}
            step["screen"] = before.get("state", {}).get("screen", {})
        elif action == "type_text":
            if args.get("text_redacted"):
                step["text_redacted"] = True
            else:
                step["text"] = args.get("text", "")
            step["clear_first"] = bool(args.get("clear_first", False))
            step["clear_count"] = int(args.get("clear_count", 80))
            step["enter"] = bool(args.get("enter", False))
        elif action == "press_key":
            step["key"] = args.get("key")
        elif action == "open_url":
            step["url"] = args.get("url")
        elif action == "open_app":
            step["package"] = args.get("package")
            if args.get("activity"):
                step["activity"] = args.get("activity")
        elif action == "wake_unlock":
            step["dismiss_keyguard"] = bool(args.get("dismiss_keyguard", True))
        else:
            continue
        after = record.get("after") if isinstance(record.get("after"), dict) else {}
        if after:
            step["verify"] = verify_from_snapshot(after)
        recipe["steps"].append(step)
    return recipe


def replay_coordinate(step: dict[str, Any], key: str, serial: str) -> int:
    value = int(step[key])
    source_screen = step.get("screen") if isinstance(step.get("screen"), dict) else {}
    source_width = int(source_screen.get("width") or 0)
    source_height = int(source_screen.get("height") or 0)
    current = get_screen_size(serial)
    current_width = int(current.get("width") or 0)
    current_height = int(current.get("height") or 0)
    if key.endswith("_x") and source_width > 0 and current_width > 0:
        return round(value * current_width / source_width)
    if key.endswith("_y") and source_height > 0 and current_height > 0:
        return round(value * current_height / source_height)
    return value


def execute_recipe_step(serial: str, step: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    action = str(step.get("action", "")).strip()
    if dry_run:
        return {"ok": True, "action": action, "dry_run": True}
    if action in {"tap", "tap_text"}:
        point, node = resolve_target_point(serial, step.get("target", {}))
        adb(["shell", "input", "tap", str(point["x"]), str(point["y"])], serial=serial, timeout=10)
        return {"ok": True, "action": action, "point": point, "matched_node": compact_node(node)}
    if action == "swipe":
        start_x = replay_coordinate(step, "start_x", serial)
        start_y = replay_coordinate(step, "start_y", serial)
        end_x = replay_coordinate(step, "end_x", serial)
        end_y = replay_coordinate(step, "end_y", serial)
        duration_ms = int(step.get("duration_ms", 300))
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
        return {"ok": True, "action": "swipe"}
    if action == "type_text":
        if step.get("text_redacted"):
            raise AndroidUseError("Recipe text is redacted; edit the recipe and provide `text` before replay.")
        text = str(step.get("text", ""))
        method = type_focused_text_fast(
            serial,
            text,
            clear_first=bool(step.get("clear_first")),
            clear_count=int(step.get("clear_count", 80)),
            enter=bool(step.get("enter")),
        )
        return {"ok": True, "action": "type_text", "chars": len(text), "method": method}
    if action == "press_key":
        key = keycode(step["key"])
        adb(["shell", "input", "keyevent", key], serial=serial, timeout=10)
        return {"ok": True, "action": "press_key", "key": key}
    if action == "open_url":
        adb(["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", str(step["url"])], serial=serial, timeout=15)
        return {"ok": True, "action": "open_url", "url": step["url"]}
    if action == "open_app":
        package = str(step["package"])
        activity = str(step.get("activity", "")).strip()
        if activity:
            component = activity if "/" in activity else f"{package}/{activity}"
            adb(["shell", "am", "start", "-n", component], serial=serial, timeout=15)
        else:
            adb(["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"], serial=serial, timeout=15)
        return {"ok": True, "action": "open_app", "package": package, "activity": activity or None}
    if action == "wake_unlock":
        adb(["shell", "input", "keyevent", "KEYCODE_WAKEUP"], serial=serial, timeout=10)
        if step.get("dismiss_keyguard", True):
            adb(["shell", "wm", "dismiss-keyguard"], serial=serial, timeout=10)
        return {"ok": True, "action": "wake_unlock"}
    raise AndroidUseError(f"Unsupported recipe action: {action}")


def verify_recipe_step(serial: str, step: dict[str, Any]) -> dict[str, Any]:
    verify = step.get("verify") if isinstance(step.get("verify"), dict) else {}
    if not verify:
        return {"checked": False, "ok": True}
    snapshot = capture_record_snapshot(serial)
    fingerprint = snapshot.get("fingerprint", {})
    current_labels = set(fingerprint.get("labels") or [])
    expected_labels = [label for label in verify.get("labels_any", []) if label]
    matched_labels = [label for label in expected_labels if label in current_labels]
    expected_window = verify.get("focused_window")
    current_window = fingerprint.get("focused_window")
    window_ok = not expected_window or expected_window == current_window
    labels_ok = not expected_labels or bool(matched_labels)
    return {
        "checked": True,
        "ok": bool(window_ok and labels_ok),
        "window_ok": window_ok,
        "labels_ok": labels_ok,
        "matched_labels": matched_labels,
        "expected_window": expected_window,
        "current_window": current_window,
    }


def resolve_json_path(value: str, directory: Path, suffix: str = ".json") -> Path:
    path = Path(value).expanduser()
    if path.exists():
        return path
    candidate = directory / (value if value.endswith(suffix) else f"{value}{suffix}")
    if candidate.exists():
        return candidate
    raise AndroidUseError(f"JSON file not found: {value}")


SOURCE_EXTENSIONS = {".kt", ".java", ".xml", ".tsx", ".jsx", ".ts", ".js", ".dart"}


def extract_source_entry(path: Path, root: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None
    rel = str(path.relative_to(root))
    entry: dict[str, Any] = {"file": rel, "activities": [], "routes": [], "controls": []}
    for match in re.finditer(r"class\s+([A-Za-z0-9_]*Activity)\b", text):
        entry["activities"].append(match.group(1))
    for match in re.finditer(r"<activity[^>]+android:name=[\"']([^\"']+)[\"']", text):
        entry["activities"].append(match.group(1))
    for pattern in (r"composable\s*\(\s*[\"']([^\"']+)[\"']", r"route\s*=\s*[\"']([^\"']+)[\"']", r"android:path(?:Prefix|Pattern)?=[\"']([^\"']+)[\"']"):
        for match in re.finditer(pattern, text):
            entry["routes"].append(match.group(1))
    controls: list[dict[str, Any]] = []
    for match in re.finditer(r"android:id=[\"']@\+?id/([^\"']+)[\"']", text):
        controls.append({"kind": "id", "value": match.group(1)})
    for match in re.finditer(r"\bR\.id\.([A-Za-z0-9_]+)", text):
        controls.append({"kind": "id", "value": match.group(1)})
    for match in re.finditer(r"android:(?:text|hint|contentDescription)=[\"']([^\"']{1,120})[\"']", text):
        controls.append({"kind": "label", "value": match.group(1)})
    for match in re.finditer(r"\b(?:Text|Button|ClickableText)\s*\(\s*[\"']([^\"']{1,120})[\"']", text):
        controls.append({"kind": "label", "value": match.group(1)})
    for match in re.finditer(r"\b(?:contentDescription|testTag)\s*=\s*[\"']([^\"']{1,120})[\"']", text):
        controls.append({"kind": "semantic", "value": match.group(1)})
    for match in re.finditer(r"\b(?:testTag|contentDescription)\s*\(\s*[\"']([^\"']{1,120})[\"']\s*\)", text):
        controls.append({"kind": "semantic", "value": match.group(1)})
    seen_controls: set[tuple[str, str]] = set()
    for control in controls:
        key = (control["kind"], control["value"])
        if key not in seen_controls:
            entry["controls"].append(control)
            seen_controls.add(key)
    for key in ("activities", "routes"):
        entry[key] = sorted(set(entry[key]))
    if entry["activities"] or entry["routes"] or entry["controls"]:
        return entry
    return None


def index_source_tree(root: Path, *, max_files: int = 2000) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if not root.exists():
        raise AndroidUseError(f"source_path does not exist: {root}")
    if root.is_file():
        files = [root]
        scan_root = root.parent
    else:
        scan_root = root
        files = []
        for path in root.rglob("*"):
            if len(files) >= max_files:
                break
            if path.is_file() and path.suffix in SOURCE_EXTENSIONS:
                if any(part in {".git", "build", ".gradle", "node_modules", "Pods"} for part in path.parts):
                    continue
                files.append(path)
    entries = [entry for file_path in files if (entry := extract_source_entry(file_path, scan_root))]
    controls: list[dict[str, Any]] = []
    seen_controls: set[tuple[str, str]] = set()
    for entry in entries:
        for control in entry.get("controls", []):
            key = (control["kind"], control["value"])
            if key not in seen_controls:
                controls.append(control)
                seen_controls.add(key)
    return {
        "schema_version": 1,
        "generated_at": timestamp_iso(),
        "root": str(root),
        "files_scanned": len(files),
        "files_indexed": len(entries),
        "pages": entries,
        "controls": controls,
    }


def active_recording(serial: str) -> dict[str, Any] | None:
    return ACTIVE_RECORDINGS.get(serial)


def sanitize_action_arguments(action: str, args: dict[str, Any], recording: dict[str, Any]) -> dict[str, Any]:
    sanitized = {key: value for key, value in args.items() if key != "serial"}
    if action == "type_text" and recording.get("redact_text"):
        text = str(sanitized.pop("text", ""))
        sanitized["chars"] = len(text)
        sanitized["text_redacted"] = True
    return sanitized


def append_recording_step(
    serial: str,
    action: str,
    args: dict[str, Any],
    result: dict[str, Any],
    *,
    before: dict[str, Any] | None = None,
) -> None:
    timeline_hook = globals().get("append_screen_timeline_action")
    if callable(timeline_hook):
        with contextlib.suppress(Exception):
            timeline_hook(serial, action, args, result, before=before)
    recording = active_recording(serial)
    if not recording:
        return
    try:
        index = len(recording["steps"]) + 1
        if before is None:
            before = capture_record_snapshot(
                serial,
                include_screenshot=bool(recording.get("include_screenshots")),
                base_dir=Path(recording["dir"]),
                name=f"{index:03d}-before-{action}",
            )
        delay = float(recording.get("after_delay_sec", 0.25))
        if delay > 0:
            time.sleep(min(delay, 2.0))
        after = capture_record_snapshot(
            serial,
            include_screenshot=bool(recording.get("include_screenshots")),
            base_dir=Path(recording["dir"]),
            name=f"{index:03d}-after-{action}",
        )
        recording["steps"].append(
            {
                "kind": "action",
                "index": index,
                "timestamp": timestamp_iso(),
                "action": action,
                "arguments": sanitize_action_arguments(action, args, recording),
                "result": result,
                "before": before,
                "after": after,
            }
        )
    except Exception as exc:
        recording.setdefault("errors", []).append({"timestamp": timestamp_iso(), "action": action, "error": str(exc)})


def append_recording_checkpoint(serial: str, label: str) -> dict[str, Any]:
    recording = active_recording(serial)
    if not recording:
        raise AndroidUseError("No active recording for this device. Start one with android_start_recording first.")
    index = len(recording["steps"]) + 1
    snapshot = capture_record_snapshot(
        serial,
        include_screenshot=bool(recording.get("include_screenshots")),
        base_dir=Path(recording["dir"]),
        name=f"{index:03d}-checkpoint-{label}",
    )
    step = {
        "kind": "checkpoint",
        "index": index,
        "timestamp": timestamp_iso(),
        "label": label,
        "snapshot": snapshot,
    }
    recording["steps"].append(step)
    return step


def recording_trace_payload(recording: dict[str, Any], *, stopped_at: str | None = None) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "id": recording["id"],
        "name": recording["name"],
        "serial": recording["serial"],
        "started_at": recording["started_at"],
        "steps": recording.get("steps", []),
        "errors": recording.get("errors", []),
    }
    if stopped_at:
        payload["stopped_at"] = stopped_at
    return payload


def write_recording_trace(recording: dict[str, Any], *, stopped_at: str | None = None) -> Path:
    trace_path = Path(recording["trace_path"])
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(json.dumps(recording_trace_payload(recording, stopped_at=stopped_at), ensure_ascii=False, indent=2) + "\n")
    return trace_path


def tool_start_recording(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    if active_recording(serial):
        raise AndroidUseError("A recording is already active for this device. Stop it with android_stop_recording first.")
    name = slugify(str(args.get("name") or "android-recording"))
    recording_id = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime())}-{name}"
    directory = RECORDINGS_DIR / recording_id
    directory.mkdir(parents=True, exist_ok=True)
    recording = {
        "id": recording_id,
        "name": name,
        "serial": serial,
        "started_at": timestamp_iso(),
        "dir": str(directory),
        "trace_path": str(directory / "trace.json"),
        "include_screenshots": bool(args.get("include_screenshots", False)),
        "redact_text": bool(args.get("redact_text", False)),
        "after_delay_sec": float(args.get("after_delay_sec", 0.25)),
        "steps": [],
        "errors": [],
    }
    ACTIVE_RECORDINGS[serial] = recording
    write_recording_trace(recording)
    return [text_content({"ok": True, "recording": recording_trace_payload(recording), "trace_path": recording["trace_path"]})]


def tool_record_checkpoint(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    label = slugify(str(args.get("label") or "checkpoint"), default="checkpoint")
    checkpoint = append_recording_checkpoint(serial, label)
    recording = active_recording(serial)
    if recording:
        write_recording_trace(recording)
    return [text_content({"ok": True, "serial": serial, "checkpoint": checkpoint})]


def tool_stop_recording(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    recording = active_recording(serial)
    if not recording:
        raise AndroidUseError("No active recording for this device.")
    stopped_at = timestamp_iso()
    trace_path = write_recording_trace(recording, stopped_at=stopped_at)
    ACTIVE_RECORDINGS.pop(serial, None)
    return [
        text_content(
            {
                "ok": True,
                "serial": serial,
                "recording_id": recording["id"],
                "trace_path": str(trace_path),
                "steps": len(recording.get("steps", [])),
                "errors": recording.get("errors", []),
                "stopped_at": stopped_at,
            }
        )
    ]


def resolve_recording_trace_path(value: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise AndroidUseError("trace is required.")
    path = Path(raw).expanduser()
    if path.is_dir():
        path = path / "trace.json"
    if path.exists():
        return path
    candidate = RECORDINGS_DIR / raw / "trace.json"
    if candidate.exists():
        return candidate
    matches = sorted(RECORDINGS_DIR.glob(f"*{slugify(raw, default='trace')}*/trace.json"))
    if matches:
        return matches[-1]
    raise AndroidUseError(f"Recording trace not found: {raw}")


def resolve_recipe_path(value: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise AndroidUseError("recipe is required.")
    path = Path(raw).expanduser()
    if path.is_dir():
        path = path / "recipe.json"
    if path.exists():
        return path
    candidate = RECIPES_DIR / raw / "recipe.json"
    if candidate.exists():
        return candidate
    matches = sorted(RECIPES_DIR.glob(f"*{slugify(raw, default='recipe')}*/recipe.json"))
    if matches:
        return matches[-1]
    raise AndroidUseError(f"Recipe not found: {raw}")


def tool_create_recipe(args: dict[str, Any]) -> list[dict[str, Any]]:
    trace_path = resolve_recording_trace_path(str(args.get("trace") or ""))
    trace = json.loads(trace_path.read_text())
    name = slugify(str(args.get("name") or trace.get("name") or trace_path.parent.name), default="android-recipe")
    recipe = recipe_from_trace(trace, recipe_name=name)
    if args.get("output_path"):
        output_path = Path(str(args["output_path"])).expanduser()
    else:
        output_path = RECIPES_DIR / f"{time.strftime('%Y%m%d-%H%M%S', time.localtime())}-{name}" / "recipe.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(recipe, ensure_ascii=False, indent=2) + "\n")
    return [text_content({"ok": True, "recipe": recipe, "path": str(output_path), "steps": len(recipe.get("steps", []))})]


def tool_replay_recipe(args: dict[str, Any]) -> list[dict[str, Any]]:
    recipe_path = resolve_recipe_path(str(args.get("recipe") or ""))
    recipe = json.loads(recipe_path.read_text())
    serial = choose_serial(args.get("serial") or recipe.get("serial"))
    dry_run = bool(args.get("dry_run", False))
    strict_verify = bool(args.get("strict_verify", False))
    step_delay_sec = max(0.0, min(float(args.get("step_delay_sec", 0.25)), 5.0))
    results: list[dict[str, Any]] = []
    for index, step in enumerate(recipe.get("steps", []), start=1):
        if not isinstance(step, dict):
            continue
        result = execute_recipe_step(serial, step, dry_run=dry_run)
        verification = verify_recipe_step(serial, step) if not dry_run else {"checked": False, "ok": True}
        entry = {"index": index, "step": step, "result": result, "verify": verification}
        results.append(entry)
        if strict_verify and verification.get("checked") and not verification.get("ok"):
            raise AndroidUseError(f"Recipe verification failed at step {index}: {verification}")
        if step_delay_sec > 0 and index < len(recipe.get("steps", [])):
            time.sleep(step_delay_sec)
    return [
        text_content(
            {
                "ok": True,
                "serial": serial,
                "recipe": str(recipe_path),
                "dry_run": dry_run,
                "steps": len(results),
                "results": results,
            }
        )
    ]


KEY_ALIASES = {
    "BACK": "KEYCODE_BACK",
    "HOME": "KEYCODE_HOME",
    "ENTER": "KEYCODE_ENTER",
    "MENU": "KEYCODE_MENU",
    "POWER": "KEYCODE_POWER",
    "RECENTS": "KEYCODE_APP_SWITCH",
    "APP_SWITCH": "KEYCODE_APP_SWITCH",
    "VOLUME_UP": "KEYCODE_VOLUME_UP",
    "VOLUME_DOWN": "KEYCODE_VOLUME_DOWN",
    "DEL": "KEYCODE_DEL",
    "DELETE": "KEYCODE_DEL",
    "TAB": "KEYCODE_TAB",
    "ESCAPE": "KEYCODE_ESCAPE",
}

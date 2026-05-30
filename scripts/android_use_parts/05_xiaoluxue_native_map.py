# Loaded by scripts/android_use_mcp.py. Keep this file below 2000 lines.

def normalize_xiaoluxue_map_index(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    match = re.search(r"(?<!\d)(\d+(?:\.\d+)+)(?!\d)", raw)
    if match:
        return match.group(1)
    match = re.search(r"(?:地图|节点|课节|第)\s*(\d+)(?:\s*(?:节|课|章|单元))?", raw)
    return match.group(1) if match else None


def normalize_xiaoluxue_map_action(value: Any) -> str:
    raw = str(value or "").strip()
    lowered = raw.casefold()
    canonical = {
        "select",
        "practise",
        "expand",
        "wrong",
        "notebook",
        "report",
        "tasks",
        "weak",
        "chapter_picker",
        "done",
        "back",
    }
    if lowered in canonical:
        return lowered
    if lowered in {"practice", "practise_item", "challenge"}:
        return "practise"
    if lowered in {"expand_item", "expansion", "exclusive", "exclusive_practice", "special", "special_practice", "train", "training"}:
        return "expand"
    if lowered in {"task"}:
        return "tasks"
    if lowered in {"chapter", "textbook", "picker"}:
        return "chapter_picker"
    if any(term in raw for term in ("错题", "错题本")):
        return "wrong"
    if any(term in raw for term in ("笔记本", "笔记")):
        return "notebook"
    if any(term in raw for term in ("看报告", "报告")):
        return "report"
    if any(term in raw for term in ("专属精练", "专属练习", "专属", "精练", "专练", "巩固练习", "巩固")):
        return "expand"
    if any(term in raw for term in ("题型突破", "题型", "突破", "章节挑战")):
        return "practise"
    if any(term in raw for term in ("学习任务", "任务")):
        return "tasks"
    if any(term in raw for term in ("薄弱知识", "薄弱")):
        return "weak"
    if any(term in raw for term in ("教材", "课本", "章节", "单元")):
        return "chapter_picker"
    if any(term in raw for term in ("完成", "收起", "关闭")):
        return "done"
    if any(term in raw for term in ("返回", "退出")) or lowered in {"back", "go back"}:
        return "back"
    return "select"


def normalize_xiaoluxue_subject_id(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        subject_id = int(value)
        return subject_id if subject_id > 0 else None
    if isinstance(value, dict):
        for key in ("subject_id", "subjectId", "subject"):
            subject_id = normalize_xiaoluxue_subject_id(value.get(key))
            if subject_id:
                return subject_id
        return None
    raw = str(value or "").strip()
    if not raw:
        return None
    match = re.search(r"(?:subject[_-]?id|subjectId)\s*[=:]\s*(\d+)", raw, flags=re.IGNORECASE)
    if match:
        subject_id = int(match.group(1))
        return subject_id if subject_id > 0 else None
    lowered = raw.casefold()
    latin_aliases = {
        "yuwen": 1,
        "chinese": 1,
        "math": 2,
        "maths": 2,
        "shuxue": 2,
        "english": 3,
        "yingyu": 3,
    }
    for alias, subject_id in latin_aliases.items():
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", lowered):
            return subject_id
    for alias, subject_id in sorted(XIAOLUXUE_SUBJECT_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if alias in raw:
            return subject_id
    return None


def xiaoluxue_study_subject_route_url(
    subject_id: int,
    *,
    textbook_id: Any = None,
    chapter_id: Any = None,
    knowledge_id: Any = None,
    go_next_knowledge: Any = None,
) -> str:
    params: list[tuple[str, str]] = [("subject_id", str(int(subject_id)))]
    for key, value in (
        ("textbook_id", textbook_id),
        ("chapter_id", chapter_id),
        ("knowledge_id", knowledge_id),
    ):
        if value is None or value == "":
            continue
        params.append((key, str(int(value))))
    if go_next_knowledge is not None:
        params.append(("go_next_knowledge", "true" if bool(go_next_knowledge) else "false"))
    return f"{XIAOLUXUE_STUDY_SUBJECT_ROUTE}?{urllib.parse.urlencode(params)}"


def xiaoluxue_map_fast_action_from_instruction(instruction: str) -> dict[str, Any] | None:
    if not xiaoluxue_instruction_looks_like_map(instruction):
        return None
    action_name = normalize_xiaoluxue_map_action(instruction)
    index = normalize_xiaoluxue_map_index(instruction)
    subject_id = normalize_xiaoluxue_subject_id(instruction)
    proposed: dict[str, Any] = {
        "action": "xiaoluxue_map_fast_path",
        "instruction": instruction,
        "action_name": action_name,
        "source": "xiaoluxue-native-map",
    }
    if index:
        proposed["index"] = index
    if subject_id:
        proposed["subject_id"] = subject_id
        proposed["route_if_subject"] = True
    if xiaoluxue_instruction_wants_direct_practice(instruction):
        proposed["enter_direct_practice"] = True
    return proposed


def normalize_xiaoluxue_lesson_action(value: Any) -> str:
    raw = str(value or "").strip()
    lowered = raw.casefold().replace("_", "-").replace(" ", "")
    if lowered in {"direct-practice", "direct", "practice", "practise"}:
        return "direct_practice"
    if any(keyword in raw for keyword in ("直接练", "开始练", "马上练", "做题")):
        return "direct_practice"
    if lowered in {"continue-answer", "next-answer", "next-question", "continue"}:
        return "continue_answer"
    if any(keyword in raw for keyword in ("继续答题", "继续", "下一题", "下一道", "下一问", "答题页")):
        return "continue_answer"
    if lowered in {"finish-result", "finish", "done"}:
        return "finish_result"
    if any(keyword in raw for keyword in ("完成", "结束", "返回地图")):
        return "finish_result"
    return lowered.replace("-", "_") or "direct_practice"


def xiaoluxue_instruction_wants_direct_practice(instruction: str) -> bool:
    text = str(instruction or "").strip()
    return bool(text) and any(keyword in text for keyword in ("直接练", "开始练", "马上练"))


def xiaoluxue_instruction_wants_continue_answer(instruction: str) -> bool:
    text = str(instruction or "").strip()
    return bool(text) and any(keyword in text for keyword in ("继续答题", "继续", "下一题", "下一道", "下一问"))


def xiaoluxue_instruction_wants_finish_result(instruction: str) -> bool:
    text = str(instruction or "").strip()
    return bool(text) and any(keyword in text for keyword in ("完成", "结束", "返回地图"))


def xiaoluxue_lesson_fast_action_from_instruction(instruction: str) -> dict[str, Any] | None:
    action_name: str | None = None
    if xiaoluxue_instruction_wants_direct_practice(instruction):
        action_name = "direct_practice"
    elif xiaoluxue_instruction_wants_continue_answer(instruction):
        action_name = "continue_answer"
    elif xiaoluxue_instruction_wants_finish_result(instruction):
        action_name = "finish_result"
    if not action_name:
        return None
    return {
        "action": "xiaoluxue_lesson_fast_path",
        "instruction": instruction,
        "action_name": action_name,
        "source": "xiaoluxue-native-lesson",
    }


def xiaoluxue_instruction_looks_like_map(instruction: str) -> bool:
    text = str(instruction or "").strip()
    if not text:
        return False
    if any(keyword in text for keyword in ("知识讲解", "讲解", "倍速", "2x", "2X")):
        return any(keyword in text for keyword in XIAOLUXUE_MAP_FAST_KEYWORDS)
    has_map_keyword = any(keyword in text for keyword in XIAOLUXUE_MAP_FAST_KEYWORDS)
    has_index = normalize_xiaoluxue_map_index(text) is not None
    has_subject = normalize_xiaoluxue_subject_id(text) is not None
    return has_map_keyword and (
        has_index
        or has_subject
        or any(keyword in text for keyword in ("地图", "任务", "薄弱", "完成", "返回"))
    )


def xiaoluxue_node_resource_endswith(node: dict[str, Any] | None, suffix: str) -> bool:
    if not node:
        return False
    resource_id = str(node.get("resource_id") or "")
    normalized = suffix if suffix.startswith(":id/") else f":id/{suffix.lstrip('/')}"
    return resource_id.endswith(normalized)


def find_xiaoluxue_node_by_resource_suffix(
    nodes: list[dict[str, Any]],
    suffix: str | tuple[str, ...],
) -> dict[str, Any] | None:
    suffixes = (suffix,) if isinstance(suffix, str) else suffix
    for node in nodes:
        if any(xiaoluxue_node_resource_endswith(node, item) for item in suffixes):
            return node
    return None


def find_xiaoluxue_map_index_node(nodes: list[dict[str, Any]], index: str) -> dict[str, Any] | None:
    normalized = str(index).strip()
    for node in nodes:
        if str(node.get("text") or "").strip() == normalized and xiaoluxue_node_resource_endswith(node, "index"):
            return node
    return find_ui_node(nodes, normalized, exact=True)


def xiaoluxue_map_action_node(nodes: list[dict[str, Any]], action_name: str) -> dict[str, Any] | None:
    action = normalize_xiaoluxue_map_action(action_name)
    if action == "practise":
        return find_xiaoluxue_node_by_resource_suffix(nodes, "practiseItem")
    if action == "expand":
        return find_xiaoluxue_node_by_resource_suffix(nodes, "expandItem")
    if action == "wrong":
        return find_xiaoluxue_node_by_resource_suffix(nodes, "wrong_textbook")
    if action == "notebook":
        return find_xiaoluxue_node_by_resource_suffix(nodes, "textbook")
    if action == "tasks":
        return find_xiaoluxue_node_by_resource_suffix(nodes, ("ll_task_tip", "task_tip", "task_view"))
    if action == "weak":
        return find_xiaoluxue_node_by_resource_suffix(nodes, ("ll_weak_knowledge_tip", "weak_knowledge_tip"))
    if action == "chapter_picker":
        return find_xiaoluxue_node_by_resource_suffix(nodes, ("textbook_view", "chapter_name"))
    if action == "done":
        return find_ui_node(nodes, "完成", exact=True)
    if action == "back":
        return find_xiaoluxue_node_by_resource_suffix(nodes, ("img_back", "iv_back", "back"))
    if action == "report":
        return find_ui_node(nodes, "看报告", exact=True) or find_ui_node(nodes, "报告", exact=False)
    return None


def xiaoluxue_selected_map_index(nodes: list[dict[str, Any]]) -> str | None:
    anchors = [
        node
        for node in nodes
        if xiaoluxue_node_resource_endswith(node, "wrong_textbook")
        or xiaoluxue_node_resource_endswith(node, "textbook")
        or xiaoluxue_node_resource_endswith(node, "practiseItem")
        or xiaoluxue_node_resource_endswith(node, "expandItem")
    ]
    for anchor in anchors:
        center = anchor.get("center")
        if not isinstance(center, dict):
            continue
        x = int(center.get("x", 0))
        y = int(center.get("y", 0))
        for node in nodes:
            text = str(node.get("text") or "").strip()
            if not text or not xiaoluxue_node_resource_endswith(node, "index"):
                continue
            click_target = node.get("click_target") if isinstance(node.get("click_target"), dict) else {}
            bounds = click_target.get("bounds") if isinstance(click_target, dict) else None
            if isinstance(bounds, dict) and point_in_bounds(bounds, x, y):
                return text
    selected = [node for node in nodes if xiaoluxue_node_resource_endswith(node, "index") and node.get("selected")]
    if selected:
        return str(selected[0].get("text") or "").strip() or None
    return None


def xiaoluxue_map_visible_indexes(nodes: list[dict[str, Any]]) -> list[str]:
    indexes: list[str] = []
    for node in nodes:
        text = str(node.get("text") or "").strip()
        if not text or not xiaoluxue_node_resource_endswith(node, "index"):
            continue
        if re.fullmatch(r"\d+(?:\.\d+)*", text) and text not in indexes:
            indexes.append(text)
    return indexes


def xiaoluxue_map_snapshot_from_observation(observation: dict[str, Any]) -> dict[str, Any]:
    nodes = observation.get("ui", {}).get("nodes", [])
    if not isinstance(nodes, list):
        nodes = []
    subject_node = find_xiaoluxue_node_by_resource_suffix(nodes, "txt_subject_name")
    chapter_node = find_xiaoluxue_node_by_resource_suffix(nodes, "chapter_name")
    actions: dict[str, bool] = {}
    for action_name in ("practise", "expand", "wrong", "notebook", "tasks", "weak", "chapter_picker", "done", "back", "report"):
        actions[action_name] = xiaoluxue_map_action_node(nodes, action_name) is not None
    focus = str(observation.get("state", {}).get("focused_window") or "")
    return {
        "focused_window": focus,
        "is_map": XIAOLUXUE_STUDY_SUBJECT_ACTIVITY in focus
        or bool(find_xiaoluxue_node_by_resource_suffix(nodes, "study_subject_map") or chapter_node),
        "subject": str(subject_node.get("text") or "").strip() if subject_node else None,
        "chapter": str(chapter_node.get("text") or "").strip() if chapter_node else None,
        "selected_index": xiaoluxue_selected_map_index(nodes),
        "visible_indexes": xiaoluxue_map_visible_indexes(nodes),
        "visible_actions": actions,
    }


def xiaoluxue_map_point(point: dict[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(point, dict):
        return None
    try:
        return {"x": int(point["x"]), "y": int(point["y"])}
    except (KeyError, TypeError, ValueError):
        return None


def xiaoluxue_map_cache_from_nodes(serial: str, nodes: list[dict[str, Any]], snapshot: dict[str, Any]) -> dict[str, Any]:
    index_tap_points: dict[str, dict[str, int]] = {}
    index_centers: dict[str, dict[str, int]] = {}
    for node in nodes:
        text = str(node.get("text") or "").strip()
        if not text or not xiaoluxue_node_resource_endswith(node, "index"):
            continue
        tap_point = xiaoluxue_map_point(node_click_point(node))
        center = xiaoluxue_map_point(node.get("center") if isinstance(node.get("center"), dict) else None)
        if tap_point:
            index_tap_points[text] = tap_point
        if center:
            index_centers[text] = center
    action_points: dict[str, dict[str, int]] = {}
    for action_name in ("practise", "expand", "wrong", "notebook", "tasks", "weak", "chapter_picker", "done", "back", "report"):
        action_node = xiaoluxue_map_action_node(nodes, action_name)
        point = xiaoluxue_map_point(node_click_point(action_node) if action_node else None)
        if point:
            action_points[action_name] = point
    return {
        "serial": serial,
        "updated_at": time.time(),
        "snapshot": snapshot,
        "selected_index": snapshot.get("selected_index"),
        "index_tap_points": index_tap_points,
        "index_centers": index_centers,
        "action_points": action_points,
    }


def xiaoluxue_load_native_map_cache() -> dict[str, Any]:
    if XIAOLUXUE_NATIVE_MAP_CACHE:
        return XIAOLUXUE_NATIVE_MAP_CACHE
    try:
        payload = json.loads(XIAOLUXUE_NATIVE_MAP_CACHE_PATH.read_text())
        if isinstance(payload, dict):
            XIAOLUXUE_NATIVE_MAP_CACHE.update(payload)
    except (OSError, json.JSONDecodeError):
        pass
    return XIAOLUXUE_NATIVE_MAP_CACHE


def xiaoluxue_save_native_map_cache() -> None:
    try:
        write_json(XIAOLUXUE_NATIVE_MAP_CACHE_PATH, XIAOLUXUE_NATIVE_MAP_CACHE)
    except OSError:
        pass


def xiaoluxue_remember_native_map_cache(serial: str, nodes: list[dict[str, Any]], snapshot: dict[str, Any]) -> dict[str, Any]:
    cache = xiaoluxue_map_cache_from_nodes(serial, nodes, snapshot)
    xiaoluxue_load_native_map_cache()[serial] = cache
    xiaoluxue_save_native_map_cache()
    return cache


def xiaoluxue_cached_native_map(serial: str, max_age_sec: float) -> dict[str, Any] | None:
    cache = xiaoluxue_load_native_map_cache().get(serial)
    if not isinstance(cache, dict):
        return None
    try:
        age = time.time() - float(cache.get("updated_at", 0))
    except (TypeError, ValueError):
        return None
    if age < 0 or age > max_age_sec:
        return None
    return cache


def xiaoluxue_update_cached_selected_index(serial: str, index: str | None) -> None:
    if not index:
        return
    cache = xiaoluxue_load_native_map_cache().get(serial)
    if not isinstance(cache, dict):
        return
    cache["selected_index"] = index
    snapshot = cache.get("snapshot")
    if isinstance(snapshot, dict):
        snapshot["selected_index"] = index
    cache["updated_at"] = time.time()
    xiaoluxue_save_native_map_cache()


def xiaoluxue_map_predicted_point_from_center(center: dict[str, Any] | None, action_name: str) -> dict[str, int] | None:
    if not isinstance(center, dict):
        return None
    x = int(center.get("x", 0))
    y = int(center.get("y", 0))
    action = normalize_xiaoluxue_map_action(action_name)
    offsets = {
        "practise": (0, -266),
        "expand": (198, -74),
        "wrong": (-74, 138),
        "notebook": (74, 138),
    }
    if action not in offsets:
        return None
    dx, dy = offsets[action]
    return {"x": x + dx, "y": y + dy}


def xiaoluxue_map_predicted_action_point(index_node: dict[str, Any] | None, action_name: str) -> dict[str, int] | None:
    if not index_node:
        return None
    center = index_node.get("center")
    return xiaoluxue_map_predicted_point_from_center(center if isinstance(center, dict) else None, action_name)


def xiaoluxue_map_tap_point(
    serial: str,
    point: dict[str, int],
    label: str,
    steps: list[dict[str, Any]],
    started_at: float,
    matched_node: dict[str, Any] | None = None,
) -> None:
    adb(["shell", "input", "tap", str(point["x"]), str(point["y"])], serial=serial, timeout=4)
    steps.append(
        {
            "action": "tap",
            "label": label,
            "x": point["x"],
            "y": point["y"],
            "matched_node": compact_node(matched_node),
            "at_sec": round(time.monotonic() - started_at, 3),
        }
    )


def xiaoluxue_map_tap_node(
    serial: str,
    node: dict[str, Any] | None,
    label: str,
    steps: list[dict[str, Any]],
    started_at: float,
) -> dict[str, int]:
    point = node_click_point(node) if node else None
    if not point:
        raise AndroidUseError(f"Could not tap Xiaoluxue map node: {label}")
    xiaoluxue_map_tap_point(serial, point, label, steps, started_at, node)
    return point


def xiaoluxue_should_enter_study_module(
    action_name: str,
    args: dict[str, Any],
    *,
    open_report_when_done: bool = False,
) -> bool:
    if open_report_when_done:
        return False
    action = normalize_xiaoluxue_map_action(action_name)
    if action not in XIAOLUXUE_MAP_MODULE_ENTRY_ACTIONS:
        return False
    return bool(args.get("enter_module", bool(args.get("enter_direct_practice", False))))


def xiaoluxue_clamp_native_point(point: tuple[int, int]) -> tuple[int, int]:
    x, y = point
    return (
        min(max(int(x), 0), XIAOLUXUE_NATIVE_BASE_WIDTH - 1),
        min(max(int(y), 0), XIAOLUXUE_NATIVE_BASE_HEIGHT - 1),
    )


def xiaoluxue_module_entry_point_from_screen(point: dict[str, int], window_info: dict[str, Any]) -> dict[str, int]:
    width = int(window_info.get("width") or XIAOLUXUE_NATIVE_BASE_WIDTH)
    height = int(window_info.get("height") or XIAOLUXUE_NATIVE_BASE_HEIGHT)
    offset_y = round(XIAOLUXUE_NATIVE_MODULE_CARD_ENTER_OFFSET_Y * height / XIAOLUXUE_NATIVE_BASE_HEIGHT)
    return {
        "x": min(max(int(point["x"]), 0), max(width - 1, 0)),
        "y": min(max(int(point["y"]) + offset_y, 0), max(height - 1, 0)),
    }


def xiaoluxue_wait_for_lesson_activity(serial: str, timeout_sec: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    info = xiaoluxue_native_window_info(serial)
    focus = str(info.get("focus") or "")
    while XIAOLUXUE_LESSON_ACTIVITY not in focus and time.monotonic() < deadline:
        time.sleep(0.05)
        info = xiaoluxue_native_window_info(serial)
        focus = str(info.get("focus") or "")
    return info


def xiaoluxue_wait_for_lesson_content_ready(
    serial: str,
    timeout_sec: float,
    poll_sec: float,
    steps: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    wait_started_at = time.monotonic()
    timeout_sec = min(max(float(timeout_sec), 0.0), 8.0)
    poll_sec = min(max(float(poll_sec), 0.03), 0.5)
    deadline = wait_started_at + timeout_sec
    attempts = 0
    last_stats: dict[str, Any] = {"ready": False}
    while True:
        attempts += 1
        try:
            last_stats = raw_screenshot_content_stats(screenshot_raw(serial))
        except AndroidUseError as exc:
            last_stats = {"ready": False, "error": str(exc)}
        if bool(last_stats.get("ready")) or time.monotonic() >= deadline:
            break
        time.sleep(min(poll_sec, max(deadline - time.monotonic(), 0.0)))
    result = {
        **last_stats,
        "attempts": attempts,
        "wait_sec": round(time.monotonic() - wait_started_at, 3),
        "timeout_sec": timeout_sec,
    }
    steps.append(
        {
            "action": "lesson:content-ready",
            "ready": bool(result.get("ready")),
            "attempts": attempts,
            "wait_sec": result["wait_sec"],
            "at_sec": round(time.monotonic() - started_at, 3),
        }
    )
    return result


def android_get_global_settings(serial: str, keys: tuple[str, ...]) -> dict[str, str]:
    if not keys:
        return {}
    output = shell(serial, "; ".join(f"settings get global {key}" for key in keys), timeout=4)
    values = output.splitlines()
    return {key: (values[index].strip() if index < len(values) else "") for index, key in enumerate(keys)}


def android_put_global_settings(serial: str, values: dict[str, str]) -> None:
    commands: list[str] = []
    for key, value in values.items():
        if key not in ANDROID_ANIMATION_SCALE_SETTINGS:
            continue
        normalized = str(value).strip()
        if not normalized or normalized == "null":
            commands.append(f"settings delete global {key} >/dev/null 2>&1 || true")
        else:
            commands.append(f"settings put global {key} {normalized}")
    if commands:
        shell(serial, "; ".join(commands), timeout=4)


def xiaoluxue_prepare_answer_speed_settings(
    serial: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any] | None:
    if not bool(args.get("disable_system_animations", True)):
        return None
    scale = str(args.get("system_animation_scale", "0")).strip() or "0"
    if not re.fullmatch(r"\d+(?:\.\d+)?", scale):
        scale = "0"
    try:
        previous = android_get_global_settings(serial, ANDROID_ANIMATION_SCALE_SETTINGS)
        updates = {
            key: scale
            for key in ANDROID_ANIMATION_SCALE_SETTINGS
            if previous.get(key) != scale
        }
        if updates:
            android_put_global_settings(serial, updates)
        state = {"previous": previous, "scale": scale, "changed": bool(updates)}
        steps.append(
            {
                "label": "lesson:disable-animations",
                "scale": scale,
                "changed": bool(updates),
                "at_sec": round(time.monotonic() - started_at, 3),
            }
        )
        return state
    except AndroidUseError as exc:
        steps.append(
            {
                "label": "lesson:disable-animations",
                "error": str(exc),
                "at_sec": round(time.monotonic() - started_at, 3),
            }
        )
        return None


def xiaoluxue_restore_answer_speed_settings(
    serial: str,
    args: dict[str, Any],
    animation_state: dict[str, Any] | None,
    steps: list[dict[str, Any]],
    started_at: float,
) -> None:
    if not animation_state or not bool(args.get("restore_system_animations", True)):
        return
    previous = animation_state.get("previous")
    if not isinstance(previous, dict) or not animation_state.get("changed"):
        return
    try:
        android_put_global_settings(serial, {str(key): str(value) for key, value in previous.items()})
        steps.append(
            {
                "label": "lesson:restore-animations",
                "at_sec": round(time.monotonic() - started_at, 3),
            }
        )
    except AndroidUseError as exc:
        steps.append(
            {
                "label": "lesson:restore-animations",
                "error": str(exc),
                "at_sec": round(time.monotonic() - started_at, 3),
            }
        )


def xiaoluxue_wait_for_lesson_answer_ready(
    serial: str,
    timeout_sec: float,
    poll_sec: float,
    steps: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    wait_started_at = time.monotonic()
    timeout_sec = min(max(float(timeout_sec), 0.0), 8.0)
    poll_sec = min(max(float(poll_sec), 0.03), 0.5)
    deadline = wait_started_at + timeout_sec
    attempts = 0
    last_stats: dict[str, Any] = {"ready": False}
    while True:
        attempts += 1
        try:
            last_stats = raw_screenshot_lesson_answer_stats(screenshot_raw(serial))
        except AndroidUseError as exc:
            last_stats = {"ready": False, "error": str(exc)}
        if bool(last_stats.get("ready")) or time.monotonic() >= deadline:
            break
        time.sleep(min(poll_sec, max(deadline - time.monotonic(), 0.0)))
    result = {
        **last_stats,
        "attempts": attempts,
        "wait_sec": round(time.monotonic() - wait_started_at, 3),
        "timeout_sec": timeout_sec,
    }
    steps.append(
        {
            "label": "lesson:answer-ready",
            "ready": bool(result.get("ready")),
            "attempts": attempts,
            "wait_sec": result["wait_sec"],
            "at_sec": round(time.monotonic() - started_at, 3),
        }
    )
    return result


def xiaoluxue_tap_lesson_direct_practice(
    serial: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
    *,
    default_wait_sec: float = 0.0,
) -> dict[str, Any]:
    wait_sec = min(max(float(args.get("direct_practice_wait_sec", default_wait_sec)), 0.0), 2.0)
    if wait_sec:
        time.sleep(wait_sec)
    assumed_focus = bool(args.get("assume_lesson_activity", False))
    focus_timeout_sec = 0.0 if assumed_focus else min(max(float(args.get("lesson_focus_timeout_sec", 0.7)), 0.0), 2.0)
    info = (
        {
            "focus": XIAOLUXUE_LESSON_ACTIVITY,
            "width": XIAOLUXUE_NATIVE_BASE_WIDTH,
            "height": XIAOLUXUE_NATIVE_BASE_HEIGHT,
        }
        if assumed_focus
        else xiaoluxue_wait_for_lesson_activity(serial, focus_timeout_sec)
    )
    focus = str(info.get("focus") or "")
    if XIAOLUXUE_LESSON_ACTIVITY not in focus and not bool(args.get("skip_lesson_focus_check", False)):
        raise AndroidUseError(f"Current Xiaoluxue screen is not LessonActivity: {focus or 'unknown focus'}")
    ready_timeout_sec = min(max(float(args.get("lesson_ready_timeout_sec", 0.0)), 0.0), 8.0)
    tap_until_answer_ready = bool(args.get("tap_direct_practice_until_answer_ready", False))
    ready_result: dict[str, Any] | None = None
    if ready_timeout_sec and not tap_until_answer_ready:
        ready_result = xiaoluxue_wait_for_lesson_content_ready(
            serial,
            ready_timeout_sec,
            float(args.get("lesson_ready_poll_sec", 0.08)),
            steps,
            started_at,
        )
        if not bool(ready_result.get("ready")) and bool(args.get("require_lesson_ready", False)):
            raise AndroidUseError(
                "LessonActivity content is still loading; direct practice button is not ready."
            )
    animation_state = xiaoluxue_prepare_answer_speed_settings(serial, args, steps, started_at)
    answer_ready: dict[str, Any] | None = None
    direct_taps = 0
    try:
        after_wait_sec = min(max(float(args.get("after_direct_practice_wait_sec", 0.08)), 0.0), 2.0)
        answer_ready_timeout_sec = min(max(float(args.get("answer_ready_timeout_sec", 5.0)), 0.0), 8.0)
        if tap_until_answer_ready and answer_ready_timeout_sec:
            deadline = time.monotonic() + answer_ready_timeout_sec
            attempts = 0
            last_stats: dict[str, Any] = {"ready": False}
            tap_interval_sec = min(max(float(args.get("direct_practice_tap_interval_sec", 0.12)), 0.03), 0.5)
            poll_after_taps = min(max(int(args.get("answer_ready_poll_after_taps", 3)), 1), 12)
            probe_points = (
                (XIAOLUXUE_NATIVE_DIRECT_PRACTICE_ENTER, "lesson:direct-practice"),
                (XIAOLUXUE_NATIVE_CURRENT_CARD_DIRECT_PRACTICE_ENTER, "lesson:card-direct-practice"),
                (XIAOLUXUE_NATIVE_RIGHT_CARD_DIRECT_PRACTICE_ENTER, "lesson:right-card-direct-practice"),
                (XIAOLUXUE_NATIVE_TRANSITION_START, "lesson:transition-start"),
            )
            while True:
                point, label = probe_points[direct_taps % len(probe_points)]
                xiaoluxue_native_tap(serial, point, info, label, steps, started_at)
                direct_taps += 1
                if after_wait_sec:
                    time.sleep(after_wait_sec)
                if direct_taps % poll_after_taps == 0 or time.monotonic() >= deadline:
                    attempts += 1
                    try:
                        last_stats = raw_screenshot_lesson_answer_stats(screenshot_raw(serial))
                    except AndroidUseError as exc:
                        last_stats = {"ready": False, "error": str(exc)}
                    if bool(last_stats.get("ready")) or time.monotonic() >= deadline:
                        break
                time.sleep(min(tap_interval_sec, max(deadline - time.monotonic(), 0.0)))
            answer_ready = {
                **last_stats,
                "attempts": attempts,
                "direct_taps": direct_taps,
                "wait_sec": round(time.monotonic() - (deadline - answer_ready_timeout_sec), 3),
                "timeout_sec": answer_ready_timeout_sec,
            }
            steps.append(
                {
                    "label": "lesson:answer-ready",
                    "ready": bool(answer_ready.get("ready")),
                    "attempts": attempts,
                    "direct_taps": direct_taps,
                    "wait_sec": answer_ready["wait_sec"],
                    "at_sec": round(time.monotonic() - started_at, 3),
                }
            )
        else:
            xiaoluxue_native_tap(
                serial,
                XIAOLUXUE_NATIVE_DIRECT_PRACTICE_ENTER,
                info,
                "lesson:direct-practice",
                steps,
                started_at,
            )
            direct_taps += 1
            if after_wait_sec:
                time.sleep(after_wait_sec)
        if answer_ready is None and answer_ready_timeout_sec:
            answer_ready = xiaoluxue_wait_for_lesson_answer_ready(
                serial,
                answer_ready_timeout_sec,
                float(args.get("answer_ready_poll_sec", 0.12)),
                steps,
                started_at,
            )
    finally:
        xiaoluxue_restore_answer_speed_settings(serial, args, animation_state, steps, started_at)
    return {
        "action": "direct_practice",
        "wait_sec": wait_sec,
        "focus_timeout_sec": focus_timeout_sec,
        "assumed_focus": assumed_focus,
        "ready": ready_result,
        "after_wait_sec": after_wait_sec,
        "answer_ready": answer_ready,
        "direct_taps": direct_taps,
        "enter_point": {
            "base_x": XIAOLUXUE_NATIVE_DIRECT_PRACTICE_ENTER[0],
            "base_y": XIAOLUXUE_NATIVE_DIRECT_PRACTICE_ENTER[1],
        },
    }


def xiaoluxue_tap_lesson_continue_answer(
    serial: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    assumed_focus = bool(args.get("assume_lesson_activity", True))
    focus_timeout_sec = 0.0 if assumed_focus else min(max(float(args.get("lesson_focus_timeout_sec", 0.7)), 0.0), 2.0)
    info = (
        {
            "focus": XIAOLUXUE_LESSON_ACTIVITY,
            "width": XIAOLUXUE_NATIVE_BASE_WIDTH,
            "height": XIAOLUXUE_NATIVE_BASE_HEIGHT,
        }
        if assumed_focus
        else xiaoluxue_wait_for_lesson_activity(serial, focus_timeout_sec)
    )
    focus = str(info.get("focus") or "")
    if XIAOLUXUE_LESSON_ACTIVITY not in focus and not bool(args.get("skip_lesson_focus_check", False)):
        raise AndroidUseError(f"Current Xiaoluxue screen is not LessonActivity: {focus or 'unknown focus'}")
    animation_state = xiaoluxue_prepare_answer_speed_settings(serial, args, steps, started_at)
    answer_ready: dict[str, Any] | None = None
    skip_taps = 0
    card_direct_taps = 0
    try:
        xiaoluxue_native_tap(
            serial,
            XIAOLUXUE_NATIVE_ANSWER_CONTINUE,
            info,
            "lesson:continue-answer",
            steps,
            started_at,
        )
        continue_tapped_at = time.monotonic()
        initial_wait_sec = min(max(float(args.get("after_continue_wait_sec", 0.18)), 0.0), 2.0)
        if initial_wait_sec:
            time.sleep(initial_wait_sec)
        timeout_sec = min(max(float(args.get("answer_ready_timeout_sec", 5.0)), 0.0), 8.0)
        poll_sec = min(max(float(args.get("answer_ready_poll_sec", 0.12)), 0.03), 0.5)
        max_skip_taps = min(max(int(args.get("transition_skip_taps", 6)), 0), 20)
        min_ready_after_continue_sec = min(max(float(args.get("min_answer_ready_after_continue_sec", 2.2)), 0.0), 4.0)
        card_direct_enabled = bool(args.get("tap_card_direct_practice_if_needed", True))
        max_card_direct_taps = min(max(int(args.get("card_direct_practice_taps", 4)), 0), 20)
        deadline = time.monotonic() + timeout_sec
        attempts = 0
        last_stats: dict[str, Any] = {"ready": False}
        last_card_stats: dict[str, Any] = {"ready": False}
        while True:
            attempts += 1
            if skip_taps < max_skip_taps:
                xiaoluxue_native_tap(
                    serial,
                    XIAOLUXUE_NATIVE_TRANSITION_START,
                    info,
                    "lesson:transition-start",
                    steps,
                    started_at,
                )
                skip_taps += 1
                time.sleep(min(max(float(args.get("transition_skip_interval_sec", 0.10)), 0.03), 0.5))
            try:
                raw = screenshot_raw(serial)
                last_stats = raw_screenshot_lesson_answer_stats(raw)
                last_card_stats = raw_screenshot_lesson_card_list_stats(raw)
            except AndroidUseError as exc:
                last_stats = {"ready": False, "error": str(exc)}
                last_card_stats = {"ready": False, "error": str(exc)}
            if (
                card_direct_enabled
                and bool(last_card_stats.get("ready"))
                and card_direct_taps < max_card_direct_taps
            ):
                card_points = (
                    (XIAOLUXUE_NATIVE_CURRENT_CARD_DIRECT_PRACTICE_ENTER, "lesson:card-direct-practice"),
                    (XIAOLUXUE_NATIVE_RIGHT_CARD_DIRECT_PRACTICE_ENTER, "lesson:right-card-direct-practice"),
                    (XIAOLUXUE_NATIVE_DIRECT_PRACTICE_ENTER, "lesson:left-card-direct-practice"),
                )
                card_point, card_label = card_points[card_direct_taps % len(card_points)]
                xiaoluxue_native_tap(
                    serial,
                    card_point,
                    info,
                    card_label,
                    steps,
                    started_at,
                )
                card_direct_taps += 1
                time.sleep(min(max(float(args.get("card_direct_practice_interval_sec", 0.12)), 0.03), 0.5))
            ready_enough = bool(last_stats.get("ready")) and (
                time.monotonic() - continue_tapped_at >= min_ready_after_continue_sec
            )
            if ready_enough or time.monotonic() >= deadline:
                break
            time.sleep(min(poll_sec, max(deadline - time.monotonic(), 0.0)))
        answer_ready = {
            **last_stats,
            "attempts": attempts,
            "skip_taps": skip_taps,
            "card_direct_taps": card_direct_taps,
            "card_list": last_card_stats,
            "wait_sec": round(time.monotonic() - (deadline - timeout_sec), 3),
            "timeout_sec": timeout_sec,
        }
        steps.append(
            {
                "label": "lesson:answer-ready",
                "ready": bool(answer_ready.get("ready")),
                "attempts": attempts,
                "skip_taps": skip_taps,
                "card_direct_taps": card_direct_taps,
                "wait_sec": answer_ready["wait_sec"],
                "at_sec": round(time.monotonic() - started_at, 3),
            }
        )
    finally:
        xiaoluxue_restore_answer_speed_settings(serial, args, animation_state, steps, started_at)
    return {
        "action": "continue_answer",
        "focus_timeout_sec": focus_timeout_sec,
        "assumed_focus": assumed_focus,
        "answer_ready": answer_ready,
        "card_direct_taps": card_direct_taps,
        "continue_point": {
            "base_x": XIAOLUXUE_NATIVE_ANSWER_CONTINUE[0],
            "base_y": XIAOLUXUE_NATIVE_ANSWER_CONTINUE[1],
        },
        "transition_start_point": {
            "base_x": XIAOLUXUE_NATIVE_TRANSITION_START[0],
            "base_y": XIAOLUXUE_NATIVE_TRANSITION_START[1],
        },
    }


def xiaoluxue_tap_lesson_finish_result(
    serial: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    assumed_focus = bool(args.get("assume_lesson_activity", True))
    focus_timeout_sec = 0.0 if assumed_focus else min(max(float(args.get("lesson_focus_timeout_sec", 0.7)), 0.0), 2.0)
    info = (
        {
            "focus": XIAOLUXUE_LESSON_ACTIVITY,
            "width": XIAOLUXUE_NATIVE_BASE_WIDTH,
            "height": XIAOLUXUE_NATIVE_BASE_HEIGHT,
        }
        if assumed_focus
        else xiaoluxue_wait_for_lesson_activity(serial, focus_timeout_sec)
    )
    focus = str(info.get("focus") or "")
    if XIAOLUXUE_LESSON_ACTIVITY not in focus and not bool(args.get("skip_lesson_focus_check", False)):
        raise AndroidUseError(f"Current Xiaoluxue screen is not LessonActivity: {focus or 'unknown focus'}")
    xiaoluxue_native_tap(
        serial,
        XIAOLUXUE_NATIVE_RESULT_FINISH,
        info,
        "lesson:finish-result",
        steps,
        started_at,
    )
    wait_sec = min(max(float(args.get("after_finish_wait_sec", 0.35)), 0.0), 2.0)
    if wait_sec:
        time.sleep(wait_sec)
    return {
        "action": "finish_result",
        "focus_timeout_sec": focus_timeout_sec,
        "assumed_focus": assumed_focus,
        "finish_point": {
            "base_x": XIAOLUXUE_NATIVE_RESULT_FINISH[0],
            "base_y": XIAOLUXUE_NATIVE_RESULT_FINISH[1],
        },
    }


def xiaoluxue_tap_study_module_entry(
    serial: str,
    *,
    action_name: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
    open_report_when_done: bool = False,
    base_action_point: tuple[int, int] | None = None,
    screen_action_point: dict[str, int] | None = None,
    window_info: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    action = normalize_xiaoluxue_map_action(action_name)
    if not xiaoluxue_should_enter_study_module(action, args, open_report_when_done=open_report_when_done):
        return None
    wait_sec = min(max(float(args.get("module_card_wait_sec", 0.16)), 0.05), 1.0)
    time.sleep(wait_sec)
    info = window_info or xiaoluxue_native_window_info(serial)
    module_entry: dict[str, Any] = {"action": action, "card_wait_sec": wait_sec}
    if base_action_point:
        entry_base = xiaoluxue_clamp_native_point(
            (base_action_point[0], base_action_point[1] + XIAOLUXUE_NATIVE_MODULE_CARD_ENTER_OFFSET_Y)
        )
        xiaoluxue_native_tap(serial, entry_base, info, f"{action}:module-enter", steps, started_at)
        module_entry["enter_point"] = {"base_x": entry_base[0], "base_y": entry_base[1]}
    elif screen_action_point:
        entry_point = xiaoluxue_module_entry_point_from_screen(screen_action_point, info)
        xiaoluxue_map_tap_point(serial, entry_point, f"{action}:module-enter", steps, started_at, None)
        module_entry["enter_point"] = entry_point
    else:
        return None

    if action == "expand" and bool(args.get("confirm_expand_enter", True)):
        confirm_wait_sec = min(max(float(args.get("confirm_wait_sec", 0.08)), 0.05), 1.5)
        confirm_started_at = time.monotonic()
        if bool(args.get("confirm_expand_focus_check", False)):
            focus_info = xiaoluxue_native_window_info(serial)
            focus = str(focus_info.get("focus") or "")
            deadline = confirm_started_at + confirm_wait_sec
            while XIAOLUXUE_STUDY_SUBJECT_ACTIVITY in focus and time.monotonic() < deadline:
                time.sleep(min(0.06, max(deadline - time.monotonic(), 0)))
                focus_info = xiaoluxue_native_window_info(serial)
                focus = str(focus_info.get("focus") or "")
            should_tap_confirm = XIAOLUXUE_STUDY_SUBJECT_ACTIVITY in focus
        else:
            if confirm_wait_sec:
                time.sleep(confirm_wait_sec)
            focus_info = info
            should_tap_confirm = True
        if should_tap_confirm:
            xiaoluxue_native_tap(
                serial,
                XIAOLUXUE_NATIVE_EXPAND_CONFIRM_ENTER,
                focus_info,
                "expand:confirm-enter",
                steps,
                started_at,
            )
            module_entry["confirm_tapped"] = True
        else:
            module_entry["confirm_tapped"] = False
        module_entry["confirm_wait_sec"] = round(time.monotonic() - confirm_started_at, 3)
    if action == "practise" and bool(args.get("enter_direct_practice", False)):
        direct_args = dict(args)
        direct_args.setdefault("tap_direct_practice_until_answer_ready", True)
        direct_args.setdefault("answer_ready_poll_after_taps", 2)
        direct_args.setdefault("lesson_focus_timeout_sec", 0.55)
        direct_args.setdefault("lesson_ready_timeout_sec", 5.5)
        direct_args.setdefault("lesson_ready_poll_sec", 0.15)
        direct_args.setdefault("require_lesson_ready", True)
        module_entry["direct_practice"] = xiaoluxue_tap_lesson_direct_practice(
            serial,
            direct_args,
            steps,
            started_at,
            default_wait_sec=0.12,
        )
    elif action in XIAOLUXUE_MAP_MODULE_ENTRY_ACTIONS and bool(args.get("verify_module_activity", True)):
        focus_timeout_sec = min(max(float(args.get("module_focus_timeout_sec", 0.9)), 0.0), 2.0)
        focus_info = xiaoluxue_wait_for_lesson_activity(serial, focus_timeout_sec)
        focus = str(focus_info.get("focus") or "")
        module_entry["focus_timeout_sec"] = focus_timeout_sec
        module_entry["focus_after_enter"] = focus
        if XIAOLUXUE_LESSON_ACTIVITY not in focus:
            raise AndroidUseError(f"Xiaoluxue module did not open LessonActivity: {focus or 'unknown focus'}")
    return module_entry


def xiaoluxue_start_scheme_route(
    serial: str,
    url: str,
    steps: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    xiaoluxue_dismiss_debug_overlay_if_needed(serial, steps, started_at)
    output = adb(
        [
            "shell",
            "am",
            "start",
            "-n",
            f"{XIAOLUXUE_STUDENT_PACKAGE}/{XIAOLUXUE_SCHEME_PROXY_ACTIVITY}",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            url,
        ],
        serial=serial,
        timeout=8,
    ).decode("utf-8", errors="replace")
    step = {
        "action": "route",
        "component": XIAOLUXUE_SCHEME_PROXY_ACTIVITY,
        "url": url,
        "at_sec": round(time.monotonic() - started_at, 3),
    }
    if output.strip():
        step["output"] = output.strip().splitlines()[-1]
    steps.append(step)
    return {"ok": True, "url": url, "output": output}


def xiaoluxue_leave_lesson_before_subject_route(
    serial: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
) -> bool:
    if not bool(args.get("leave_lesson_before_route", True)):
        return False
    try:
        focus = str(xiaoluxue_native_window_info(serial).get("focus") or "")
    except AndroidUseError:
        focus = ""
    if XIAOLUXUE_LESSON_ACTIVITY not in focus:
        return False
    adb(["shell", "input", "keyevent", "BACK"], serial=serial, timeout=4)
    steps.append(
        {
            "action": "back",
            "reason": "leave-lesson-before-subject-route",
            "from_focus": focus,
            "at_sec": round(time.monotonic() - started_at, 3),
        }
    )
    wait_sec = min(max(float(args.get("lesson_back_wait_sec", 0.35)), 0.0), 1.2)
    if wait_sec:
        time.sleep(wait_sec)
    return True


def xiaoluxue_open_native_subject_map(
    serial: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    subject_id = normalize_xiaoluxue_subject_id(
        args.get("subject_id")
        or args.get("subjectId")
        or args.get("subject")
        or args.get("instruction")
    )
    if not subject_id:
        raise AndroidUseError("Xiaoluxue native subject route needs `subject_id` or a subject name such as 语文/数学.")
    url = xiaoluxue_study_subject_route_url(
        subject_id,
        textbook_id=args.get("textbook_id") or args.get("textbookId"),
        chapter_id=args.get("chapter_id") or args.get("chapterId"),
        knowledge_id=args.get("knowledge_id") or args.get("knowledgeId"),
        go_next_knowledge=args.get("go_next_knowledge") if "go_next_knowledge" in args else args.get("goNextKnowledge"),
    )
    xiaoluxue_leave_lesson_before_subject_route(serial, args, steps, started_at)
    route = xiaoluxue_start_scheme_route(serial, url, steps, started_at)
    wait_sec = min(max(float(args.get("route_wait_sec", 0.45)), 0.0), 3.0)
    window_info: dict[str, Any] | None = None
    if wait_sec:
        wait_started = time.monotonic()
        deadline = wait_started + wait_sec
        poll_sec = min(max(float(args.get("route_focus_poll_sec", 0.08)), 0.03), 0.3)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_sec, remaining))
            try:
                window_info = xiaoluxue_native_window_info(serial)
            except AndroidUseError:
                window_info = None
            focus = str((window_info or {}).get("focus") or "")
            if XIAOLUXUE_STUDY_SUBJECT_ACTIVITY in focus:
                break
        actual_wait_sec = round(time.monotonic() - wait_started, 3)
        steps.append(
            {
                "action": "wait",
                "reason": "subject-route-render",
                "seconds": actual_wait_sec,
                "target_focus": XIAOLUXUE_STUDY_SUBJECT_ACTIVITY,
                "focus": str((window_info or {}).get("focus") or ""),
                "at_sec": round(time.monotonic() - started_at, 3),
            }
        )
    if window_info is None:
        window_info = xiaoluxue_native_window_info(serial)
    if XIAOLUXUE_STUDY_SUBJECT_ACTIVITY in str(window_info.get("focus") or ""):
        settle_sec = min(max(float(args.get("route_settle_sec", 0.0)), 0.0), 0.3)
        if settle_sec:
            time.sleep(settle_sec)
    if bool(args.get("close_progress_popup", True)):
        close_taps = min(max(int(args.get("close_progress_taps", 1)), 1), 3)
        close_wait_sec = min(max(float(args.get("close_progress_wait_sec", 0.05)), 0.0), 0.8)
        for attempt in range(close_taps):
            if attempt and close_wait_sec:
                time.sleep(close_wait_sec)
            xiaoluxue_native_tap(
                serial,
                XIAOLUXUE_NATIVE_PROGRESS_POPUP_CLOSE,
                window_info,
                f"subject-route:close-progress-popup:{attempt + 1}",
                steps,
                started_at,
            )
    return {"subject_id": subject_id, "route": route, "window_info": window_info}


def xiaoluxue_route_preset_for(subject_id: int | None, index: str | None) -> dict[str, tuple[int, int]] | None:
    if not subject_id or not index:
        return None
    subject_presets = XIAOLUXUE_NATIVE_MAP_ROUTE_PRESETS.get(int(subject_id))
    if not subject_presets:
        return None
    preset = subject_presets.get(str(index))
    return preset if isinstance(preset, dict) else None


def xiaoluxue_run_selected_module_shortcut(
    serial: str,
    *,
    subject_id: int | None,
    action_name: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
    open_report_when_done: bool,
    window_info: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    action = normalize_xiaoluxue_map_action(action_name)
    if action not in XIAOLUXUE_MAP_MODULE_ENTRY_ACTIONS:
        return None
    if not bool(args.get("selected_module_shortcut", True)):
        return None
    window_info = window_info or xiaoluxue_native_window_info(serial)
    focus = str(window_info.get("focus") or "")
    if XIAOLUXUE_STUDY_SUBJECT_ACTIVITY not in focus:
        return None
    action_point = XIAOLUXUE_NATIVE_SELECTED_MODULE_POINTS.get(action)
    if not action_point:
        return None
    xiaoluxue_native_tap(serial, action_point, window_info, f"{action}:selected-shortcut", steps, started_at)
    module_entry = xiaoluxue_tap_study_module_entry(
        serial,
        action_name=action,
        args=args,
        steps=steps,
        started_at=started_at,
        open_report_when_done=open_report_when_done,
        base_action_point=action_point,
        window_info=window_info,
    )
    result = {
        "ok": True,
        "action": "xiaoluxue_map_fast_path",
        "map_action": action,
        "subject_id": subject_id,
        "selected_module_shortcut": True,
        "entered_module": bool(module_entry),
        "module_entry": module_entry,
        "elapsed_sec": round(time.monotonic() - started_at, 3),
        "steps": steps,
        "snapshot": {"subject_id": subject_id, "selected_index": "current", "selected_module_shortcut": True},
    }
    return result


def xiaoluxue_run_route_preset_map_fast_path(
    serial: str,
    *,
    subject_id: int,
    index: str | None,
    action_name: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
    wait_after_select: float,
    open_report_when_done: bool,
    report_wait_sec: float,
    window_info: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    preset = xiaoluxue_route_preset_for(subject_id, index)
    if not preset:
        return None
    action = normalize_xiaoluxue_map_action(action_name)
    index_point = preset.get("index")
    if not index_point:
        return None
    window_info = window_info or xiaoluxue_native_window_info(serial)
    xiaoluxue_native_tap(serial, index_point, window_info, f"index:{index}:preset", steps, started_at)
    xiaoluxue_update_cached_selected_index(serial, str(index))
    module_entry: dict[str, Any] | None = None
    if action == "select":
        return {
            "ok": True,
            "action": "xiaoluxue_map_fast_path",
            "map_action": "select",
            "index": index,
            "subject_id": subject_id,
            "routed": True,
            "preset": True,
            "elapsed_sec": round(time.monotonic() - started_at, 3),
            "steps": steps,
            "snapshot": {"subject_id": subject_id, "selected_index": index, "preset": True},
        }
    time.sleep(wait_after_select)
    if action == "report":
        practise_point = preset.get("practise")
        report_point = preset.get("report")
        if not practise_point or not report_point:
            return None
        xiaoluxue_native_tap(serial, practise_point, window_info, "practise:preset", steps, started_at)
        time.sleep(report_wait_sec)
        xiaoluxue_native_tap(serial, report_point, window_info, "report:preset", steps, started_at)
    else:
        action_point = preset.get(action)
        if not action_point:
            return None
        xiaoluxue_native_tap(serial, action_point, window_info, f"{action}:preset", steps, started_at)
        module_entry = xiaoluxue_tap_study_module_entry(
            serial,
            action_name=action,
            args=args,
            steps=steps,
            started_at=started_at,
            open_report_when_done=open_report_when_done,
            base_action_point=action_point,
            window_info=window_info,
        )
        if open_report_when_done:
            report_point = preset.get("report")
            if report_point:
                time.sleep(report_wait_sec)
                xiaoluxue_native_tap(serial, report_point, window_info, "report:preset", steps, started_at)
    return {
        "ok": True,
        "action": "xiaoluxue_map_fast_path",
        "map_action": action,
        "index": index,
        "subject_id": subject_id,
        "routed": True,
        "preset": True,
        "entered_module": bool(module_entry),
        "module_entry": module_entry,
        "elapsed_sec": round(time.monotonic() - started_at, 3),
        "steps": steps,
        "snapshot": {"subject_id": subject_id, "selected_index": index, "preset": True},
    }


def xiaoluxue_run_cached_map_fast_path(
    serial: str,
    cache: dict[str, Any],
    *,
    index: str | None,
    action_name: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
    wait_after_select: float,
    prefer_predicted: bool,
    open_report_when_done: bool,
) -> dict[str, Any] | None:
    action_points = cache.get("action_points") if isinstance(cache.get("action_points"), dict) else {}
    index_tap_points = cache.get("index_tap_points") if isinstance(cache.get("index_tap_points"), dict) else {}
    index_centers = cache.get("index_centers") if isinstance(cache.get("index_centers"), dict) else {}
    snapshot = cache.get("snapshot") if isinstance(cache.get("snapshot"), dict) else {}
    selected_index = str(cache.get("selected_index") or snapshot.get("selected_index") or "").strip() or None
    target_index = index or selected_index
    action = normalize_xiaoluxue_map_action(action_name)

    if action in {"tasks", "weak", "chapter_picker", "done", "back"}:
        point = xiaoluxue_map_point(action_points.get(action))
        if not point:
            return None
        xiaoluxue_map_tap_point(serial, point, action, steps, started_at, None)
        return {
            "ok": True,
            "action": "xiaoluxue_map_fast_path",
            "map_action": action,
            "cached": True,
            "elapsed_sec": round(time.monotonic() - started_at, 3),
            "steps": steps,
            "snapshot": snapshot,
        }

    if action == "report":
        point = xiaoluxue_map_point(action_points.get("report"))
        if point:
            xiaoluxue_map_tap_point(serial, point, "report", steps, started_at, None)
            return {
                "ok": True,
                "action": "xiaoluxue_map_fast_path",
                "map_action": "report",
                "cached": True,
                "elapsed_sec": round(time.monotonic() - started_at, 3),
                "steps": steps,
                "snapshot": snapshot,
            }
        action = "practise"

    if not target_index:
        return None
    target_tap = xiaoluxue_map_point(index_tap_points.get(target_index))
    if not target_tap:
        return None

    if action == "select":
        xiaoluxue_map_tap_point(serial, target_tap, f"index:{target_index}:cached", steps, started_at, None)
        xiaoluxue_update_cached_selected_index(serial, target_index)
        return {
            "ok": True,
            "action": "xiaoluxue_map_fast_path",
            "map_action": "select",
            "index": target_index,
            "cached": True,
            "elapsed_sec": round(time.monotonic() - started_at, 3),
            "steps": steps,
            "snapshot": snapshot,
        }

    if selected_index == target_index:
        point = xiaoluxue_map_point(action_points.get(action))
        if point:
            xiaoluxue_map_tap_point(serial, point, f"{action}:cached", steps, started_at, None)
            module_entry = xiaoluxue_tap_study_module_entry(
                serial,
                action_name=action,
                args=args,
                steps=steps,
                started_at=started_at,
                open_report_when_done=open_report_when_done,
                screen_action_point=point,
            )
            return {
                "ok": True,
                "action": "xiaoluxue_map_fast_path",
                "map_action": action,
                "index": target_index,
                "cached": True,
                "predicted": False,
                "entered_module": bool(module_entry),
                "module_entry": module_entry,
                "elapsed_sec": round(time.monotonic() - started_at, 3),
                "steps": steps,
                "snapshot": snapshot,
            }

    xiaoluxue_map_tap_point(serial, target_tap, f"index:{target_index}:cached", steps, started_at, None)
    time.sleep(wait_after_select)
    if not prefer_predicted:
        return None
    center = xiaoluxue_map_point(index_centers.get(target_index))
    predicted = xiaoluxue_map_predicted_point_from_center(center, action)
    if not predicted:
        return None
    xiaoluxue_map_tap_point(serial, predicted, f"{action}:predicted:cached", steps, started_at, None)
    module_entry = xiaoluxue_tap_study_module_entry(
        serial,
        action_name=action,
        args=args,
        steps=steps,
        started_at=started_at,
        open_report_when_done=open_report_when_done,
        screen_action_point=predicted,
    )
    xiaoluxue_update_cached_selected_index(serial, target_index)
    return {
        "ok": True,
        "action": "xiaoluxue_map_fast_path",
        "map_action": action,
        "index": target_index,
        "cached": True,
        "predicted": True,
        "entered_module": bool(module_entry),
        "module_entry": module_entry,
        "elapsed_sec": round(time.monotonic() - started_at, 3),
        "steps": steps,
        "snapshot": snapshot,
    }


def xiaoluxue_observe_native_map(serial: str, *, limit: int = 800, include_focus: bool = False) -> dict[str, Any]:
    xml_text = dump_ui_xml(serial)
    nodes = parse_ui_nodes(xml_text, limit=limit)
    observation: dict[str, Any] = {
        "state": {"focused_window": get_focused_window(serial) if include_focus else ""},
        "ui": {"nodes": nodes, "count": len(nodes)},
    }
    snapshot = xiaoluxue_map_snapshot_from_observation(observation)
    if not snapshot.get("is_map"):
        raise AndroidUseError("Current screen is not the Xiaoluxue native study map.")
    observation["xiaoluxue_map"] = snapshot
    return observation


def run_xiaoluxue_map_fast_path(serial: str, args: dict[str, Any], *, record: bool) -> dict[str, Any]:
    started_at = time.monotonic()
    steps: list[dict[str, Any]] = []
    action_name = normalize_xiaoluxue_map_action(args.get("action_name") or args.get("action") or args.get("instruction"))
    requested_report = action_name == "report"
    index = normalize_xiaoluxue_map_index(args.get("index") or args.get("instruction"))
    subject_id = normalize_xiaoluxue_subject_id(
        args.get("subject_id")
        or args.get("subjectId")
        or args.get("subject")
        or args.get("instruction")
    )
    prefer_predicted = bool(args.get("prefer_predicted", True))
    wait_after_select = min(max(float(args.get("after_select_wait_sec", 0.08)), 0.05), 1.0)
    report_wait_sec = min(max(float(args.get("report_wait_sec", 0.32)), 0.05), 1.0)
    open_report_when_done = bool(args.get("open_report_when_done", requested_report))
    route_subject = bool(args.get("route_subject", False) or args.get("open_subject", False) or args.get("route_if_subject", False))
    before: dict[str, Any] | None = None
    if route_subject and subject_id:
        route_args = args
        if (
            not index
            and action_name in XIAOLUXUE_MAP_MODULE_ENTRY_ACTIONS
            and "route_wait_sec" not in args
        ):
            route_args = {**args, "route_wait_sec": 0.65}
        route_info = xiaoluxue_open_native_subject_map(serial, route_args, steps, started_at)
        route_window_info = route_info.get("window_info") if isinstance(route_info, dict) else None
        preset_result = xiaoluxue_run_route_preset_map_fast_path(
            serial,
            subject_id=subject_id,
            index=index,
            action_name=action_name,
            args=args,
            steps=steps,
            started_at=started_at,
            wait_after_select=wait_after_select,
            open_report_when_done=open_report_when_done,
            report_wait_sec=report_wait_sec,
            window_info=route_window_info if isinstance(route_window_info, dict) else None,
        )
        if preset_result:
            if record:
                append_recording_step(
                    serial,
                    "xiaoluxue_map_fast_path",
                    args,
                    preset_result,
                    before={"state": {}, "ui": {"nodes": []}, "xiaoluxue_map": preset_result.get("snapshot", {})},
                )
            return preset_result
        if not index:
            shortcut_result = xiaoluxue_run_selected_module_shortcut(
                serial,
                subject_id=subject_id,
                action_name=action_name,
                args=args,
                steps=steps,
                started_at=started_at,
                open_report_when_done=open_report_when_done,
                window_info=route_window_info if isinstance(route_window_info, dict) else None,
            )
            if shortcut_result:
                shortcut_result["routed"] = True
                if record:
                    append_recording_step(
                        serial,
                        "xiaoluxue_map_fast_path",
                        args,
                        shortcut_result,
                        before={"state": {}, "ui": {"nodes": []}, "xiaoluxue_map": shortcut_result.get("snapshot", {})},
                    )
                return shortcut_result

    use_cache = bool(args.get("use_cache", True)) and not bool(args.get("force_observe", False))
    if route_subject and subject_id:
        use_cache = False
    if not index and action_name in XIAOLUXUE_MAP_MODULE_ENTRY_ACTIONS:
        use_cache = False
    if use_cache:
        cache = xiaoluxue_cached_native_map(serial, max_age_sec=float(args.get("cache_max_age_sec", 6 * 60 * 60)))
        if cache:
            cached_result = xiaoluxue_run_cached_map_fast_path(
                serial,
                cache,
                index=index,
                action_name=action_name,
                args=args,
                steps=steps,
                started_at=started_at,
                wait_after_select=wait_after_select,
                prefer_predicted=prefer_predicted,
                open_report_when_done=open_report_when_done,
            )
            if cached_result:
                if record:
                    append_recording_step(
                        serial,
                        "xiaoluxue_map_fast_path",
                        args,
                        cached_result,
                        before={"state": {}, "ui": {"nodes": []}, "xiaoluxue_map": cached_result.get("snapshot", {})},
                )
                return cached_result

    if not index:
        shortcut_result = xiaoluxue_run_selected_module_shortcut(
            serial,
            subject_id=subject_id,
            action_name=action_name,
            args=args,
            steps=steps,
            started_at=started_at,
            open_report_when_done=open_report_when_done,
        )
        if shortcut_result:
            if record:
                append_recording_step(
                    serial,
                    "xiaoluxue_map_fast_path",
                    args,
                    shortcut_result,
                    before={"state": {}, "ui": {"nodes": []}, "xiaoluxue_map": shortcut_result.get("snapshot", {})},
                )
            return shortcut_result

    before = xiaoluxue_observe_native_map(serial, limit=800)
    nodes = before["ui"]["nodes"]
    snapshot = before["xiaoluxue_map"]
    xiaoluxue_remember_native_map_cache(serial, nodes, snapshot)

    if action_name in {"tasks", "weak", "chapter_picker", "done", "back"}:
        node = xiaoluxue_map_action_node(nodes, action_name)
        xiaoluxue_map_tap_node(serial, node, action_name, steps, started_at)
        result = {
            "ok": True,
            "action": "xiaoluxue_map_fast_path",
            "map_action": action_name,
            "elapsed_sec": round(time.monotonic() - started_at, 3),
            "steps": steps,
            "snapshot": snapshot,
        }
        if record:
            append_recording_step(serial, "xiaoluxue_map_fast_path", args, result, before=before)
        return result

    if action_name == "report":
        report_node = xiaoluxue_map_action_node(nodes, "report")
        if report_node:
            xiaoluxue_map_tap_node(serial, report_node, "report", steps, started_at)
            result = {
                "ok": True,
                "action": "xiaoluxue_map_fast_path",
                "map_action": "report",
                "elapsed_sec": round(time.monotonic() - started_at, 3),
                "steps": steps,
                "snapshot": snapshot,
            }
            if record:
                append_recording_step(serial, "xiaoluxue_map_fast_path", args, result, before=before)
            return result
        action_name = "practise"

    selected_index = str(snapshot.get("selected_index") or "").strip() or None
    if not index:
        index = selected_index
    if not index:
        raise AndroidUseError("Xiaoluxue map fast path needs an index such as 1.5, or a selected map node.")

    index_node = find_xiaoluxue_map_index_node(nodes, index)
    if not index_node:
        visible = ", ".join(snapshot.get("visible_indexes") or [])
        raise AndroidUseError(f"Could not find Xiaoluxue map index {index!r}. Visible indexes: {visible}")

    if action_name == "select":
        xiaoluxue_map_tap_node(serial, index_node, f"index:{index}", steps, started_at)
        xiaoluxue_update_cached_selected_index(serial, index)
        result = {
            "ok": True,
            "action": "xiaoluxue_map_fast_path",
            "map_action": "select",
            "index": index,
            "elapsed_sec": round(time.monotonic() - started_at, 3),
            "steps": steps,
            "snapshot": snapshot,
        }
        if record:
            append_recording_step(serial, "xiaoluxue_map_fast_path", args, result, before=before)
        return result

    action_node = xiaoluxue_map_action_node(nodes, action_name)
    action_point = node_click_point(action_node) if action_node else None
    action_is_for_selected = selected_index == index and action_point is not None

    if not action_is_for_selected:
        xiaoluxue_map_tap_node(serial, index_node, f"index:{index}", steps, started_at)
        time.sleep(wait_after_select)
        if prefer_predicted:
            predicted = xiaoluxue_map_predicted_action_point(index_node, action_name)
            if predicted:
                xiaoluxue_map_tap_point(serial, predicted, f"{action_name}:predicted", steps, started_at, None)
                module_entry = xiaoluxue_tap_study_module_entry(
                    serial,
                    action_name=action_name,
                    args=args,
                    steps=steps,
                    started_at=started_at,
                    open_report_when_done=open_report_when_done,
                    screen_action_point=predicted,
                )
                xiaoluxue_update_cached_selected_index(serial, index)
                result = {
                    "ok": True,
                    "action": "xiaoluxue_map_fast_path",
                    "map_action": action_name,
                    "index": index,
                    "predicted": True,
                    "entered_module": bool(module_entry),
                    "module_entry": module_entry,
                    "elapsed_sec": round(time.monotonic() - started_at, 3),
                    "steps": steps,
                    "snapshot": snapshot,
                }
                if record:
                    append_recording_step(serial, "xiaoluxue_map_fast_path", args, result, before=before)
                return result
        after_select = xiaoluxue_observe_native_map(serial, limit=800)
        nodes = after_select["ui"]["nodes"]
        snapshot = after_select["xiaoluxue_map"]
        xiaoluxue_remember_native_map_cache(serial, nodes, snapshot)
        action_node = xiaoluxue_map_action_node(nodes, action_name)
        action_point = node_click_point(action_node) if action_node else None

    if not action_point:
        raise AndroidUseError(f"Could not find Xiaoluxue map action {action_name!r} for index {index!r}.")
    xiaoluxue_map_tap_point(serial, action_point, action_name, steps, started_at, action_node)
    module_entry = xiaoluxue_tap_study_module_entry(
        serial,
        action_name=action_name,
        args=args,
        steps=steps,
        started_at=started_at,
        open_report_when_done=open_report_when_done,
        screen_action_point=xiaoluxue_map_point(action_point),
    )

    if open_report_when_done:
        time.sleep(report_wait_sec)
        report_observation = observe_ui(serial, limit=500)
        report_node = find_ui_node(report_observation.get("ui", {}).get("nodes", []), "看报告", exact=True)
        if report_node:
            xiaoluxue_map_tap_node(serial, report_node, "看报告", steps, started_at)

    result = {
        "ok": True,
        "action": "xiaoluxue_map_fast_path",
        "map_action": action_name,
        "index": index,
        "predicted": False,
        "entered_module": bool(module_entry),
        "module_entry": module_entry,
        "elapsed_sec": round(time.monotonic() - started_at, 3),
        "steps": steps,
        "snapshot": snapshot,
    }
    if record:
        append_recording_step(serial, "xiaoluxue_map_fast_path", args, result, before=before)
    return result


def tool_xiaoluxue_map_snapshot(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    observation = xiaoluxue_observe_native_map(serial, limit=int(args.get("limit", 800)))
    return [
        text_content(
            {
                "ok": True,
                "serial": serial,
                "snapshot": observation["xiaoluxue_map"],
            }
        )
    ]


def run_xiaoluxue_open_native_subject(serial: str, args: dict[str, Any], *, record: bool) -> dict[str, Any]:
    started_at = time.monotonic()
    steps: list[dict[str, Any]] = []
    route_info = xiaoluxue_open_native_subject_map(serial, args, steps, started_at)
    result: dict[str, Any] = {
        "ok": True,
        "action": "xiaoluxue_open_native_subject",
        "subject_id": route_info["subject_id"],
        "route_url": route_info["route"]["url"],
        "elapsed_sec": round(time.monotonic() - started_at, 3),
        "steps": steps,
    }
    if bool(args.get("verify_focus", False)):
        info = xiaoluxue_wait_native_app_focus(serial, float(args.get("focus_timeout_sec", 1.5)))
        result["window"] = info
        result["elapsed_sec"] = round(time.monotonic() - started_at, 3)
    if record:
        append_recording_step(
            serial,
            "xiaoluxue_open_native_subject",
            args,
            result,
            before={"state": {}, "ui": {"nodes": []}},
        )
    return result


def tool_xiaoluxue_open_native_subject(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    result = run_xiaoluxue_open_native_subject(serial, args, record=True)
    return [text_content({"ok": True, "serial": serial, **result})]


def tool_xiaoluxue_map_fast_path(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    result = run_xiaoluxue_map_fast_path(serial, args, record=True)
    return [text_content({"ok": True, "serial": serial, **result})]


def run_xiaoluxue_lesson_fast_path(serial: str, args: dict[str, Any], *, record: bool) -> dict[str, Any]:
    started_at = time.monotonic()
    steps: list[dict[str, Any]] = []
    action_name = normalize_xiaoluxue_lesson_action(args.get("action_name") or args.get("action") or args.get("instruction"))
    if action_name not in {"direct_practice", "continue_answer", "finish_result"}:
        raise AndroidUseError(f"Unsupported Xiaoluxue lesson fast path action: {action_name!r}")
    before = (
        {"state": {}, "ui": {"nodes": []}}
        if active_recording(serial)
        else None
    )
    action_args = dict(args)
    action_args.setdefault("assume_lesson_activity", True)
    action_result_payload: dict[str, Any]
    if action_name == "direct_practice":
        action_result_payload = xiaoluxue_tap_lesson_direct_practice(
            serial,
            action_args,
            steps,
            started_at,
            default_wait_sec=0.0,
        )
    elif action_name == "continue_answer":
        action_result_payload = xiaoluxue_tap_lesson_continue_answer(
            serial,
            action_args,
            steps,
            started_at,
        )
    else:
        action_result_payload = xiaoluxue_tap_lesson_finish_result(
            serial,
            action_args,
            steps,
            started_at,
        )
    result = {
        "ok": True,
        "action": "xiaoluxue_lesson_fast_path",
        "lesson_action": action_name,
        "result": action_result_payload,
        "elapsed_sec": round(time.monotonic() - started_at, 3),
        "steps": steps,
    }
    if action_name == "direct_practice":
        result["direct_practice"] = action_result_payload
    elif action_name == "continue_answer":
        result["continue_answer"] = action_result_payload
    else:
        result["finish_result"] = action_result_payload
    if record:
        append_recording_step(serial, "xiaoluxue_lesson_fast_path", args, result, before=before)
    return result


def tool_xiaoluxue_lesson_fast_path(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    result = run_xiaoluxue_lesson_fast_path(serial, args, record=True)
    return [text_content({"ok": True, "serial": serial, **result})]

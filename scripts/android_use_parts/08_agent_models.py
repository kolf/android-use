# Loaded by scripts/android_use_mcp.py. Keep this file below 2000 lines.

def vlm_endpoint() -> str:
    base_url = os.environ.get("ANDROID_USE_VLM_BASE_URL")
    if not base_url:
        raise AndroidUseError("ANDROID_USE_VLM_BASE_URL is not configured.")
    base_url = base_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def openai_api_key() -> str:
    api_key = os.environ.get("ANDROID_USE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise AndroidUseError("OPENAI_API_KEY or ANDROID_USE_OPENAI_API_KEY is not configured.")
    return api_key


def openai_responses_endpoint() -> str:
    base_url = os.environ.get("ANDROID_USE_OPENAI_BASE_URL", OPENAI_BASE_URL).rstrip("/")
    if base_url.endswith("/responses"):
        return base_url
    return f"{base_url}/responses"


def post_openai_responses(payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        openai_responses_endpoint(),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {openai_api_key()}",
        },
        method="POST",
    )
    timeout = float(os.environ.get("ANDROID_USE_OPENAI_TIMEOUT", "45"))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise AndroidUseError(f"OpenAI Responses request failed: HTTP {exc.code}\n{body}") from exc
    except urllib.error.URLError as exc:
        raise AndroidUseError(f"OpenAI Responses request failed: {exc}") from exc


MOBILE_TARS_PROMPT = """You are a GUI agent controlling an Android device. You are given a task, action history, current device state, UI tree text, and a screenshot.
You need to perform the next action to complete the task.

## Output Format
Thought: ...
Action: ...

## Action Space
click(point='x y')
long_press(point='x y')
type(content='text')
scroll(point='x y', direction='down or up or right or left')
open_app(app_name='name')
drag(start_point='x1 y1', end_point='x2 y2')
press_home()
press_back()
wait()
finished(content='summary')

## Rules
- Output exactly one Thought and one Action.
- Use the screenshot for visual grounding and the UI tree for text grounding.
- Prefer direct click on visible targets.
- Coordinates are screen pixels unless the model is trained to output normalized UI-TARS coordinates.
- If the task is already complete, use finished(content='...').
"""


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            return json.loads(stripped[start : end + 1])
        raise


def clean_model_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:\w+)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def parse_call_string(value: str) -> str:
    value = value.strip()
    try:
        return str(ast.literal_eval(value))
    except (SyntaxError, ValueError):
        return value.strip("\"'")


def parse_tars_point(value: str) -> tuple[int, int]:
    numbers = re.findall(r"-?\d+(?:\.\d+)?", value)
    if len(numbers) < 2:
        raise AndroidUseError(f"Could not parse point from {value!r}")
    return round(float(numbers[0])), round(float(numbers[1]))


def infer_coordinate_mode(model: str | None, explicit_mode: str | None = None) -> str:
    if explicit_mode:
        return explicit_mode
    env_mode = os.environ.get("ANDROID_USE_VLM_COORDINATE_MODE")
    if env_mode:
        return env_mode
    model_name = (model or os.environ.get("ANDROID_USE_VLM_MODEL") or "").casefold()
    if any(token in model_name for token in ("ui-tars", "uitars", "seed", "doubao", "tars")):
        return "normalized_1000"
    return "absolute"


def scale_point_for_screen(
    x: int,
    y: int,
    screen: dict[str, int | None],
    coordinate_mode: str,
) -> tuple[int, int]:
    width = int(screen.get("width") or 0)
    height = int(screen.get("height") or 0)
    mode = coordinate_mode.lower()
    if mode in {"normalized_1000", "qwen25vl", "uitars", "ui-tars"} and width > 0 and height > 0:
        return round(x / 1000 * width), round(y / 1000 * height)
    return x, y


def parse_tars_action_response(
    response_text: str,
    screen: dict[str, int | None],
    *,
    coordinate_mode: str = "absolute",
) -> dict[str, Any]:
    text = clean_model_text(response_text)
    try:
        action = extract_json_object(text)
        action["_raw_model_response"] = response_text
        return action
    except json.JSONDecodeError:
        pass

    thought = ""
    thought_match = re.search(r"Thought:\s*(.*?)(?:\n\s*Action:|$)", text, flags=re.S | re.I)
    if thought_match:
        thought = thought_match.group(1).strip()
    action_match = re.search(r"Action:\s*(.*)", text, flags=re.S | re.I)
    if action_match:
        action_line = action_match.group(1).strip().splitlines()[0].strip()
    else:
        first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
        if not re.match(r"^(click|long_press|type|scroll|open_app|drag|press_home|press_back|wait|finished)\(", first_line, flags=re.I):
            raise AndroidUseError(f"VLM response did not include an Action: {response_text}")
        action_line = first_line

    def point_arg(name: str = "point") -> tuple[int, int]:
        match = re.search(rf"{name}\s*=\s*('[^']*'|\"[^\"]*\"|\([^)]+\)|[^,\)]+)", action_line)
        if not match and name == "point":
            match = re.search(r"start_box\s*=\s*('[^']*'|\"[^\"]*\"|\([^)]+\)|[^,\)]+)", action_line)
        if not match:
            raise AndroidUseError(f"Missing {name}=... in action: {action_line}")
        x_raw, y_raw = parse_tars_point(match.group(1))
        return scale_point_for_screen(x_raw, y_raw, screen, coordinate_mode)

    def string_arg(name: str) -> str:
        match = re.search(rf"{name}\s*=\s*('[^']*'|\"[^\"]*\")", action_line, flags=re.S)
        if not match:
            return ""
        return parse_call_string(match.group(1))

    lower = action_line.lower()
    if lower.startswith("click("):
        x, y = point_arg("point")
        return {"action": "tap", "x": x, "y": y, "thought": thought, "_raw_model_response": response_text}
    if lower.startswith("long_press("):
        x, y = point_arg("point")
        return {
            "action": "long_press",
            "x": x,
            "y": y,
            "duration_ms": 700,
            "thought": thought,
            "_raw_model_response": response_text,
        }
    if lower.startswith("drag("):
        start_x, start_y = point_arg("start_point")
        end_x, end_y = point_arg("end_point")
        return {
            "action": "swipe",
            "start_x": start_x,
            "start_y": start_y,
            "end_x": end_x,
            "end_y": end_y,
            "duration_ms": 350,
            "thought": thought,
            "_raw_model_response": response_text,
        }
    if lower.startswith("scroll("):
        x, y = point_arg("point")
        direction = string_arg("direction").lower() or "down"
        width = int(screen.get("width") or 1080)
        height = int(screen.get("height") or 1920)
        distance = max(180, min(width, height) // 4)
        start_x = end_x = x
        start_y = end_y = y
        if direction == "down":
            end_y = max(1, y - distance)
        elif direction == "up":
            end_y = min(height - 1, y + distance)
        elif direction == "left":
            end_x = min(width - 1, x + distance)
        elif direction == "right":
            end_x = max(1, x - distance)
        return {
            "action": "swipe",
            "start_x": start_x,
            "start_y": start_y,
            "end_x": end_x,
            "end_y": end_y,
            "duration_ms": 300,
            "direction": direction,
            "thought": thought,
            "_raw_model_response": response_text,
        }
    if lower.startswith("type("):
        content = string_arg("content")
        enter = content.endswith("\n")
        if enter:
            content = content[:-1]
        return {
            "action": "type_text",
            "text": content,
            "enter": enter,
            "thought": thought,
            "_raw_model_response": response_text,
        }
    if lower.startswith("open_app("):
        app_name = string_arg("app_name")
        return {"action": "open_app_name", "app_name": app_name, "thought": thought, "_raw_model_response": response_text}
    if lower.startswith("press_home("):
        return {"action": "press_key", "key": "HOME", "thought": thought, "_raw_model_response": response_text}
    if lower.startswith("press_back("):
        return {"action": "press_key", "key": "BACK", "thought": thought, "_raw_model_response": response_text}
    if lower.startswith("wait("):
        return {"action": "wait", "seconds": 1, "thought": thought, "_raw_model_response": response_text}
    if lower.startswith("finished("):
        return {"action": "done", "summary": string_arg("content"), "thought": thought, "_raw_model_response": response_text}
    raise AndroidUseError(f"Unsupported VLM action syntax: {action_line}")


def compact_ui_for_prompt(nodes: list[dict[str, Any]], *, limit: int = 80) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for node in nodes:
        labels = node_labels(node)
        if not labels:
            continue
        point = node_click_point(node)
        compact.append(
            {
                "index": node.get("index"),
                "text": node.get("text"),
                "content_desc": node.get("content_desc"),
                "resource_id": node.get("resource_id"),
                "class": node.get("class"),
                "bounds": node.get("bounds"),
                "tap": point,
                "selected": node.get("selected"),
            }
        )
        if len(compact) >= limit:
            break
    return compact


def build_agent_user_text(
    instruction: str,
    state: dict[str, Any],
    screen: dict[str, int | None],
    *,
    history: list[dict[str, Any]] | None = None,
    ui_nodes: list[dict[str, Any]] | None = None,
    coordinate_mode: str | None = None,
) -> str:
    return (
        f"Task: {instruction}\n"
        f"Device state: {json.dumps(state, ensure_ascii=False)}\n"
        f"Action history: {json.dumps(history or [], ensure_ascii=False)}\n"
        f"Visible UI nodes: {json.dumps(compact_ui_for_prompt(ui_nodes or []), ensure_ascii=False)}\n"
        f"Screenshot size: {screen.get('width')}x{screen.get('height')}\n"
        f"Coordinate mode expected by executor: {coordinate_mode or 'absolute'}\n"
        "Return the single best next action."
    )


def extract_openai_response_text(response_payload: dict[str, Any]) -> str:
    output_text = response_payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    chunks: list[str] = []
    for item in response_payload.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content", []) or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"output_text", "text"} and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return "\n".join(chunks).strip()


def map_openai_computer_action(action: dict[str, Any], screen: dict[str, int | None]) -> dict[str, Any]:
    action_type = str(action.get("type") or action.get("action") or "").lower()
    if action_type in {"click", "double_click"}:
        return {
            "action": "double_tap" if action_type == "double_click" else "tap",
            "x": int(round(float(action["x"]))),
            "y": int(round(float(action["y"]))),
            "button": action.get("button", "left"),
            "source": "openai-computer",
        }
    if action_type in {"drag", "drag_path"}:
        path = action.get("path") or []
        if len(path) >= 2:
            start = path[0]
            end = path[-1]
            return {
                "action": "swipe",
                "start_x": int(round(float(start["x"]))),
                "start_y": int(round(float(start["y"]))),
                "end_x": int(round(float(end["x"]))),
                "end_y": int(round(float(end["y"]))),
                "duration_ms": 350,
                "source": "openai-computer",
            }
    if action_type == "scroll":
        x = int(round(float(action.get("x", (screen.get("width") or 1080) / 2))))
        y = int(round(float(action.get("y", (screen.get("height") or 1920) / 2))))
        scroll_x = float(action.get("scroll_x", action.get("scrollX", 0)) or 0)
        scroll_y = float(action.get("scroll_y", action.get("scrollY", 0)) or 0)
        width = int(screen.get("width") or 1080)
        height = int(screen.get("height") or 1920)
        distance_x = max(120, min(width // 3, int(abs(scroll_x) or 0)))
        distance_y = max(120, min(height // 3, int(abs(scroll_y) or 0)))
        end_x = x
        end_y = y
        if abs(scroll_y) >= abs(scroll_x):
            end_y = y - distance_y if scroll_y > 0 else y + distance_y
            end_y = max(1, min(height - 1, end_y))
        else:
            end_x = x - distance_x if scroll_x > 0 else x + distance_x
            end_x = max(1, min(width - 1, end_x))
        return {
            "action": "swipe",
            "start_x": x,
            "start_y": y,
            "end_x": end_x,
            "end_y": end_y,
            "duration_ms": 300,
            "source": "openai-computer",
        }
    if action_type == "type":
        return {"action": "type_text", "text": str(action.get("text", "")), "source": "openai-computer"}
    if action_type in {"keypress", "key"}:
        keys = action.get("keys") or [action.get("key")]
        mapped_keys = [str(key).upper().replace("ARROW", "DPAD_") for key in keys if key]
        if len(mapped_keys) == 1:
            return {"action": "press_key", "key": mapped_keys[0], "source": "openai-computer"}
        return {
            "action": "batch",
            "actions": [{"action": "press_key", "key": key, "source": "openai-computer"} for key in mapped_keys],
            "source": "openai-computer",
        }
    if action_type in {"wait", "screenshot"}:
        return {"action": "wait", "seconds": 0.5, "source": "openai-computer"}
    raise AndroidUseError(f"Unsupported OpenAI computer action: {action}")


def extract_openai_computer_actions(
    response_payload: dict[str, Any],
    screen: dict[str, int | None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    mapped: list[dict[str, Any]] = []
    raw_calls: list[dict[str, Any]] = []
    for item in response_payload.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type not in {"computer_call", "computer_use_call"}:
            continue
        raw_calls.append(item)
        actions = item.get("actions")
        if isinstance(actions, list):
            for action in actions:
                if isinstance(action, dict):
                    mapped.append(map_openai_computer_action(action, screen))
        elif isinstance(item.get("action"), dict):
            mapped.append(map_openai_computer_action(item["action"], screen))
    return mapped, raw_calls


def call_openai_vision(
    instruction: str,
    png: bytes,
    state: dict[str, Any],
    model_override: str | None = None,
    *,
    history: list[dict[str, Any]] | None = None,
    ui_nodes: list[dict[str, Any]] | None = None,
    coordinate_mode: str | None = None,
) -> dict[str, Any]:
    model = model_override or os.environ.get("ANDROID_USE_OPENAI_VISION_MODEL") or os.environ.get("ANDROID_USE_OPENAI_MODEL") or "gpt-5.5"
    screen = png_size(png)
    resolved_coordinate_mode = infer_coordinate_mode(model, coordinate_mode)
    payload: dict[str, Any] = {
        "model": model,
        "instructions": MOBILE_TARS_PROMPT,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": build_agent_user_text(
                            instruction,
                            state,
                            screen,
                            history=history,
                            ui_nodes=ui_nodes,
                            coordinate_mode=resolved_coordinate_mode,
                        ),
                    },
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
                        "detail": os.environ.get("ANDROID_USE_OPENAI_IMAGE_DETAIL", "low"),
                    },
                ],
            }
        ],
    }
    reasoning_effort = os.environ.get("ANDROID_USE_OPENAI_REASONING_EFFORT")
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}
    response_payload = post_openai_responses(payload)
    content = extract_openai_response_text(response_payload)
    if not content:
        raise AndroidUseError(f"OpenAI vision response did not include text: {json.dumps(response_payload)[:1000]}")
    return parse_tars_action_response(content, screen, coordinate_mode=resolved_coordinate_mode)


def call_openai_computer(
    instruction: str,
    png: bytes,
    state: dict[str, Any],
    model_override: str | None = None,
    *,
    history: list[dict[str, Any]] | None = None,
    ui_nodes: list[dict[str, Any]] | None = None,
    coordinate_mode: str | None = None,
) -> dict[str, Any]:
    model = model_override or os.environ.get("ANDROID_USE_OPENAI_COMPUTER_MODEL") or os.environ.get("ANDROID_USE_OPENAI_MODEL") or "gpt-5.5"
    screen = png_size(png)
    display_width = int(screen.get("width") or state.get("screen", {}).get("width") or 1080)
    display_height = int(screen.get("height") or state.get("screen", {}).get("height") or 1920)
    user_text = build_agent_user_text(
        instruction,
        state,
        screen,
        history=history,
        ui_nodes=ui_nodes,
        coordinate_mode=coordinate_mode or "absolute",
    )
    use_preview = model == "computer-use-preview" or os.environ.get("ANDROID_USE_OPENAI_COMPUTER_TOOL") == "computer_use_preview"
    if use_preview:
        tools = [
            {
                "type": "computer_use_preview",
                "display_width": display_width,
                "display_height": display_height,
                "environment": os.environ.get("ANDROID_USE_OPENAI_COMPUTER_ENVIRONMENT", "browser"),
            }
        ]
    else:
        tools = [
            {
                "type": "computer",
                "display_width": display_width,
                "display_height": display_height,
                "environment": os.environ.get("ANDROID_USE_OPENAI_COMPUTER_ENVIRONMENT", "browser"),
            }
        ]

    payload: dict[str, Any] = {
        "model": model,
        "tools": tools,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": user_text
                        + "\nYou are controlling an Android device through adb. Request a screenshot if needed, then choose the next concrete computer action.",
                    }
                ],
            }
        ],
        "truncation": "auto",
    }
    response_payload = post_openai_responses(payload)
    mapped, raw_calls = extract_openai_computer_actions(response_payload, screen)

    # GA computer models can first ask for a screenshot. Satisfy one screenshot request internally.
    if not mapped and raw_calls:
        call_id = raw_calls[-1].get("call_id") or raw_calls[-1].get("id")
        if call_id:
            response_payload = post_openai_responses(
                {
                    "model": model,
                    "tools": tools,
                    "previous_response_id": response_payload.get("id"),
                    "input": [
                        {
                            "type": "computer_call_output",
                            "call_id": call_id,
                            "output": {
                                "type": "input_image",
                                "image_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
                            },
                        }
                    ],
                    "truncation": "auto",
                }
            )
            mapped, raw_calls = extract_openai_computer_actions(response_payload, screen)

    if mapped:
        if len(mapped) == 1:
            mapped[0]["_raw_model_response"] = json.dumps(response_payload, ensure_ascii=False)
            return mapped[0]
        return {
            "action": "batch",
            "actions": mapped,
            "source": "openai-computer",
            "_raw_model_response": json.dumps(response_payload, ensure_ascii=False),
        }

    content = extract_openai_response_text(response_payload)
    if content:
        return parse_tars_action_response(content, screen, coordinate_mode=coordinate_mode or "absolute")
    raise AndroidUseError(f"OpenAI computer response did not include executable actions: {json.dumps(response_payload)[:1000]}")


def call_vlm(
    instruction: str,
    png: bytes,
    state: dict[str, Any],
    model_override: str | None = None,
    *,
    history: list[dict[str, Any]] | None = None,
    ui_nodes: list[dict[str, Any]] | None = None,
    coordinate_mode: str | None = None,
) -> dict[str, Any]:
    api_key = os.environ.get("ANDROID_USE_VLM_API_KEY")
    model = model_override or os.environ.get("ANDROID_USE_VLM_MODEL")
    if not api_key:
        raise AndroidUseError("ANDROID_USE_VLM_API_KEY is not configured.")
    if not model:
        raise AndroidUseError("ANDROID_USE_VLM_MODEL is not configured.")

    screen = png_size(png)
    resolved_coordinate_mode = infer_coordinate_mode(model, coordinate_mode)
    user_text = (
        f"Task: {instruction}\n"
        f"Device state: {json.dumps(state, ensure_ascii=False)}\n"
        f"Action history: {json.dumps(history or [], ensure_ascii=False)}\n"
        f"Visible UI nodes: {json.dumps(compact_ui_for_prompt(ui_nodes or []), ensure_ascii=False)}\n"
        f"Screenshot size: {screen.get('width')}x{screen.get('height')}\n"
        f"Coordinate mode expected by executor: {resolved_coordinate_mode}\n"
        "Return the single best next action."
    )
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 900,
        "messages": [
            {"role": "system", "content": MOBILE_TARS_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(png).decode("ascii")
                        },
                    },
                ],
            },
        ],
    }
    request = urllib.request.Request(
        vlm_endpoint(),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    timeout = float(os.environ.get("ANDROID_USE_VLM_TIMEOUT", "45"))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise AndroidUseError(f"VLM request failed: HTTP {exc.code}\n{body}") from exc
    except urllib.error.URLError as exc:
        raise AndroidUseError(f"VLM request failed: {exc}") from exc

    try:
        content = response_payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AndroidUseError(f"Unexpected VLM response: {json.dumps(response_payload)[:1000]}") from exc
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str):
        raise AndroidUseError(f"Unexpected VLM message content: {content!r}")
    return parse_tars_action_response(content, screen, coordinate_mode=resolved_coordinate_mode)


def resolve_agent_provider(provider: str | None = None) -> str:
    selected = (provider or os.environ.get("ANDROID_USE_AGENT_PROVIDER") or "").strip().lower()
    if selected == "auto":
        selected = ""
    if selected:
        return selected
    if os.environ.get("ANDROID_USE_VLM_BASE_URL"):
        return "openai-compatible"
    if os.environ.get("ANDROID_USE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        return "openai-computer"
    return "openai-compatible"


def call_agent_model(
    instruction: str,
    png: bytes,
    state: dict[str, Any],
    model_override: str | None = None,
    *,
    history: list[dict[str, Any]] | None = None,
    ui_nodes: list[dict[str, Any]] | None = None,
    coordinate_mode: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    resolved_provider = resolve_agent_provider(provider)
    if resolved_provider in {"openai-computer", "openai-cua", "openai-cua-preview", "computer"}:
        return call_openai_computer(
            instruction,
            png,
            state,
            model_override,
            history=history,
            ui_nodes=ui_nodes,
            coordinate_mode=coordinate_mode,
        )
    if resolved_provider in {"openai-vision", "openai-responses", "openai"}:
        return call_openai_vision(
            instruction,
            png,
            state,
            model_override,
            history=history,
            ui_nodes=ui_nodes,
            coordinate_mode=coordinate_mode,
        )
    if resolved_provider in {"openai-compatible", "chat-completions", "vlm", "seed"}:
        return call_vlm(
            instruction,
            png,
            state,
            model_override,
            history=history,
            ui_nodes=ui_nodes,
            coordinate_mode=coordinate_mode,
        )
    raise AndroidUseError(
        "Unsupported Android agent provider. Use openai-computer, openai-vision, or openai-compatible."
    )


def execute_action(serial: str, action: dict[str, Any]) -> list[dict[str, Any]]:
    action_type = str(action.get("action", "")).lower()
    if action_type == "batch":
        content: list[dict[str, Any]] = []
        for child_action in action.get("actions", []):
            if isinstance(child_action, dict):
                content.extend(execute_action(serial, child_action))
        return content or [text_content(action_result("batch", serial, {"actions": 0}))]
    if action_type == "click":
        action_type = "tap"
    if action_type == "tap":
        return tool_tap({"serial": serial, "x": action["x"], "y": action["y"]})
    if action_type == "tap_text":
        return tool_tap_text(
            {
                "serial": serial,
                "text": action["text"],
                "exact": bool(action.get("exact", True)),
                "include_resource_id": bool(action.get("include_resource_id", False)),
            }
        )
    if action_type == "double_tap":
        x = int(action["x"])
        y = int(action["y"])
        adb(["shell", "input", "tap", str(x), str(y)], serial=serial, timeout=10)
        time.sleep(0.08)
        adb(["shell", "input", "tap", str(x), str(y)], serial=serial, timeout=10)
        return [text_content(action_result("double_tap", serial, {"x": x, "y": y}))]
    if action_type == "long_press":
        duration_ms = int(action.get("duration_ms", 700))
        x = int(action["x"])
        y = int(action["y"])
        adb(
            ["shell", "input", "swipe", str(x), str(y), str(x), str(y), str(duration_ms)],
            serial=serial,
            timeout=10,
        )
        return [text_content(action_result("long_press", serial, {"x": x, "y": y, "duration_ms": duration_ms}))]
    if action_type == "swipe":
        return tool_swipe(
            {
                "serial": serial,
                "start_x": action["start_x"],
                "start_y": action["start_y"],
                "end_x": action["end_x"],
                "end_y": action["end_y"],
                "duration_ms": action.get("duration_ms", 300),
            }
        )
    if action_type == "type_text":
        return tool_type_text({"serial": serial, "text": action.get("text", ""), "enter": bool(action.get("enter"))})
    if action_type == "press_key":
        return tool_press_key({"serial": serial, "key": action["key"]})
    if action_type == "open_url":
        return tool_open_url({"serial": serial, "url": action["url"]})
    if action_type == "open_app":
        return tool_open_app({"serial": serial, "package": action["package"], "activity": action.get("activity", "")})
    if action_type == "open_app_name":
        app_name = str(action.get("app_name", "")).strip()
        if "." in app_name and " " not in app_name:
            return tool_open_app({"serial": serial, "package": app_name})
        raise AndroidUseError(
            f"open_app(app_name={app_name!r}) needs a package name on Android. "
            "Ask the model to click the launcher icon if it is visible, or use android_open_app with a package."
        )
    if action_type == "wait":
        seconds = min(float(action.get("seconds", 1)), 10)
        time.sleep(seconds)
        return [text_content(action_result("wait", serial, {"seconds": seconds}))]
    if action_type == "done":
        return [text_content({"ok": True, "serial": serial, "action": "done", "summary": action.get("summary")})]
    raise AndroidUseError(f"Unsupported VLM action: {action_type}")


def tool_agent_step(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    instruction = str(args["instruction"])
    execute = bool(args.get("execute", False))
    mode = str(args.get("mode", "hybrid")).lower()
    history = args.get("history") if isinstance(args.get("history"), list) else []
    scrcpy_result = ensure_default_scrcpy_window(serial, args) if execute else {"ok": True, "skipped": "not-executing"}

    if mode == "hybrid":
        fast_action = fast_webview_action_from_instruction(serial, instruction)
        if fast_action:
            content = [
                text_content(
                    {
                        "serial": serial,
                        "proposed_action": fast_action,
                        "execute": execute,
                        "mode": mode,
                        "scrcpy": scrcpy_result,
                    }
                )
            ]
            if execute:
                content.extend(execute_action(serial, fast_action))
            return content

    if mode in {"hybrid", "uiautomator", "accessibility"}:
        fast_action = fast_ui_action_from_instruction(serial, instruction)
        if fast_action:
            content = [
                text_content(
                    {
                        "serial": serial,
                        "proposed_action": fast_action,
                        "execute": execute,
                        "mode": mode,
                        "scrcpy": scrcpy_result,
                    }
                )
            ]
            if execute:
                content.extend(execute_action(serial, fast_action))
            return content
        if mode in {"uiautomator", "accessibility"}:
            raise AndroidUseError(f"Could not satisfy instruction from Android UI tree alone: {instruction}")

    observation = observe_ui(serial, limit=220)
    state = observation["state"]
    ui_nodes = observation["ui"]["nodes"]
    png = screenshot_png(serial)
    action = call_agent_model(
        instruction,
        png,
        state,
        args.get("model"),
        history=history,
        ui_nodes=ui_nodes,
        coordinate_mode=args.get("coordinate_mode"),
        provider=args.get("provider"),
    )
    action_for_display = {key: value for key, value in action.items() if key != "_raw_model_response"}
    content = [
        text_content(
            {
                "serial": serial,
                "proposed_action": action_for_display,
                "execute": execute,
                "mode": mode,
                "scrcpy": scrcpy_result,
            }
        )
    ]
    if execute:
        content.extend(execute_action(serial, action))
    return content


def tool_agent_run(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    instruction = str(args["instruction"])
    max_steps = max(1, min(int(args.get("max_steps", 5)), 20))
    dry_run = bool(args.get("dry_run", False))
    delay_sec = min(float(args.get("delay_sec", 0.25)), 5)
    mode = str(args.get("mode", "hybrid")).lower()
    history: list[dict[str, Any]] = []
    scrcpy_result = ensure_default_scrcpy_window(serial, args) if not dry_run else {"ok": True, "skipped": "dry-run"}

    for step_index in range(max_steps):
        action: dict[str, Any] | None = None
        source = "vlm"
        if mode == "hybrid":
            action = fast_webview_action_from_instruction(serial, instruction)
            if action:
                source = "webview"
        if mode in {"hybrid", "uiautomator", "accessibility"}:
            if not action:
                action = fast_ui_action_from_instruction(serial, instruction)
            if action and source != "webview":
                source = "uiautomator"
            elif mode in {"uiautomator", "accessibility"}:
                raise AndroidUseError(f"Could not satisfy instruction from Android UI tree alone: {instruction}")
        if not action:
            observation = observe_ui(serial, limit=220)
            state = observation["state"]
            ui_nodes = observation["ui"]["nodes"]
            png = screenshot_png(serial)
            action = call_agent_model(
                instruction,
                png,
                state,
                args.get("model"),
                history=history,
                ui_nodes=ui_nodes,
                coordinate_mode=args.get("coordinate_mode"),
                provider=args.get("provider"),
            )
        action_for_history = {key: value for key, value in action.items() if key != "_raw_model_response"}
        history.append({"step": step_index + 1, "source": source, "action": action_for_history})
        if dry_run:
            break
        if str(action.get("action", "")).lower() == "done":
            break
        execute_action(serial, action)
        if source in {"webview", "uiautomator"}:
            break
        time.sleep(delay_sec)

    return [text_content({"serial": serial, "dry_run": dry_run, "scrcpy": scrcpy_result, "steps": history})]

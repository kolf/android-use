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

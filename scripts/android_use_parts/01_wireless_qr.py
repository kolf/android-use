# Loaded by scripts/android_use_mcp.py. Keep this file below 2000 lines.

import secrets
import string
import zlib


ADB_QR_VERSION = 4
ADB_QR_ECC_CODEWORDS = 20
ADB_QR_DATA_CODEWORDS = 80
ADB_QR_SCALE = 8
ADB_QR_BORDER = 4
ADB_QR_TOKEN_CHARS = string.ascii_letters + string.digits


def android_connection_help(devices: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    known_devices = devices if devices is not None else []
    return {
        "title": "没有发现已授权的 Android 设备",
        "message": "请选择有线或无线方式连接设备，然后重新执行 Android 工具。",
        "known_devices": known_devices,
        "methods": [
            {
                "name": "有线连接",
                "steps": [
                    "用 USB 线连接手机或平板到这台电脑。",
                    "在设备开发者选项里打开 USB 调试。",
                    "设备弹出授权时选择允许，然后确认 adb devices -l 里状态是 device。",
                ],
            },
            {
                "name": "无线连接",
                "steps": [
                    "在设备上打开开发者选项里的无线调试。",
                    "调用 android_wireless_pair_qr(action='create') 生成配对二维码。",
                    "在设备无线调试页面选择 Pair device with QR code / 使用二维码配对设备 并扫码。",
                    "扫码后调用 android_wireless_pair_qr(action='complete', session_id='返回的 session_id') 完成 adb 配对和连接。",
                ],
            },
        ],
        "tools": {
            "qr_pair_create": "android_wireless_pair_qr(action='create')",
            "qr_pair_complete": "android_wireless_pair_qr(action='complete', session_id='<session_id>')",
            "manual_pair": "android_wireless_pair(host='<ip>', pair_port=<port>, code='<code>')",
        },
    }


def android_connection_help_text(devices: list[dict[str, Any]] | None = None) -> str:
    help_payload = android_connection_help(devices)
    known = ", ".join(str(item.get("serial") or "?") for item in help_payload["known_devices"]) or "none"
    return (
        "没有发现已授权的 Android 设备。\n"
        f"当前 adb 已知设备: {known}\n\n"
        "请用以下任一方式连接设备：\n"
        "1. 有线连接：用 USB 线连接设备，打开开发者选项里的 USB 调试，在设备弹窗里允许调试，"
        "然后确认 `adb devices -l` 显示 `device`。\n"
        "2. 无线连接：打开设备开发者选项里的无线调试，调用 "
        "`android_wireless_pair_qr(action='create')` 生成配对二维码；扫码后调用 "
        "`android_wireless_pair_qr(action='complete', session_id='<session_id>')` 完成配对。"
    )


def random_adb_qr_token(length: int) -> str:
    return "".join(secrets.choice(ADB_QR_TOKEN_CHARS) for _ in range(length))


def wireless_qr_payload(service_name: str, password: str) -> str:
    return f"WIFI:T:ADB;S:{service_name};P:{password};;"


def wireless_qr_pairing_dir() -> Path:
    return ANDROID_USE_DIR / "wireless-pairing"


def wireless_qr_session_path(session_id: str) -> Path:
    safe_session = slugify(session_id, default="session")
    return wireless_qr_pairing_dir() / f"{safe_session}.json"


def qr_png_path(session_id: str) -> Path:
    safe_session = slugify(session_id, default="session")
    return SCREEN_DIR / f"adb-pairing-qr-{safe_session}.png"


def save_wireless_qr_session(payload: dict[str, Any]) -> None:
    path = wireless_qr_session_path(str(payload["session_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def load_wireless_qr_session(session_id: str) -> dict[str, Any]:
    path = wireless_qr_session_path(session_id)
    try:
        return json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise AndroidUseError(f"Wireless QR pairing session not found: {session_id}") from exc


def create_wireless_qr_session() -> dict[str, Any]:
    session_id = uuid.uuid4().hex[:12]
    service_name = "studio-" + random_adb_qr_token(10)
    password = random_adb_qr_token(12)
    payload = wireless_qr_payload(service_name, password)
    png = make_qr_png(payload)
    image_path = qr_png_path(session_id)
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(png)
    session = {
        "session_id": session_id,
        "service_name": service_name,
        "password": password,
        "qr_payload": payload,
        "qr_path": str(image_path),
        "created_at": timestamp_iso(),
        "completed_at": None,
    }
    save_wireless_qr_session(session)
    return {**session, "png": png}


def parse_adb_mdns_pairing_services(
    output: str,
    *,
    service_name: str | None = None,
    host: str | None = None,
) -> list[dict[str, Any]]:
    services = parse_adb_mdns_services(output, service_type="_adb-tls-pairing._tcp", host=host)
    if service_name:
        services = [
            service
            for service in services
            if service.get("service_name") == service_name or service_name in str(service.get("service") or "")
        ]
    return services


def adb_mdns_pairing_services(
    *,
    service_name: str | None = None,
    host: str | None = None,
) -> list[dict[str, Any]]:
    try:
        stdout, _stderr = run_command([adb_binary(), "mdns", "services"], timeout=8)
    except AndroidUseError:
        return []
    return parse_adb_mdns_pairing_services(decode_bytes(stdout), service_name=service_name, host=host)


def refresh_adb_mdns_services() -> None:
    with contextlib.suppress(Exception):
        run_command([adb_binary(), "kill-server"], timeout=5)
    with contextlib.suppress(Exception):
        run_command([adb_binary(), "start-server"], timeout=10)


def complete_wireless_qr_session(
    session_id: str,
    *,
    timeout_sec: float = 60,
    save: bool = True,
    start_scrcpy: bool = True,
) -> dict[str, Any]:
    session = load_wireless_qr_session(session_id)
    service_name = str(session.get("service_name") or "")
    password = str(session.get("password") or "")
    if not service_name or not password:
        raise AndroidUseError(f"Wireless QR pairing session is incomplete: {session_id}")

    deadline = time.monotonic() + max(timeout_sec, 1)
    services: list[dict[str, Any]] = []
    refreshed_adb = False
    while time.monotonic() <= deadline:
        services = adb_mdns_pairing_services(service_name=service_name)
        if services:
            break
        if not refreshed_adb and time.monotonic() > deadline - max(timeout_sec, 1) / 2:
            refresh_adb_mdns_services()
            refreshed_adb = True
        time.sleep(0.5)
    if not services:
        raise AndroidUseError(
            "没有发现扫码后的无线配对服务。请确认设备仍停留在无线调试二维码配对流程，"
            "手机和电脑在同一个网络，然后重新扫码。"
        )

    service = services[0]
    host = str(service["host"])
    pair_port = int(service["port"])
    target = f"{host}:{pair_port}"
    stdout, stderr = run_command([adb_binary(), "pair", target, password], timeout=30)
    pair_output = "\n".join(part for part in [decode_bytes(stdout), decode_bytes(stderr)] if part)
    reconnect_result = wireless_reconnect(host=host, save=save, start_scrcpy=start_scrcpy)
    completed = {
        **session,
        "completed_at": timestamp_iso(),
        "pair_target": target,
        "pair_output": pair_output,
        "pairing_service": service,
        "reconnect": reconnect_result,
        "env_file": str(USER_ENV_FILE) if save else None,
    }
    save_wireless_qr_session(completed)
    return completed


def tool_wireless_pair_qr(args: dict[str, Any]) -> list[dict[str, Any]]:
    action = str(args.get("action") or "create").strip().casefold()
    if action == "create":
        session = create_wireless_qr_session()
        png = session.pop("png")
        session["instructions"] = [
            "在 Android 设备上打开 设置 > 开发者选项 > 无线调试。",
            "选择 Pair device with QR code / 使用二维码配对设备。",
            "扫描返回的二维码图片。",
            f"扫码后调用 android_wireless_pair_qr(action='complete', session_id='{session['session_id']}')。",
        ]
        return [text_content(session), image_content(png)]

    if action == "complete":
        session_id = str(args.get("session_id") or "").strip()
        if not session_id:
            raise AndroidUseError("session_id is required when action='complete'.")
        timeout_sec = float(args.get("timeout_sec") or 60)
        save = bool(args.get("save", True))
        start_scrcpy = bool(args.get("start_scrcpy", True))
        result = complete_wireless_qr_session(
            session_id,
            timeout_sec=timeout_sec,
            save=save,
            start_scrcpy=start_scrcpy,
        )
        return [text_content(result)]

    raise AndroidUseError("action must be 'create' or 'complete'.")


def _qr_gf_tables() -> tuple[list[int], list[int]]:
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


QR_GF_EXP, QR_GF_LOG = _qr_gf_tables()


def _qr_gf_mul(left: int, right: int) -> int:
    if left == 0 or right == 0:
        return 0
    return QR_GF_EXP[QR_GF_LOG[left] + QR_GF_LOG[right]]


def _qr_poly_mul(left: list[int], right: list[int]) -> list[int]:
    result = [0] * (len(left) + len(right) - 1)
    for i, left_value in enumerate(left):
        for j, right_value in enumerate(right):
            result[i + j] ^= _qr_gf_mul(left_value, right_value)
    return result


def _qr_generator_poly(degree: int) -> list[int]:
    result = [1]
    for i in range(degree):
        result = _qr_poly_mul(result, [1, QR_GF_EXP[i]])
    return result


def _qr_reed_solomon_remainder(data: list[int], degree: int) -> list[int]:
    generator = _qr_generator_poly(degree)
    result = [0] * degree
    for byte in data:
        factor = byte ^ result[0]
        result = result[1:] + [0]
        for i in range(degree):
            result[i] ^= _qr_gf_mul(generator[i + 1], factor)
    return result


def _append_bits(bits: list[int], value: int, width: int) -> None:
    for shift in range(width - 1, -1, -1):
        bits.append((value >> shift) & 1)


def _qr_data_codewords(payload: str) -> list[int]:
    data = payload.encode("utf-8")
    if len(data) > ADB_QR_DATA_CODEWORDS - 3:
        raise AndroidUseError("Wireless QR payload is too long.")
    bits: list[int] = []
    _append_bits(bits, 0b0100, 4)
    _append_bits(bits, len(data), 8)
    for byte in data:
        _append_bits(bits, byte, 8)
    capacity_bits = ADB_QR_DATA_CODEWORDS * 8
    _append_bits(bits, 0, min(4, capacity_bits - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    codewords = [
        int("".join(str(bit) for bit in bits[index : index + 8]), 2)
        for index in range(0, len(bits), 8)
    ]
    pads = [0xEC, 0x11]
    pad_index = 0
    while len(codewords) < ADB_QR_DATA_CODEWORDS:
        codewords.append(pads[pad_index % 2])
        pad_index += 1
    return codewords


def _qr_format_bits(mask: int) -> int:
    data = (0b01 << 3) | mask
    bits = data << 10
    generator = 0x537
    for shift in range(bits.bit_length() - 1, 9, -1):
        if bits & (1 << shift):
            bits ^= generator << (shift - 10)
    return ((data << 10) | bits) ^ 0x5412


def _make_empty_qr() -> tuple[list[list[bool]], list[list[bool]]]:
    size = 17 + 4 * ADB_QR_VERSION
    modules = [[False] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    def set_function(x: int, y: int, dark: bool) -> None:
        if 0 <= x < size and 0 <= y < size:
            modules[y][x] = dark
            reserved[y][x] = True

    def draw_finder(left: int, top: int) -> None:
        for dy in range(-1, 8):
            for dx in range(-1, 8):
                x = left + dx
                y = top + dy
                dark = (
                    0 <= dx <= 6
                    and 0 <= dy <= 6
                    and (dx in {0, 6} or dy in {0, 6} or (2 <= dx <= 4 and 2 <= dy <= 4))
                )
                set_function(x, y, dark)

    def draw_alignment(center_x: int, center_y: int) -> None:
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                set_function(center_x + dx, center_y + dy, max(abs(dx), abs(dy)) != 1)

    draw_finder(0, 0)
    draw_finder(size - 7, 0)
    draw_finder(0, size - 7)
    draw_alignment(26, 26)
    for i in range(8, size - 8):
        set_function(i, 6, i % 2 == 0)
        set_function(6, i, i % 2 == 0)
    set_function(8, size - 8, True)
    _draw_qr_format_bits(modules, reserved, mask=0)
    return modules, reserved


def _draw_qr_format_bits(modules: list[list[bool]], reserved: list[list[bool]], *, mask: int) -> None:
    size = len(modules)
    bits = _qr_format_bits(mask)

    def set_function(x: int, y: int, index: int) -> None:
        modules[y][x] = ((bits >> index) & 1) != 0
        reserved[y][x] = True

    for i in range(6):
        set_function(8, i, i)
    set_function(8, 7, 6)
    set_function(8, 8, 7)
    set_function(7, 8, 8)
    for i in range(9, 15):
        set_function(14 - i, 8, i)
    for i in range(8):
        set_function(size - 1 - i, 8, i)
    for i in range(8, 15):
        set_function(8, size - 15 + i, i)


def make_qr_matrix(payload: str) -> list[list[bool]]:
    data_codewords = _qr_data_codewords(payload)
    ecc = _qr_reed_solomon_remainder(data_codewords, ADB_QR_ECC_CODEWORDS)
    bits: list[int] = []
    for byte in data_codewords + ecc:
        _append_bits(bits, byte, 8)

    modules, reserved = _make_empty_qr()
    size = len(modules)
    bit_index = 0
    upward = True
    right = size - 1
    while right >= 1:
        if right == 6:
            right -= 1
        for vertical in range(size):
            y = size - 1 - vertical if upward else vertical
            for x in (right, right - 1):
                if reserved[y][x]:
                    continue
                modules[y][x] = bit_index < len(bits) and bits[bit_index] == 1
                bit_index += 1
        upward = not upward
        right -= 2

    for y in range(size):
        for x in range(size):
            if not reserved[y][x] and (x + y) % 2 == 0:
                modules[y][x] = not modules[y][x]
    _draw_qr_format_bits(modules, reserved, mask=0)
    return modules


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def make_qr_png(payload: str, *, scale: int = ADB_QR_SCALE, border: int = ADB_QR_BORDER) -> bytes:
    matrix = make_qr_matrix(payload)
    module_count = len(matrix)
    size = (module_count + border * 2) * scale
    rows: list[bytes] = []
    for y in range(size):
        module_y = y // scale - border
        row = bytearray()
        for x in range(size):
            module_x = x // scale - border
            dark = 0 <= module_x < module_count and 0 <= module_y < module_count and matrix[module_y][module_x]
            row.append(0 if dark else 255)
        rows.append(b"\x00" + bytes(row))
    header = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header) + png_chunk(b"IDAT", zlib.compress(b"".join(rows))) + png_chunk(b"IEND", b"")

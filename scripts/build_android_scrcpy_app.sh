#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/macos/AndroidScrcpyLauncher.m"
ICON_PNG="$ROOT/assets/android.png"
APP_DIR="${1:-$ROOT/.android-use/AndroidUse.app}"
APP_NAME="${2:-Android}"
MACOS_DIR="$APP_DIR/Contents/MacOS"
RESOURCES_DIR="$APP_DIR/Contents/Resources"
EXE="$MACOS_DIR/AndroidUse"
PLIST="$APP_DIR/Contents/Info.plist"
ICONSET="$RESOURCES_DIR/AndroidUse.iconset"
ICON_FILE="$RESOURCES_DIR/AndroidUse.icns"
ICON_WHITE_PNG="$RESOURCES_DIR/AndroidUse-white.png"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "AndroidUse.app can only be built on macOS." >&2
  exit 1
fi

if ! command -v clang >/dev/null 2>&1; then
  echo "clang not found. Install Xcode Command Line Tools first." >&2
  exit 1
fi

if [ ! -f "$SRC" ]; then
  echo "source not found: $SRC" >&2
  exit 1
fi

if [ ! -f "$ICON_PNG" ]; then
  echo "icon source not found: $ICON_PNG" >&2
  exit 1
fi

mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"
python3 - "$ICON_PNG" "$ICON_WHITE_PNG" <<'PY'
import os
import struct
import sys
import zlib

source, target = sys.argv[1], sys.argv[2]


def read_chunks(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit("icon source must be a PNG")
    offset = 8
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        yield kind, payload
        offset += 12 + length


def unfilter_scanlines(raw, width, height):
    channels = 4
    stride = width * channels
    rows = []
    cursor = 0
    previous = bytearray(stride)
    for _ in range(height):
        filter_type = raw[cursor]
        cursor += 1
        row = bytearray(raw[cursor : cursor + stride])
        cursor += stride
        for i in range(stride):
            left = row[i - channels] if i >= channels else 0
            up = previous[i]
            up_left = previous[i - channels] if i >= channels else 0
            if filter_type == 1:
                row[i] = (row[i] + left) & 0xFF
            elif filter_type == 2:
                row[i] = (row[i] + up) & 0xFF
            elif filter_type == 3:
                row[i] = (row[i] + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                predictor = left + up - up_left
                distances = (abs(predictor - left), abs(predictor - up), abs(predictor - up_left))
                row[i] = (row[i] + (left, up, up_left)[distances.index(min(distances))]) & 0xFF
            elif filter_type != 0:
                raise SystemExit(f"unsupported PNG filter: {filter_type}")
        rows.append(bytes(row))
        previous = row
    return rows


def png_chunk(kind, payload):
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


data = open(source, "rb").read()
chunks = list(read_chunks(data))
ihdr = next(payload for kind, payload in chunks if kind == b"IHDR")
width, height, bit_depth, color_type, compression, png_filter, interlace = struct.unpack(">IIBBBBB", ihdr)
if (bit_depth, color_type, compression, png_filter, interlace) != (8, 6, 0, 0, 0):
    raise SystemExit("icon source must be a non-interlaced 8-bit RGBA PNG")
compressed = b"".join(payload for kind, payload in chunks if kind == b"IDAT")
rows = unfilter_scanlines(zlib.decompress(compressed), width, height)
try:
    content_scale = float(os.environ.get("ANDROID_USE_ICON_CONTENT_SCALE", "0.76"))
except ValueError:
    content_scale = 0.76
content_scale = min(max(content_scale, 0.5), 1.0)
inner_width = max(1, round(width * content_scale))
inner_height = max(1, round(height * content_scale))
left = (width - inner_width) // 2
top = (height - inner_height) // 2
encoded_rows = []
for y in range(height):
    rgb = bytearray()
    for x in range(width):
        if left <= x < left + inner_width and top <= y < top + inner_height:
            source_x = min(width - 1, ((x - left) * width) // inner_width)
            source_y = min(height - 1, ((y - top) * height) // inner_height)
            offset = source_x * 4
            red, green, blue, alpha = rows[source_y][offset : offset + 4]
            rgb.extend(
                (
                    (red * alpha + 255 * (255 - alpha) + 127) // 255,
                    (green * alpha + 255 * (255 - alpha) + 127) // 255,
                    (blue * alpha + 255 * (255 - alpha) + 127) // 255,
                )
            )
        else:
            rgb.extend((255, 255, 255))
    encoded_rows.append(b"\x00" + bytes(rgb))
output = b"\x89PNG\r\n\x1a\n"
output += png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
output += png_chunk(b"IDAT", zlib.compress(b"".join(encoded_rows), 9))
output += png_chunk(b"IEND", b"")
open(target, "wb").write(output)
PY
sips -z 16 16 "$ICON_WHITE_PNG" --out "$ICONSET/icon_16x16.png" >/dev/null
sips -z 32 32 "$ICON_WHITE_PNG" --out "$ICONSET/icon_16x16@2x.png" >/dev/null
sips -z 32 32 "$ICON_WHITE_PNG" --out "$ICONSET/icon_32x32.png" >/dev/null
sips -z 64 64 "$ICON_WHITE_PNG" --out "$ICONSET/icon_32x32@2x.png" >/dev/null
sips -z 128 128 "$ICON_WHITE_PNG" --out "$ICONSET/icon_128x128.png" >/dev/null
sips -z 256 256 "$ICON_WHITE_PNG" --out "$ICONSET/icon_128x128@2x.png" >/dev/null
sips -z 256 256 "$ICON_WHITE_PNG" --out "$ICONSET/icon_256x256.png" >/dev/null
sips -z 512 512 "$ICON_WHITE_PNG" --out "$ICONSET/icon_256x256@2x.png" >/dev/null
sips -z 512 512 "$ICON_WHITE_PNG" --out "$ICONSET/icon_512x512.png" >/dev/null
sips -z 1024 1024 "$ICON_WHITE_PNG" --out "$ICONSET/icon_512x512@2x.png" >/dev/null
iconutil -c icns "$ICONSET" -o "$ICON_FILE"
rm -rf "$ICONSET"
rm -f "$ICON_WHITE_PNG"
python3 - "$PLIST" "$APP_NAME" <<'PY'
import plistlib
import sys

plist_path, app_name = sys.argv[1], sys.argv[2]
plist = {
    "CFBundleDevelopmentRegion": "en",
    "CFBundleDisplayName": app_name,
    "CFBundleExecutable": "AndroidUse",
    "CFBundleIdentifier": "com.kolf.android-use",
    "CFBundleIconFile": "AndroidUse",
    "CFBundleInfoDictionaryVersion": "6.0",
    "CFBundleName": app_name,
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": "0.1.0",
    "CFBundleVersion": "1",
    "LSMinimumSystemVersion": "13.0",
    "NSHighResolutionCapable": True,
}
with open(plist_path, "wb") as file:
    plistlib.dump(plist, file)
PY

clang -fobjc-arc -framework Foundation "$SRC" -o "$EXE"
chmod +x "$EXE"
if command -v xattr >/dev/null 2>&1; then
  xattr -dr com.apple.quarantine "$APP_DIR" 2>/dev/null || true
fi
touch "$APP_DIR"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [ -x "$LSREGISTER" ]; then
  "$LSREGISTER" -f "$APP_DIR" >/dev/null 2>&1 || true
fi
echo "$APP_DIR"

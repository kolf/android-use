#!/usr/bin/env node
"use strict";

const readline = require("readline");

function loadPlaywrightAndroid() {
  for (const name of ["playwright-core", "playwright"]) {
    try {
      const mod = require(name);
      if (mod && mod._android) {
        return { android: mod._android, packageName: name };
      }
    } catch (_error) {
      // Try the next package.
    }
  }
  throw new Error("Playwright Android API is not installed. Run npm install in the plugin directory.");
}

function withTimeout(promise, timeoutMs, label) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out after ${timeoutMs}ms`)), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function timeoutMs(input) {
  return Math.max(100, Number(input.timeout_sec || 10) * 1000);
}

function serialOf(device) {
  return typeof device.serial === "function" ? device.serial() : String(device.serial || "");
}

function modelOf(device) {
  return typeof device.model === "function" ? device.model() : "";
}

function webViewPkg(webview) {
  return typeof webview.pkg === "function" ? webview.pkg() : "";
}

function webViewPid(webview) {
  return typeof webview.pid === "function" ? webview.pid() : null;
}

function webViewSocketName(webview) {
  if (typeof webview.socketName === "function") return webview.socketName();
  if (typeof webview.socket === "function") return webview.socket();
  return "";
}

function valueType(value) {
  if (value === null) return "object";
  if (Array.isArray(value)) return "array";
  return typeof value;
}

function matchesPage(item, input) {
  if (input.page_id) {
    const pageId = String(input.page_id);
    if (![item.id, item.pid, item.pkg, item.socket].map(value => String(value || "")).includes(pageId)) {
      return false;
    }
  }
  if (input.package && item.pkg !== input.package) {
    return false;
  }
  if (input.socket_name && item.socket && item.socket !== input.socket_name) {
    return false;
  }
  if (input.url_contains && !String(item.url || "").includes(input.url_contains)) {
    return false;
  }
  if (input.title_contains && !String(item.title || "").includes(input.title_contains)) {
    return false;
  }
  return true;
}

function pageCacheKeys(serial, item) {
  const keys = [];
  for (const [name, value] of [
    ["id", item.id],
    ["pkg", item.pkg],
    ["pid", item.pid],
    ["socket", item.socket],
  ]) {
    if (value !== undefined && value !== null && String(value)) {
      keys.push(`${serial}|${name}|${value}`);
    }
  }
  return keys;
}

class AndroidWebViewWorker {
  constructor() {
    this.android = null;
    this.packageName = null;
    this.deviceCache = new Map();
    this.pageCache = new Map();
  }

  load() {
    if (!this.android) {
      const loaded = loadPlaywrightAndroid();
      this.android = loaded.android;
      this.packageName = loaded.packageName;
    }
  }

  async close() {
    const devices = [...this.deviceCache.values()];
    this.deviceCache.clear();
    this.pageCache.clear();
    for (const device of devices) {
      if (device && typeof device.close === "function") {
        await device.close().catch(() => {});
      }
    }
  }

  clearSerial(serial) {
    if (!serial) return;
    const device = this.deviceCache.get(serial);
    if (device && typeof device.close === "function") {
      device.close().catch(() => {});
    }
    this.deviceCache.delete(serial);
    for (const key of [...this.pageCache.keys()]) {
      if (key.startsWith(`${serial}|`)) {
        this.pageCache.delete(key);
      }
    }
  }

  async selectDevice(input, forceRefresh) {
    this.load();
    const wanted = input.serial ? String(input.serial) : "";
    if (!forceRefresh && wanted && this.deviceCache.has(wanted)) {
      return { device: this.deviceCache.get(wanted), devices: null };
    }
    if (!forceRefresh && !wanted && this.deviceCache.size === 1) {
      return { device: [...this.deviceCache.values()][0], devices: null };
    }
    const devices = await withTimeout(this.android.devices(), timeoutMs(input), "android.devices()");
    if (!devices.length) {
      throw new Error("No Android devices found through Playwright Android. Ensure adb daemon is running and authorized.");
    }
    const device = wanted ? devices.find(item => serialOf(item) === wanted) : devices[0];
    if (!device) {
      throw new Error(`Android device ${wanted} was not found through Playwright Android.`);
    }
    this.deviceCache.set(serialOf(device), device);
    return { device, devices };
  }

  setDeviceTimeout(device, input) {
    if (device && typeof device.setDefaultTimeout === "function") {
      device.setDefaultTimeout(timeoutMs(input));
    }
  }

  async describeWebView(serial, webview, page, index, input) {
    const pkg = webViewPkg(webview);
    const pid = webViewPid(webview);
    const socket = webViewSocketName(webview) || String(input.socket_name || "");
    const item = {
      id: `${pkg || "webview"}:${pid || index}`,
      pkg,
      pid,
      socket,
      backend: "playwright-android",
      worker: true,
      url: page.url(),
      title: await withTimeout(page.title(), timeoutMs(input), "page.title()"),
      type: "page",
      ok: true,
    };
    this.cachePage(serial, item, webview, page);
    return item;
  }

  cachePage(serial, item, webview, page) {
    const record = { serial, item: { ...item }, webview, page, updatedAt: Date.now() };
    for (const key of pageCacheKeys(serial, item)) {
      this.pageCache.set(key, record);
    }
  }

  cachedRecords(serial) {
    const records = [];
    const seen = new Set();
    for (const record of this.pageCache.values()) {
      if (record.serial !== serial) continue;
      const key = `${record.item.id}|${record.item.pid}|${record.item.pkg}`;
      if (seen.has(key)) continue;
      seen.add(key);
      records.push(record);
    }
    return records;
  }

  async selectCachedPage(serial, input) {
    for (const record of this.cachedRecords(serial)) {
      try {
        const item = {
          ...record.item,
          url: record.page.url(),
          title: await withTimeout(record.page.title(), timeoutMs(input), "cached page.title()"),
          ok: true,
        };
        if (matchesPage(item, input)) {
          record.item = { ...item };
          return { webview: record.webview, page: record.page, item };
        }
      } catch (_error) {
        for (const key of pageCacheKeys(record.serial, record.item)) {
          this.pageCache.delete(key);
        }
      }
    }
    return null;
  }

  async collectWebViews(device, serial, input) {
    const webviews = await withTimeout(Promise.resolve(device.webViews()), timeoutMs(input), "device.webViews()");
    const pages = [];
    let index = 0;
    for (const webview of webviews) {
      const pkg = webViewPkg(webview);
      const pid = webViewPid(webview);
      const item = {
        id: `${pkg || "webview"}:${pid || index}`,
        pkg,
        pid,
        socket: webViewSocketName(webview),
        backend: "playwright-android",
        worker: true,
      };
      try {
        const page = await withTimeout(webview.page(), timeoutMs(input), "webview.page()");
        pages.push(await this.describeWebView(serial, webview, page, index, input));
      } catch (error) {
        item.error = String(error && error.message ? error.message : error);
        pages.push(item);
      }
      index += 1;
    }
    return pages;
  }

  async selectWebView(device, serial, input) {
    const cached = await this.selectCachedPage(serial, input);
    if (cached) return cached;

    if (input.socket_name || (input.package && !input.page_id && !input.url_contains && !input.title_contains)) {
      const selector = {};
      if (input.socket_name) selector.socketName = String(input.socket_name);
      if (input.package) selector.pkg = String(input.package);
      const webview = await withTimeout(
        device.webView(selector, { timeout: timeoutMs(input) }),
        timeoutMs(input),
        "device.webView()",
      );
      const page = await withTimeout(webview.page(), timeoutMs(input), "webview.page()");
      const item = await this.describeWebView(serial, webview, page, 0, input);
      return { webview, page, item };
    }

    const webviews = await withTimeout(Promise.resolve(device.webViews()), timeoutMs(input), "device.webViews()");
    let index = 0;
    for (const webview of webviews) {
      try {
        const page = await withTimeout(webview.page(), timeoutMs(input), "webview.page()");
        const item = await this.describeWebView(serial, webview, page, index, input);
        if (matchesPage(item, input)) {
          return { webview, page, item };
        }
      } catch (_error) {
        // Skip inaccessible WebViews and let the final error describe no match.
      }
      index += 1;
    }
    throw new Error("No matching Android WebView was found through Playwright Android.");
  }

  async dispatch(input, forceRefresh) {
    if (input.action === "status") {
      return {
        ok: true,
        backend: "playwright-android",
        worker: true,
        cached_devices: this.deviceCache.size,
        cached_pages: this.cachedRecords(String(input.serial || "")).length,
      };
    }
    if (input.action === "close") {
      await this.close();
      return { ok: true, backend: "playwright-android", worker: true, closed: true };
    }
    if (!["list", "eval"].includes(input.action)) {
      throw new Error(`Unsupported action: ${input.action}`);
    }

    const { device, devices } = await this.selectDevice(input, forceRefresh);
    this.setDeviceTimeout(device, input);
    const serial = serialOf(device);

    if (input.action === "list") {
      const pages = await this.collectWebViews(device, serial, input);
      return {
        ok: true,
        backend: "playwright-android",
        worker: true,
        package: this.packageName,
        serial,
        devices: devices ? devices.map(item => ({ serial: serialOf(item), model: modelOf(item) })) : undefined,
        pages,
      };
    }

    const selected = await this.selectWebView(device, serial, input);
    const expression = String(input.expression || "");
    const value = await withTimeout(
      selected.page.evaluate(
        async ({ source, awaitPromise }) => {
          const result = (0, eval)(source);
          return awaitPromise ? await result : result;
        },
        { source: expression, awaitPromise: input.await_promise !== false },
      ),
      timeoutMs(input),
      "page.evaluate()",
    );
    return {
      ok: true,
      backend: "playwright-android",
      worker: true,
      package: this.packageName,
      serial,
      page: selected.item,
      result: {
        type: valueType(value),
        value,
      },
    };
  }

  async handle(input) {
    try {
      return await this.dispatch(input, false);
    } catch (error) {
      const serial = String(input.serial || "");
      if (["list", "eval"].includes(input.action)) {
        this.clearSerial(serial);
        try {
          return await this.dispatch(input, true);
        } catch (_retryError) {
          throw error;
        }
      }
      throw error;
    }
  }
}

const worker = new AndroidWebViewWorker();
const queue = { current: Promise.resolve() };

function responseFor(input, result) {
  const payload = { id: input.id, ...result };
  if (payload.id === undefined) delete payload.id;
  return payload;
}

function errorFor(input, error) {
  const payload = {
    id: input.id,
    ok: false,
    error: String(error && error.message ? error.message : error),
    stack: error && error.stack ? String(error.stack).split("\n").slice(0, 5) : undefined,
  };
  if (payload.id === undefined) delete payload.id;
  return payload;
}

function writeJson(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

async function handleLine(line) {
  let input;
  try {
    input = JSON.parse(line || "{}");
  } catch (error) {
    writeJson({ ok: false, error: `Invalid JSON: ${error.message || error}` });
    return;
  }
  try {
    writeJson(responseFor(input, await worker.handle(input)));
  } catch (error) {
    writeJson(errorFor(input, error));
  }
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", line => {
  if (!line.trim()) return;
  queue.current = queue.current.then(() => handleLine(line), () => handleLine(line));
});
rl.on("close", () => {
  shutdown().finally(() => process.exit(0));
});

async function shutdown() {
  await worker.close();
}

process.on("SIGTERM", () => {
  shutdown().finally(() => process.exit(0));
});
process.on("SIGINT", () => {
  shutdown().finally(() => process.exit(0));
});

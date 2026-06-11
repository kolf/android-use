#!/usr/bin/env node
"use strict";

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

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", chunk => {
      data += chunk;
    });
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });
}

function withTimeout(promise, timeoutMs, label) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out after ${timeoutMs}ms`)), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
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

function valueType(value) {
  if (value === null) return "object";
  if (Array.isArray(value)) return "array";
  return typeof value;
}

function matchesPage(item, input) {
  if (input.page_id) {
    const pageId = String(input.page_id);
    if (![item.id, item.pid, item.pkg].map(value => String(value || "")).includes(pageId)) {
      return false;
    }
  }
  if (input.package && item.pkg !== input.package) {
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

async function selectDevice(android, input) {
  const devices = await android.devices();
  if (!devices.length) {
    throw new Error("No Android devices found through Playwright Android. Ensure adb daemon is running and authorized.");
  }
  if (!input.serial) {
    return { device: devices[0], devices };
  }
  const wanted = String(input.serial);
  const device = devices.find(item => serialOf(item) === wanted);
  if (!device) {
    throw new Error(`Android device ${wanted} was not found through Playwright Android.`);
  }
  return { device, devices };
}

async function collectWebViews(device, input) {
  const timeoutMs = Math.max(100, Number(input.timeout_sec || 10) * 1000);
  const webviews = await withTimeout(Promise.resolve(device.webViews()), timeoutMs, "device.webViews()");
  const pages = [];
  for (const webview of webviews) {
    const pkg = webViewPkg(webview);
    const pid = webViewPid(webview);
    const item = {
      id: `${pkg || "webview"}:${pid || pages.length}`,
      pkg,
      pid,
      backend: "playwright-android",
    };
    try {
      const page = await withTimeout(webview.page(), timeoutMs, "webview.page()");
      item.url = page.url();
      item.title = await withTimeout(page.title(), timeoutMs, "page.title()");
      item.type = "page";
      item.ok = true;
    } catch (error) {
      item.error = String(error && error.message ? error.message : error);
    }
    pages.push(item);
  }
  return pages;
}

async function selectWebView(device, input) {
  const timeoutMs = Math.max(100, Number(input.timeout_sec || 10) * 1000);
  if (input.socket_name || input.package) {
    const selector = {};
    if (input.socket_name) selector.socketName = String(input.socket_name);
    if (input.package) selector.pkg = String(input.package);
    const webview = await withTimeout(device.webView(selector, { timeout: timeoutMs }), timeoutMs, "device.webView()");
    const page = await withTimeout(webview.page(), timeoutMs, "webview.page()");
    return {
      webview,
      page,
      item: {
        id: `${webViewPkg(webview) || "webview"}:${webViewPid(webview) || "selected"}`,
        pkg: webViewPkg(webview),
        pid: webViewPid(webview),
        url: page.url(),
        title: await withTimeout(page.title(), timeoutMs, "page.title()"),
        backend: "playwright-android",
      },
    };
  }
  const webviews = await withTimeout(Promise.resolve(device.webViews()), timeoutMs, "device.webViews()");
  const candidates = [];
  for (const webview of webviews) {
    try {
      const page = await withTimeout(webview.page(), timeoutMs, "webview.page()");
      const item = {
        id: `${webViewPkg(webview) || "webview"}:${webViewPid(webview) || candidates.length}`,
        pkg: webViewPkg(webview),
        pid: webViewPid(webview),
        url: page.url(),
        title: await withTimeout(page.title(), timeoutMs, "page.title()"),
        backend: "playwright-android",
      };
      if (matchesPage(item, input)) {
        candidates.push({ webview, page, item });
      }
    } catch (_error) {
      // Skip inaccessible WebViews and let the final error describe no match.
    }
  }
  if (!candidates.length) {
    throw new Error("No matching Android WebView was found through Playwright Android.");
  }
  return candidates[0];
}

async function run() {
  const input = JSON.parse(await readStdin() || "{}");
  const { android, packageName } = loadPlaywrightAndroid();
  const { device, devices } = await selectDevice(android, input);
  try {
    if (typeof device.setDefaultTimeout === "function") {
      device.setDefaultTimeout(Math.max(100, Number(input.timeout_sec || 10) * 1000));
    }
    if (input.action === "list") {
      const pages = await collectWebViews(device, input);
      return {
        ok: true,
        backend: "playwright-android",
        package: packageName,
        serial: serialOf(device),
        devices: devices.map(item => ({ serial: serialOf(item), model: modelOf(item) })),
        pages,
      };
    }
    if (input.action === "eval") {
      const selected = await selectWebView(device, input);
      const expression = String(input.expression || "");
      const value = await selected.page.evaluate(async source => await (0, eval)(source), expression);
      return {
        ok: true,
        backend: "playwright-android",
        package: packageName,
        serial: serialOf(device),
        page: selected.item,
        result: {
          type: valueType(value),
          value,
        },
      };
    }
    throw new Error(`Unsupported action: ${input.action}`);
  } finally {
    if (typeof device.close === "function") {
      await device.close().catch(() => {});
    }
  }
}

run()
  .then(result => {
    process.stdout.write(JSON.stringify(result));
  })
  .catch(error => {
    const payload = {
      ok: false,
      error: String(error && error.message ? error.message : error),
      stack: error && error.stack ? String(error.stack).split("\n").slice(0, 5) : undefined,
    };
    process.stdout.write(JSON.stringify(payload));
    process.exitCode = 1;
  });

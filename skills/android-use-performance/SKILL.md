---
name: android-use-performance
description: Gather and interpret Android performance evidence on an attached physical Android device with Android Use tools, Simpleperf, Perfetto, gfxinfo, meminfo, heap dumps, and native allocation traces. Use when asked to profile or diagnose CPU, startup, frame, jank, memory, or leak issues on a real Android device.
---

# Android Use: Performance

Use this skill to profile a focused Android app flow on a real attached Android phone or tablet. Physical-device evidence is the default. Use an emulator only when the user explicitly asks for emulator profiling or when no physical device is available and the user accepts that emulator timing is less representative.

CPU sampling usually requires a debuggable or profileable build. Frame stats, Perfetto, logcat, and memory snapshots can still help when the app cannot be sampled.

## Core Workflow

1. Pick one focused user-visible flow.
2. Choose the trace type that matches the question.
3. Capture device, build, app, thermal, and battery context before the run.
4. Record the flow with clear start and stop boundaries.
5. Pull or preserve the exact trace/report files from that run.
6. Interpret results with caveats about physical device model, Android version, build type, sample count, symbols, thermal state, and profiler limits.

Avoid broad "use the app for a while" captures. They make traces hard to attribute and usually hide the functions that matter.

Use a local adb target for meaningful timing. Store outputs in a run-specific artifact folder outside the skill directory:

```bash
if [ -z "${ARTIFACT_DIR:-}" ]; then
  ARTIFACT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/android-use-perf.XXXXXX")"
fi
mkdir -p "$ARTIFACT_DIR"
```

Do not put `ARTIFACT_DIR` under `SKILL_DIR`; the skill folder is for bundled instructions and scripts, not run artifacts.

## Device And Build Preflight

Prefer Android Use tools for setup:

- `android_check_dependencies` when local setup is uncertain;
- `android_list_devices(include_details=true)` and choose a physical authorized device;
- `android_get_state(include_screenshot=false)` for current device state;
- `android_start_screen_viewer` or `android_start_scrcpy` when visual evidence helps drive the flow.

Use narrow shell probes for reproducibility:

```bash
SERIAL="<adb-serial>"
PACKAGE="<app package>"

adb -s "$SERIAL" shell getprop ro.product.manufacturer
adb -s "$SERIAL" shell getprop ro.product.model
adb -s "$SERIAL" shell getprop ro.build.version.release
adb -s "$SERIAL" shell getprop ro.build.version.sdk
adb -s "$SERIAL" shell wm size
adb -s "$SERIAL" shell wm density
adb -s "$SERIAL" shell dumpsys battery | grep -Ei 'level|temperature|status|AC powered|USB powered|Wireless powered'
adb -s "$SERIAL" shell dumpsys package "$PACKAGE" | grep -Ei 'versionName|versionCode|DEBUGGABLE|profileable|isProfileable' || true
```

If only an emulator is available, state that the capture is emulator-only and avoid generalizing absolute timing to real devices.

## Choosing A Trace

- Use **Simpleperf** when the question is "what functions are taking CPU time?" or when you need a sampled profile of Kotlin, Java, native, or framework execution.
- Use **Perfetto** when the question is frame timing, startup timeline, scheduler gaps, binder work, lock contention, main-thread stalls, Compose recomposition, or why a flow felt janky.
- Use **gfxinfo framestats** for a quick manual frame/jank snapshot. Pair it with Perfetto when you need root cause.
- Use **meminfo / heap dumps** when the question is retained Java/Kotlin objects, PSS, native heap, or object counts after a focused flow.

## Simpleperf CPU Profiles

Simpleperf `--app` works best when the installed package is debuggable or profileable from shell. Preflight before recording:

```bash
SERIAL="<adb-serial>"
PACKAGE="<app package>"

adb -s "$SERIAL" shell dumpsys package "$PACKAGE" | grep -Ei 'DEBUGGABLE|profileable|isProfileable' || true
```

If the package is not debuggable/profileable and `simpleperf record --app` fails, install a debug/profileable build when possible. If that is not possible, use Perfetto or `gfxinfo` instead of treating missing CPU samples as evidence.

Start recording in one terminal or as a long-running Codex command session:

```bash
SERIAL="<adb-serial>"
PACKAGE="<app package>"
MAX_DURATION_SECONDS=60

adb -s "$SERIAL" shell rm -f /data/local/tmp/perf.data
adb -s "$SERIAL" logcat -c

adb -s "$SERIAL" shell simpleperf record \
  --app "$PACKAGE" \
  -o /data/local/tmp/perf.data \
  -e cpu-clock -f 4000 -g \
  --duration "$MAX_DURATION_SECONDS"
```

While that command is running, perform exactly one focused flow with Android Use tools or deterministic adb input.

Stop Simpleperf from another command and wait for the recording command to exit:

```bash
adb -s "$SERIAL" shell 'pid="$(pidof simpleperf 2>/dev/null || true)"; [ -n "$pid" ] && kill -INT $pid'
```

If that returns `Operation not permitted`, send Ctrl-C to the original `adb shell simpleperf record` command session and wait for it to exit.

Pull and report the capture:

```bash
adb -s "$SERIAL" pull /data/local/tmp/perf.data "$ARTIFACT_DIR/perf.data"
adb -s "$SERIAL" logcat -d -v time > "$ARTIFACT_DIR/logcat.txt"

SKILL_DIR="<absolute path to this skill directory>"
FIRST_PARTY_REGEX="$(printf '%s' "$PACKAGE" | sed 's/\./\\./g')"
"$SKILL_DIR/scripts/simpleperf_hotspots.sh" \
  "$ARTIFACT_DIR/perf.data" \
  "$ARTIFACT_DIR" \
  --serial "$SERIAL" \
  --first-party-regex "$FIRST_PARTY_REGEX"
```

The helper writes:

- `$ARTIFACT_DIR/simpleperf-self.txt`
- `$ARTIFACT_DIR/simpleperf-children.txt`
- `$ARTIFACT_DIR/simpleperf.csv` when supported by the installed Simpleperf

If host Simpleperf is not installed, the helper searches Android Studio and Android SDK/NDK locations. If unavailable, it falls back to device-side `adb shell simpleperf report` when the device still has `/data/local/tmp/perf.data`.

## Reading Simpleperf

Simpleperf reports sampled CPU execution. It does not directly measure suspended coroutines, network latency, lock wait time, or other wall-clock waits. If a flow feels slow but Simpleperf shows little app CPU, capture Perfetto to inspect scheduler gaps, binder work, locks, frame timing, and app trace sections.

Read reports this way:

- **Self/Overhead**: samples where the function itself was executing. Use this for hot leaf work such as parsing, formatting, diffing, sorting, allocation-heavy iteration, or JSON/protobuf processing.
- **Children/inclusive**: samples in the function and its callees. Use this for expensive entry points such as repositories, use cases, view models, Composables, startup initializers, or feature coordinators.
- **Shared Object / Symbol**: prefer app-owned package frames, feature modules, domain/data/UI modules, and generated app code. Treat Android framework, Kotlin runtime, Compose, and native/runtime frames as context unless the app-owned caller is visible.
- **Percentages**: useful for ranking functions inside one capture. For user-facing timing claims, pair with Perfetto, `gfxinfo`, or repeated wall-clock measurements.

When interpreting a hotspot, note symbol/function name, self or inclusive percentage, approximate sampled CPU time when available, caller stack or owning source file, flow steps, artifact paths, and whether the capture is single-run or repeated.

## Perfetto / Compose Trace

If the app repo already documents a Perfetto/System Trace command for that project, use it. Otherwise use Perfetto directly. The light command below captures scheduler/frequency/Android atrace categories and app `Trace` sections for `PACKAGE`; it is not a substitute for a full project-specific Perfetto config when you need detailed frame timeline or Compose runtime internals.

```bash
SERIAL="<adb-serial>"
PACKAGE="<app package>"
TRACE_DURATION_SECONDS=30
TRACE_BASENAME="app-flow-$(date +%Y%m%d-%H%M%S).pftrace"
TRACE_DEVICE="/data/misc/perfetto-traces/$TRACE_BASENAME"

PERFETTO_PID="$(adb -s "$SERIAL" shell perfetto \
  --background-wait \
  -o "$TRACE_DEVICE" \
  -t "${TRACE_DURATION_SECONDS}s" \
  --app "$PACKAGE" \
  sched freq idle am wm gfx view binder_driver hal dalvik | tr -d '\r' | tail -n 1)"
printf 'Perfetto PID: %s\n' "$PERFETTO_PID"
```

Run exactly one focused flow before `TRACE_DURATION_SECONDS` expires. Prefer letting the capture expire naturally. To stop early, gracefully terminate the background Perfetto process and give it a moment to flush:

```bash
adb -s "$SERIAL" shell kill -TERM "$PERFETTO_PID" 2>/dev/null || true
adb -s "$SERIAL" shell "
  last_size=-1
  stable_count=0
  i=0
  while [ \$i -lt 30 ]; do
    size=\$(ls -l '$TRACE_DEVICE' 2>/dev/null | awk '{ print \$5 }')
    if [ -n \"\$size\" ] && [ \"\$size\" -gt 0 ] && [ \"\$size\" = \"\$last_size\" ]; then
      stable_count=\$((stable_count + 1))
      [ \$stable_count -ge 2 ] && exit 0
    else
      stable_count=0
    fi
    last_size=\"\${size:-0}\"
    i=\$((i + 1))
    sleep 1
  done
  exit 1
"
```

Pull the exact on-device trace from this run:

```bash
adb -s "$SERIAL" pull "$TRACE_DEVICE" "$ARTIFACT_DIR/$TRACE_BASENAME"
```

In Perfetto, inspect:

- main-thread slices around missed frames or long startup sections;
- frame scheduling, frame timeline, and render thread lanes;
- Compose runtime tracing sections for recomposition work when enabled;
- binder transactions, monitor contention, scheduler gaps, and app log markers.

Only report frame timeline or Compose recomposition details when those tracks/events are actually present in the captured trace.

## gfxinfo Framestats

Use this for a quick manual frame snapshot:

```bash
SERIAL="<adb-serial>"
PACKAGE="<app package>"

adb -s "$SERIAL" shell pidof "$PACKAGE"
adb -s "$SERIAL" shell dumpsys window | grep -F "$PACKAGE"
adb -s "$SERIAL" shell dumpsys gfxinfo "$PACKAGE" reset
# Perform the focused flow.
adb -s "$SERIAL" shell dumpsys gfxinfo "$PACKAGE" > "$ARTIFACT_DIR/gfxinfo.txt"
adb -s "$SERIAL" shell dumpsys gfxinfo "$PACKAGE" framestats > "$ARTIFACT_DIR/gfxinfo-framestats.txt"
```

Capture from a stable, responsive screen. If `dumpsys gfxinfo` fails to dump the process, or the device shows an ANR/dialog/splash screen instead of the flow, discard that capture and use Perfetto for root cause.

Read the headline summary first: total frames, janky frames, frame percentiles, slow UI thread, slow draw commands, and frame deadline misses. Physical devices give more useful absolute smoothness numbers than emulators, but thermal throttling, battery saver, charging state, refresh rate, and background load still matter.

## Memory / Leak Artifacts

Use this after narrowing the investigation to one flow. Exercise the flow, return to a stable screen, then capture memory artifacts from that state.

For quick Java/native/PSS/object-count snapshots:

```bash
SERIAL="<adb-serial>"
PACKAGE="<app package>"

adb -s "$SERIAL" shell am force-stop "$PACKAGE"
adb -s "$SERIAL" shell monkey -p "$PACKAGE" 1
# Exercise the focused flow, then navigate back to a stable idle screen.
adb -s "$SERIAL" shell dumpsys meminfo "$PACKAGE" > "$ARTIFACT_DIR/meminfo-flow.txt"
```

Read `TOTAL PSS`, Java heap, native heap, graphics, `Views`, `Activities`, binder counts, and object counts. Treat one noisy sample as a lead, not a conclusion.

For retained Kotlin/Java objects, prefer Shark CLI when it is available. It works with Android heap dumps and produces text output the agent can inspect and cite.

```bash
HEAP="/data/local/tmp/app-flow.hprof"
HPROF="$ARTIFACT_DIR/app-flow.hprof"

if ! command -v shark-cli >/dev/null; then
  echo "Install Shark CLI, or analyze the HPROF with Android Studio Profiler / MAT." >&2
fi

adb -s "$SERIAL" shell am dumpheap -g "$PACKAGE" "$HEAP"
adb -s "$SERIAL" pull "$HEAP" "$HPROF"
adb -s "$SERIAL" shell rm -f "$HEAP"

if command -v shark-cli >/dev/null; then
  shark-cli --hprof "$HPROF" analyze | tee "$ARTIFACT_DIR/shark-analysis.txt"
fi
```

Read `shark-analysis.txt` first when it exists. Report suspected leaking objects, retained sizes, and reference chains. Look for retained feature objects, activities, fragments, view models, Compose state holders, repositories, listeners, callbacks, and caches that should have been released after leaving the flow. If Shark CLI is unavailable, still preserve the HPROF path and inspect it with the best available heap analyzer; do not claim leak roots from `meminfo` alone.

For native allocation growth, capture a Perfetto trace with heapprofd enabled. Keep the duration in the config; current Android `perfetto` rejects `-t` together with `--config`.

```bash
TRACE_DEVICE="/data/misc/perfetto-traces/native-alloc.pftrace"

adb -s "$SERIAL" shell perfetto -o "$TRACE_DEVICE" \
  --txt -c - <<EOF
duration_ms: 60000
buffers { size_kb: 262144 fill_policy: RING_BUFFER }
data_sources {
  config {
    name: "android.heapprofd"
    heapprofd_config {
      sampling_interval_bytes: 65536
      shmem_size_bytes: 8388608
      block_client: true
      process_cmdline: "$PACKAGE"
    }
  }
}
EOF

adb -s "$SERIAL" pull "$TRACE_DEVICE" "$ARTIFACT_DIR/native-alloc.pftrace"
```

Analyze the trace with `trace_processor_shell` and save the outputs:

```bash
SKILL_DIR="<absolute path to this skill directory>"
"$SKILL_DIR/scripts/heapprofd_reports.sh" \
  "$ARTIFACT_DIR/native-alloc.pftrace" \
  "$ARTIFACT_DIR"
```

Read `heapprofd-summary.txt`, `heapprofd-top-allocations.txt`, `heapprofd-top-stack.txt`, `heapprofd-health.txt`, and `meminfo` together. Report net native allocation size, top allocating frames/mappings, the expanded stack for the largest callsite, and whether trace stats show heapprofd health issues such as client errors, packet loss, or buffer overruns. Prefer Java heap dumps for retained app objects; heapprofd is for native allocation behavior.

## Report

Report:

- exact flow, physical device serial/model, Android version, build variant, app package/version, battery/thermal state, and run count;
- artifact paths for every trace/report used;
- top hotspots or frame/jank evidence with percentages, durations, or counts;
- whether evidence is CPU samples, frame timeline, frame stats, memory artifacts, or log/screenshots;
- caveats such as physical-device variability, low sample count, cold-start compilation, battery saver, thermal throttling, missing symbols, non-profileable build, or unavailable real-device target;
- next smallest trace or code change when current evidence is insufficient.

Do not claim physical-device performance verification unless a real device was actually used in this turn.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Project context

ESP32-S3/C6 firmware for a desk-side Claude Code usage monitor. Each supported
board lives in its own `firmware/src/boards/<name>/` folder and is selected
via PlatformIO's `build_src_filter`. Adding a board means dropping in a new
folder + a new `[env:...]` block — `main.cpp`, `ui.cpp`, and `splash.cpp`
never see board-specific code. See [`docs/porting/adding-a-board.md`](docs/porting/adding-a-board.md).

Three reference ports today:

- `boards/waveshare_amoled_216/` — Waveshare ESP32-**S3**-Touch-AMOLED-2.16 (CO5300, 480×480 square, CST9220 touch, QMI8658 IMU auto-rotation). Build env: `waveshare_amoled_216`.
- `boards/waveshare_amoled_18/` — Waveshare ESP32-**S3**-Touch-AMOLED-1.8 (SH8601, 368×448 portrait, FT3168 touch, XCA9554 IO expander). Build env: `waveshare_amoled_18`.
- `boards/waveshare_amoled_216_c6/` — Waveshare ESP32-**C6**-Touch-AMOLED-2.16 (SH8601, 480×480 square, CST9217 touch, no PSRAM). Build env: `waveshare_amoled_216_c6`.

The shared code calls a small HAL (`firmware/src/hal/`) that each board implements: display, touch, input, power, IMU. Optional features are guarded by `BoardCaps` (runtime) and `BOARD_HAS_*` (compile-time) rather than `#ifdef BOARD_*`.

Connects to a host daemon over BLE; daemon polls Anthropic API for usage data. This file is for future Claude Code sessions to bootstrap quickly. Read this first.

## Hardware (critical pins)

### AMOLED-2.16 S3 (original)
- Display: **CO5300** AMOLED via QSPI (CS=12, SCLK=38, SDIO0..3=4..7, RST=2)
- Touch: **CST9220** via I2C (SDA=15, SCL=14, INT=11, addr=0x5A)
- PMU: **AXP2101** on same I2C bus (addr=0x34) — battery, USB VBUS, PWR button IRQ
- IMU: **QMI8658** on same I2C bus (addr=0x6B) — accelerometer for auto-rotation
- Buttons: GPIO 0 (left → Space/voice-mode), GPIO 18 (right → Shift+Tab/mode-toggle), AXP PKEY (middle → cycle screens; on splash → cycle animations)

### AMOLED-1.8 S3
- Display: **SH8601** AMOLED via QSPI (CS=12, **SCLK=11** ← different!, SDIO0..3=4..7, RST routed via XCA9554 EXIO1)
- Touch: **FT3168** via I2C (SDA=15, SCL=14, INT=21, addr=0x38). Driven by minimal inline reader in `main.cpp` (FocalTech standard register layout — avoids vendoring the GPLv3 `Arduino_DriveBus` library).
- PMU: AXP2101 @ 0x34 (same chip as 2.16 — `XPowersLib` reused)
- IMU: QMI8658 @ 0x6B (initialized for I2C bus health, rotation logic disabled)
- IO expander: **XCA9554 / PCA9554** @ I2C 0x20. Gates LCD_RST, TP_RST, audio amp enable, and reads the PWR button. **`io_expander_init()` MUST run before `gfx->begin()` or `ft3168_init()`** — otherwise display/touch stay in reset and silently fail. PWR button is on EXIO4, active HIGH.
- Orientation: **fixed at 0°**. IMU auto-rotation is disabled; `rotate_strip()` / `handle_rotation_change()` are excluded via `#ifndef BOARD_AMOLED_18`.
- Buttons: GPIO 0 (BOOT → Space/voice-mode), XCA9554 EXIO4 (PWR → cycle screens; on splash → cycle animations). **No third button.**

### AMOLED-2.16 C6 (newest)
- Display: **SH8601** AMOLED via QSPI (CS=15, SCLK=0, SDIO0..3=1..4, **no RST GPIO** — SH8601 uses power-on reset)
- Touch: **CST9217** via I2C (SDA=8, SCL=7, INT=5, RST=11, addr=0x5A). Register-compatible with CST9220; SensorLib `TouchDrvCST92xx` driver works for both.
- PMU: AXP2101 @ 0x34. IMU: QMI8658 @ 0x6B (same chips carried over).
- IO expander: TCA9554 exists but only services the audio amp — **not used for display/touch reset** (no `io_expander_init()` needed).
- **No PSRAM** — no rotation strip, `BOARD_HAS_ROTATION = 0`.
- Buttons: GPIO 9 (BOOT → Space/voice-mode), GPIO 10 (KEY → Shift+Tab, identified empirically), AXP PKEY (PWR → cycle screens).

## Architecture

```text
firmware/src/
  hal/                      — board-agnostic interfaces shared code calls into
    board_caps.h            — runtime BoardCaps struct (W, H, button_count, has_* flags)
    display_hal.h           — init / begin / set_brightness / draw_bitmap / tick / round_area
    touch_hal.h             — init / read(&x, &y, &pressed)
    input_hal.h             — init / is_held(PRIMARY|SECONDARY)
    power_hal.h             — init / tick / battery_pct / is_charging / pwr_pressed (edge)
    imu_hal.h               — init / tick / rotation_quadrant
  boards/
    waveshare_amoled_216/   — CO5300 + CST9220 + AXP PKEY + QMI8658 rotation
    waveshare_amoled_18/    — SH8601 + FT3168 + AXP + XCA9554 (PWR via EXIO4), no rotation
    waveshare_amoled_216_c6/ — SH8601 + CST9217 + AXP PKEY, no PSRAM, no rotation
    template/               — copy this to bootstrap a new port
  main.cpp                  — setup() + loop(): HAL calls only, zero #ifdef BOARD_*
  ui.{h,cpp}                — 3-screen UI (splash, usage, bluetooth). compute_layout() picks fonts/positions from board_caps() (responsive — current breakpoint: H >= 460 → large, else compact)
  splash.{h,cpp}            — 20×20 pixel-art engine. CELL = min(W,H)/20, centered.
  ble.{h,cpp}               — NimBLE peripheral: custom data service + HID keyboard
  idle.{h,cpp}              — auto-sleep: 30 min timeout, fade-out/in, USB-aware, wake on touch/button
  idle_cfg.h                — idle tunables (IDLE_TIMEOUT_MS, DISPLAY_DEFAULT_BRIGHTNESS, etc.)
  usage_rate.{h,cpp}        — short-term %/min rate tracker; returns group 0-3 for splash animation selection
  theme.h                   — LVGL color design tokens (THEME_BG, THEME_ACCENT terra-cotta, etc.)
  data.h                    — UsageData struct
  icons.h                   — icon arrays. Battery (5×) are RGB565A8 with alpha; rest are raw RGB565.
  logo.h                    — 80×80 RGB565 logo
  font_*.c                  — pre-compiled LVGL 9 bitmap fonts (Tiempos 56/34, Styrene 48/28/24/20/16/14/12, Mono 32/18)
  splash_animations.h       — generated, do not hand-edit
docs/porting/               — adding-a-board.md, hal-contract.md, capability-flags.md
```

Each board folder contains: `board.h` (pins, I2C addresses, `BOARD_HAS_*` flags),
`board_init.cpp` (Wire.begin + any IO expander), `display.cpp`, `touch.cpp`,
`input.cpp`, `power.cpp`, `imu.cpp`, `caps.cpp` (the `BoardCaps` instance), plus
any board-private hardware drivers (e.g. `io_expander.{h,cpp}` on AMOLED-1.8).
PlatformIO's `build_src_filter` includes shared code + one board's folder per env.

## Build / flash

```bash
pio run -d firmware -e waveshare_amoled_216       # build S3 2.16
pio run -d firmware -e waveshare_amoled_18        # build S3 1.8
pio run -d firmware -e waveshare_amoled_216_c6    # build C6 2.16

# Convenience scripts (handle port auto-detect):
./flash-mac.sh waveshare_amoled_216_c6            # macOS: build + flash, auto-picks /dev/cu.usbmodem*
./flash-mac.sh waveshare_amoled_18 /dev/cu.usbmodem1101  # explicit port
./flash.sh waveshare_amoled_216                   # Linux: build + flash, auto-picks /dev/ttyACM*

# Monitor after flash:
pio device monitor -p /dev/cu.usbmodem101 -b 115200
```

If `pio` isn't on PATH: `~/.platformio/penv/bin/pio` or `brew install platformio` on macOS.

Device path: `/dev/cu.usbmodem*` on macOS, `/dev/ttyACM0` on Linux. Both expose ESP32 native USB-JTAG (no boot-mode dance needed).

## QA your own UI changes — don't ask the user

The firmware ships a `screenshot` serial command that dumps the LVGL framebuffer. `./screenshot.sh out.png [port]` captures a PNG sized to the active display (480×480 or 368×448). **Use this on every UI iteration** — Read the PNG with the Read tool, verify the change visually, iterate. Script auto-picks the macOS/Linux default port and falls back to pio's bundled Python if pyserial isn't on the system Python.

The boot screen is `SCREEN_SPLASH` and only advances on a physical button press, so a fresh flash will sit on the splash. To screenshot the screen you're actually editing without asking the user to press a button, **temporarily change the default boot screen** in `main.cpp` (search for `ui_show_screen(SCREEN_SPLASH);`) to `SCREEN_USAGE` / `SCREEN_CONTROLLER` / `SCREEN_BLUETOOTH`, do your iteration, then revert before committing.

## Critical gotchas

1. **CO5300 cannot rotate.** Its MADCTL only supports axis flips, not column/row exchange. Rotation is done by **CPU pixel remapping inside `display_hal_draw_bitmap`** in `boards/waveshare_amoled_216/display.cpp`. We use **PARTIAL render mode with strip rotation** (small 480×40 strips, fast). On rotation change → AMOLED brightness flash → force redraw (handled inside `display_hal_tick`).
2. **OPI PSRAM** required on S3 boards: `board_build.arduino.memory_type = qio_opi` in platformio.ini. Without this, `MALLOC_CAP_SPIRAM` returns NULL and the screen is black. The C6 board has no PSRAM — framebuffer must fit in internal RAM (no rotation strip).
3. **pioarduino platform required.** GFX Library for Arduino needs Arduino Core 3.x (`esp32-hal-periman.h`), not the 2.x that standard `espressif32` ships. We pin `pioarduino/platform-espressif32` 55.03.38-1.
4. **LVGL 9 font patching.** `lv_font_conv` outputs LVGL 8 format. Must remove `#if LVGL_VERSION_MAJOR >= 8` guards, drop `.cache` field, add `.release_glyph`, `.kerning`, `.static_bitmap`, `.fallback`, `.user_data`. Without patching, fonts render invisible.
5. **Touch reading is centralized inside each board's `touch.cpp`.** The HAL `touch_hal_read()` is called once per loop from `my_touch_cb`; the board's implementation owns its latched `touch_pressed/x/y` state. Don't call the underlying controller from anywhere else — CST9220's `getPoint()` etc. do a full I2C transaction and concurrent callers consume each other's data.
6. **Even-aligned flush regions.** `display_hal_round_area` (called from `rounder_cb`) is what each board uses to enforce this. Required on CO5300, harmless on SH8601.
7. **Touch axis swap/mirror is per-board.** The 2.16's CST9220 needs `setSwapXY(true)` + `setMirrorXY(true, false)` — applied inside `boards/waveshare_amoled_216/touch.cpp::touch_hal_init()`. New ports apply their own.
8. **LVGL RGB565A8 is planar.** `w*h` RGB565 pixels followed by `w*h` alpha bytes; `data_size = w*h*3`, `stride = w*2`. Use `init_icon_dsc_rgb565a8()` for icons that overlap non-uniform backgrounds (e.g. battery over splash). Lucide source PNGs are black-on-transparent — converter must tint to white or icons render invisible. See `tools/png_to_lvgl.js`.
9. **Per-board pre-init is `board_init()`.** Each board's `board_init.cpp` brings up `Wire` and any reset-gating IO expander BEFORE `display_hal_init()`. Skipping the IO expander release on AMOLED-1.8 leaves SH8601 + FT3168 in reset and they silently fail to probe.
10. **No `#ifdef BOARD_*` in shared code.** The whole point of the refactor — if you're about to add one, you probably want a `BoardCaps` field or a per-board file instead. See `docs/porting/capability-flags.md`.
11. **Idle / auto-sleep.** `idle_tick()` is called each loop. `idle_consume_wake_press()` must be called on button events — returns `true` (and blocks the button's normal action) if the device was asleep. Touch wakes by default (`IDLE_WAKE_ON_TOUCH true`) but touch events are silently dropped while asleep (`idle_is_asleep()` guard in `my_touch_cb`). USB presence suppresses sleep (`IDLE_SLEEP_WHEN_CHARGING false`). Tunables in `idle_cfg.h`.
12. **Usage rate grouping.** `usage_rate_sample(session_pct)` is called on every BLE data receipt. `usage_rate_group()` returns 0–3 (idle→heavy); splash animation selection uses this to pick category — don't hardcode animation indices.

## Icons

`tools/png_to_lvgl.js <input.png> <symbol> [W_MACRO] [H_MACRO] [--tint=RRGGBB | --no-tint]` converts an alpha PNG to RGB565A8. Default tint is white (`0xFFFFFF`) — necessary for Lucide PNGs. Splice output into `firmware/src/icons.h` and use `init_icon_dsc_rgb565a8()` in ui.cpp. Currently only the 5 battery icons use this format; the rest are still raw RGB565 baked over the panel background, fine because they live inside opaque zones.

## Splash animations

13 × 20×20 pixel-art creature animations sourced from
[claudepix.vercel.app](https://claudepix.vercel.app). Pipeline:

```bash
node tools/scrape_claudepix.js  # → tools/claudepix_data/*.json
node tools/convert_to_c.js      # → firmware/src/splash_animations.h
```

Each animation has a per-animation 10-color RGB565 palette. Cell values 0..9 index it. Default boot screen. Splash picks animation group based on `usage_rate_group()` (0=idle, 1=normal, 2=active, 3=heavy).

## User profile / preferences

See `~/.claude/projects/.../memory/` files for persistent context (user is an embedded-beginner senior dev, brand-conscious, prefers iterative UI refinement, dislikes me authoring my own art when third-party assets are intended). Always read those memory files at session start.

## Daemon / host side

Two daemon implementations exist:

**macOS (preferred):** `daemon/claude_usage_daemon.py` — Python daemon using `bleak` (CoreBluetooth). Reads OAuth token from macOS Keychain (`Claude Code-credentials`) or `~/.claude/.credentials.json`. Falls back to extracting `sessionKey` from browser cookies. Managed via launchd.

```bash
./install-mac.sh    # creates daemon/.venv, installs bleak+httpx, registers LaunchAgent
launchctl list | grep claude-usage-daemon   # check status
tail -f ~/Library/Logs/claude-usage-daemon.out.log
```

**Linux:** `daemon/claude-usage-daemon.sh` — Bash daemon. Run with `systemctl --user start claude-usage-daemon`. The unit file's `ExecStart` is absolute path — repoint it when switching checkouts.

**Discovery & resilience:**

- Connects by name (`"Claude Controller"`) on first run, caches resolved MAC at `~/.config/claude-usage-monitor/ble-address`. ESP32 BLE addresses are factory-burned per-chip, so swapping any board invalidates the cache.
- On connect failure: cache is dropped AND device is removed from bluez (`bluetoothctl remove`) so the next scan won't re-pick a dead MAC.
- `POLL_INTERVAL=60`, `TICK=5`. Inner loop wakes every 5s to detect disconnects fast; polls Anthropic when 60s elapsed OR when ESP fires a refresh request.

**GATT characteristics on service `4c41555a-...0001`:**

- `...0002` RX — daemon writes JSON usage payload here.
- `...0003` TX — firmware notifies ack/nack (daemon doesn't subscribe).
- `...0004` REQ — firmware fires `0x01` notify in `onSubscribe` if `has_received_data` is false. Daemon subscribes and drops a flag file the inner loop picks up.

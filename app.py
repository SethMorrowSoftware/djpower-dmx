#!/usr/bin/env python3
"""
DMX Controller for DJPOWER H-IP20V Fog Machine
16-channel mode with LED control and GPIO trigger
"""

from flask import Flask, jsonify, request, send_file
import time
import json
import os
import re
import sys
import glob
import atexit
import signal
import tempfile
from collections import deque
from threading import Lock, Timer, Thread

try:
    from pyftdi.ftdi import Ftdi
    FTDI_AVAILABLE = True
    FTDI_IMPORT_ERROR = None
except Exception as e:
    # Capture the message now: the "except ... as" name is unbound once
    # this block exits, so it cannot be referenced later at runtime.
    Ftdi = None
    FTDI_AVAILABLE = False
    FTDI_IMPORT_ERROR = str(e)
import importlib
import importlib.util

# Detect GPIO libraries (gpiod preferred, lgpio as fallback; works on Pi 4 & Pi 5)
GPIO_AVAILABLE = False
GPIO_LIB = None
gpiod = None
lgpio = None

# Import ALL available GPIO libraries so init_gpio() can fall back between them.
if importlib.util.find_spec("gpiod"):
    try:
        gpiod = importlib.import_module("gpiod")
    except Exception:
        gpiod = None

if importlib.util.find_spec("lgpio"):
    try:
        lgpio = importlib.import_module("lgpio")
    except Exception:
        lgpio = None

if gpiod is not None:
    GPIO_AVAILABLE = True
    GPIO_LIB = 'gpiod'
elif lgpio is not None:
    GPIO_AVAILABLE = True
    GPIO_LIB = 'lgpio'
else:
    print("WARNING: No GPIO library available")

app = Flask(__name__)

# Path for persisting scene config across restarts
CONFIG_DIR = os.environ.get("DMX_CONFIG_DIR", "/var/lib/dmx")
CONFIG_FILE = os.environ.get(
    "DMX_CONFIG_FILE",
    os.path.join(CONFIG_DIR, "config.json"),
)

# ============================================
# CONFIGURATION
# ============================================

class Config:
    """Application configuration"""

    # GPIO Settings
    CONTACT_PIN = 17
    SAFETY_SWITCH_PIN = 27
    GPIO_CHIP = None  # Optional override: int index or string like "gpiochip0" or "/dev/gpiochip0"

    # DMX Settings
    DMX_CHANNELS = 512
    DMX_REFRESH_RATE = 44
    FTDI_URL = os.environ.get("DMX_FTDI_URL", "ftdi://0403:6001/1")

    # Timing
    SCENE_B_DURATION = 10.0  # seconds

    # GPIO debounce - cooldown between consecutive triggers
    DEBOUNCE_TIME = 0.3  # seconds
    # A new line level must persist continuously for this long before it is
    # accepted as a real state change (filters out interference / transient
    # signals in BOTH directions). Must stay below the width of the real
    # trigger pulse or real triggers will be rejected — field data shows
    # some controller outputs pulse for only ~10ms, so this is tunable from
    # the web UI down to 0 (0 = latch on the first sample seen: maximum
    # sensitivity, no noise filtering).
    TRIGGER_HOLD_TIME = 0.05  # seconds
    # How often the monitor thread samples the GPIO pins. 5ms resolves
    # ~10ms pulses (any pulse >= 2x this interval is guaranteed to be seen
    # by two consecutive samples).
    GPIO_POLL_INTERVAL = 0.005  # seconds
    # The safety line is a maintained toggle; give it a stability floor so
    # a very low TRIGGER_HOLD_TIME can't make the lockout flap on bounce.
    SAFETY_STABLE_TIME_MIN = 0.02  # seconds
    # Which stable contact transition fires the sequence:
    #   'close' - fires when the contact closes to GND (normally-open wiring,
    #             the documented default: line idles HIGH/open)
    #   'open'  - fires when the contact opens (normally-closed / inverted
    #             wiring: line idles LOW/closed and lifts on trigger)
    # Changeable at runtime from the web UI and persisted to disk.
    TRIGGER_ON = 'close'
    # How the trigger is detected (edge-detection backend only):
    #   'level' - a debounced stable level change fires (default; needs the
    #             line to actually reach and hold the firing level)
    #   'burst' - a shower of raw edges fires. For electrically marginal
    #             lines that chatter across the logic threshold instead of
    #             switching cleanly (weak pull-up vs. long/loaded cable):
    #             each physical actuation produces a burst of brief edges
    #             rather than one clean pulse. Fires when BURST_MIN_EDGES
    #             raw edges (either direction) land within BURST_WINDOW,
    #             then re-arms only after BURST_QUIET_REARM of silence, so
    #             one actuation fires exactly once. TRIGGER_ON polarity is
    #             ignored in burst mode.
    TRIGGER_MODE = 'level'
    BURST_MIN_EDGES = 5
    BURST_WINDOW = 0.2  # seconds
    BURST_QUIET_REARM = 1.0  # seconds
    # Hardware safety-switch interlock on SAFETY_SWITCH_PIN. When disabled,
    # the controller operates as if the switch were ON (useful when pin 27
    # is not actually wired to a switch, or while diagnosing wiring).
    SAFETY_SWITCH_ENABLED = True
    # If GPIO reads keep failing for this long while initialized, tear down
    # and re-initialize the GPIO stack.
    GPIO_READ_FAIL_REINIT = 5.0  # seconds

    # DJPOWER H-IP20V Fog Machine (16-channel mode)
    # Full channel map:
    # Ch1: Fog (0-9 Off, 10-255 On)
    # Ch2: Disabled
    # Ch3: Outer LED Red (0-9 Off, 10-255 Dim to bright)
    # Ch4: Outer LED Green (0-9 Off, 10-255 Dim to bright)
    # Ch5: Outer LED Blue (0-9 Off, 10-255 Dim to bright)
    # Ch6: Outer LED Amber (0-9 Off, 10-255 Dim to bright)
    # Ch7: Inner LED Red (0-9 Off, 10-255 Dim to bright)
    # Ch8: Inner LED Green (0-9 Off, 10-255 Dim to bright)
    # Ch9: Inner LED Blue (0-9 Off, 10-255 Dim to bright)
    # Ch10: Inner LED Amber (0-9 Off, 10-255 Dim to bright)
    # Ch11: LED Mix Color 1 (0-9 Off, 10-255 Mix color)
    # Ch12: LED Mix Color 2 (0-9 Off, 10-255 Mix color)
    # Ch13: LED Auto Color (0-9 Off, 10-255 Slow to fast)
    # Ch14: Strobe (0-9 Off, 10-255 Slow to fast)
    # Ch15: Dimmer (0-9 Off, 10-255 Dim to bright)
    # Ch16: Safety Channel (0-49 Invalid, 50-200 Valid, 201-255 Invalid)

    SCENES = {
        'scene_a': {
            'name': 'All OFF (Default)',
            'channels': {
                1: 0,     # Fog: Off
                2: 0,     # Disabled
                3: 0,     # Outer Red: Off
                4: 0,     # Outer Green: Off
                5: 0,     # Outer Blue: Off
                6: 0,     # Outer Amber: Off
                7: 0,     # Inner Red: Off
                8: 0,     # Inner Green: Off
                9: 0,     # Inner Blue: Off
                10: 0,    # Inner Amber: Off
                11: 0,    # LED Mix 1: Off
                12: 0,    # LED Mix 2: Off
                13: 0,    # Auto Color: Off
                14: 0,    # Strobe: Off
                15: 0,    # Dimmer: Off
                16: 100,  # Safety: Valid
            }
        },
        'scene_b': {
            'name': 'Fog ON (Triggered)',
            'channels': {
                1: 255,   # Fog: Full
                2: 0,     # Disabled
                3: 255,   # Outer Red: Full
                4: 255,   # Outer Green: Full
                5: 255,   # Outer Blue: Full
                6: 0,     # Outer Amber: Off
                7: 255,   # Inner Red: Full
                8: 255,   # Inner Green: Full
                9: 255,   # Inner Blue: Full
                10: 0,    # Inner Amber: Off
                11: 0,    # LED Mix 1: Off
                12: 0,    # LED Mix 2: Off
                13: 0,    # Auto Color: Off
                14: 0,    # Strobe: Off
                15: 255,  # Dimmer: Full
                16: 100,  # Safety: Valid
            }
        },
        'scene_c': {
            'name': 'Custom Scene 1',
            'channels': {
                1: 255,   # Fog: Full
                2: 0,     # Disabled
                3: 0,     # Outer Red: Off
                4: 0,     # Outer Green: Off
                5: 255,   # Outer Blue: Full
                6: 0,     # Outer Amber: Off
                7: 0,     # Inner Red: Off
                8: 0,     # Inner Green: Off
                9: 255,   # Inner Blue: Full
                10: 0,    # Inner Amber: Off
                11: 0,    # LED Mix 1: Off
                12: 0,    # LED Mix 2: Off
                13: 0,    # Auto Color: Off
                14: 50,   # Strobe: Slow
                15: 200,  # Dimmer: 80%
                16: 100,  # Safety: Valid
            }
        },
        'scene_d': {
            'name': 'Custom Scene 2',
            'channels': {
                1: 200,   # Fog: High
                2: 0,     # Disabled
                3: 255,   # Outer Red: Full
                4: 0,     # Outer Green: Off
                5: 0,     # Outer Blue: Off
                6: 200,   # Outer Amber: High
                7: 255,   # Inner Red: Full
                8: 0,     # Inner Green: Off
                9: 0,     # Inner Blue: Off
                10: 200,  # Inner Amber: High
                11: 0,    # LED Mix 1: Off
                12: 0,    # LED Mix 2: Off
                13: 100,  # Auto Color: Medium
                14: 0,    # Strobe: Off
                15: 255,  # Dimmer: Full
                16: 100,  # Safety: Valid
            }
        }
    }

config = Config()

# ============================================
# Config Persistence
# ============================================

def _clamp_hold_time(value):
    """Keep the glitch-filter window in a sane range.

    0 is allowed: it latches on the first sample seen (maximum
    sensitivity for very short trigger pulses, no noise filtering).
    """
    if value != value:  # NaN
        raise ValueError("NaN")
    return max(0.0, min(2.0, value))


def _clamp_debounce_time(value):
    """Keep the post-trigger cooldown in a sane range."""
    if value != value:  # NaN
        raise ValueError("NaN")
    return max(0.0, min(30.0, value))


def _clamp_burst_min_edges(value):
    edges = int(value)
    if not (2 <= edges <= 100):
        raise ValueError("burst_min_edges must be between 2 and 100")
    return edges


def _clamp_burst_window(value):
    v = float(value)
    if v != v:
        raise ValueError("NaN")
    return max(0.02, min(5.0, v))


def _clamp_burst_quiet(value):
    v = float(value)
    if v != v:
        raise ValueError("NaN")
    return max(0.1, min(30.0, v))


def _validate_bcm_pin(value):
    """Validate a header GPIO as a BCM number (2-27; 0/1 are HAT EEPROM)."""
    pin = int(value)
    if not (2 <= pin <= 27):
        raise ValueError("GPIO pin must be a BCM number between 2 and 27")
    return pin


def _normalize_gpio_chip_setting(value):
    """Normalize the chip override: None/''/'auto' -> None (scan all chips),
    otherwise a chip index or gpiochip name."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ('', 'auto'):
        return None
    if text.isdigit():
        return int(text)
    if re.fullmatch(r'(/dev/)?gpiochip\d+', text):
        return text
    raise ValueError("GPIO chip must be 'auto', a chip number, or gpiochipN")


def save_config():
    """Save current scene config and duration to disk (atomic write)"""
    try:
        config_dir = os.path.dirname(CONFIG_FILE)
        os.makedirs(config_dir, exist_ok=True)
        data = {
            'scene_b_duration': config.SCENE_B_DURATION,
            'trigger_on': config.TRIGGER_ON,
            'trigger_mode': config.TRIGGER_MODE,
            'trigger_hold_time': config.TRIGGER_HOLD_TIME,
            'debounce_time': config.DEBOUNCE_TIME,
            'burst_min_edges': config.BURST_MIN_EDGES,
            'burst_window': config.BURST_WINDOW,
            'burst_quiet_rearm': config.BURST_QUIET_REARM,
            'safety_switch_enabled': config.SAFETY_SWITCH_ENABLED,
            'contact_pin': config.CONTACT_PIN,
            'safety_switch_pin': config.SAFETY_SWITCH_PIN,
            'gpio_chip': config.GPIO_CHIP,
            'scenes': {}
        }
        for key, scene in config.SCENES.items():
            data['scenes'][key] = {
                'name': scene['name'],
                'channels': {str(k): v for k, v in scene['channels'].items()}
            }
        # Write to temp file then atomically rename to prevent corruption
        fd, tmp_path = tempfile.mkstemp(dir=config_dir, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, CONFIG_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        print(f"WARNING: Could not save config: {e}")


def load_config():
    """Load scene config from disk if it exists"""
    if not os.path.exists(CONFIG_FILE):
        return
    try:
        with open(CONFIG_FILE, 'r') as f:
            data = json.load(f)
        if 'scene_b_duration' in data:
            config.SCENE_B_DURATION = float(data['scene_b_duration'])
        if data.get('trigger_on') in ('close', 'open'):
            config.TRIGGER_ON = data['trigger_on']
        if data.get('trigger_mode') in ('level', 'burst'):
            config.TRIGGER_MODE = data['trigger_mode']
        for key, clamp, attr in (
                ('burst_min_edges', _clamp_burst_min_edges, 'BURST_MIN_EDGES'),
                ('burst_window', _clamp_burst_window, 'BURST_WINDOW'),
                ('burst_quiet_rearm', _clamp_burst_quiet, 'BURST_QUIET_REARM')):
            if key in data:
                try:
                    setattr(config, attr, clamp(data[key]))
                except (TypeError, ValueError):
                    pass
        if 'trigger_hold_time' in data:
            try:
                config.TRIGGER_HOLD_TIME = _clamp_hold_time(float(data['trigger_hold_time']))
            except (TypeError, ValueError):
                pass
        if 'debounce_time' in data:
            try:
                config.DEBOUNCE_TIME = _clamp_debounce_time(float(data['debounce_time']))
            except (TypeError, ValueError):
                pass
        if isinstance(data.get('safety_switch_enabled'), bool):
            config.SAFETY_SWITCH_ENABLED = data['safety_switch_enabled']
        if 'contact_pin' in data and 'safety_switch_pin' in data:
            try:
                contact_pin = _validate_bcm_pin(data['contact_pin'])
                safety_pin = _validate_bcm_pin(data['safety_switch_pin'])
                if contact_pin != safety_pin:
                    config.CONTACT_PIN = contact_pin
                    config.SAFETY_SWITCH_PIN = safety_pin
            except (TypeError, ValueError) as e:
                print(f"WARNING: Invalid saved GPIO pins; keeping defaults: {e}")
        if 'gpio_chip' in data:
            try:
                config.GPIO_CHIP = _normalize_gpio_chip_setting(data['gpio_chip'])
            except (TypeError, ValueError) as e:
                print(f"WARNING: Invalid saved GPIO chip; keeping default: {e}")
        if 'scenes' in data:
            for key, scene in data['scenes'].items():
                if key in config.SCENES:
                    config.SCENES[key]['name'] = scene.get('name', config.SCENES[key]['name'])
                    raw_channels = scene.get('channels', {})
                    try:
                        config.SCENES[key]['channels'] = _normalize_scene_channels(
                            raw_channels,
                            base_channels=config.SCENES[key]['channels'],
                        )
                    except (TypeError, ValueError) as e:
                        print(f"WARNING: Invalid channel data in saved {key}; keeping previous values: {e}")
        print("Loaded saved configuration from disk")
    except Exception as e:
        print(f"WARNING: Could not load config (using defaults): {e}")

# ============================================
# Event Log (remote diagnostics)
# ============================================

class EventLog:
    """Thread-safe ring buffer of recent hardware/trigger events.

    Lets the wiring and trigger behavior be diagnosed from the web UI
    without SSH or physical access: every debounced line transition,
    trigger (fired/blocked), and GPIO lifecycle event is recorded here
    and also printed to the journal.
    """

    def __init__(self, maxlen=500):
        self._events = deque(maxlen=maxlen)
        self._lock = Lock()
        self._counter = 0

    def add(self, kind, message, **fields):
        with self._lock:
            self._counter += 1
            evt = {'id': self._counter, 'ts': time.time(), 'kind': kind, 'message': message}
            evt.update(fields)
            self._events.append(evt)
        print(f"[{kind}] {message}")

    def snapshot(self):
        """Return events newest-first."""
        with self._lock:
            return list(self._events)[::-1]


event_log = EventLog()


class GlitchTracker:
    """Collects rejected sub-filter pulses (interference) for one line.

    Every glitch is counted and its width recorded; event-log output is
    rate-limited so sustained interference (e.g. mains-coupled bounce)
    produces periodic summaries instead of flooding the ring buffer.
    """

    LOG_INTERVAL = 10.0  # seconds between per-line glitch log entries

    def __init__(self, name):
        self.name = name
        self.count = 0
        self.max_duration = 0.0
        self.last = None  # {'ts': wall clock, 'duration': s, 'level': 0/1}
        self._pending = 0
        self._pending_min = None
        self._pending_max = None
        self._last_log_mono = None

    def record(self, evt, now_mono):
        self.count += 1
        dur = evt['duration']
        self.max_duration = max(self.max_duration, dur)
        self.last = {'ts': time.time(), 'duration': dur, 'level': evt['level']}
        if self._last_log_mono is None or (now_mono - self._last_log_mono) >= self.LOG_INTERVAL:
            level_txt = 'LOW' if evt['level'] == 0 else 'HIGH'
            event_log.add(
                'glitch',
                f"Interference on {self.name} line: ~{dur * 1000:.1f}ms {level_txt} "
                f"pulse rejected by glitch filter (total {self.count})",
            )
            self._last_log_mono = now_mono
        else:
            self._pending += 1
            self._pending_min = dur if self._pending_min is None else min(self._pending_min, dur)
            self._pending_max = dur if self._pending_max is None else max(self._pending_max, dur)

    def flush(self, now_mono):
        """Emit a summary of glitches accumulated during the quiet window."""
        if self._pending and (now_mono - self._last_log_mono) >= self.LOG_INTERVAL:
            event_log.add(
                'glitch',
                f"Interference on {self.name} line: {self._pending} more rejected "
                f"pulses (~{self._pending_min * 1000:.1f}-{self._pending_max * 1000:.1f}ms, "
                f"total {self.count})",
            )
            self._pending = 0
            self._pending_min = None
            self._pending_max = None
            self._last_log_mono = now_mono

    def stats(self):
        return {
            'count': self.count,
            'max_duration_ms': round(self.max_duration * 1000, 1),
            'last': self.last,
        }


# ============================================
# Global State
# ============================================

class SystemState:
    """Global system state manager"""

    def __init__(self):
        self.ftdi_device = None
        self.dmx_data = bytearray([0] * (config.DMX_CHANNELS + 1))
        self.dmx_lock = Lock()
        self.current_scene = None
        self.scene_b_timer = None
        self.timer_lock = Lock()  # Protects scene_b_timer access
        self.gpio_line = None
        self.gpio_safety_line = None
        self.gpio_chip = None
        self.gpio_chip_id = None
        self.gpio_ready = False  # Explicit flag for GPIO readiness
        self.gpio_edge_mode = False  # True when kernel edge events are active
        self.dmx_thread = None
        self.dmx_running = False
        self.enttec_url = None
        self.enttec_last_error = None
        self.last_trigger = None   # {'ts': wall-clock, 'source': 'gpio'|'web'}
        self.trigger_count = 0
        self.started_at = time.time()
        self.glitch_trackers = {
            'contact': GlitchTracker('contact'),
            'safety': GlitchTracker('safety'),
        }

state = SystemState()

# ============================================
# ENTTEC DMX Functions
# ============================================

def init_enttec():
    """Initialize ENTTEC Open DMX USB"""
    if not FTDI_AVAILABLE:
        state.enttec_last_error = f"pyftdi unavailable: {FTDI_IMPORT_ERROR}"
        print(f"ERROR: {state.enttec_last_error}")
        return False

    def _candidate_urls(devices):
        urls = []

        # Always try explicitly configured URL first.
        urls.append(config.FTDI_URL)

        # Then try generic FTDI URLs that often work for single-device setups.
        urls.extend([
            "ftdi://::/1",
            "ftdi://::/2",
            "ftdi://0403:6001/1",
            "ftdi://0403:6001/2",
        ])

        # Finally, try serial-targeted URLs from discovered devices.
        for desc, _iface in devices:
            serial = getattr(desc, 'sn', None)
            if serial:
                urls.append(f"ftdi://::{serial}/1")
                urls.append(f"ftdi://::{serial}/2")

        # De-dupe while preserving order.
        return list(dict.fromkeys(urls))

    try:
        print("Initializing ENTTEC Open DMX USB...")

        devices = Ftdi.list_devices()

        if not devices:
            print("ERROR: No FTDI devices found!")
            state.enttec_last_error = "No FTDI devices found"
            return False

        print(f"Found {len(devices)} FTDI device(s)")
        for idx, (desc, _iface) in enumerate(devices, start=1):
            print(
                f"  Device {idx}: vid=0x{getattr(desc, 'vid', 0):04x} "
                f"pid=0x{getattr(desc, 'pid', 0):04x} "
                f"serial={getattr(desc, 'sn', 'n/a')}"
            )

        last_error = None
        for url in _candidate_urls(devices):
            try:
                ftdi = Ftdi()
                ftdi.open_from_url(url)
                state.ftdi_device = ftdi
                state.enttec_url = url
                state.enttec_last_error = None
                print(f"Opened FTDI device with URL: {url}")
                break
            except Exception as e:
                last_error = e
                print(f"  FTDI open failed for {url}: {e}")

        if state.ftdi_device is None:
            hint = (
                "Unable to open any detected FTDI device. "
                "Make sure ftdi_sio is unloaded/blacklisted, udev permissions are set, "
                "and DMX_FTDI_URL points at the correct adapter/interface."
            )
            state.enttec_last_error = f"{hint} Last error: {last_error}"
            print(f"ERROR: {state.enttec_last_error}")
            return False

        # Configure for DMX512
        state.ftdi_device.set_baudrate(250000)
        state.ftdi_device.set_line_property(8, 2, 'N')
        state.ftdi_device.set_latency_timer(1)

        print("ENTTEC initialized successfully")
        return True

    except Exception as e:
        state.enttec_last_error = str(e)
        print(f"ERROR initializing ENTTEC: {e}")
        return False


def reinit_enttec():
    """Attempt to re-initialize the ENTTEC after a failure"""
    try:
        if state.ftdi_device:
            try:
                state.ftdi_device.close()
            except Exception:
                pass
            state.ftdi_device = None
            state.enttec_url = None
        return init_enttec()
    except Exception as e:
        print(f"ERROR re-initializing ENTTEC: {e}")
        return False


def dmx_refresh_thread():
    """Background thread to continuously send DMX frames.

    Automatically recovers from USB errors by re-initializing the ENTTEC device.
    """
    refresh_interval = 1.0 / config.DMX_REFRESH_RATE
    consecutive_errors = 0
    MAX_ERRORS_BEFORE_REINIT = 3
    REINIT_BACKOFF = 2.0  # seconds to wait before attempting reinit
    offline_backoff = 1.0
    offline_backoff_max = 10.0

    print(f"DMX refresh thread started ({config.DMX_REFRESH_RATE} Hz)")

    while state.dmx_running:
        try:
            if state.ftdi_device is None:
                raise Exception("FTDI device not available")
            with state.dmx_lock:
                # Send BREAK
                state.ftdi_device.set_break(True)
                time.sleep(0.000088)
                state.ftdi_device.set_break(False)
                time.sleep(0.000008)

                # Send data
                state.ftdi_device.write_data(state.dmx_data)

            consecutive_errors = 0
            offline_backoff = 1.0
            time.sleep(refresh_interval)

        except Exception as e:
            consecutive_errors += 1
            if "FTDI device not available" in str(e):
                print(f"WARNING: DMX refresh offline: {e}")
                time.sleep(offline_backoff)
                offline_backoff = min(offline_backoff * 2, offline_backoff_max)
                reinit_enttec()
                continue

            if consecutive_errors <= MAX_ERRORS_BEFORE_REINIT:
                print(f"WARNING: DMX refresh error ({consecutive_errors}/{MAX_ERRORS_BEFORE_REINIT}): {e}")
                time.sleep(0.1)
                continue

            # Too many consecutive errors - attempt to re-initialize
            print(f"ERROR: {consecutive_errors} consecutive DMX failures. Attempting ENTTEC re-init...")
            time.sleep(REINIT_BACKOFF)

            if reinit_enttec():
                print("ENTTEC re-initialized successfully, resuming DMX output")
                consecutive_errors = 0
            else:
                print(f"ENTTEC re-init failed. Retrying in {REINIT_BACKOFF}s...")
                # Keep looping - don't break out. Will retry on next iteration.

    print("DMX refresh thread stopped")


def start_dmx_refresh():
    """Start background DMX refresh thread"""
    if state.dmx_thread is None or not state.dmx_thread.is_alive():
        state.dmx_running = True
        state.dmx_thread = Thread(target=dmx_refresh_thread, daemon=True)
        state.dmx_thread.start()


def stop_dmx_refresh():
    """Stop background DMX refresh thread"""
    if state.dmx_thread is not None:
        state.dmx_running = False
        state.dmx_thread.join(timeout=2)
        state.dmx_thread = None


def set_channel(channel, value):
    """Set a single DMX channel value"""
    if 1 <= channel <= config.DMX_CHANNELS:
        with state.dmx_lock:
            state.dmx_data[int(channel)] = max(0, min(255, int(value)))


def apply_scene(scene_name):
    """Apply a scene to DMX channels"""
    if scene_name not in config.SCENES:
        print(f"ERROR: Scene {scene_name} not found")
        return False

    scene = config.SCENES[scene_name]

    # Apply scene values atomically
    with state.dmx_lock:
        for channel, value in scene['channels'].items():
            if 1 <= int(channel) <= config.DMX_CHANNELS:
                state.dmx_data[int(channel)] = max(0, min(255, int(value)))

    state.current_scene = scene_name
    print(f"Applied scene: {scene['name']}")

    return True


def get_current_channels():
    """Get current DMX channel values"""
    with state.dmx_lock:
        return {
            'fog': state.dmx_data[1],
            'outer_red': state.dmx_data[3],
            'outer_green': state.dmx_data[4],
            'outer_blue': state.dmx_data[5],
            'outer_amber': state.dmx_data[6],
            'inner_red': state.dmx_data[7],
            'inner_green': state.dmx_data[8],
            'inner_blue': state.dmx_data[9],
            'inner_amber': state.dmx_data[10],
            'led_mix1': state.dmx_data[11],
            'led_mix2': state.dmx_data[12],
            'auto_color': state.dmx_data[13],
            'strobe': state.dmx_data[14],
            'dimmer': state.dmx_data[15],
            'safety': state.dmx_data[16],
        }

# ============================================
# GPIO Functions
# ============================================

class LineMonitor:
    """Debounced state tracker for one GPIO line.

    Feed raw samples via sample(); a new raw level must persist
    continuously for stable_time seconds before it is accepted as the
    line's stable level, which filters glitches in BOTH directions.

    sample() returns None when nothing happened, or an event dict:
      {'kind': 'transition', 'from': 0/1 (None = startup baseline),
       'to': 0/1, 'at': mono, 'prev_duration': s}
      {'kind': 'glitch', 'level': 0/1, 'duration': s, 'at': mono}
        — a pulse that ended before satisfying stable_time and was
        rejected. 'duration' is an upper-bound estimate at poll
        resolution: the pulse was first seen at 'at' and had ended by
        the sample that reports it. These are the interference /
        false-trigger events the glitch filter absorbs; surfacing
        them lets interference be measured instead of just filtered.

    The very first stable level is reported with 'from' set to None (a
    baseline, not a real transition), so callers can log the startup
    state without treating it as an edge — a line that is already in
    its "active" state at startup must leave that state and come back
    before it can fire a trigger.

    Invalid reads (None) discard any pending candidate so noise around
    read errors cannot accumulate into a false transition; such aborted
    candidates are not reported as glitches because their length can't
    be trusted.
    """

    def __init__(self, name, stable_time):
        self.name = name
        self.stable_time = stable_time
        self.reset()

    def reset(self):
        self.stable_level = None   # last accepted level (0/1), None until baseline
        self.stable_since = None   # monotonic time current stable level began
        self._candidate = None
        self._candidate_since = None

    def sample(self, level, now):
        """Feed one raw sample (0/1/None). Returns an event dict or None."""
        if level is None:
            self._candidate = None
            self._candidate_since = None
            return None
        if level == self.stable_level:
            if self._candidate is not None and self.stable_level is not None:
                # The line bounced away and came back before stable_time:
                # a rejected pulse. Report it so interference is measurable.
                glitch = {
                    'kind': 'glitch',
                    'level': self._candidate,
                    'duration': now - self._candidate_since,
                    'at': self._candidate_since,
                }
                self._candidate = None
                self._candidate_since = None
                return glitch
            self._candidate = None
            self._candidate_since = None
            return None
        if level != self._candidate:
            self._candidate = level
            self._candidate_since = now
            # No return: with stable_time == 0 the candidate promotes on
            # this same sample (single-sample edge latch).
        # Epsilon keeps hold == poll-interval semantics exact ("two
        # consecutive samples") despite float subtraction error.
        if (now - self._candidate_since) >= self.stable_time - 1e-9:
            prev = self.stable_level
            prev_duration = None
            if self.stable_since is not None:
                prev_duration = self._candidate_since - self.stable_since
            self.stable_level = level
            self.stable_since = self._candidate_since
            self._candidate = None
            self._candidate_since = None
            return {
                'kind': 'transition',
                'from': prev,  # None => baseline (startup state), not an edge
                'to': level,
                'at': self.stable_since,
                'prev_duration': prev_duration,
            }
        return None


class EdgeDebouncer:
    """Event-driven debouncer fed with exact-timestamped kernel edges.

    Same commit semantics as LineMonitor — a new level must survive
    stable_time before it becomes the stable level, shorter excursions
    are reported as glitches — but driven by kernel edge events instead
    of polling, so no pulse is ever missed regardless of width, and
    glitch durations are exact rather than poll-resolution estimates.

    edge(level, ts) ingests one kernel edge and returns a LIST of event
    dicts (possibly empty): a batched pulse can complete a pending
    commit and open a new one in a single call. tick(now) commits a
    pending level that has survived stable_time with no opposing edge
    (a held contact) — call it periodically. seed(level, now) sets the
    startup baseline, returned as a transition with 'from' = None.

    With stable_time == 0 every edge commits immediately (edge latch);
    the post-trigger cooldown still limits fire rate downstream.
    """

    def __init__(self, name, stable_time):
        self.name = name
        self.stable_time = stable_time
        self.reset()

    def reset(self):
        self.stable_level = None
        self.stable_since = None
        self._pending = None
        self._pending_since = None

    def _commit(self, level, at):
        prev = self.stable_level
        prev_duration = None
        if self.stable_since is not None:
            prev_duration = at - self.stable_since
        self.stable_level = level
        self.stable_since = at
        self._pending = None
        self._pending_since = None
        return {
            'kind': 'transition',
            'from': prev,  # None => baseline (startup state), not an edge
            'to': level,
            'at': at,
            'prev_duration': prev_duration,
        }

    def seed(self, level, now):
        """Latch the startup level (baseline). Returns its event or None."""
        if level is None:
            return None
        return self._commit(level, now)

    def edge(self, level, ts):
        """Ingest one kernel edge. Returns a list of event dicts."""
        events = []
        if self._pending is not None:
            if level == self._pending:
                return events  # duplicate same-direction edge
            width = ts - self._pending_since
            if width >= self.stable_time - 1e-9:
                events.append(self._commit(self._pending, self._pending_since))
            else:
                glitch_level = self._pending
                glitch_since = self._pending_since
                self._pending = None
                self._pending_since = None
                if self.stable_level is not None:
                    events.append({
                        'kind': 'glitch',
                        'level': glitch_level,
                        'duration': width,
                        'at': glitch_since,
                    })
        if level == self.stable_level:
            return events
        self._pending = level
        self._pending_since = ts
        if self.stable_time <= 0:
            events.append(self._commit(level, ts))
        return events

    def tick(self, now):
        """Commit a pending level that survived stable_time (held contact)."""
        if (self._pending is not None
                and (now - self._pending_since) >= self.stable_time - 1e-9):
            return self._commit(self._pending, self._pending_since)
        return None


class BurstDetector:
    """Fires on showers of raw kernel edges (trigger mode 'burst').

    Field signature this handles: an electrically marginal line (weak
    pull-up against long/loaded cable) never switches cleanly — each
    physical actuation produces hundreds of microsecond-scale edges
    chattering across the logic threshold instead of one clean pulse.
    The shower itself becomes the trigger: min_edges raw edges within
    window fire once, and re-arming requires quiet_rearm seconds of
    silence first, so a long shower still fires exactly once.
    """

    def __init__(self, min_edges, window, quiet_rearm):
        self.min_edges = min_edges
        self.window = window
        self.quiet_rearm = quiet_rearm
        self.reset()

    def reset(self):
        self._times = deque()
        self._armed = True
        self._last_edge = None

    def edge(self, ts):
        """Feed one raw edge timestamp. Returns burst info when firing."""
        if (self._last_edge is not None
                and (ts - self._last_edge) >= self.quiet_rearm):
            self._armed = True
            self._times.clear()
        self._last_edge = ts
        self._times.append(ts)
        while self._times and (ts - self._times[0]) > self.window:
            self._times.popleft()
        if self._armed and len(self._times) >= self.min_edges:
            self._armed = False
            return {'edges': len(self._times), 'span': ts - self._times[0], 'at': ts}
        return None


def _normalize_gpiochip_id(chip_id):
    if chip_id is None:
        return None
    if isinstance(chip_id, int):
        return chip_id
    chip_id = str(chip_id).strip()
    if chip_id.isdigit():
        return int(chip_id)
    if chip_id.startswith("/dev/") or chip_id.startswith("gpiochip"):
        return chip_id
    return chip_id


def _gpiochip_candidates():
    if config.GPIO_CHIP is not None:
        return [_normalize_gpiochip_id(config.GPIO_CHIP)]
    candidates = []
    for path in sorted(glob.glob("/dev/gpiochip*")):
        candidates.append(path)
    return candidates


def _chip_id_to_path(chip_id):
    """Normalize a chip identifier to a /dev/gpiochipN path string."""
    if chip_id is None:
        return "/dev/gpiochip0"
    if isinstance(chip_id, int):
        return f"/dev/gpiochip{chip_id}"
    chip_id = str(chip_id)
    if chip_id.isdigit():
        return f"/dev/gpiochip{chip_id}"
    if chip_id.startswith("gpiochip"):
        return f"/dev/{chip_id}"
    return chip_id  # Already a full path


def _open_gpiod_line(chip_id):
    """Open the GPIO lines. Returns (chip, line(s), edge_mode)."""
    chip_id = _normalize_gpiochip_id(chip_id)

    # gpiod v2 API: request_lines takes a path string, not a Chip object
    if hasattr(gpiod, "request_lines") and hasattr(gpiod, "LineSettings"):
        chip_path = _chip_id_to_path(chip_id)
        direction_enum = getattr(gpiod, "LineDirection", None)
        bias_enum = getattr(gpiod, "LineBias", None)
        edge_enum = None
        if direction_enum is None and hasattr(gpiod, "line"):
            direction_enum = gpiod.line.Direction
            bias_enum = gpiod.line.Bias
        if hasattr(gpiod, "line"):
            edge_enum = getattr(gpiod.line, "Edge", None)

        # Prefer kernel edge detection: edges are latched with exact
        # timestamps no matter how short the pulse, so sub-poll trigger
        # pulses (some controllers emit ~5ms) can never be missed.
        if edge_enum is not None:
            try:
                line_settings = gpiod.LineSettings(
                    direction=direction_enum.INPUT,
                    bias=bias_enum.PULL_UP,
                    edge_detection=edge_enum.BOTH,
                )
                request = gpiod.request_lines(
                    chip_path,
                    consumer="dmx_controller",
                    config={
                        config.CONTACT_PIN: line_settings,
                        config.SAFETY_SWITCH_PIN: line_settings,
                    },
                    event_buffer_size=64,
                )
                if hasattr(request, "read_edge_events") and hasattr(request, "wait_edge_events"):
                    return None, request, True
                # Library too old to deliver events; fall back to polling.
                request.release()
            except (TypeError, AttributeError):
                pass  # older v2 bindings without edge support -> polling

        line_settings = gpiod.LineSettings(
            direction=direction_enum.INPUT,
            bias=bias_enum.PULL_UP,
        )
        request = gpiod.request_lines(
            chip_path,
            consumer="dmx_controller",
            config={
                config.CONTACT_PIN: line_settings,
                config.SAFETY_SWITCH_PIN: line_settings,
            },
        )
        return None, request, False  # v2: no separate Chip object needed

    # gpiod v1 API: create Chip then request the line
    chip = gpiod.Chip(chip_id) if chip_id is not None else None
    try:
        contact_line = chip.get_line(config.CONTACT_PIN)
        contact_line.request(
            consumer="dmx_controller",
            type=gpiod.LINE_REQ_DIR_IN,
            flags=gpiod.LINE_REQ_FLAG_BIAS_PULL_UP,
        )
        safety_line = chip.get_line(config.SAFETY_SWITCH_PIN)
        safety_line.request(
            consumer="dmx_controller",
            type=gpiod.LINE_REQ_DIR_IN,
            flags=gpiod.LINE_REQ_FLAG_BIAS_PULL_UP,
        )
        return chip, (contact_line, safety_line), False
    except Exception:
        if chip is not None:
            try:
                chip.close()
            except Exception:
                pass
        raise


def _open_lgpio_line(chip_id):
    chip_id = _normalize_gpiochip_id(chip_id)
    if isinstance(chip_id, str):
        digits = "".join(ch for ch in chip_id if ch.isdigit())
        chip_id = int(digits) if digits else None
    chip_id = 0 if chip_id is None else chip_id
    chip = lgpio.gpiochip_open(chip_id)
    try:
        lgpio.gpio_claim_input(chip, config.CONTACT_PIN, lgpio.SET_PULL_UP)
        lgpio.gpio_claim_input(chip, config.SAFETY_SWITCH_PIN, lgpio.SET_PULL_UP)
        return chip
    except Exception:
        try:
            lgpio.gpiochip_close(chip)
        except Exception:
            pass
        raise


_gpio_init_failure_logged = False


def init_gpio():
    """Initialize GPIO for contact closure detection.

    Tries the preferred library first (gpiod), then falls back to the other
    (lgpio) if all chips fail.  This is important for Pi 4 where gpiod may
    have version/compatibility issues while lgpio works fine.
    """
    global GPIO_LIB, _gpio_init_failure_logged

    if not GPIO_AVAILABLE:
        print("GPIO not available (not running on Raspberry Pi)")
        return False

    if state.gpio_line is not None or state.gpio_chip is not None:
        try:
            if GPIO_LIB == 'gpiod' and state.gpio_line is not None:
                state.gpio_line.release()
            if GPIO_LIB == 'gpiod' and state.gpio_safety_line is not None:
                state.gpio_safety_line.release()
            if GPIO_LIB == 'gpiod' and state.gpio_chip is not None:
                state.gpio_chip.close()
            if GPIO_LIB == 'lgpio' and state.gpio_chip is not None:
                lgpio.gpiochip_close(state.gpio_chip)
        except Exception as e:
            print(f"WARNING: GPIO cleanup before init failed: {e}")
        state.gpio_line = None
        state.gpio_safety_line = None
        state.gpio_chip = None
        state.gpio_chip_id = None
        state.gpio_edge_mode = False

    # Build ordered list of libraries to attempt.
    # Preferred library first, then fallback.
    libs_to_try = []
    if GPIO_LIB == 'gpiod':
        libs_to_try.append('gpiod')
        if lgpio is not None:
            libs_to_try.append('lgpio')
    elif GPIO_LIB == 'lgpio':
        libs_to_try.append('lgpio')
        if gpiod is not None:
            libs_to_try.append('gpiod')
    else:
        if gpiod is not None:
            libs_to_try.append('gpiod')
        if lgpio is not None:
            libs_to_try.append('lgpio')

    for lib in libs_to_try:
        try:
            if lib == 'gpiod':
                for chip_id in _gpiochip_candidates():
                    try:
                        state.gpio_chip, opened_line, edge_mode = _open_gpiod_line(chip_id)
                        if isinstance(opened_line, tuple):
                            state.gpio_line, state.gpio_safety_line = opened_line
                        else:
                            state.gpio_line = opened_line
                            state.gpio_safety_line = None
                        state.gpio_edge_mode = edge_mode
                        state.gpio_ready = True
                        state.gpio_chip_id = chip_id
                        GPIO_LIB = 'gpiod'
                        _gpio_init_failure_logged = False
                        event_log.add(
                            'gpio',
                            f"GPIO initialized (gpiod) - {chip_id} pin {config.CONTACT_PIN} "
                            f"with pull-up"
                            + (" - kernel edge detection active" if edge_mode
                               else " - polling mode"),
                        )
                        return True
                    except Exception as e:
                        print(f"GPIO init failed on {chip_id} (gpiod): {e}")

            elif lib == 'lgpio':
                for chip_id in _gpiochip_candidates():
                    try:
                        state.gpio_chip = _open_lgpio_line(chip_id)
                        state.gpio_edge_mode = False
                        state.gpio_ready = True
                        state.gpio_chip_id = chip_id
                        GPIO_LIB = 'lgpio'
                        _gpio_init_failure_logged = False
                        event_log.add(
                            'gpio',
                            f"GPIO initialized (lgpio) - {chip_id} pin {config.CONTACT_PIN} with pull-up",
                        )
                        return True
                    except Exception as e:
                        print(f"GPIO init failed on {chip_id} (lgpio): {e}")

        except Exception as e:
            print(f"GPIO initialization failed ({lib}): {e}")

    state.gpio_ready = False
    if not _gpio_init_failure_logged:
        _gpio_init_failure_logged = True
        event_log.add(
            'gpio',
            "GPIO init failed: all libraries and chips exhausted — trigger unavailable "
            "(will keep retrying silently)",
        )
    return False


def request_gpio_reinit():
    """Ask the monitor thread to re-initialize GPIO with current settings.

    Used after pin/chip config changes so they take effect immediately;
    also re-arms the one-shot init-failure log so the outcome of the
    change is visible in the event log.
    """
    global _gpio_init_failure_logged
    _gpio_init_failure_logged = False
    state.gpio_ready = False


def _gpio_value_to_int(val):
    """Normalize a GPIO read value to int 0 or 1.

    gpiod v2 returns a Value enum (not IntEnum) so direct == comparisons
    against 0/1 would silently fail.  This helper handles both v1 (int)
    and v2 (enum) return types.
    """
    if hasattr(val, 'value'):
        return int(val.value)  # enum -> underlying int
    return int(val)


def _read_gpio_pin(pin):
    if GPIO_LIB == 'gpiod':
        try:
            return _gpio_value_to_int(state.gpio_line.get_value(pin))
        except TypeError:
            line = state.gpio_line if pin == config.CONTACT_PIN else state.gpio_safety_line
            if line is None:
                return None
            return _gpio_value_to_int(line.get_value())
    if GPIO_LIB == 'lgpio':
        return lgpio.gpio_read(state.gpio_chip, pin)
    return None


def check_contact_state():
    """Check current contact closure state. Returns 0 (closed) or 1 (open), or None."""
    if not GPIO_AVAILABLE or not state.gpio_ready:
        return None

    try:
        return _read_gpio_pin(config.CONTACT_PIN)
    except Exception as e:
        print(f"WARNING: GPIO read error: {e}")
        return None


def check_safety_switch_state():
    """Check safety toggle switch state. 0=ON/safe, 1=OFF/unsafe."""
    if not GPIO_AVAILABLE or not state.gpio_ready:
        return None

    try:
        return _read_gpio_pin(config.SAFETY_SWITCH_PIN)
    except Exception as e:
        print(f"WARNING: Safety GPIO read error: {e}")
        return None


def is_safe_to_operate():
    """True when safety switch allows machine operation.

    With the interlock disabled in config, operation is always allowed
    (used when pin 27 is not wired to a real switch, or while diagnosing
    wiring remotely).
    """
    if not config.SAFETY_SWITCH_ENABLED:
        return True
    safety_state = check_safety_switch_state()
    return safety_state == 0


def trigger_sequence(source='web'):
    """Execute the lighting sequence (thread-safe)"""
    if not is_safe_to_operate():
        event_log.add('trigger', f"Trigger from {source} BLOCKED: safety switch is OFF")
        return False

    state.trigger_count += 1
    state.last_trigger = {'ts': time.time(), 'source': source}
    event_log.add(
        'trigger',
        f"Trigger from {source}: Scene B for {config.SCENE_B_DURATION:g}s "
        f"(#{state.trigger_count})",
    )

    with state.timer_lock:
        # Cancel any existing timer
        if state.scene_b_timer is not None:
            state.scene_b_timer.cancel()

        # Apply Scene B (Light ON)
        apply_scene('scene_b')

        # Set timer to return to Scene A (Light OFF)
        def _return_to_scene_a():
            with state.timer_lock:
                apply_scene('scene_a')
                state.scene_b_timer = None

        state.scene_b_timer = Timer(config.SCENE_B_DURATION, _return_to_scene_a)
        state.scene_b_timer.daemon = True
        state.scene_b_timer.start()

    print(f"Timer set: Scene A (OFF) in {config.SCENE_B_DURATION} seconds")
    return True

# ============================================
# Flask Routes
# ============================================



def _validate_channel(channel):
    return 1 <= channel <= config.DMX_CHANNELS


def _sanitize_channel_value(channel, value):
    if channel == 16:
        if not (50 <= value <= 200):
            raise ValueError("Safety channel must be between 50 and 200")
    return max(0, min(255, int(value)))


def _normalize_scene_channels(raw_channels, base_channels=None):
    """Validate scene channel map and merge onto a known-safe base.

    Returns a complete channel map when a base is supplied, preserving any
    unspecified channels instead of dropping them.
    """
    if not isinstance(raw_channels, dict):
        raise ValueError("Scene channel data must be an object")

    channels = dict(base_channels) if isinstance(base_channels, dict) else {}
    for raw_channel, raw_value in raw_channels.items():
        channel = int(raw_channel)
        if not _validate_channel(channel):
            raise ValueError(f"Channel out of range: {channel}")
        channels[channel] = _sanitize_channel_value(channel, int(raw_value))

    # Never permit an invalid safety value in normalized scenes.
    channels[16] = _sanitize_channel_value(16, int(channels.get(16, 100)))
    return channels


@app.route('/')
def index():
    """Main web interface - serve index.html directly"""
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html'))


@app.route('/api/status')
def api_status():
    """Get current system status"""
    contact_state = check_contact_state()
    safety_switch_state = check_safety_switch_state()

    return jsonify({
        'enttec_connected': state.ftdi_device is not None,
        'enttec_url': state.enttec_url,
        'enttec_last_error': state.enttec_last_error,
        'dmx_running': state.dmx_running and (state.dmx_thread is not None and state.dmx_thread.is_alive()),
        'current_scene': state.current_scene,
        'contact_state': 'closed' if contact_state == 0 else 'open' if contact_state == 1 else 'unknown',
        'safety_switch_state': 'on' if safety_switch_state == 0 else 'off' if safety_switch_state == 1 else 'unknown',
        'safe_to_operate': is_safe_to_operate(),
        'gpio_available': GPIO_AVAILABLE,
        'gpio_ready': state.gpio_ready,
        'gpio_lib': GPIO_LIB if state.gpio_ready else None,
        'gpio_chip': str(state.gpio_chip_id) if state.gpio_chip_id is not None else None,
        'gpio_edge_mode': state.gpio_edge_mode,
        'gpio_chip_setting': str(config.GPIO_CHIP) if config.GPIO_CHIP is not None else None,
        'contact_pin': config.CONTACT_PIN,
        'safety_switch_pin': config.SAFETY_SWITCH_PIN,
        'trigger_on': config.TRIGGER_ON,
        'trigger_mode': config.TRIGGER_MODE,
        'burst_min_edges': config.BURST_MIN_EDGES,
        'burst_window': config.BURST_WINDOW,
        'burst_quiet_rearm': config.BURST_QUIET_REARM,
        'trigger_hold_time': config.TRIGGER_HOLD_TIME,
        'debounce_time': config.DEBOUNCE_TIME,
        'safety_switch_enabled': config.SAFETY_SWITCH_ENABLED,
        'last_trigger': state.last_trigger,
        'trigger_count': state.trigger_count,
        'glitches': {
            name: tracker.stats()
            for name, tracker in state.glitch_trackers.items()
        },
        'scene_b_duration': config.SCENE_B_DURATION,
        'channels': get_current_channels(),
    })


@app.route('/api/events')
def api_events():
    """Recent hardware/trigger events (newest first) for remote diagnostics."""
    return jsonify({'events': event_log.snapshot()})


@app.route('/api/trigger', methods=['POST'])
def api_trigger():
    """Manually trigger the sequence"""
    if trigger_sequence():
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Safety switch is OFF - operation blocked'}), 409


@app.route('/api/scene/<scene_name>', methods=['POST'])
def api_apply_scene(scene_name):
    """Apply a specific scene"""
    if scene_name != 'scene_a' and not is_safe_to_operate():
        return jsonify({'error': 'Safety switch is OFF - operation blocked'}), 409
    with state.timer_lock:
        if state.scene_b_timer is not None:
            state.scene_b_timer.cancel()
            state.scene_b_timer = None

    if apply_scene(scene_name):
        return jsonify({'success': True, 'scene': scene_name})
    else:
        return jsonify({'error': 'Scene not found'}), 404


@app.route('/api/scenes', methods=['GET'])
def api_list_scenes():
    """List all available scenes"""
    scenes = {}
    for key, scene in config.SCENES.items():
        scenes[key] = {
            'name': scene['name'],
            'channels': scene['channels']
        }
    return jsonify(scenes)


@app.route('/api/channel', methods=['POST'])
def api_set_channel():
    """Set individual channel value"""
    data = request.get_json()
    if not data or 'channel' not in data or 'value' not in data:
        return jsonify({'error': 'Missing channel or value'}), 400

    try:
        channel = int(data['channel'])
        value = int(data['value'])
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid channel or value'}), 400

    if not _validate_channel(channel):
        return jsonify({'error': 'Channel out of range'}), 400

    try:
        safe_value = _sanitize_channel_value(channel, value)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    set_channel(channel, safe_value)
    return jsonify({'success': True, 'channel': channel, 'value': safe_value})


@app.route('/api/blackout', methods=['POST'])
def api_blackout():
    """Emergency blackout - all channels to zero"""
    with state.timer_lock:
        if state.scene_b_timer is not None:
            state.scene_b_timer.cancel()
            state.scene_b_timer = None
    with state.dmx_lock:
        for i in range(1, config.DMX_CHANNELS + 1):
            state.dmx_data[i] = 0
        # Keep safety channel valid so fixture stays responsive to future commands
        state.dmx_data[16] = 100
    state.current_scene = None
    print("BLACKOUT - All channels zeroed (safety channel kept valid)")
    return jsonify({'success': True})


@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    """Get or update configuration"""
    if request.method == 'POST':
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'Invalid or missing JSON body'}), 400

        # Update any scene
        for scene_key in ['scene_a', 'scene_b', 'scene_c', 'scene_d']:
            if scene_key in data:
                try:
                    raw = data[scene_key]
                    channels = _normalize_scene_channels(
                        raw,
                        base_channels=config.SCENES[scene_key]['channels'],
                    )
                except (TypeError, ValueError) as e:
                    return jsonify({'error': f'Invalid channel data in {scene_key}: {e}'}), 400
                config.SCENES[scene_key]['channels'] = channels
                print(f"Updated {scene_key}: {config.SCENES[scene_key]['channels']}")
                # Re-apply if it's the current scene
                if state.current_scene == scene_key:
                    apply_scene(scene_key)

        # Update duration (clamp to safe range)
        if 'scene_b_duration' in data:
            try:
                dur = float(data['scene_b_duration'])
                if dur != dur:  # NaN check
                    return jsonify({'error': 'Invalid duration value'}), 400
                dur = max(0.5, min(300.0, dur))
                config.SCENE_B_DURATION = dur
                print(f"Updated Scene B duration: {config.SCENE_B_DURATION}s")
            except (TypeError, ValueError):
                return jsonify({'error': 'Invalid duration value'}), 400

        # Trigger polarity: which stable contact transition fires
        if 'trigger_on' in data:
            if data['trigger_on'] not in ('close', 'open'):
                return jsonify({'error': "trigger_on must be 'close' or 'open'"}), 400
            if data['trigger_on'] != config.TRIGGER_ON:
                config.TRIGGER_ON = data['trigger_on']
                event_log.add(
                    'config',
                    f"Trigger polarity set to fire on contact {config.TRIGGER_ON.upper()}",
                )

        # Trigger detection mode: debounced level change vs raw edge burst
        if 'trigger_mode' in data:
            if data['trigger_mode'] not in ('level', 'burst'):
                return jsonify({'error': "trigger_mode must be 'level' or 'burst'"}), 400
            if data['trigger_mode'] != config.TRIGGER_MODE:
                config.TRIGGER_MODE = data['trigger_mode']
                event_log.add(
                    'config',
                    "Trigger detection set to "
                    + ("EDGE BURST (fires on showers of raw edges)"
                       if config.TRIGGER_MODE == 'burst'
                       else "LEVEL (fires on debounced state change)"),
                )

        # Burst-mode tuning
        for key, clamp, attr, label in (
                ('burst_min_edges', _clamp_burst_min_edges, 'BURST_MIN_EDGES', 'min edges'),
                ('burst_window', _clamp_burst_window, 'BURST_WINDOW', 'window'),
                ('burst_quiet_rearm', _clamp_burst_quiet, 'BURST_QUIET_REARM', 're-arm quiet')):
            if key in data:
                try:
                    setattr(config, attr, clamp(data[key]))
                except (TypeError, ValueError) as e:
                    return jsonify({'error': f'Invalid {key}: {e}'}), 400
                event_log.add('config', f"Burst {label} set to {getattr(config, attr)}")

        # Glitch-filter window and post-trigger cooldown
        if 'trigger_hold_time' in data:
            try:
                config.TRIGGER_HOLD_TIME = _clamp_hold_time(float(data['trigger_hold_time']))
            except (TypeError, ValueError):
                return jsonify({'error': 'Invalid trigger_hold_time value'}), 400
            event_log.add('config', f"Glitch filter set to {config.TRIGGER_HOLD_TIME * 1000:g}ms")

        if 'debounce_time' in data:
            try:
                config.DEBOUNCE_TIME = _clamp_debounce_time(float(data['debounce_time']))
            except (TypeError, ValueError):
                return jsonify({'error': 'Invalid debounce_time value'}), 400
            event_log.add('config', f"Trigger cooldown set to {config.DEBOUNCE_TIME:g}s")

        # Hardware safety-switch interlock enable/bypass
        if 'safety_switch_enabled' in data:
            if not isinstance(data['safety_switch_enabled'], bool):
                return jsonify({'error': 'safety_switch_enabled must be true or false'}), 400
            if data['safety_switch_enabled'] != config.SAFETY_SWITCH_ENABLED:
                config.SAFETY_SWITCH_ENABLED = data['safety_switch_enabled']
                event_log.add(
                    'config',
                    "Safety switch interlock "
                    + ("ENABLED" if config.SAFETY_SWITCH_ENABLED else "BYPASSED"),
                )

        # GPIO pin / chip assignment — takes effect immediately via re-init
        gpio_changed = False
        if 'contact_pin' in data or 'safety_switch_pin' in data:
            try:
                new_contact = _validate_bcm_pin(data.get('contact_pin', config.CONTACT_PIN))
                new_safety = _validate_bcm_pin(data.get('safety_switch_pin', config.SAFETY_SWITCH_PIN))
            except (TypeError, ValueError) as e:
                return jsonify({'error': f'Invalid GPIO pin: {e}'}), 400
            if new_contact == new_safety:
                return jsonify({'error': 'Contact and safety pins must be different'}), 400
            if (new_contact, new_safety) != (config.CONTACT_PIN, config.SAFETY_SWITCH_PIN):
                config.CONTACT_PIN = new_contact
                config.SAFETY_SWITCH_PIN = new_safety
                gpio_changed = True
                event_log.add(
                    'config',
                    f"GPIO pins set: contact={new_contact}, safety={new_safety}",
                )
        if 'gpio_chip' in data:
            try:
                new_chip = _normalize_gpio_chip_setting(data['gpio_chip'])
            except (TypeError, ValueError) as e:
                return jsonify({'error': str(e)}), 400
            if new_chip != config.GPIO_CHIP:
                config.GPIO_CHIP = new_chip
                gpio_changed = True
                event_log.add(
                    'config',
                    f"GPIO chip set to {new_chip if new_chip is not None else 'auto'}",
                )
        if gpio_changed:
            request_gpio_reinit()

        # Persist to disk
        save_config()

        return jsonify({'success': True})
    else:
        return jsonify({
            'scene_a': config.SCENES['scene_a']['channels'],
            'scene_b': config.SCENES['scene_b']['channels'],
            'scene_c': config.SCENES['scene_c']['channels'],
            'scene_d': config.SCENES['scene_d']['channels'],
            'scene_b_duration': config.SCENE_B_DURATION,
            'contact_pin': config.CONTACT_PIN,
            'safety_switch_pin': config.SAFETY_SWITCH_PIN,
            'gpio_chip': str(config.GPIO_CHIP) if config.GPIO_CHIP is not None else None,
            'trigger_on': config.TRIGGER_ON,
            'trigger_mode': config.TRIGGER_MODE,
            'trigger_hold_time': config.TRIGGER_HOLD_TIME,
            'debounce_time': config.DEBOUNCE_TIME,
            'burst_min_edges': config.BURST_MIN_EDGES,
            'burst_window': config.BURST_WINDOW,
            'burst_quiet_rearm': config.BURST_QUIET_REARM,
            'safety_switch_enabled': config.SAFETY_SWITCH_ENABLED,
        })

# ============================================
# Health Check
# ============================================

@app.route('/api/health')
def api_health():
    """Health check endpoint for monitoring and install verification."""
    healthy = state.dmx_running and state.dmx_thread is not None and state.dmx_thread.is_alive()
    status_code = 200 if healthy else 503
    return jsonify({
        'status': 'ok' if healthy else 'degraded',
        'enttec_connected': state.ftdi_device is not None,
        'enttec_url': state.enttec_url,
        'enttec_last_error': state.enttec_last_error,
        'dmx_running': healthy,
        'gpio_available': GPIO_AVAILABLE,
        'gpio_ready': state.gpio_ready,
        'safe_to_operate': is_safe_to_operate(),
    }), status_code

# ============================================
# Initialization & Shutdown
# ============================================

_initialized = False


def _fmt_duration(seconds):
    """Human-readable duration for event log messages."""
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s"


def _level_text(level):
    return 'CLOSED' if level == 0 else 'OPEN'


def gpio_fire_level():
    """Stable contact level that fires the trigger, per configured polarity."""
    return 0 if config.TRIGGER_ON == 'close' else 1


def _fire_gpio_trigger(now, trigger_ctx, detail=None):
    """Fire the sequence from a GPIO detection, honoring the cooldown."""
    last = trigger_ctx.get('last_trigger_time')
    if last is None or (now - last) >= config.DEBOUNCE_TIME:
        trigger_ctx['last_trigger_time'] = now
        if detail:
            event_log.add('contact', detail)
        trigger_sequence(source='gpio')
    else:
        event_log.add('trigger', "GPIO trigger suppressed (cooldown active)")


def _apply_contact_event(evt, now, trigger_ctx, fire=True):
    """Handle one debounced contact-line event (shared by both monitor modes).

    fire=False records transitions/glitches for diagnostics without firing
    the sequence — used while burst mode owns the trigger decision.
    """
    if evt['kind'] == 'glitch':
        state.glitch_trackers['contact'].record(evt, now)
        return
    if evt['from'] is None:
        event_log.add('contact', f"Contact line is {_level_text(evt['to'])} at startup")
        return
    event_log.add(
        'contact',
        f"Contact {_level_text(evt['to'])} "
        f"(was {_level_text(evt['from'])} for {_fmt_duration(evt['prev_duration'])})",
    )
    if fire and evt['to'] == gpio_fire_level():
        _fire_gpio_trigger(now, trigger_ctx)


def _apply_safety_event(evt, now):
    """Handle one debounced safety-line event (shared by both monitor modes)."""
    if evt['kind'] == 'glitch':
        state.glitch_trackers['safety'].record(evt, now)
        return
    if evt['from'] is None:
        event_log.add(
            'safety',
            f"Safety switch is {'ON' if evt['to'] == 0 else 'OFF'} at startup",
        )
        return
    if evt['to'] == 1:
        event_log.add('safety', "Safety switch turned OFF")
        if config.SAFETY_SWITCH_ENABLED:
            event_log.add('safety', "Forcing Scene A (safety lockout)")
            with state.timer_lock:
                if state.scene_b_timer is not None:
                    state.scene_b_timer.cancel()
                    state.scene_b_timer = None
            apply_scene('scene_a')
    else:
        event_log.add('safety', "Safety switch turned ON")


def _gpio_monitor():
    """Background thread: watches both GPIO lines and fires the trigger
    sequence on the configured contact transition.

    Two modes, selected at init time:
    - Kernel edge detection (gpiod v2): the kernel latches every edge
      with exact timestamps, so arbitrarily short trigger pulses are
      never missed and glitch widths are measured precisely.
    - Polling fallback (gpiod v1 / lgpio): samples every
      GPIO_POLL_INTERVAL with the same debounce semantics.
    """
    contact = LineMonitor('contact', config.TRIGGER_HOLD_TIME)
    safety = LineMonitor('safety', config.TRIGGER_HOLD_TIME)
    e_contact = EdgeDebouncer('contact', config.TRIGGER_HOLD_TIME)
    e_safety = EdgeDebouncer('safety', config.TRIGGER_HOLD_TIME)
    burst = BurstDetector(config.BURST_MIN_EDGES, config.BURST_WINDOW,
                          config.BURST_QUIET_REARM)
    trigger_ctx = {'last_trigger_time': None}
    last_good_read = None
    consecutive_errors = 0
    max_errors_before_reinit = 3
    edge_seeded = False
    burst_fallback_warned = False

    while True:
        try:
            if not state.gpio_ready:
                if init_gpio():
                    contact.reset()
                    safety.reset()
                    e_contact.reset()
                    e_safety.reset()
                    burst.reset()
                    last_good_read = None
                    edge_seeded = False
                    burst_fallback_warned = False
                else:
                    time.sleep(5.0)
                    continue

            # Latch startup baselines from a direct read. GPIO is usually
            # initialized by _initialize() before this thread starts, so
            # seeding must happen here on the first pass, not only after a
            # monitor-driven re-init.
            if state.gpio_edge_mode and not edge_seeded:
                edge_seeded = True
                now = time.monotonic()
                evt = e_contact.seed(check_contact_state(), now)
                if evt is not None:
                    _apply_contact_event(evt, now, trigger_ctx)
                evt = e_safety.seed(check_safety_switch_state(), now)
                if evt is not None:
                    _apply_safety_event(evt, now)

            # Pick up runtime config changes made from the web UI. The
            # safety toggle keeps a stability floor so a near-zero glitch
            # filter can't make the lockout flap on switch bounce.
            safety_stable = max(config.TRIGGER_HOLD_TIME, config.SAFETY_STABLE_TIME_MIN)
            contact.stable_time = e_contact.stable_time = config.TRIGGER_HOLD_TIME
            safety.stable_time = e_safety.stable_time = safety_stable

            burst_mode = config.TRIGGER_MODE == 'burst'
            if burst_mode and not state.gpio_edge_mode and not burst_fallback_warned:
                burst_fallback_warned = True
                event_log.add(
                    'gpio',
                    "Burst trigger mode needs kernel edge detection; this backend "
                    "is polling — falling back to level detection",
                )

            if state.gpio_edge_mode:
                # --- kernel edge-event mode ---
                burst.min_edges = config.BURST_MIN_EDGES
                burst.window = config.BURST_WINDOW
                burst.quiet_rearm = config.BURST_QUIET_REARM

                have_events = state.gpio_line.wait_edge_events(0.02)
                now = time.monotonic()
                if have_events:
                    for ev in state.gpio_line.read_edge_events():
                        level = 1 if ev.event_type == gpiod.EdgeEvent.Type.RISING_EDGE else 0
                        ts = ev.timestamp_ns / 1e9
                        if ev.line_offset == config.CONTACT_PIN:
                            hit = burst.edge(ts)
                            if burst_mode and hit is not None:
                                _fire_gpio_trigger(
                                    now, trigger_ctx,
                                    detail=(f"Edge burst detected: {hit['edges']} edges "
                                            f"in {hit['span'] * 1000:.0f}ms"),
                                )
                            for evt in e_contact.edge(level, ts):
                                _apply_contact_event(evt, now, trigger_ctx,
                                                     fire=not burst_mode)
                        else:
                            for evt in e_safety.edge(level, ts):
                                _apply_safety_event(evt, now)
                evt = e_contact.tick(now)
                if evt is not None:
                    _apply_contact_event(evt, now, trigger_ctx, fire=not burst_mode)
                evt = e_safety.tick(now)
                if evt is not None:
                    _apply_safety_event(evt, now)
                for tracker in state.glitch_trackers.values():
                    tracker.flush(now)
                consecutive_errors = 0
                continue

            # --- polling fallback mode ---
            now = time.monotonic()
            contact_level = check_contact_state()
            safety_level = check_safety_switch_state()

            # The read helpers swallow their own exceptions and return None.
            # If both lines stay unreadable the GPIO handle is dead — tear
            # down and re-initialize instead of spinning silently forever.
            if contact_level is None and safety_level is None:
                if last_good_read is None:
                    last_good_read = now
                elif (now - last_good_read) >= config.GPIO_READ_FAIL_REINIT:
                    event_log.add('gpio', "GPIO reads failing continuously - re-initializing")
                    state.gpio_ready = False
                    last_good_read = None
                    continue
            else:
                last_good_read = now

            evt = contact.sample(contact_level, now)
            if evt is not None:
                _apply_contact_event(evt, now, trigger_ctx)

            evt = safety.sample(safety_level, now)
            if evt is not None:
                _apply_safety_event(evt, now)

            for tracker in state.glitch_trackers.values():
                tracker.flush(now)

            consecutive_errors = 0
            time.sleep(config.GPIO_POLL_INTERVAL)
        except Exception as e:
            consecutive_errors += 1
            print(f"WARNING: GPIO monitor error ({consecutive_errors}/{max_errors_before_reinit}): {e}")
            if consecutive_errors >= max_errors_before_reinit:
                event_log.add('gpio', f"GPIO monitor errors - re-initializing ({e})")
                state.gpio_ready = False
                consecutive_errors = 0
            time.sleep(1.0)


def _cleanup():
    """Release hardware resources on shutdown."""
    global _initialized
    if not _initialized:
        return
    _initialized = False

    print("Shutting down DMX controller...")
    stop_dmx_refresh()

    with state.timer_lock:
        if state.scene_b_timer:
            state.scene_b_timer.cancel()

    if state.ftdi_device:
        try:
            state.ftdi_device.close()
        except Exception:
            pass
    state.ftdi_device = None
    state.enttec_url = None

    if GPIO_AVAILABLE:
        try:
            if GPIO_LIB == 'gpiod' and state.gpio_line is not None:
                state.gpio_line.release()
            if GPIO_LIB == 'gpiod' and state.gpio_safety_line is not None:
                state.gpio_safety_line.release()
            if GPIO_LIB == 'gpiod' and state.gpio_chip is not None:
                state.gpio_chip.close()
            if GPIO_LIB == 'lgpio' and state.gpio_chip is not None:
                lgpio.gpiochip_close(state.gpio_chip)
        except Exception as e:
            print(f"WARNING: GPIO cleanup failed: {e}")

    print("Shutdown complete")


def _initialize():
    """Initialize all hardware and start background threads.

    Safe to call multiple times — only the first call takes effect.
    Called automatically at module load so that gunicorn workers
    (which import this module but never call main()) are fully
    initialized.
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    print("=" * 60)
    print("DMX CONTROLLER - DJPOWER H-IP20V Fog Machine")
    print("=" * 60)
    print()

    load_config()

    if not init_enttec():
        print("WARNING: ENTTEC not available at startup. Will keep retrying in the background.")

    start_dmx_refresh()
    time.sleep(0.5)

    init_gpio()
    apply_scene('scene_a')

    if GPIO_AVAILABLE:
        Thread(target=_gpio_monitor, daemon=True).start()

    # Register cleanup for graceful shutdown
    atexit.register(_cleanup)

    print()
    print("=" * 60)
    print("System ready!")
    print("   Default: All OFF (Scene A)")
    print(f"   On trigger: Fog ON for {config.SCENE_B_DURATION} seconds (Scene B)")
    print("   Custom scenes: C & D available")
    print()
    print("   Web interface: http://0.0.0.0:5000")
    if GPIO_AVAILABLE and state.gpio_ready:
        print(f"   GPIO trigger pin {config.CONTACT_PIN} monitoring active")
        print(f"   GPIO safety switch pin {config.SAFETY_SWITCH_PIN} monitoring active")
    elif GPIO_AVAILABLE:
        print("   GPIO available but init failed - monitor will retry automatically")
    print("=" * 60)
    print()


def _on_sigterm(_signum, _frame):
    """Convert SIGTERM to a clean exit so atexit handlers run."""
    sys.exit(0)


# --- Module-level initialization ---
# This runs when gunicorn imports the module (app:app) OR when run directly.
# Set DMX_SKIP_AUTOINIT=1 to import without touching hardware (tests/tooling).
if os.environ.get("DMX_SKIP_AUTOINIT") != "1":
    signal.signal(signal.SIGTERM, _on_sigterm)
    _initialize()


def main():
    """Entry point for direct execution (python app.py)."""
    try:
        app.run(host='0.0.0.0', port=5000, debug=False)
    except KeyboardInterrupt:
        print("\nKeyboard interrupt received")


if __name__ == "__main__":
    main()

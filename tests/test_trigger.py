#!/usr/bin/env python3
"""Tests for the GPIO trigger engine (LineMonitor + polarity decision).

Dependency-free: run directly with the project venv's python:

    DMX_SKIP_AUTOINIT=1 python3 tests/test_trigger.py

Simulates the monitor loop's sampling of realistic waveforms (button
presses, relay pulses, EMI glitches, inverted/normally-closed wiring)
across every poll alignment, and asserts on exactly when triggers fire.
"""

import os
import sys

os.environ["DMX_SKIP_AUTOINIT"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


POLL = 0.01
HOLD = 0.05
COOLDOWN = 0.3


def simulate(pulses, trigger_on, duration=4.0, offset=0.0,
             hold=HOLD, cooldown=COOLDOWN, none_windows=()):
    """Replicate the monitor loop's trigger decision over a waveform.

    pulses: list of (start, length) intervals where the line reads LOW (0);
            outside them it reads HIGH (1) via the pull-up.
    none_windows: intervals where reads fail (None), as (start, length).
    Returns (trigger_times, transitions, baseline).
    """
    def read(t):
        if any(s <= t < s + l for s, l in none_windows):
            return None
        return 0 if any(s <= t < s + l for s, l in pulses) else 1

    monitor = app.LineMonitor('contact', hold)
    fire_level = 0 if trigger_on == 'close' else 1
    triggers = []
    transitions = []
    baseline = None
    last_trigger_time = None

    t = offset
    while t < duration:
        tr = monitor.sample(read(t), t)
        if tr is not None:
            if tr['from'] is None:
                baseline = tr['to']
            else:
                transitions.append(tr)
                if tr['to'] == fire_level:
                    if last_trigger_time is None or (t - last_trigger_time) >= cooldown:
                        last_trigger_time = t
                        triggers.append(round(t, 4))
        t = round(t + POLL, 6)
    return triggers, transitions, baseline


def sweep(pulses, trigger_on, **kw):
    """Run simulate() across 10 poll alignments; return set of trigger counts."""
    counts = set()
    for o in range(10):
        trig, _, _ = simulate(pulses, trigger_on, offset=o * 0.001, **kw)
        counts.add(len(trig))
    return counts


passed = 0


def check(name, condition, detail=""):
    global passed
    if not condition:
        raise AssertionError(f"FAIL: {name} {detail}")
    passed += 1
    print(f"  ok - {name}")


print("Normally-open wiring (trigger_on='close', line idles HIGH):")
check("80ms tap fires exactly once (all alignments)",
      sweep([(0.5, 0.08)], 'close') == {1})
check("100ms relay pulse fires exactly once",
      sweep([(0.5, 0.10)], 'close') == {1})
check("2s sustained hold fires exactly once",
      sweep([(0.5, 2.0)], 'close') == {1})
check("1ms EMI glitch never fires",
      sweep([(0.5, 0.001)], 'close') == {0})
check("burst of five 2ms glitches never fires",
      sweep([(0.5 + i * 0.05, 0.002) for i in range(5)], 'close') == {0})
check("30ms transient never fires (below 50ms filter)",
      sweep([(0.5, 0.03)], 'close') == {0})
check("two presses 1s apart fire twice",
      sweep([(0.5, 0.15), (1.5, 0.15)], 'close') == {2})
check("re-press within cooldown fires once",
      sweep([(0.5, 0.1), (0.75, 0.1)], 'close') == {1})
check("re-press after cooldown fires twice",
      sweep([(0.5, 0.1), (1.0, 0.1)], 'close') == {2})
check("contact bounce then press fires once",
      sweep([(0.5, 0.005), (0.51, 0.005), (0.52, 0.2)], 'close') == {1})

print("Inverted / normally-closed wiring (trigger_on='open', line idles LOW):")
# Line low from t=0; trigger event lifts it high for the pulse duration.
def inverted(events, total=4.0):
    """Build LOW intervals covering everything except the given HIGH events."""
    lows = []
    cursor = 0.0
    for s, l in events:
        lows.append((cursor, s - cursor))
        cursor = s + l
    lows.append((cursor, total - cursor))
    return lows

check("boot with line already LOW never fires by itself",
      sweep(inverted([]), 'open') == {0})
check("150ms high pulse fires exactly once",
      sweep(inverted([(1.0, 0.15)]), 'open') == {1})
check("100ms high pulse fires exactly once",
      sweep(inverted([(1.0, 0.10)]), 'open') == {1})
check("30ms high transient never fires",
      sweep(inverted([(1.0, 0.03)]), 'open') == {0})
check("two pulses 1s apart fire twice",
      sweep(inverted([(1.0, 0.15), (2.0, 0.15)]), 'open') == {2})

trig, transitions, baseline = simulate(inverted([(1.0, 0.15)]), 'open')
check("baseline latched as CLOSED (0) without an edge",
      baseline == 0)
check("pulse produces open+close transition pair",
      [t['to'] for t in transitions] == [1, 0])
check("fire happens near pulse start (within 80ms)",
      trig and 1.0 <= trig[0] <= 1.08, f"got {trig}")

print("Boot-state safety (the restart-fires-fog bug):")
check("boot with line LOW in 'close' mode never fires by itself",
      sweep(inverted([]), 'close') == {0})
check("boot LOW then release then press fires once ('close' mode)",
      sweep(inverted([(1.0, 3.0)]) + [(1.5, 0.2)], 'close') == {1})

print("Read-failure robustness:")
check("None reads during a press don't create false transitions",
      sweep([(0.5, 0.5)], 'close', none_windows=[(0.6, 0.1)]) == {1})
check("None streak alone produces nothing",
      sweep([], 'close', none_windows=[(0.5, 1.0)]) == {0})
# A glitch bracketed by read errors must not accumulate into a transition.
check("glitch split by read errors never fires",
      sweep([(0.5, 0.02), (0.54, 0.02)], 'close',
            none_windows=[(0.52, 0.02)]) == {0})

print("Runtime tunability:")
# Pulses placed at t=2.0 so the 800ms filter has an established baseline.
check("800ms hold rejects 300ms press (old behavior reproducible)",
      sweep([(2.0, 0.3)], 'close', hold=0.8) == {0})
check("800ms hold accepts 1s press",
      sweep([(2.0, 1.0)], 'close', hold=0.8) == {1})

print("Config clamps:")
check("hold time clamps low", app._clamp_hold_time(0.0001) == 0.02)
check("hold time clamps high", app._clamp_hold_time(99) == 2.0)
check("debounce clamps low", app._clamp_debounce_time(-5) == 0.0)
check("debounce clamps high", app._clamp_debounce_time(999) == 30.0)
for bad in (float('nan'),):
    try:
        app._clamp_hold_time(bad)
        raise AssertionError("FAIL: NaN accepted by _clamp_hold_time")
    except ValueError:
        passed += 1
        print("  ok - NaN rejected by clamp")

print("Pin/chip validation:")
check("pin 17 valid", app._validate_bcm_pin(17) == 17)
check("pin string '5' valid", app._validate_bcm_pin("5") == 5)
for bad in (0, 1, 28, -3, "x", None):
    try:
        app._validate_bcm_pin(bad)
        raise AssertionError(f"FAIL: pin {bad!r} accepted")
    except (TypeError, ValueError):
        passed += 1
        print(f"  ok - pin {bad!r} rejected")
check("chip 'auto' -> None", app._normalize_gpio_chip_setting('auto') is None)
check("chip '' -> None", app._normalize_gpio_chip_setting('') is None)
check("chip None -> None", app._normalize_gpio_chip_setting(None) is None)
check("chip '4' -> 4", app._normalize_gpio_chip_setting('4') == 4)
check("chip 'gpiochip4' passes", app._normalize_gpio_chip_setting('gpiochip4') == 'gpiochip4')
check("chip '/dev/gpiochip0' passes",
      app._normalize_gpio_chip_setting('/dev/gpiochip0') == '/dev/gpiochip0')
try:
    app._normalize_gpio_chip_setting('bogus')
    raise AssertionError("FAIL: bogus chip accepted")
except ValueError:
    passed += 1
    print("  ok - bogus chip name rejected")

print("Event log:")
log = app.EventLog(maxlen=3)
for i in range(5):
    log.add('test', f"event {i}")
snap = log.snapshot()
check("ring buffer keeps newest entries, newest first",
      [e['message'] for e in snap] == ["event 4", "event 3", "event 2"])
check("entries carry id/ts/kind",
      all('id' in e and 'ts' in e and 'kind' in e for e in snap))

print(f"\nAll {passed} checks passed.")

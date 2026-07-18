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
             hold=HOLD, cooldown=COOLDOWN, none_windows=(), poll=POLL):
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
    glitches = []
    baseline = None
    last_trigger_time = None

    t = offset
    while t < duration:
        tr = monitor.sample(read(t), t)
        if tr is not None:
            if tr['kind'] == 'glitch':
                glitches.append(tr)
            elif tr['from'] is None:
                baseline = tr['to']
            else:
                transitions.append(tr)
                if tr['to'] == fire_level:
                    if last_trigger_time is None or (t - last_trigger_time) >= cooldown:
                        last_trigger_time = t
                        triggers.append(round(t, 4))
        t = round(t + poll, 6)
    return triggers, transitions, baseline, glitches


def sweep(pulses, trigger_on, **kw):
    """Run simulate() across 10 poll alignments; return set of trigger counts."""
    counts = set()
    for o in range(10):
        trig, _, _, _ = simulate(pulses, trigger_on, offset=o * 0.001, **kw)
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

trig, transitions, baseline, _ = simulate(inverted([(1.0, 0.15)]), 'open')
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

print("Short-pulse triggers (the ~10ms field case):")
# Production config: 5ms poll. A 10ms HIGH pulse on an inverted (idles LOW)
# line with a 5ms filter must fire on every alignment.
check("10ms pulse fires with 5ms filter at 5ms poll (inverted wiring)",
      sweep(inverted([(1.0, 0.010)]), 'open', hold=0.005, poll=0.005) == {1})
check("10ms pulse rejected by 50ms filter (the reported bug)",
      sweep(inverted([(1.0, 0.010)]), 'open', hold=0.05, poll=0.005) == {0})
check("sub-5ms spike still rejected with 5ms filter",
      sweep(inverted([(1.0, 0.002)]), 'open', hold=0.005, poll=0.005) == {0})
check("two 10ms pulses 1s apart fire twice (5ms filter)",
      sweep(inverted([(1.0, 0.010), (2.0, 0.010)]), 'open',
            hold=0.005, poll=0.005) == {2})

print("Zero filter (single-sample edge latch):")
check("10ms pulse always fires with 0 filter at 10ms poll",
      sweep([(0.5, 0.010)], 'close', hold=0.0) <= {1} and
      1 in sweep([(0.5, 0.010)], 'close', hold=0.0))
check("12ms pulse fires exactly once with 0 filter",
      sweep([(0.5, 0.012)], 'close', hold=0.0) == {1})
check("boot with line LOW and 0 filter still never self-fires",
      sweep(inverted([]), 'open', hold=0.0) == {0} and
      sweep(inverted([]), 'close', hold=0.0) == {0})

print("EdgeDebouncer (kernel edge-event mode):")


def edge_run(seq, hold, seed_level=0, trigger_on='open', tick_every=0.02, until=None):
    """Feed (level, ts) kernel edges through EdgeDebouncer with periodic
    ticks, replicating the monitor's fire decision. Returns
    (fires, transitions, glitches)."""
    deb = app.EdgeDebouncer('contact', hold)
    fire_level = 0 if trigger_on == 'close' else 1
    fires, transitions, glitches = [], [], []
    last_fire = None

    def handle(evt, now):
        nonlocal last_fire
        if evt['kind'] == 'glitch':
            glitches.append(evt)
        elif evt['from'] is not None:
            transitions.append(evt)
            if evt['to'] == fire_level:
                if last_fire is None or (now - last_fire) >= COOLDOWN:
                    last_fire = now
                    fires.append(round(evt['at'], 6))

    evt = deb.seed(seed_level, 0.0)
    assert evt is not None and evt['from'] is None  # baseline, never a fire
    end = until if until is not None else (seq[-1][1] + 1.0 if seq else 1.0)
    t = 0.0
    i = 0
    while t <= end:
        while i < len(seq) and seq[i][1] <= t:
            level, ts = seq[i]
            for e in deb.edge(level, ts):
                handle(e, t)
            i += 1
        e = deb.tick(t)
        if e is not None:
            handle(e, t)
        t = round(t + tick_every, 6)
    return fires, transitions, glitches


# Their rig: line idles LOW, controller emits short HIGH pulses.
fires, trans, gl = edge_run([(1, 1.0), (0, 1.005)], hold=0.002)
check("5ms pulse fires with 2ms filter (edge mode)",
      fires == [1.0] and len(gl) == 0, f"fires={fires} glitches={gl}")
fires, trans, gl = edge_run([(1, 1.0), (0, 1.0005)], hold=0.002)
check("0.5ms pulse rejected by 2ms filter with EXACT width",
      fires == [] and len(gl) == 1 and abs(gl[0]['duration'] - 0.0005) < 1e-12,
      f"glitch width {gl[0]['duration'] if gl else '-'}")
fires, trans, gl = edge_run([(1, 1.0), (0, 1.0005)], hold=0.0)
check("0.5ms pulse FIRES with 0 filter (kernel latched, never missed)",
      fires == [1.0], str(fires))
fires, trans, gl = edge_run([(1, 1.0), (0, 1.2)], hold=0.05)
check("held 200ms pulse fires once via tick commit",
      fires == [1.0] and [t['to'] for t in trans] == [1, 0], str(fires))
fires, trans, gl = edge_run([(1, 1.0), (0, 1.005), (1, 2.0), (0, 2.005)], hold=0.002)
check("two 5ms pulses 1s apart fire twice", fires == [1.0, 2.0], str(fires))
fires, trans, gl = edge_run([(1, 1.0), (0, 1.005), (1, 1.1), (0, 1.105)], hold=0.002)
check("second pulse inside 300ms cooldown suppressed", fires == [1.0], str(fires))
fires, trans, gl = edge_run([(1, 1.0), (1, 1.001), (0, 1.005)], hold=0.002)
check("duplicate same-direction kernel edge ignored",
      fires == [1.0] and len(gl) == 0)
fires, trans, gl = edge_run([(0, 1.0), (1, 1.004), (0, 1.05), (1, 1.051)], hold=0.002,
                            trigger_on='close', seed_level=1)
check("close-mode: 4ms LOW pulse fires, later 1ms LOW spike is a glitch",
      fires == [1.0] and len(gl) == 1 and gl[0]['level'] == 0
      and abs(gl[0]['duration'] - 0.001) < 1e-12, f"{fires} {gl}")

# Batched delivery: a whole pulse can arrive in one read_edge_events() batch.
deb = app.EdgeDebouncer('contact', 0.002)
deb.seed(1, 0.0)
evts = deb.edge(0, 1.0)
evts += deb.edge(1, 1.005)  # pulse end + new pending back to idle
kinds = [e['kind'] for e in evts]
check("batched pulse commits transition at closing edge",
      kinds == ['transition'] and evts[0]['to'] == 0 and evts[0]['at'] == 1.0, str(evts))
evt = deb.tick(1.01)
check("return-to-idle commits on next tick", evt is not None and evt['to'] == 1)

# Pre-baseline bounce must not glitch-count (unknown resting state).
deb = app.EdgeDebouncer('x', 0.05)
out = deb.edge(0, 0.01) + deb.edge(1, 0.02)
check("edge bounce before baseline produces no glitch records",
      all(e['kind'] != 'glitch' for e in out), str(out))

print("BurstDetector (accumulated time away from idle):")
IDLE = 0  # line idles LOW, excursions go HIGH (the field rig)


def blips(b, blip_list, idle=IDLE, tick_every=0.02, tail=2.0):
    """Feed (start, width) HIGH blips as edge pairs with periodic ticks.
    Returns list of fire events."""
    hits = []
    events = []
    for s, w in blip_list:
        events.append((1, s))
        events.append((0, s + w))
    events.sort(key=lambda e: e[1])
    t = 0.0
    end = (events[-1][1] if events else 0.0) + tail
    i = 0
    while t <= end:
        while i < len(events) and events[i][1] <= t:
            level, ts = events[i]
            h = b.ingest(level, ts, idle)
            if h:
                hits.append(h)
            i += 1
        h = b.tick(t, idle)
        if h:
            hits.append(h)
        t = round(t + tick_every, 6)
    return hits


# The 20:52 field log: loading/unloading chatter = ~200 blips of ~50µs
# over 10s. Must NEVER fire (this fired falsely under edge counting).
b = app.BurstDetector(0.010, 0.5, 1.0)
ambient = [(10.0 + i * 0.05, 0.00005) for i in range(200)]
check("field ambient chatter (200x 50µs over 10s) never fires",
      blips(b, ambient) == [], "fired!")

# Even with the occasional wider blip pair seen in the log (3.6ms), stays quiet.
b = app.BurstDetector(0.010, 0.5, 1.0)
mixed = ambient[:50] + [(11.0, 0.0036), (11.2, 0.0036)] + ambient[50:]
check("ambient + two 3.6ms blips still under 10ms threshold",
      blips(b, mixed) == [])

# A real actuation carries signal mass: e.g. eight 2ms excursions in 300ms.
b = app.BurstDetector(0.010, 0.5, 1.0)
press = [(10.0 + i * 0.04, 0.002) for i in range(8)]
hits = blips(b, press)
check("actuation shower (8x 2ms in 300ms = 16ms mass) fires exactly once",
      len(hits) == 1 and hits[0]['high_ms'] >= 10.0, str(hits))

# One clean sustained pulse (post-pull-up future) fires via tick mid-pulse.
b = app.BurstDetector(0.010, 0.5, 1.0)
hits = blips(b, [(10.0, 0.5)])
check("clean 500ms pulse fires once, ~10ms after it starts",
      len(hits) == 1 and 10.0 <= hits[0]['at'] <= 10.05, str(hits))

# Long chatter storm fires once, then re-arms only after quiet.
b = app.BurstDetector(0.010, 0.5, 1.0)
storm = [(10.0 + i * 0.03, 0.003) for i in range(100)]   # 3s of dense 3ms blips
second = [(20.0 + i * 0.04, 0.002) for i in range(8)]     # separate press later
hits = blips(b, storm + second)
check("3s chatter storm fires once; separate press after quiet fires again",
      len(hits) == 2, str(len(hits)))

# Old-code equivalence: anything the 50ms sampler could reliably catch
# (tens of ms of accumulated HIGH) fires deterministically here.
b = app.BurstDetector(0.010, 0.5, 1.0)
dispatch = [(10.0 + i * 0.01, 0.004) for i in range(20)]  # 80ms mass in 200ms
check("dispatch-class signal (80ms mass) fires",
      len(blips(b, dispatch)) == 1)

check("burst threshold clamp: 5ms ok", app._clamp_burst_high_time(0.005) == 0.005)
for bad in (0.0001, 2.0, "x"):
    try:
        app._clamp_burst_high_time(bad)
        raise AssertionError(f"FAIL: burst_high_time {bad!r} accepted")
    except (TypeError, ValueError):
        passed += 1
        print(f"  ok - burst_high_time {bad!r} rejected")
check("burst window clamps low", app._clamp_burst_window(0.001) == 0.02)
check("burst window clamps high", app._clamp_burst_window(60) == 5.0)
check("burst quiet clamps", app._clamp_burst_quiet(0.01) == 0.1
      and app._clamp_burst_quiet(120) == 30.0)

print("Config clamps:")
check("hold time clamps low (0 allowed)", app._clamp_hold_time(-1) == 0.0)
check("hold time zero passes through", app._clamp_hold_time(0.0) == 0.0)
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

print("Interference (glitch) tracking:")
_, _, _, glitches = simulate([(0.5, 0.03)], 'close')
check("30ms transient reported as exactly one glitch",
      len(glitches) == 1, str(glitches))
check("glitch reports LOW level and plausible duration",
      glitches[0]['level'] == 0 and 0.02 <= glitches[0]['duration'] <= 0.06,
      f"{glitches[0]['duration']:.3f}s")
_, _, _, glitches = simulate([(0.5 + i * 0.05, 0.002) for i in range(5)], 'close')
check("burst of five 2ms spikes reported as five glitches",
      len(glitches) == 5, str(len(glitches)))
_, _, _, glitches = simulate([(0.5, 0.1)], 'close')
check("real 100ms press produces no glitch records", len(glitches) == 0)
# Bounce before the baseline is established must not count as interference.
mon = app.LineMonitor('x', 0.05)
pre_baseline = []
t = 0.0
for lvl in [1, 0, 1, 1, 1, 1, 1, 1, 1, 1]:
    evt = mon.sample(lvl, t)
    if evt and evt['kind'] == 'glitch':
        pre_baseline.append(evt)
    t += 0.01
check("bounce before baseline is not counted as a glitch", len(pre_baseline) == 0)

print("GlitchTracker aggregation:")
tracker = app.GlitchTracker('test-line')
base_events = len(app.event_log.snapshot())
now = 100.0
for i in range(6):
    tracker.record({'level': 0, 'duration': 0.005 * (i + 1), 'at': now}, now)
    now += 0.5
logged = [e for e in app.event_log.snapshot() if e['kind'] == 'glitch']
check("first glitch logged immediately, burst suppressed",
      len(logged) == 1, f"{len(logged)} log entries for 6 glitches")
tracker.flush(now)  # still inside the 10s window
check("flush inside quiet window emits nothing",
      len([e for e in app.event_log.snapshot() if e['kind'] == 'glitch']) == 1)
tracker.flush(now + 10.0)
logged = [e for e in app.event_log.snapshot() if e['kind'] == 'glitch']
check("flush after window emits one summary", len(logged) == 2)
check("summary includes pending count and total",
      "5 more" in logged[0]['message'] and "total 6" in logged[0]['message'],
      logged[0]['message'])
stats = tracker.stats()
check("stats: count=6, max duration 30ms",
      stats['count'] == 6 and abs(stats['max_duration_ms'] - 30.0) < 0.5,
      str(stats))

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

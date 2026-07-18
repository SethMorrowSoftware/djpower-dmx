# DMX Controller - DJPOWER H-IP20V Fog Machine

A Python-based DMX lighting controller for the **DJPOWER H-IP20V** fog machine (16-channel mode) with a web interface and GPIO trigger support. Uses an ENTTEC Open DMX USB adapter to send DMX512 frames from a Raspberry Pi.

## Features

- **Web Interface** - Control all 16 DMX channels via a modern dark-themed UI
- **GPIO Trigger** - Automatic fog/lighting sequences via contact closure (pin 17)
- **GPIO Safety Toggle** - Hardware safety interlock switch (pin 27) blocks operation when OFF
- **Scene Management** - 4 scenes (A-D), save/recall custom settings from the UI
- **ENTTEC Support** - Communicates over ENTTEC Open DMX USB (FTDI-based)
- **Live Control** - Real-time sliders for fog, LEDs (RGBA), strobe, dimmer, and effects
- **Emergency Blackout** - One-button kill to zero all channels instantly
- **Auto-start** - Runs as a systemd service, starts on boot
- **Health Check** - `/api/health` endpoint for monitoring and install verification
- **Auto-recovery** - Automatic ENTTEC USB reconnection and GPIO re-initialization

## Hardware Requirements

- **Raspberry Pi** (tested on Pi 5 with Raspberry Pi OS Bookworm)
- **ENTTEC Open DMX USB** adapter (FTDI-based)
- **DJPOWER H-IP20V** fog machine (or any compatible 16-channel DMX fixture)
- Standard USB and XLR DMX cables

### Wiring

```
Raspberry Pi USB --> ENTTEC Open DMX USB --> (XLR) --> DJPOWER H-IP20V

GPIO Pin 17 --> Contact closure switch --> GND
GPIO Pin 27 --> Safety toggle switch (ON = tied to GND)
```

Pi 4 physical header references:

- **GPIO17 (BCM 17)** = physical pin **11**
- **GPIO27 (BCM 27)** = physical pin **13**
- **GND** = physical pin **14** (or any GND pin)

## DMX Channel Map (16-channel mode)

| Channel | Function           | Range                                  |
|---------|--------------------|----------------------------------------|
| 1       | Fog output         | 0-9 Off, 10-255 On                    |
| 2       | *(Disabled)*       | --                                     |
| 3       | Outer LED Red      | 0-9 Off, 10-255 Dim to bright         |
| 4       | Outer LED Green    | 0-9 Off, 10-255 Dim to bright         |
| 5       | Outer LED Blue     | 0-9 Off, 10-255 Dim to bright         |
| 6       | Outer LED Amber    | 0-9 Off, 10-255 Dim to bright         |
| 7       | Inner LED Red      | 0-9 Off, 10-255 Dim to bright         |
| 8       | Inner LED Green    | 0-9 Off, 10-255 Dim to bright         |
| 9       | Inner LED Blue     | 0-9 Off, 10-255 Dim to bright         |
| 10      | Inner LED Amber    | 0-9 Off, 10-255 Dim to bright         |
| 11      | LED Mix Color 1    | 0-9 Off, 10-255 Mix color selection   |
| 12      | LED Mix Color 2    | 0-9 Off, 10-255 Mix color selection   |
| 13      | LED Auto Color     | 0-9 Off, 10-255 Slow to fast cycling  |
| 14      | Strobe             | 0-9 Off, 10-255 Slow to fast          |
| 15      | Dimmer             | 0-9 Off, 10-255 Dim to bright         |
| 16      | Safety Channel     | 0-49 Invalid, 50-200 Valid, 201-255 Invalid |

## Quick Install (Recommended)

The installer handles everything: system packages, Python venv, FTDI kernel module blacklisting, udev rules, and systemd service.

```bash
# 1. Clone the repository
git clone https://github.com/morroware/djpower-dmx.git
cd djpower-dmx

# 2. Run the installer
sudo ./install.sh
```

That's it. The controller is now running and will auto-start on every boot.

Open `http://<your-pi-ip>:5000` in a browser to access the web interface.

### What the installer does

1. Installs system packages (`python3`, `python3-venv`, `libusb-1.0`, `libgpiod-dev`, `curl`)
2. Blacklists the `ftdi_sio` kernel module so pyftdi can access the ENTTEC adapter
3. Creates udev rules so the ENTTEC adapter is accessible without root
4. Stops any existing DMX service (safe for reinstalls/upgrades)
5. Copies application files to `/opt/dmx`
6. Creates a persistent config directory at `/var/lib/dmx`
7. Creates an environment file at `/etc/dmx/dmx.env`
8. Creates a Python virtual environment and installs dependencies
9. Creates and enables a `dmx` systemd service
10. Starts the service and runs a health check

### Reinstalling / Upgrading

Just re-run the installer. It is idempotent:

```bash
cd djpower-dmx
git pull
sudo ./install.sh
```

Existing configuration (`/var/lib/dmx/config.json`) and environment settings (`/etc/dmx/dmx.env`) are preserved across reinstalls.

## Uninstall

Run the included uninstall script:

```bash
sudo ./uninstall.sh
```

This will:
- Stop and disable the systemd service
- Remove application files from `/opt/dmx`
- Remove udev rules and the ftdi_sio blacklist
- Optionally remove saved configuration and environment files (you will be prompted)

System packages (`python3`, `libusb`, etc.) are left in place.

## Manual Installation

If you prefer to set things up yourself:

```bash
# System packages
sudo apt update
sudo apt install -y python3 python3-venv python3-pip python3-dev \
    libusb-1.0-0-dev libgpiod-dev curl git

# Blacklist ftdi_sio (required for ENTTEC access)
echo "blacklist ftdi_sio" | sudo tee /etc/modprobe.d/ftdi-blacklist.conf
sudo rmmod ftdi_sio 2>/dev/null || true

# Clone
git clone https://github.com/morroware/djpower-dmx.git
cd djpower-dmx

# Python environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run manually (uses gunicorn)
./start.sh
```

### Setting up udev rules (required for non-root access)

```bash
sudo tee /etc/udev/rules.d/99-ftdi-dmx.rules <<'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="0403", ATTR{idProduct}=="6001", MODE="0666", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="0403", ATTR{idProduct}=="6014", MODE="0666", GROUP="plugdev"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger
sudo usermod -aG plugdev $USER
```

Unplug and re-plug the ENTTEC adapter after applying these rules.

### Setting up the systemd service manually

```bash
sudo tee /etc/systemd/system/dmx.service <<EOF
[Unit]
Description=DMX Controller - DJPOWER H-IP20V
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/venv/bin/gunicorn --workers 1 --threads 4 --bind 0.0.0.0:5000 app:app
Restart=on-failure
RestartSec=5
SupplementaryGroups=plugdev gpio

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable dmx
sudo systemctl start dmx
```

## Service Management

```bash
sudo systemctl status dmx        # Check if running
sudo systemctl restart dmx       # Restart after config changes
sudo systemctl stop dmx          # Stop the controller
sudo systemctl disable dmx       # Disable auto-start on boot
sudo journalctl -u dmx -f        # Follow live logs
sudo journalctl -u dmx --since "5 min ago"  # Recent logs
```

## ENTTEC Detection Troubleshooting

If the app starts but reports ENTTEC disconnected, use this sequence:

```bash
# 1) Confirm USB device is present
lsusb | rg -i '0403|ftdi|enttec'

# 2) Confirm kernel driver is not claiming it
lsmod | rg ftdi_sio

# 3) Check app health/status payload (includes ENTTEC URL + last error)
curl -s http://127.0.0.1:5000/api/health | python3 -m json.tool
curl -s http://127.0.0.1:5000/api/status | python3 -m json.tool

# 4) Check live service logs for URL attempts/open failures
sudo journalctl -u dmx -f
```

What to look for:

- `enttec_connected: false` with `enttec_last_error` mentioning permissions:
  - Re-apply udev rules and re-plug the adapter.
  - Ensure service user is in `plugdev`.
- `enttec_last_error` mentioning kernel claim / busy device:
  - `sudo rmmod ftdi_sio` and reboot if needed.
- Multiple FTDI devices connected:
  - Set `DMX_FTDI_URL` explicitly in `/etc/dmx/dmx.env` to the correct adapter.

Example explicit URL:

```bash
DMX_FTDI_URL=ftdi://0403:6001/1
```

After changes:

```bash
sudo systemctl restart dmx
```

## Usage

Access the web interface at `http://<your-pi-ip>:5000`

### Quick Controls
- **Trigger** - Activates Scene B (fog + full LEDs) for the configured duration (default 10 s), then auto-reverts to Scene A
- **Scene A-D** - Switch between predefined scenes
- **Emergency Blackout** - Zeros all DMX channels except the safety channel (kept at 100 so the fixture stays responsive)

### Live Controls
- **Fog** - Direct fog output level
- **Dimmer** - Overall LED brightness
- **Strobe** - Strobe speed
- **Outer / Inner LEDs** - Individual RGBA color control
- **LED Effects** - Mix colors and auto-color cycling
- **Safety Channel** - Must be in the 50-200 range for the fixture to operate; the API and UI both enforce this valid range

### Scene Editor
Adjust controls to your desired settings, pick a scene slot (A-D), and click **Save Current Settings to Scene** to store them for quick recall.

### GPIO Trigger
Connect a contact closure between **GPIO pin 17** and **GND**. When the contact closes, the controller fires the trigger sequence (same as the web Trigger button). The pin uses an internal pull-up, so no external resistor is needed.

### GPIO Safety Toggle
Connect a maintained toggle switch between **GPIO pin 27** and **GND**.

- **Switch ON (closed to GND)** = SAFE to operate
- **Switch OFF (open)** = LOCKED OUT (trigger + non-off scenes blocked)

The UI shows this state as **Safety SW: SAFE/LOCK** and disables trigger operations while locked.

---

## Operator's Guide

This section explains everything an operator needs to know to run the DMX controller safely and effectively.

### Understanding the Safety Channel (Channel 16)

The DJPOWER H-IP20V uses **DMX Channel 16 as a safety interlock**. This is the most important concept for safe operation:

| Safety Channel Value | Fixture Behavior |
|----------------------|------------------|
| **0 - 49** | **Invalid** — fixture ignores all DMX commands |
| **50 - 200** | **Valid** — fixture responds to DMX commands normally |
| **201 - 255** | **Invalid** — fixture ignores all DMX commands |

**Why it matters:**
- The safety channel acts as a "dead man's switch." If the controller crashes, loses USB connection, or stops sending DMX frames, the fixture will stop responding on its own because it is no longer receiving a valid safety value.
- The controller keeps Channel 16 at **100** (the default safe value) during normal operation. You should not need to change this in most cases.
- The web UI restricts the safety channel to three preset values (50, 100, 200) to prevent accidental out-of-range values.
- The API enforces the 50-200 range and will reject any request that tries to set it outside that range.
- If the safety channel somehow gets set to an invalid value (e.g., from a corrupted config file), the controller automatically corrects it to 100 on startup.

**During Emergency Blackout**, the safety channel is deliberately kept at 100 (not zeroed), so the fixture remains responsive to future commands. This means you can recover from a blackout without restarting.

### Pre-Operation Checklist

Before each use:

1. **Check fluid level** — Ensure the fog machine has adequate fog fluid. Running dry can damage the heating element.
2. **Verify ventilation** — Fog machines produce dense output. Ensure the venue has adequate ventilation and that fog will not obstruct fire exits, smoke detectors, or emergency signage.
3. **Confirm USB connection** — The ENTTEC adapter should be plugged in before starting the service. Check the web UI status bar: "ENTTEC" should show a green indicator.
4. **Confirm DMX cable** — Verify the XLR cable between the ENTTEC adapter and the fog machine is securely connected at both ends.
5. **Allow warm-up time** — The DJPOWER H-IP20V has an internal heater that needs time to reach operating temperature. The machine will not produce fog until it is ready, regardless of DMX commands.
6. **Test the trigger** — Press the Trigger button in the web UI (or close the GPIO contact) and confirm the machine responds. If using GPIO, verify the contact closure wiring is correct.
7. **Verify scene settings** — Check that Scene B (the triggered scene) has the fog and lighting values you want. Adjust via the Scene Editor if needed.
8. **Check the health endpoint** — Visit `http://<pi-ip>:5000/api/health` or check the web UI status indicators. All should show green/connected.

### Understanding the Status Indicators

The web interface displays four status indicators in the header:

| Indicator | Green (OK) | Red (Problem) |
|-----------|-----------|---------------|
| **ENTTEC** | USB adapter connected and communicating | Adapter disconnected, kernel driver conflict, or USB error |
| **DMX** | Background thread is actively sending frames at 44 Hz | DMX refresh thread has stopped |
| **Scene** | Shows the currently active scene name (A/B/C/D) | No scene active (individual channels set manually) |
| **Contact** | Shows "Open" or "Closed" reflecting GPIO pin state | Shows "Unknown" if GPIO is unavailable |

**If ENTTEC shows red:** The controller will automatically attempt to reconnect. Check that the USB adapter is plugged in, that `ftdi_sio` is not loaded (`lsmod | grep ftdi_sio`), and review logs with `sudo journalctl -u dmx -f`.

**If DMX shows red:** The DMX refresh thread has stopped, which should not happen under normal conditions. Restart the service with `sudo systemctl restart dmx`.

### Emergency Procedures

#### Software Blackout
Press the **Emergency Blackout** button in the web UI, or send:
```bash
curl -X POST http://<pi-ip>:5000/api/blackout
```
This immediately zeros all output channels (fog, LEDs, strobe, dimmer) while keeping the safety channel valid. The fixture stops producing fog and turns off all lights but remains responsive to new commands.

#### Hardware Kill
If the software is unresponsive:
1. **Disconnect the USB cable** between the ENTTEC adapter and the fog machine. Without valid DMX frames, the fixture's safety channel interlock will cause it to stop responding.
2. **Disconnect power** to the fog machine as a last resort.

#### Recovering from Blackout
After a blackout, the fixture is still in a responsive state (safety channel = 100). Simply select any scene (A, B, C, or D) to resume normal operation.

### Fog Machine Safety

**Heat hazard:** The DJPOWER H-IP20V heats fog fluid to high temperatures internally. Do not touch the nozzle or internal components during or immediately after use.

**Fog fluid:** Use only manufacturer-recommended water-based fog fluid. Never use flammable liquids. Running the machine dry can damage the heat exchanger — monitor fluid levels.

**Ventilation:** Dense fog can:
- Trigger smoke detectors and fire suppression systems — coordinate with venue management
- Reduce visibility in emergency exits — position the machine so fog does not accumulate in exit paths
- Cause respiratory irritation in enclosed spaces — ensure adequate airflow

**Duty cycle:** Although this controller can command continuous fog output, the machine has physical limits. Excessive continuous use without allowing the heater to recover may reduce fog density or trigger the machine's internal thermal protection. Scene B defaults to a 10-second duration to encourage pulsed operation.

### Trigger Timing and Behavior

When a trigger fires (via GPIO contact closure or the web Trigger button):

1. **Scene B activates immediately** — all channels snap to Scene B values
2. **A countdown timer starts** — default 10 seconds (configurable from 0.5 to 300 seconds)
3. **Scene A restores automatically** — when the timer expires, all channels return to Scene A (all off)

**Re-triggering:** If a trigger fires while Scene B is already active, the timer resets. The machine stays on Scene B for a fresh full duration from the new trigger. Triggers do not stack.

**Manual scene changes:** Selecting any scene (A, B, C, D) from the web UI cancels any active trigger timer. The manually selected scene stays active until you change it.

### GPIO Wiring and Behavior

The controller uses two GPIO inputs with internal pull-ups:

1. **Trigger input** on GPIO 17 (BCM) for momentary contact closures
2. **Safety toggle input** on GPIO 27 (BCM) for maintained lockout state

Trigger wiring (normally-open momentary switch):

```
GPIO Pin 17 ──── Contact Switch ──── GND
                (normally open)
```

- **Internal pull-up** is enabled, so pin 17 reads HIGH (1) when the contact is open
- **Trigger polarity is configurable** (web UI → *GPIO Trigger & Diagnostics*):
  - **CLOSES to GND** (default) — for normally-open wiring: the line idles HIGH and the trigger fires when it settles LOW
  - **OPENS** — for normally-closed / inverted wiring where the line idles LOW (e.g. a relay's NC contact, or an external system that holds the line at ground): the trigger fires when the line settles HIGH
- **Kernel edge detection** (gpiod v2 backend — the standard install): the kernel latches every edge on the line with nanosecond timestamps, so **no pulse can be missed regardless of width** and interference measurements are exact. The GPIO Backend tile shows `· edge` when active. On backends without edge support (gpiod v1, lgpio) the controller falls back to 5 ms polling (`· poll`), where pulses shorter than ~10 ms may not register
- **Glitch filter** (default 50 ms, tunable 0–2000 ms from the UI): a new line level must persist for the whole window before it counts, in **both** directions — in edge mode this means exactly "pulse width ≥ filter fires, shorter is logged as interference". **Set the filter below your trigger pulse width**: hand-held buttons and most relays are fine at 50 ms, but some controller outputs pulse for only ~5–10 ms — the event log's interference entries show the measured width of rejected pulses, so if your triggers are landing there, lower the filter below the logged width (e.g. 2 ms for ~5 ms pulses). A setting of 0 fires on every edge (maximum sensitivity, no noise rejection — the cooldown still prevents repeats)
- **Trigger cooldown** (default 300 ms, tunable) prevents multiple triggers from switch bounce or rapid re-closures
- **Held contacts fire once** — the line must leave the firing level and return to fire again
- **No trigger at boot** — whatever state the line is in when the service starts is latched as the baseline; a service restart can never fire the fog machine by itself
- **No external resistor needed** — the internal pull-up is sufficient

Safety toggle wiring (maintained ON/OFF switch):

```
GPIO Pin 27 ──── Safety Toggle ──── GND
```

- **Switch ON (closed to GND)** -> pin reads LOW (0) -> **SAFE TO OPERATE**
- **Switch OFF (open)** -> pin reads HIGH (1) -> **LOCKED OUT**
- While locked out, `/api/trigger` is blocked and scene applies for `scene_b`, `scene_c`, and `scene_d` return HTTP 409
- If the safety switch is turned OFF during operation, the controller cancels any active trigger timer and forces Scene A
- **The interlock can be disabled** from the web UI (with confirmation) if pin 27 is not wired to a real switch; the controller then operates as if the switch were ON. The header tile shows **BYPASS** while disabled

If the GPIO hardware is unavailable (e.g., not running on a Raspberry Pi), the controller continues to run but GPIO-triggered and GPIO-safety behavior will remain unavailable until GPIO initialization succeeds.

### GPIO Diagnostics and Troubleshooting

The **GPIO Trigger & Diagnostics** card in the web UI shows the live raw level of both lines, which GPIO backend/chip is in use, a trigger counter with the last trigger's time and source (`gpio` or `web`), an **interference counter**, and a live **event log** of every debounced line transition, trigger (fired, suppressed, or blocked), rejected interference pulse, config change, and GPIO lifecycle event. The same data is available at `GET /api/events`, and the **⬇ Download CSV** button exports the current log for analysis. Because transitions are logged with timestamps and how long the previous state lasted, you can have someone press the physical button on site and read exactly what the line did afterward — no SSH required.

**Interference tracking:** pulses that end before satisfying the glitch filter are not just discarded — each one is counted per line with its approximate width (poll-resolution upper bound) and surfaced three ways: the Interference tile (count + widest pulse seen, tooltip shows the most recent), `glitch` entries in the event log, and per-line stats in `/api/status` (`glitches.contact` / `glitches.safety`: count, `max_duration_ms`, last pulse). During sustained interference the log rate-limits to one entry per line per 10 s window (a first report, then summaries with count and min–max widths) so bursts can't drown the history. If the interference counter climbs while nobody is touching the trigger, the wiring is picking up noise — the pulse widths tell you whether the glitch filter margin is adequate or should be raised.

**Full history:** the in-app log keeps the most recent 500 events; every event is also printed to the systemd journal, so the complete record since boot is available with `journalctl -u dmx | grep -E '^\S+ \S+ \S+ gunicorn\[[0-9]+\]: \[(contact|safety|trigger|glitch|gpio|config)\]'` (or simply `journalctl -u dmx`).

**Trigger never fires, UI always shows Contact CLOSED:** the line is sitting at ground. Either the wiring is normally-closed/inverted (fix: set *Trigger fires when contact OPENS* in the UI) or the wire is shorted/on the wrong terminal. If pressing the button produces **no event at all** in the event log, the signal is not reaching GPIO 17 (BCM, physical pin 11) — check the wiring with `pinctrl get 17` (the level should flip while the contact is actuated).

**Triggers show up as interference/glitch entries instead of firing:** your trigger pulse is shorter than the glitch filter. The logged entry shows the pulse's width (exact in edge mode, e.g. `~5.2ms HIGH pulse rejected`); set the Glitch Filter below that width (2 ms works for ~5 ms pulses) and save. This is the expected signature of pulse-style controller outputs rather than held contacts. In polling mode (`· poll` on the Backend tile) the width readings are upper-bound estimates and very short pulses may be missed entirely — edge mode has neither limitation.

**Web trigger works but blocked with "Safety switch is OFF":** nothing is pulling GPIO 27 to ground. Wire a toggle between pin 27 and GND, or disable the interlock from the diagnostics card.

**Trigger config keys** (`POST /api/config`, persisted to disk): `trigger_on` (`"close"`/`"open"`), `trigger_hold_time` (seconds, clamped 0.02–2.0), `debounce_time` (seconds, clamped 0–30), `safety_switch_enabled` (bool).

**GPIO assignment keys** (also persisted, applied immediately via GPIO re-init): `contact_pin` and `safety_switch_pin` (BCM numbers 2–27, must differ — send both together), `gpio_chip` (`"auto"` to scan all chips, or a chip index / `gpiochipN` name to force one — e.g. `gpiochip4` on Pi 5 kernels older than ~6.6.47 where the header chip is not `gpiochip0`). The same settings are editable in the diagnostics card; the event log records the re-init outcome so a bad pin choice is immediately visible and reversible from the UI.

### Tests

The trigger engine has a dependency-free test suite that simulates realistic waveforms (button presses, relay pulses, EMI glitches, inverted wiring, boot states, read failures) across every poll alignment:

```bash
DMX_SKIP_AUTOINIT=1 python3 tests/test_trigger.py
```

`DMX_SKIP_AUTOINIT=1` imports the app without touching hardware.

### Network Security Considerations

The controller listens on **port 5000 on all interfaces** (0.0.0.0) with **no authentication**. Anyone on your network can access the web interface and control the fog machine.

**To secure the controller:**
- **Firewall** — Restrict access to port 5000 to trusted IPs:
  ```bash
  sudo ufw allow from 192.168.1.0/24 to any port 5000  # allow local network only
  sudo ufw deny 5000/tcp                                # block all other access
  ```
- **Isolated network** — Run the Raspberry Pi on a dedicated network or VLAN that is not accessible from untrusted devices.
- **Physical security** — If the Raspberry Pi is in a public area, ensure the GPIO trigger wiring cannot be tampered with.

If the controller is exposed to an untrusted network, anyone can activate the fog machine, change scenes, or trigger a blackout. Always restrict access at the network level.

### Auto-Recovery Behavior

The controller is designed to recover automatically from common hardware issues:

| Failure | Recovery |
|---------|----------|
| **ENTTEC USB disconnected** | DMX thread detects errors within 3 frames, attempts reconnection with exponential backoff (1s to 10s). Plug the adapter back in and it will reconnect automatically. |
| **GPIO read error** | GPIO monitor retries 3 times, then fully re-initializes the GPIO subsystem. No operator action needed. |
| **Service crash** | systemd restarts the service automatically after 5 seconds (`Restart=on-failure`). |
| **Config file corrupted** | Falls back to built-in default scenes. Safety channel values are force-corrected to 100. |

Check `sudo journalctl -u dmx -f` to see live recovery messages.

### Scene Configuration Tips

- **Scene A** is the "off" scene — it is applied at startup and after every trigger timer expires. Keep all output channels at 0 (with safety channel at 100).
- **Scene B** is the "triggered" scene — this is what fires on GPIO contact closure. Configure it with the fog level and LED colors you want for your triggered effect.
- **Scenes C and D** are custom scenes for manual use. Use them for ambient lighting, testing, or alternate effects.
- Scene changes are **atomic** — all channels update in a single operation, so you will never see a partial scene.
- Scene edits made in the web UI are **saved to disk immediately** and persist across reboots.

---

## Configuration

Edit scene presets and timing in `app.py` (or `/opt/dmx/app.py` if installed via the installer) under the `Config` class. The controller persists scene updates to `/var/lib/dmx/config.json` by default.

- `CONTACT_PIN` - GPIO pin number for trigger input (default: 17)
- `SAFETY_SWITCH_PIN` - GPIO pin number for maintained safety lockout input (default: 27)
- `GPIO_CHIP` - Optional GPIO chip override (e.g., `0`, `gpiochip0`, `/dev/gpiochip0`). Auto-detects if unset.
- `SCENE_B_DURATION` - How long the triggered scene lasts in seconds (default: 10)
- `SCENES` - Channel values for each of the four scenes
- `DMX_FTDI_URL` - Environment variable to select a specific FTDI device (default: `ftdi://0403:6001/1`)

After editing, restart the service:

```bash
sudo systemctl restart dmx
```

## API Endpoints

| Method   | Endpoint            | Description                                |
|----------|---------------------|--------------------------------------------|
| GET      | `/`                 | Web interface                              |
| GET      | `/api/health`       | Health check (200 = ok, 503 = degraded)    |
| GET      | `/api/status`       | System status and channel values           |
| POST     | `/api/trigger`      | Fire the trigger sequence                  |
| POST     | `/api/scene/<name>` | Apply a scene (scene_a through scene_d)    |
| GET      | `/api/scenes`       | List all scenes and their channels         |
| POST     | `/api/channel`      | Set a single channel `{channel, value}`    |
| POST     | `/api/blackout`     | Emergency blackout (safety channel kept valid) |
| GET/POST | `/api/config`       | Read or update scene config and duration   |

Additional API state fields now exposed:

- `/api/status` includes `safety_switch_state` (`on`, `off`, `unknown`) and `safe_to_operate` (boolean)
- `/api/health` includes `safe_to_operate` (boolean)
- `/api/config` includes `safety_switch_pin`

All endpoints are open (no authentication). Restrict access via network-level controls (firewall, VPN) if needed.

## File Layout

| Path | Description |
|------|-------------|
| `app.py` | Flask application - DMX control, GPIO, API routes |
| `index.html` | Web UI (dark-themed, responsive) |
| `install.sh` | Automated installer for Raspberry Pi |
| `uninstall.sh` | Clean uninstaller |
| `start.sh` | Manual startup script (loads env, activates venv) |
| `requirements.txt` | Python dependencies |
| `/opt/dmx/` | Installed application (created by installer) |
| `/var/lib/dmx/config.json` | Persisted scene configuration |
| `/etc/dmx/dmx.env` | Environment variables (config path, FTDI URL) |
| `/etc/systemd/system/dmx.service` | Systemd service unit |
| `/etc/udev/rules.d/99-ftdi-dmx.rules` | FTDI USB permissions |
| `/etc/modprobe.d/ftdi-blacklist.conf` | Blocks ftdi_sio kernel driver |

## Troubleshooting

### ENTTEC adapter not detected
```bash
# Check USB devices
lsusb | grep -i ftdi

# Verify ftdi_sio is NOT loaded (it must be blacklisted)
lsmod | grep ftdi_sio
# If it shows output, unload it:
sudo rmmod ftdi_sio

# Check udev rules are loaded
sudo udevadm test /sys/bus/usb/devices/*  2>&1 | grep -i ftdi

# Verify permissions
ls -la /dev/bus/usb/*/*
```

### Service won't start
```bash
# Check logs for error details
sudo journalctl -u dmx -n 50 --no-pager

# Run the health check manually
curl -s http://localhost:5000/api/health | python3 -m json.tool

# Try running manually to see output
cd /opt/dmx
sudo -u $USER venv/bin/python3 app.py
```

### GPIO not working
```bash
# Verify gpiod is installed
dpkg -l | grep gpiod

# Check GPIO chip is accessible
gpioinfo gpiochip4 2>/dev/null || gpioinfo gpiochip0

# Test pins manually (trigger + safety)
gpioget gpiochip4 17
gpioget gpiochip4 27
```

If your GPIO chip is not `gpiochip4`, update `GPIO_CHIP` in `app.py` (or set it to `gpiochip0`), or use `gpioget gpiochip0 17` and `gpioget gpiochip0 27` to verify both pins.

### Web interface not loading
```bash
# Confirm the service is running
sudo systemctl status dmx

# Check if port 5000 is listening
ss -tlnp | grep 5000

# Check firewall (if enabled)
sudo ufw status
sudo ufw allow 5000/tcp   # if needed
```

### ftdi_sio kernel module keeps loading
```bash
# Verify the blacklist file exists
cat /etc/modprobe.d/ftdi-blacklist.conf

# Should contain:
# blacklist ftdi_sio

# If it doesn't exist, create it:
echo "blacklist ftdi_sio" | sudo tee /etc/modprobe.d/ftdi-blacklist.conf

# Unload immediately:
sudo rmmod ftdi_sio

# Restart the service:
sudo systemctl restart dmx
```

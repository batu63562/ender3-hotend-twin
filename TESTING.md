# Testing the twin on your printer

Thank you for trying this. The goal of the test (roadmap v0.4) is to find out whether the twin works on a printer
it has never seen, with **no code change**: only a profile file and one calibration print. Nothing is added to
your printer; the twin only reads what Klipper already reports. Allow about one hour of setup, one hour of
calibration print (no filament used), and then your normal printing for a week or two.

Before you start, please open a ["Test on my printer" issue](../../issues/new?template=test-on-my-printer.md) so
we can talk there.

## 1. What you need

- Klipper + Moonraker on a Raspberry Pi (or similar Linux host) with **Python 3.11 or newer**
  (Raspberry Pi OS / Debian 12 "Bookworm" has it: `python3 --version`).
- In `printer.cfg`: the hotend heater is `[extruder]`, the part-cooling fan is `[fan]` (not `fan_generic`), the bed
  heater is `[heater_bed]`. Other setups are not supported yet; tell us in the issue.
- A thermistor that reads steadily (no jumps of more than ~1 °C at a constant temperature).
- About 200 MB free on the Pi for logs.

## 2. Install on the Pi

```bash
cd ~
git clone https://github.com/batu63562/ender3-hotend-twin.git ender3-twin
cd ender3-twin
bash pi/install.sh
```

The script creates a Python virtual environment, installs the dependencies, writes `config.toml` (Moonraker at
`127.0.0.1:7125`) and starts the `ender3-twin` systemd service. Check that it runs:

```bash
journalctl -u ender3-twin -f
```

You should see one line per message with `T1_real`, `T1_model` and `residual`. The phone page is at
`http://<pi address>:8090`.

**Start in shadow mode.** By default the twin pauses a print when it is sure a fault is happening. On a new printer,
let it only record what it would do for the first two weeks: in `realtime_websocket.py` set

```python
PAUSE_MODE = "golge"
```

and restart with `sudo systemctl restart ender3-twin`. ("golge" is Turkish for shadow; it logs "would pause" to
`ariza_raporlari.txt` instead of pausing.)

## 3. Describe your printer: `profile.toml`

```bash
cp profile.example.toml profile.toml
nano profile.toml
```

Set what you know: room temperature, heater cartridge power (printed on the cartridge or in your printer's
documentation; 40 W is common on stock printers, 50–60 W on upgraded hotends), bed heater power. Leave the part-fan
numbers as they are if you do not know them: the calibration print measures them. Restart the service afterwards.

## 4. Calibration print (about one hour, no filament)

The calibration G-code heats, holds, cools and steps the part fan with the nozzle 50 mm above the bed centre. It does
not extrude. Generate it for your printer on any computer with Python (or on the Pi):

```bash
python3 yazici/calibration_print.py --temp 245 --center 117,117 --out calibration_245.gcode
```

- `--temp`: the temperature you print most at (e.g. 210 for PLA, 245 for PETG). It must be at least 5 °C below your
  `max_temp` in `printer.cfg`.
- `--center`: the X,Y of your bed centre in mm (117,117 is an Ender 3).

Upload `calibration_245.gcode` in Mainsail/Fluidd and print it like any file, with **the twin running**. Keep the
printer away from drafts (open windows, fans, air conditioning) during the hour. If it is interrupted, simply run it
again.

## 5. Fit the calibration

When the job has finished, the twin has archived its log as `realtime_log_ws_onceki_<date>_<time>.csv` (the newest
one). On the Pi:

```bash
cd ~/ender3-twin
venv/bin/pip install "scipy>=1.11"
ls -t realtime_log_ws_onceki_*.csv | head -1      # the calibration print's log
venv/bin/python calibrate.py <that file> --staged --write calibration.json
```

It prints two stages: the losses and part-fan curve from the steady holds, then the heat capacities and internal
resistances from the whole run. A good calibration ends with a heater power error well below 0.5 W in stage 1 and
a temperature error around 2 °C in stage 2. Copy the whole output into the issue.

Then start the twin with the new numbers and a fresh learned state (the calibration defines the reference):

```bash
sudo systemctl stop ender3-twin
mv twin_state.json twin_state_before_calibration.json 2>/dev/null
sudo systemctl start ender3-twin
```

The start-up lines in `journalctl -u ender3-twin -f` should show `calibration.json` among the printer numbers.

## 6. Print normally

Print whatever you normally print, for a week or two. **Start the twin before the print starts** (it is a service,
so it always is), so that it can plan the print from the G-code.

When something goes wrong in a print (clog, filament ran out, a blob on the nozzle, a layer shift, the sock fell
off, a print came loose), please note **the time** and what you saw, ideally with a photo. Those notes are what
turns a log into a labelled test case. False alarms are just as valuable: if the twin reported a fault and the print
was fine, say so.

## 7. Send the data back

Everything is in the twin's folder on the Pi. Please zip these:

```bash
cd ~/ender3-twin
zip -r twin_test_$(date +%Y%m%d).zip realtime_log_ws_onceki_*.csv tahminler/ profile.toml calibration.json \
    twin_gecmis.csv ariza_raporlari.txt bakim_izleme.csv
```

The logs contain only printer signals (temperatures, heater power, fan, positions, file names of your prints). If a
file name is private, rename the matching CSV or leave it out. Share the zip through a file-sharing link in the issue
(GitHub attachments are limited to 25 MB).

## What to expect

On the author's Ender 3 the twin catches heat-flow faults (filament not flowing, a blob around the nozzle, the
silicone sock missing, a blocked fan duct, a layer shift that leaves the nozzle printing in the air) within about
half a minute, with about one false alarm per ten print hours. It does not see purely mechanical problems or small
print-quality defects; see [Limitations](README.md#limitations). How well this carries over to other hotends is
exactly what the test is for, so a result of "it did not work on my printer, and here is the log" is useful too.

Questions: ask in the issue. Thank you!

#!/usr/bin/env python3
"""STR thermal glitch root cause analysis — all-in-one.

1. Pushes STR loop script to device via ADB
2. Disconnects ADB, runs STR loop via UART
3. Captures [VTZ:HW], [VTZ:EMA], [VTZ:RESULT] from UART in real-time
4. Classifies each glitch as RAW_STALE / EMA_CORRUPT / BOTH
5. Logs raw UART context + analysis per glitch round
6. Appends summary with per-sensor breakdown on exit

Usage:
    python3 str_vtz_rootcause.py                    # auto-detect
    python3 str_vtz_rootcause.py -p /dev/ttyUSB0    # explicit UART
    python3 str_vtz_rootcause.py --analyze uart.log  # offline only
"""

import argparse
import glob
import os
import platform
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime

try:
    import serial
except ImportError:
    sys.exit("pyserial required: pip install pyserial")

BAUD_DEFAULT = 921600
RAW_JUMP_THRESH = 2000

HW_RE = re.compile(
    r'\[VTZ:HW\]\s+(\S+)\s+(\S+)\s+raw=(-?\d+)\s+last=(-?\d+)\s+off=(-?\d+)'
    r'\s+a=(\d+)\s+o=(-?\d+)\s+gap=(-?\d+)ms')
EMA_RE = re.compile(
    r'\[VTZ:EMA\]\s+(\S+)\s+(\S+)\s+t=(-?\d+)\s+off_old=(-?\d+)\s+off_new=(-?\d+)'
    r'\s+init=(\d)\s+w=(-?\d+)\s+contrib=(-?\d+)\s+sum=(-?\d+)')

RESULT_RE = re.compile(
    r'\[VTZ:RESULT\]\s+(\S+)\s+tempv=(-?\d+)\s+last=(-?\d+)')
SUSPEND_RE = re.compile(r'PM:\s+suspend\s+(entry|exit)')
RESUME_VTZ_RE = re.compile(r'\[VTZ\]\s+post-resume')

# Zone name ↔ zone number mapping (Persimmon)
ZONE_NUM = {
    'cover_front_virtual': 'z22',
    'cover_left_virtual': 'z23',
    'cover_right_virtual': 'z24',
}


# ── UART / ADB helpers ──

def enumerate_uart_ports():
    if platform.system() == 'Darwin':
        return sorted(glob.glob('/dev/tty.usbserial-*'))
    return sorted(glob.glob('/dev/ttyUSB*'))


def get_adb_devices():
    r = subprocess.run(['adb', 'devices'], capture_output=True, text=True)
    devs = []
    for line in r.stdout.strip().split('\n')[1:]:
        parts = line.split('\t')
        if len(parts) == 2 and parts[1].strip() == 'device':
            devs.append(parts[0].strip())
    return devs


def read_serial_from_uart(port, baud, known):
    try:
        ser = serial.Serial(port, baud, timeout=1)
        ser.reset_input_buffer()
        for _ in range(3):
            ser.write(b'\nroot\n')
            time.sleep(1.5)
            ser.write(b'cat /sys/firmware/devicetree/base/serial-number\n')
            time.sleep(2)
            out = ser.read(ser.in_waiting or 4096).decode('utf-8', errors='ignore').replace('\x00', '')
            for sn in known:
                if sn in out:
                    ser.close()
                    return sn
        ser.close()
    except (serial.SerialException, OSError):
        pass
    return None


def autodetect(baud):
    adb_devs = get_adb_devices()
    uart_ports = enumerate_uart_ports()
    if not adb_devs:
        print("No ADB devices. Waiting 15s...")
        for _ in range(15):
            time.sleep(1)
            adb_devs = get_adb_devices()
            if adb_devs:
                break
    if not adb_devs or not uart_ports:
        return None, None
    adb_dt = {}
    for sn in adb_devs:
        r = subprocess.run(['adb', '-s', sn, 'shell',
                            'cat /sys/firmware/devicetree/base/serial-number'],
                           capture_output=True, text=True)
        dt = r.stdout.strip().replace('\x00', '')
        if dt:
            adb_dt[dt] = sn
    for port in uart_ports:
        dt = read_serial_from_uart(port, baud, list(adb_dt.keys()))
        if dt and dt in adb_dt:
            return adb_dt[dt], port
    return adb_devs[0], uart_ports[0] if uart_ports else None


def push_str_script(adb_sn):
    script = (
        '#!/bin/sh\n'
        'N=0\n'
        'while true; do '
        'N=$((N+1)); '
        'PRE22=$(cat /sys/class/thermal/thermal_zone22/temp); '
        'PRE23=$(cat /sys/class/thermal/thermal_zone23/temp); '
        'PRE24=$(cat /sys/class/thermal/thermal_zone24/temp); '
        'echo mem > /sys/power/state; '
        'POST22=$(cat /sys/class/thermal/thermal_zone22/temp); '
        'POST23=$(cat /sys/class/thermal/thermal_zone23/temp); '
        'POST24=$(cat /sys/class/thermal/thermal_zone24/temp); '
        'D22=$((POST22 - PRE22)); D23=$((POST23 - PRE23)); D24=$((POST24 - PRE24)); '
        'echo "[STR:$N] z22 pre=$PRE22 post=$POST22 d=$D22  z23 pre=$PRE23 post=$POST23 d=$D23  z24 pre=$PRE24 post=$POST24 d=$D24"; '
        'sleep 1; '
        'done'
    )
    print(f"[{adb_sn}] Pushing STR loop script...")
    subprocess.run(['adb', '-s', adb_sn, 'shell',
                    f"echo '{script}' > /data/str_loop.sh && chmod +x /data/str_loop.sh"],
                   capture_output=True, text=True)
    print(f"[{adb_sn}] Disconnecting ADB (required for STR)...")
    subprocess.run(['adb', '-s', adb_sn, 'disconnect'], capture_output=True)
    subprocess.run(['adb', 'kill-server'], capture_output=True)


def uart_login_and_start(ser):
    ser.write(b'\n')
    time.sleep(0.5)
    ser.write(b'root\n')
    time.sleep(2)
    ser.read(ser.in_waiting or 4096)
    ser.write(b'/data/str_loop.sh\n')


# ── Analyzer ──

class SensorSnapshot:
    __slots__ = ('zone', 'sensor', 'raw', 'last', 'off_before', 'alpha', 'offset',
                 'gap_ms', 'ema_t', 'off_old', 'off_new', 'init', 'weight',
                 'contrib', 'sum_so_far')

    def __init__(self):
        for s in self.__slots__:
            setattr(self, s, None)

    @property
    def raw_jump(self):
        if self.raw is not None and self.last is not None:
            return abs(self.raw - self.last)
        return 0

    @property
    def raw_bad(self):
        return self.raw_jump > RAW_JUMP_THRESH

    @property
    def off_bad(self):
        if self.off_old is None or self.raw is None or self.offset is None:
            return False
        expected = self.raw - self.offset
        return abs(self.off_old - expected) > 50000


class GlitchEvent:
    def __init__(self, str_cycle, zone, tempv, last_tempv, sensors, post_resume, raw_context):
        self.str_cycle = str_cycle
        self.zone = zone
        self.tempv = tempv
        self.last_tempv = last_tempv
        self.sensors = sensors
        self.post_resume = post_resume
        self.raw_context = raw_context
        self.raw_bad_sensors = [s for s in sensors if s.raw_bad]
        self.off_bad_sensors = [s for s in sensors if s.off_bad]
        if self.raw_bad_sensors and self.off_bad_sensors:
            self.cause = 'BOTH'
        elif self.raw_bad_sensors:
            self.cause = 'RAW_STALE'
        elif self.off_bad_sensors:
            self.cause = 'EMA_CORRUPT'
        else:
            self.cause = 'UNKNOWN'


class Analyzer:
    def __init__(self, log_path):
        self.log_path = log_path
        self.log_f = open(log_path, 'w')
        self.str_count = 0
        self.resume_count = 0
        self.post_resume = False
        self.resume_reads = 0
        self.glitches = []
        self.cur_hw = {}
        self.cur_zone_sensors = defaultdict(list)
        self.cur_vtz_lines = defaultdict(list)

    def _log(self, msg):
        self.log_f.write(msg + '\n')
        self.log_f.flush()
        print(msg)

    def feed_line(self, line):
        """Feed raw UART line — buffer VTZ lines, then parse."""
        # Collect all VTZ lines by zone for raw dump on glitch
        if '[VTZ:' in line:
            m = re.search(r'\[VTZ:\w+\]\s+(\S+)', line)
            if m:
                self.cur_vtz_lines[m.group(1)].append(line)
        self._parse(line)

    def _parse(self, line):
        m = SUSPEND_RE.search(line)
        if m:
            if m.group(1) == 'entry':
                self.str_count += 1
            elif m.group(1) == 'exit':
                self.post_resume = True
                self.resume_reads = 0
            return

        m = RESUME_VTZ_RE.search(line)
        if m:
            self.resume_count += 1
            self.post_resume = True
            self.resume_reads = 0
            return

        m = HW_RE.search(line)
        if m:
            zone, sensor = m.group(1), m.group(2)
            snap = SensorSnapshot()
            snap.zone = zone
            snap.sensor = sensor
            snap.raw = int(m.group(3))
            snap.last = int(m.group(4))
            snap.off_before = int(m.group(5))
            snap.alpha = int(m.group(6))
            snap.offset = int(m.group(7))
            snap.gap_ms = int(m.group(8))
            self.cur_hw[(zone, sensor)] = snap
            if self.post_resume:
                self.resume_reads += 1
                if self.resume_reads > 80:
                    self.post_resume = False
            return

        m = EMA_RE.search(line)
        if m:
            zone, sensor = m.group(1), m.group(2)
            snap = self.cur_hw.get((zone, sensor))
            if not snap:
                snap = SensorSnapshot()
                snap.zone = zone
                snap.sensor = sensor
            snap.ema_t = int(m.group(3))
            snap.off_old = int(m.group(4))
            snap.off_new = int(m.group(5))
            snap.init = int(m.group(6))
            snap.weight = int(m.group(7))
            snap.contrib = int(m.group(8))
            snap.sum_so_far = int(m.group(9))
            self.cur_zone_sensors[zone].append(snap)
            return

        m = RESULT_RE.search(line)
        if m:
            zone = m.group(1)
            tempv = int(m.group(2))
            last_tempv = int(m.group(3))
            sensors = self.cur_zone_sensors.pop(zone, [])
            vtz_lines = self.cur_vtz_lines.pop(zone, [])
            has_raw_bad = any(s.raw_bad for s in sensors)
            has_off_bad = any(s.off_bad for s in sensors)
            tempv_jump = abs(tempv - last_tempv) if last_tempv else 0

            if has_raw_bad or has_off_bad or tempv_jump > RAW_JUMP_THRESH:
                g = GlitchEvent(self.str_count, zone, tempv, last_tempv,
                                sensors, self.post_resume, list(vtz_lines))
                self.glitches.append(g)

                # Log with round delimiter + raw VTZ dump + analysis
                ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self._log(f"\n==== round {len(self.glitches)} STR#{self.str_count} [{ts}] ====")
                self._log(f"zone={zone}({ZONE_NUM.get(zone,'?')}) tempv={tempv} last={last_tempv} "
                          f"jump={tempv_jump} cause={g.cause} resume={self.post_resume}")
                self._log(f"--- raw VTZ dump (printk) ---")
                for vl in vtz_lines:
                    self._log(f"  {vl}")
                self._log(f"--- analysis ---")
                for s in sensors:
                    flag = []
                    if s.raw_bad:
                        flag.append('RAW_BAD')
                    if s.off_bad:
                        flag.append('OFF_BAD')
                    tag = '+'.join(flag) if flag else 'ok'
                    self._log(f"  {s.sensor:<14} raw={s.raw:>6} last={s.last:>6} "
                              f"jump={s.raw_jump:>5} off_old={s.off_old} "
                              f"off_new={s.off_new} init={s.init} "
                              f"a={s.alpha} o={s.offset} w={s.weight} "
                              f"contrib={s.contrib} [{tag}]")

            else:
                # No glitch — clear buffered VTZ lines
                self.cur_vtz_lines.pop(zone, None)

            for s in sensors:
                self.cur_hw.pop((zone, s.sensor), None)
            return

    def summary(self):
        total = len(self.glitches)
        by_cause = defaultdict(list)
        for g in self.glitches:
            by_cause[g.cause].append(g)

        sensor_raw_bad = defaultdict(int)
        sensor_off_bad = defaultdict(int)
        for g in self.glitches:
            for s in g.sensors:
                if s.raw_bad:
                    sensor_raw_bad[s.sensor] += 1
                if s.off_bad:
                    sensor_off_bad[s.sensor] += 1

        raw_involved = len(by_cause['RAW_STALE']) + len(by_cause['BOTH'])
        ema_involved = len(by_cause['EMA_CORRUPT']) + len(by_cause['BOTH'])

        lines = [
            f"\n{'='*75}",
            f"ROOT CAUSE ANALYSIS SUMMARY",
            f"{'='*75}",
            f"STR cycles:     {self.str_count}",
            f"Resume events:  {self.resume_count}",
            f"Total glitches: {total}",
            f"",
            f"By cause:",
            f"  RAW_STALE   (SCP stale cache only):  {len(by_cause['RAW_STALE'])}",
            f"  EMA_CORRUPT (EMA corruption only):  {len(by_cause['EMA_CORRUPT'])}",
            f"  BOTH:                         {len(by_cause['BOTH'])}",
            f"  UNKNOWN:                      {len(by_cause['UNKNOWN'])}",
        ]
        if total:
            lines += [
                f"",
                f"SCP stale cache involved: {raw_involved}/{total} ({100*raw_involved/total:.1f}%)",
                f"EMA state corruption involved: {ema_involved}/{total} ({100*ema_involved/total:.1f}%)",
                f"",
                f"Per-sensor breakdown (only sensors with issues):",
                f"  {'Sensor':<14} {'Raw bad':>8} {'Off bad':>8}",
                f"  {'─'*35}",
            ]
            all_sensors = set(list(sensor_raw_bad.keys()) + list(sensor_off_bad.keys()))
            for sensor in sorted(all_sensors):
                rb = sensor_raw_bad[sensor]
                ob = sensor_off_bad[sensor]
                lines.append(f"  {sensor:<14} {rb:>8} {ob:>8}")

        lines.append(f"\n{'='*75}")
        if total == 0:
            lines.append("CONCLUSION: No glitches detected.")
        elif raw_involved > 0 and ema_involved > 0:
            lines.append(f"CONCLUSION: Both SCP stale cache and EMA corruption contribute.")
            lines.append(f"  SCP stale cache: {100*raw_involved/total:.1f}%")
            lines.append(f"  EMA state corruption:   {100*ema_involved/total:.1f}%")
        elif ema_involved > 0:
            lines.append("CONCLUSION: EMA state corruption is the sole contributor.")
            lines.append("  → SCP stale cache fix may not be needed.")
        elif raw_involved > 0:
            lines.append("CONCLUSION: SCP stale cache is the sole contributor.")
        lines.append(f"{'='*75}")

        report = '\n'.join(lines)
        # Append summary to end of log file
        self.log_f.write(report + '\n')
        self.log_f.flush()
        print(report)
        print(f"\nLog file: {self.log_path}")

    def close(self):
        self.log_f.close()


# ── Main ──

def run_live(adb_sn, port, baud, log_dir):
    if adb_sn:
        push_str_script(adb_sn)
    else:
        print("No ADB device — assuming STR script already on device.")

    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    analysis_log = os.path.join(log_dir, f'vtz_analysis_{ts}.log')
    analyzer = Analyzer(analysis_log)

    print(f"\nUART: {port} @ {baud}")
    print(f"Log: {analysis_log}")
    print(f"Both SCP-cache fix and EMA-reset fix should be DISABLED in this build.")
    print(f"Press Ctrl-C to stop and see summary.\n")

    ser = serial.Serial(port, baud, timeout=1)
    uart_login_and_start(ser)

    try:
        while True:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line:
                continue
            print(f"[UART] {line}")
            analyzer.feed_line(line)
    except KeyboardInterrupt:
        print("\n\nStopping STR loop on device...")
        ser.write(b'\x03')
        time.sleep(1)
    finally:
        ser.close()
        analyzer.summary()
        analyzer.close()


def main():
    parser = argparse.ArgumentParser(description='STR VTZ glitch root cause analysis')
    parser.add_argument('-p', '--port', help='UART port (auto-detect if omitted)')
    parser.add_argument('-d', '--device', help='ADB serial (auto-detect if omitted)')
    parser.add_argument('-b', '--baud', type=int, default=BAUD_DEFAULT)
    parser.add_argument('-o', '--output', default=os.path.join(os.getcwd(), 'vtz_rootcause'))
    args = parser.parse_args()

    adb_sn = args.device
    port = args.port
    if not port:
        print("Auto-detecting UART + ADB...")
        adb_sn, port = autodetect(args.baud)
        if not port:
            sys.exit("No UART found. Use -p /dev/ttyUSBx")
        print(f"ADB: {adb_sn or 'none'}")
        print(f"UART: {port}")

    run_live(adb_sn, port, args.baud, args.output)


if __name__ == '__main__':
    main()

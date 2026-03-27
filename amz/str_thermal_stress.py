#!/usr/bin/env python3
"""STR thermal stress test: suspend-to-RAM loop via UART with thermal delta monitoring."""

import argparse
import subprocess
import shutil
import threading
import time
import re
import sys
import os
import glob
import platform
from datetime import datetime


def preflight_check():
    """Check OS, required commands, and Python packages before running."""
    os_name = platform.system()
    print(f"OS: {os_name} ({platform.platform()})")
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")

    errors = []

    # Check required commands
    for cmd in ['adb']:
        if not shutil.which(cmd):
            errors.append(f"  - '{cmd}' not found. Install Android SDK platform-tools.")

    # Check pyserial (not 'serial')
    try:
        import serial
        serial.Serial  # will fail if wrong 'serial' package
    except (ImportError, AttributeError):
        venv_hint = (
            "  - 'pyserial' package missing or shadowed by 'serial'.\n"
            "    Fix (recommended - use venv):\n"
            "      python3 -m venv .venv\n"
            "      source .venv/bin/activate\n"
            "      pip install pyserial\n"
            "    Fix (system-wide):\n"
            "      pip uninstall serial pyserial && pip install pyserial"
        )
        errors.append(venv_hint)

    # Linux-specific: check dialout group for UART access
    if os_name == 'Linux':
        import grp
        try:
            dialout = grp.getgrnam('dialout')
            if os.getlogin() not in dialout.gr_mem and os.geteuid() != 0:
                errors.append(
                    f"  - User '{os.getlogin()}' not in 'dialout' group (needed for UART).\n"
                    f"    Fix: sudo usermod -aG dialout {os.getlogin()} && logout/login"
                )
        except (KeyError, OSError):
            pass

    if errors:
        print("\n⚠️  Preflight check FAILED:\n")
        print("\n".join(errors))
        sys.exit(1)

    print("Preflight: OK\n")


import serial

SLACK_ENABLED = False
THERMAL_DELTA_THRESHOLD = 2000

def build_str_loop_cmd(no_sleep=False):
    suspend = 'sleep 2' if no_sleep else 'echo mem > /sys/power/state'
    return (
        'while true; do '
        'PRE=$(cat /sys/class/thermal/thermal_zone22/temp); '
        f'{suspend}; '
        'POST=$(cat /sys/class/thermal/thermal_zone22/temp); '
        'DELTA=$((POST - PRE)); '
        'echo "pre=$PRE  post=$POST  delta=$DELTA"; '
        'sleep 1; '
        'done\n'
    )


def log_match(log_file, match_info):
    with open(log_file, 'a') as f:
        f.write(match_info + '\n')
    print(f"[LOGGED -> {os.path.basename(log_file)}] {match_info}")


def post_to_slack(user, message):
    if not SLACK_ENABLED:
        print(f"[SLACK DISABLED] Would notify {user}: {message}")


def load_patterns(pattern_file):
    with open(pattern_file, 'r') as f:
        return [line.strip() for line in f if line.strip()]


def get_adb_devices():
    result = subprocess.run(['adb', 'devices'], capture_output=True, text=True)
    devices = []
    for line in result.stdout.strip().split('\n')[1:]:
        parts = line.split('\t')
        if len(parts) == 2 and parts[1].strip() == 'device':
            devices.append(parts[0].strip())
    return devices


def enumerate_uart_ports():
    if platform.system() == 'Darwin':
        return sorted(glob.glob('/dev/tty.usbserial-*'))
    else:
        return sorted(glob.glob('/dev/ttyUSB*'))


def read_serial_from_uart(port, baud, known_serials):
    try:
        ser = serial.Serial(port, baud, timeout=1)
        ser.reset_input_buffer()
        for attempt in range(3):
            ser.write(b'\n')
            time.sleep(0.5)
            ser.write(b'root\n')
            time.sleep(1)
            ser.read(ser.in_waiting or 1)
            ser.write(b'cat /sys/firmware/devicetree/base/serial-number\n')
            time.sleep(2)
            output = ser.read(ser.in_waiting or 4096).decode('utf-8', errors='ignore')
            output = output.replace('\x00', ' ')
            for sn in known_serials:
                if sn in output:
                    ser.close()
                    return sn
        ser.close()
        return None
    except (serial.SerialException, OSError) as e:
        print(f"[WARN] Cannot read serial from {port}: {e}")
        return None


def match_uart_via_marker(adb_devices, uart_ports, baud):
    mapping = {}
    matched_ports = set()
    for adb_sn in adb_devices:
        marker = f"UARTPROBE_{safe_filename(adb_sn)}_{int(time.time())}"
        ports_ser = {}
        for port in uart_ports:
            if port in matched_ports:
                continue
            try:
                ser = serial.Serial(port, baud, timeout=1)
                ser.reset_input_buffer()
                ports_ser[port] = ser
            except (serial.SerialException, OSError):
                continue
        subprocess.run(['adb', '-s', adb_sn, 'shell',
                        f'echo {marker} > /dev/console'],
                       capture_output=True, text=True)
        time.sleep(2)
        for port, ser in ports_ser.items():
            output = ser.read(ser.in_waiting or 4096).decode('utf-8', errors='ignore')
            ser.close()
            if marker in output:
                mapping[adb_sn] = port
                matched_ports.add(port)
                print(f"  {port} -> {adb_sn} (via marker)")
                break
        else:
            for ser in ports_ser.values():
                try:
                    ser.close()
                except Exception:
                    pass
    return mapping


def adb_device_online(sn):
    result = subprocess.run(['adb', '-s', sn, 'get-state'],
                            capture_output=True, text=True)
    return result.stdout.strip() == 'device'


def match_uart_to_adb(baud):
    adb_devices = get_adb_devices()
    if not adb_devices:
        print("No ADB devices found, waiting up to 30s...")
        for i in range(30):
            time.sleep(1)
            adb_devices = get_adb_devices()
            if adb_devices:
                break
    uart_ports = enumerate_uart_ports()
    if not adb_devices or not uart_ports:
        return {}

    mapping = {}
    print(f"ADB devices: {adb_devices}")
    print(f"UART ports:  {uart_ports}")

    for sn in adb_devices:
        print(f"Waiting for {sn} to be online...")
        while not adb_device_online(sn):
            time.sleep(1)
        print(f"  {sn} online")

    adb_dt_map = {}
    for sn in adb_devices:
        result = subprocess.run(['adb', '-s', sn, 'shell',
            'cat', '/sys/firmware/devicetree/base/serial-number'],
            capture_output=True, text=True)
        dt_sn = result.stdout.strip().replace('\x00', '')
        if dt_sn:
            adb_dt_map[dt_sn] = sn
            print(f"  {sn} -> dt:{dt_sn}")

    known_dt_serials = list(adb_dt_map.keys())
    print("Probing UART ports for device serial numbers...")

    for port in uart_ports:
        dt_sn = read_serial_from_uart(port, baud, known_dt_serials)
        if dt_sn and dt_sn in adb_dt_map:
            adb_sn = adb_dt_map[dt_sn]
            print(f"  {port} -> dt:{dt_sn} -> adb:{adb_sn}")
            mapping[adb_sn] = port
        else:
            print(f"  {port} -> (no match)")

    unmatched_adb = [sn for sn in adb_devices if sn not in mapping]
    unmatched_uart = [p for p in uart_ports if p not in mapping.values()]
    if unmatched_adb and unmatched_uart:
        print("Trying marker-based matching for remaining devices...")
        extra = match_uart_via_marker(unmatched_adb, unmatched_uart, baud)
        mapping.update(extra)

    return mapping


def safe_filename(sn):
    return sn.replace(' ', '_')


def spawn_uart_viewer(sn, raw_log, port, baud):
    safe_sn = safe_filename(sn)
    signal_file = f"/tmp/uart_picocom_signal_{safe_sn}"
    script_path = f"/tmp/uart_viewer_{safe_sn}.sh"
    try:
        os.remove(signal_file)
    except FileNotFoundError:
        pass
    if platform.system() == 'Darwin':
        console_cmd = f'picocom -b {baud} {port}'
    else:
        console_cmd = f'minicom -D {port} -b {baud}'

    with open(script_path, 'w') as f:
        f.write(f'''#!/bin/bash
echo "[UART viewer: {sn}]"
tail -f "{raw_log}" &
TAIL_PID=$!
while [ ! -f "{signal_file}" ]; do sleep 1; done
kill $TAIL_PID 2>/dev/null
wait $TAIL_PID 2>/dev/null
echo ""
echo "[Switching to serial console: {sn} on {port}]"
{console_cmd}
''')
    os.chmod(script_path, 0o755)

    if platform.system() == 'Darwin':
        subprocess.Popen(['open', '-a', 'Terminal', script_path])
    else:
        try:
            subprocess.Popen(['gnome-terminal', '--title', f'UART-{sn}',
                              '--', 'bash', '-c', f'{script_path}; exec bash'])
        except FileNotFoundError:
            subprocess.Popen(['xterm', '-title', f'UART-{sn}',
                              '-hold', '-e', script_path])


def signal_picocom(sn):
    signal_file = f"/tmp/uart_picocom_signal_{safe_filename(sn)}"
    open(signal_file, 'w').close()


THERMAL_RE = re.compile(r'pre=(\d+)\s+post=(\d+)\s+delta=(-?\d+)')


def uart_reader(sn, port, baud, patterns, user, round_holder, stop_event,
                match_event, matches, log_file, raw_log, threshold, observe_event=None):
    """Read UART, check thermal delta and patterns, log matches."""
    try:
        ser = serial.Serial(port, baud, timeout=1)
        print(f"[{sn}] Logging in via UART and running /data/str_loop.sh...")
        ser.write(b'\n')
        time.sleep(0.5)
        ser.write(b'root\n')
        time.sleep(2)
        ser.read(ser.in_waiting or 4096)  # drain login output
        ser.write(b'/data/str_loop.sh\n')
        print(f"[{sn}] Monitoring UART for thermal delta...")

        raw_f = open(raw_log, 'w')
        RAW_MAX = 1024
        buffer = []
        pending_match = None
        while not stop_event.is_set():
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if line:
                ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                raw_f.write(line + '\n')
                raw_f.flush()
                if raw_f.tell() > RAW_MAX:
                    raw_f.seek(0)
                    raw_f.truncate()

                buffer.append(line)
                if len(buffer) > 10:
                    buffer.pop(0)
                prefix = "[OBSERVE]" if observe_event and observe_event.is_set() \
                    else "[UART]"
                print(f"{prefix}[{sn}] {line}")

                # Check thermal delta
                m = THERMAL_RE.search(line)
                if m:
                    delta = abs(int(m.group(3)))
                    round_holder[0] += 1
                    rn = round_holder[0]
                    if delta >= threshold:
                        match_info = (f"==== round {rn} ====\n"
                                      f"[{ts}]\n"
                                      + '\n'.join(buffer))
                        log_match(log_file, match_info)
                        matches.append(match_info)
                        post_to_slack(user, match_info)
                        match_event.set()

                # Check pattern file matches (reboot detection etc.)
                if pending_match:
                    pending_match[4].append(line)
                    pending_match[3] -= 1
                    if pending_match[3] <= 0:
                        rn = round_holder[0]
                        match_info = (f"==== round {rn} ====\n"
                                      f"[{pending_match[0]}]\n")
                        match_info += '\n'.join(pending_match[5]) + '\n'
                        match_info += pending_match[1] + '\n'
                        match_info += '\n'.join(pending_match[4])
                        log_match(log_file, match_info)
                        matches.append(match_info)
                        post_to_slack(user, match_info)
                        match_event.set()
                        pending_match = None

                if not pending_match:
                    for pattern in patterns:
                        if re.search(re.escape(pattern), line, re.IGNORECASE):
                            pending_match = [ts, line, pattern, 10, [],
                                             list(buffer[:-1])]
                            break
        ser.close()
        raw_f.close()
    except serial.SerialException as e:
        print(f"[UART ERROR][{sn}] {e}")


def run_test(devices, args):
    patterns = load_patterns(args.pattern_file) if args.pattern_file else []
    if patterns:
        print(f"Loaded {len(patterns)} patterns from {args.pattern_file}")

    dev_state = {}
    for sn, port in devices:
        log_file = f"str_thermal_stress_matches_{safe_filename(sn)}.log"
        raw_log = os.path.join(os.getcwd(), f"uart_raw_{safe_filename(sn)}.log")
        dev_state[sn] = {
            'port': port,
            'log_file': log_file,
            'raw_log': raw_log,
            'matches': [],
            'all_matches': [],
            'round': [0],
            'stop_event': threading.Event(),
            'match_event': threading.Event(),
            'observe_event': threading.Event(),
            'thread': None,
        }
        print(f"  {sn} <-> {port}  (log: {log_file})")

    # Clean up stale raw logs
    for sn, st in dev_state.items():
        try:
            os.remove(st['raw_log'])
        except OSError:
            pass

    # Push STR loop script via ADB, then disconnect and run via UART
    suspend = 'sleep 2' if args.no_sleep else 'echo mem > /sys/power/state'
    for sn, port in devices:
        print(f"[{sn}] Pushing STR script to device...")
        subprocess.run(['adb', '-s', sn, 'shell',
                        'echo \'#!/bin/sh\nwhile true; do '
                        'PRE=$(cat /sys/class/thermal/thermal_zone22/temp); '
                        f'{suspend}; '
                        'POST=$(cat /sys/class/thermal/thermal_zone22/temp); '
                        'DELTA=$((POST - PRE)); '
                        'echo "pre=$PRE  post=$POST  delta=$DELTA"; '
                        'sleep 1; '
                        'done\' > /data/str_loop.sh && chmod +x /data/str_loop.sh'],
                       capture_output=True, text=True)
        print(f"[{sn}] Disconnecting ADB...")
        subprocess.run(['adb', '-s', sn, 'disconnect'], capture_output=True)
    subprocess.run(['adb', 'kill-server'], capture_output=True)

    # (disabled - single terminal mode)

    stop_all = threading.Event()

    def device_loop(sn, st):
        try:
            st['stop_event'].clear()
            st['observe_event'].clear()
            st['match_event'].clear()
            st['matches'] = []
            t = threading.Thread(target=uart_reader,
                args=(sn, st['port'], args.baud, patterns, args.user,
                      st['round'], st['stop_event'], st['match_event'],
                      st['matches'], st['log_file'], st['raw_log'],
                      args.threshold, st['observe_event']))
            t.daemon = True
            t.start()
            st['thread'] = t

            # Wait until stopped
            while not stop_all.is_set():
                time.sleep(1)

            st['all_matches'].extend(st['matches'])
        except Exception as e:
            print(f"[{sn}] Error: {e}")

    loops = []
    for sn, st in dev_state.items():
        t = threading.Thread(target=device_loop, args=(sn, st))
        t.daemon = True
        t.start()
        loops.append(t)

    try:
        while any(t.is_alive() for t in loops):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\nTest interrupted")
        stop_all.set()
        stop_all.interrupted = True
    finally:
        stop_all.set()
        for st in dev_state.values():
            st['stop_event'].set()

        # Kill str_loop.sh on devices via ADB
        subprocess.run(['adb', 'start-server'], capture_output=True)
        for sn in dev_state:
            print(f"[{sn}] Killing str_loop.sh on device...")
            subprocess.run(['adb', '-s', sn, 'shell', 'pkill', '-f', 'str_loop'],
                           capture_output=True, text=True)

        print(f"\n=== Summary ===")
        for sn, st in dev_state.items():
            n = len(st['all_matches'])
            r = st['round'][0]
            print(f"  {sn}: {r} STR cycles, {n} thermal matches (log: {st['log_file']})")
            for m in st['all_matches']:
                print(m)


def main():
    preflight_check()
    parser = argparse.ArgumentParser(
        description='STR thermal stress test via UART with thermal delta monitoring',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''examples:
  %(prog)s                                 # auto-detect all devices
  %(prog)s -d SERIAL -p /dev/ttyUSB0       # single device, explicit
  %(prog)s -t 1000                         # thermal delta threshold 1000''')
    parser.add_argument('-f', '--pattern-file', default=None,
                        help='optional pattern file for reboot detection')
    parser.add_argument('-u', '--user',
                        help='Slack user to notify on match (disabled)')
    parser.add_argument('-p', '--port',
                        help='UART serial port (auto-detect if omitted)')
    parser.add_argument('-b', '--baud', type=int, default=921600,
                        help='UART baud rate (default: 921600)')
    parser.add_argument('-d', '--device',
                        help='ADB device serial (auto-detect if omitted)')
    parser.add_argument('-t', '--threshold', type=int, default=THERMAL_DELTA_THRESHOLD,
                        help=f'thermal delta threshold (default: {THERMAL_DELTA_THRESHOLD})')
    parser.add_argument('--no-sleep', action='store_true',
                        help='use "sleep 2" instead of suspend-to-RAM')
    args = parser.parse_args()

    # Resolve device list (need ADB for initial UART mapping)
    if args.device and args.port:
        devices = [(args.device, args.port)]
    elif args.device or args.port:
        mapping = match_uart_to_adb(args.baud)
        if args.device:
            if args.device not in mapping:
                sys.exit(f"ERROR: Cannot find UART for {args.device}")
            devices = [(args.device, mapping[args.device])]
        else:
            reverse = {v: k for k, v in mapping.items()}
            if args.port not in reverse:
                sys.exit(f"ERROR: Cannot find device on {args.port}")
            devices = [(reverse[args.port], args.port)]
    else:
        mapping = match_uart_to_adb(args.baud)
        if not mapping:
            sys.exit("ERROR: No UART-to-ADB mapping found. Use -d and -p.")
        devices = list(mapping.items())

    print(f"\nTesting {len(devices)} device(s):")
    run_test(devices, args)


if __name__ == '__main__':
    main()

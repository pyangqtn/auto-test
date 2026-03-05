#!/usr/bin/env python3
"""ADB reboot stress test with UART monitoring for multiple devices."""

import argparse
import subprocess
import serial
import threading
import time
import re
import sys
import os
import glob
import platform
from datetime import datetime

SLACK_ENABLED = False


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
    """Open UART, login as root, read device serial from devicetree.
    Retries login since device may be at login prompt or already logged in."""
    try:
        ser = serial.Serial(port, baud, timeout=1)
        ser.reset_input_buffer()
        # Send enter, then try root login (works whether at prompt or shell)
        for attempt in range(3):
            ser.write(b'\n')
            time.sleep(0.5)
            ser.write(b'root\n')
            time.sleep(1)
            ser.read(ser.in_waiting or 1)  # drain
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


def match_uart_to_adb(baud):
    """Build {adb_serial: uart_port} mapping by probing UART ports.
    Matches devicetree serial from UART against devicetree serial from adb shell."""
    adb_devices = get_adb_devices()
    uart_ports = enumerate_uart_ports()
    if not adb_devices or not uart_ports:
        return {}

    mapping = {}

    print(f"ADB devices: {adb_devices}")
    print(f"UART ports:  {uart_ports}")

    # Wait for all ADB devices to be fully online before probing
    for sn in adb_devices:
        print(f"Waiting for {sn} to be online...")
        while not adb_device_online(sn):
            time.sleep(1)
        print(f"  {sn} online")

    # Get devicetree serial for each ADB device via adb shell
    adb_dt_map = {}  # {devicetree_serial: adb_serial}
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

    return mapping


def adb_device_online(sn):
    result = subprocess.run(['adb', '-s', sn, 'get-state'],
                            capture_output=True, text=True)
    return result.stdout.strip() == 'device'


def adb_reboot(sn):
    subprocess.run(['adb', '-s', sn, 'reboot'], capture_output=True)


def spawn_uart_viewer(sn, raw_log, port, baud):
    """Spawn a terminal window: tail -f raw log, then switch to picocom when signaled."""
    safe_sn = safe_filename(sn)
    signal_file = f"/tmp/uart_picocom_signal_{safe_sn}"
    script_path = f"/tmp/uart_viewer_{safe_sn}.sh"
    # Remove stale signal
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
    """Signal the viewer window to switch from tail -f to picocom."""
    signal_file = f"/tmp/uart_picocom_signal_{safe_filename(sn)}"
    open(signal_file, 'w').close()


ADB_TIMEOUT = 120  # seconds before declaring device stuck


def uart_reader(sn, port, baud, patterns, user, round_holder, stop_event,
                match_event, matches, log_file, raw_log, observe_event=None):
    """Read UART, check patterns, log matches. Runs in background thread."""
    try:
        ser = serial.Serial(port, baud, timeout=1)
        raw_f = open(raw_log, 'w')
        RAW_MAX = 1024
        buffer = []
        pending_match = None
        while not stop_event.is_set():
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if line:
                ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                # Write raw line to file for tail -f viewers
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
                        if re.search(pattern, line, re.IGNORECASE):
                            pending_match = [ts, line, pattern, 10, [],
                                             list(buffer[:-1])]
                            break
        ser.close()
        raw_f.close()
    except serial.SerialException as e:
        print(f"[UART ERROR][{sn}] {e}")


def safe_filename(sn):
    """Replace spaces and special chars in serial for use in filenames."""
    return sn.replace(' ', '_')


def run_test(devices, args):
    """Run reboot stress test for one or more devices from main process.

    devices: list of (adb_serial, uart_port) tuples
    """
    patterns = load_patterns(args.pattern_file)
    print(f"Loaded {len(patterns)} patterns from {args.pattern_file}")

    # Per-device state
    dev_state = {}
    for sn, port in devices:
        log_file = f"reboot_stress_matches_{safe_filename(sn)}.log"
        raw_log = os.path.join(os.getcwd(), f"uart_raw_{safe_filename(sn)}.log")
        dev_state[sn] = {
            'port': port,
            'log_file': log_file,
            'raw_log': raw_log,
            'matches': [],
            'all_matches': [],
            'round': [0],  # mutable holder so thread sees updates
            'stop_event': threading.Event(),
            'match_event': threading.Event(),
            'observe_event': threading.Event(),
            'thread': None,
        }
        print(f"  {sn} <-> {port}  (log: {log_file})")

    # Spawn UART viewer windows for each device
    for sn, port in devices:
        open(dev_state[sn]['raw_log'], 'a').close()
        print(f"Opening UART viewer for {sn}")
        spawn_uart_viewer(sn, dev_state[sn]['raw_log'], port, args.baud)
        time.sleep(1)

    stop_all = threading.Event()

    def device_loop(sn, st):
        """Independent reboot loop for one device."""
        round_num = 0
        try:
            while not stop_all.is_set() and (args.rounds == 0 or round_num < args.rounds):
                round_num += 1
                ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                print(f"\n[{ts}] === [{sn}] Round {round_num} ===")

                print(f"[{sn}] Waiting for device...")
                wait_start = time.time()
                while not stop_all.is_set() and not adb_device_online(sn):
                    time.sleep(1)
                    if time.time() - wait_start > ADB_TIMEOUT:
                        print(f"\n[TIMEOUT][{sn}] Device not online after {ADB_TIMEOUT}s")
                        st['timed_out'] = True
                        stop_all.set()
                        break
                if stop_all.is_set():
                    break
                print(f"[{sn}] Device online")

                st['stop_event'].clear()
                st['observe_event'].clear()
                st['match_event'].clear()
                st['matches'] = []
                st['round'][0] = round_num
                t = threading.Thread(target=uart_reader,
                    args=(sn, st['port'], args.baud, patterns, args.user,
                          st['round'], st['stop_event'], st['match_event'],
                          st['matches'], st['log_file'], st['raw_log'],
                          st['observe_event']))
                t.daemon = True
                t.start()
                st['thread'] = t

                print(f"[{sn}] Rebooting...")
                adb_reboot(sn)

                time.sleep(5)
                wait_start = time.time()
                while not stop_all.is_set() and not adb_device_online(sn):
                    time.sleep(1)
                    if args.stop_on_match and st['match_event'].is_set():
                        break
                    if time.time() - wait_start > ADB_TIMEOUT:
                        print(f"\n[TIMEOUT][{sn}] Device not back after {ADB_TIMEOUT}s")
                        st['timed_out'] = True
                        stop_all.set()
                        break
                if st.get('timed_out'):
                    break
                print(f"[{sn}] Back online")

                st['all_matches'].extend(st['matches'])

                if args.stop_on_match and st['match_event'].is_set():
                    print(f"\n[STOP][{sn}] Pattern matched in round {round_num}")
                    st['observe_event'].set()
                    # Stay alive so UART reader keeps running in observe mode
                    while not stop_all.is_set():
                        time.sleep(1)
                    break

                st['stop_event'].set()
        except Exception as e:
            print(f"[{sn}] Error: {e}")

    # Start independent loop per device
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

    finally:
        for st in dev_state.values():
            st['stop_event'].set()
        print(f"\n=== Summary ===")
        for sn, st in dev_state.items():
            n = len(st['all_matches'])
            r = st['round'][0]
            timeout = " [TIMED OUT]" if st.get('timed_out') else ""
            print(f"  {sn}: {r} rounds, {n} matches{timeout} (log: {st['log_file']})")
            for m in st['all_matches']:
                print(m)

        # Picocom handoff: signal viewer windows to switch to picocom
        if not args.no_picocom:
            time.sleep(1)  # let UART readers release ports
            for sn, st in dev_state.items():
                print(f"Switching {sn} viewer to picocom")
                signal_picocom(sn)

        # Clean up temp raw logs
        for sn, st in dev_state.items():
            try:
                os.remove(st['raw_log'])
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(
        description='ADB reboot stress test with UART monitoring',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''examples:
  %(prog)s                                 # auto-detect all devices
  %(prog)s -d SERIAL -p /dev/ttyUSB0       # single device, explicit
  %(prog)s -n 100 --stop-on-match          # 100 rounds, stop on match
  %(prog)s -n 0                            # infinite (Ctrl+C to stop)''')
    parser.add_argument('-f', '--pattern-file', default='pattern',
                        help='pattern file, one regex per line (default: pattern)')
    parser.add_argument('-u', '--user',
                        help='Slack user to notify on match (disabled)')
    parser.add_argument('-p', '--port',
                        help='UART serial port (auto-detect if omitted)')
    parser.add_argument('-b', '--baud', type=int, default=926000,
                        help='UART baud rate (default: 926000)')
    parser.add_argument('-n', '--rounds', type=int, default=0,
                        help='number of reboot rounds, 0=infinite (default: 0)')
    parser.add_argument('-s', '--stop-on-match', action='store_true',
                        help='stop reboot loop on first pattern match')
    parser.add_argument('-d', '--device',
                        help='ADB device serial (auto-detect if omitted)')
    parser.add_argument('--no-picocom', action='store_true',
                        help='disable picocom handoff on exit/timeout (default: enabled)')
    args = parser.parse_args()

    # Resolve device list
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

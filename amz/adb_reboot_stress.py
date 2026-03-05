#!/usr/bin/env python3
"""ADB reboot stress test with UART monitoring."""

import argparse
import subprocess
import serial
import threading
import time
import re
from datetime import datetime

# TODO: Enable Slack later
SLACK_ENABLED = False
# SLACK_TOKEN = "xoxb-your-bot-token-here"
LOG_FILE = "reboot_stress_matches.log"

def log_match(match_info):
    """Write match to log file immediately."""
    with open(LOG_FILE, 'a') as f:
        f.write(match_info + '\n')
    print(f"[LOGGED] {match_info}")

def post_to_slack(user, message):
    """Post message to Slack user (disabled for now)."""
    if not SLACK_ENABLED:
        print(f"[SLACK DISABLED] Would notify {user}: {message}")
        return
    # TODO: Implement when ready
    # from slack_sdk import WebClient
    # client = WebClient(token=SLACK_TOKEN)
    # ...

def load_patterns(pattern_file):
    """Load patterns from file."""
    with open(pattern_file, 'r') as f:
        return [line.strip() for line in f if line.strip()]

def adb_device_online():
    """Check if ADB device is online."""
    result = subprocess.run(['adb', 'devices'], capture_output=True, text=True)
    lines = result.stdout.strip().split('\n')[1:]
    return any('device' in line and 'offline' not in line for line in lines)

def adb_reboot():
    """Reboot device via ADB."""
    subprocess.run(['adb', 'reboot'], capture_output=True)

def uart_reader(port, baud, patterns, user, round_num, stop_event, matches):
    """Read UART and check for patterns."""
    try:
        ser = serial.Serial(port, baud, timeout=1)
        buffer = []
        pending_match = None  # (ts, line, pattern, after_count, before_lines)
        while not stop_event.is_set():
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if line:
                ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                buffer.append(line)
                if len(buffer) > 10:
                    buffer.pop(0)
                print(f"[UART] {line}")
                
                if pending_match:
                    pending_match[4].append(line)
                    pending_match[3] -= 1
                    if pending_match[3] <= 0:
                        match_info = f"==== round {round_num} ====\n[{pending_match[0]}]\n"
                        match_info += '\n'.join(pending_match[5]) + '\n'
                        match_info += pending_match[1] + '\n'
                        match_info += '\n'.join(pending_match[4])
                        log_match(match_info)
                        matches.append(match_info)
                        post_to_slack(user, match_info)
                        pending_match = None
                
                if not pending_match:
                    for pattern in patterns:
                        if re.search(pattern, line, re.IGNORECASE):
                            pending_match = [ts, line, pattern, 10, [], list(buffer[:-1])]
                            break
        ser.close()
    except serial.SerialException as e:
        print(f"[UART ERROR] {e}")

def main():
    parser = argparse.ArgumentParser(description='ADB reboot stress test with UART monitoring')
    parser.add_argument('-f', '--pattern-file', default='pattern', help='Pattern file (default: pattern)')
    parser.add_argument('-u', '--user', help='Slack user to notify (disabled for now)')
    parser.add_argument('-p', '--port', default='/dev/tty.usbserial-DP0415NQ', help='UART port (default: /dev/ttyUSB0)')
    parser.add_argument('-b', '--baud', type=int, default=926000, help='Baud rate (default: 926000)')
    parser.add_argument('-n', '--rounds', type=int, default=0, help='Number of rounds (0=infinite)')
    args = parser.parse_args()

    patterns = load_patterns(args.pattern_file)
    print(f"Loaded {len(patterns)} patterns from {args.pattern_file}")
    if not SLACK_ENABLED and args.user:
        print(f"[NOTE] Slack disabled - notifications to {args.user} will be printed only")

    round_num = 0
    all_matches = []

    try:
        while args.rounds == 0 or round_num < args.rounds:
            round_num += 1
            ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"\n[{ts}] === Round {round_num} ===")

            # Wait for device
            print("Waiting for device...")
            while not adb_device_online():
                time.sleep(1)
            print("Device online")

            # Start UART reader
            stop_event = threading.Event()
            matches = []
            uart_thread = threading.Thread(target=uart_reader,
                args=(args.port, args.baud, patterns, args.user, round_num, stop_event, matches))
            uart_thread.start()

            # Reboot
            print("Rebooting device...")
            adb_reboot()

            # Wait for device to go offline then come back
            time.sleep(5)
            while not adb_device_online():
                time.sleep(1)
            print("Device back online")

            # Stop UART reader
            stop_event.set()
            uart_thread.join(timeout=5)
            all_matches.extend(matches)

    except KeyboardInterrupt:
        print("\n\nTest interrupted")
    finally:
        print(f"\n=== Summary ===")
        print(f"Completed {round_num} rounds")
        print(f"Total matches: {len(all_matches)}")
        for m in all_matches:
            print(m)

if __name__ == '__main__':
    main()

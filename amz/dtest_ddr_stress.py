#!/usr/bin/env python3
"""DDR Stress Test Script - Runs dtest DDR_f_stress and logs results"""

import subprocess
import re
import argparse
import sys
from datetime import datetime

def should_filter(line):
    if 'Log: Seconds remaining' in line:
        return True
    if re.match(r'^\d{4}/\d{2}/\d{2}-\d{2}:\d{2}:\d{2}\(UTC\)\s*$', line.strip()):
        return True
    if not line.strip():
        return True
    return False

def run_cmd_live(cmd, logfile=None):
    print(f"Running: {cmd}")
    process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    full_output = []
    for line in process.stdout:
        print(line, end='')
        sys.stdout.flush()
        full_output.append(line)
        if logfile and not should_filter(line):
            logfile.write(line)
    process.wait()
    return ''.join(full_output)

parser = argparse.ArgumentParser()
parser.add_argument('-r', type=int, default=5, help='Number of rounds (default: 5)')
args = parser.parse_args()

# Get SN
print("Getting device serial number...")
sn_output = run_cmd_live('adb shell dtest SW_f_idme_read serial')
sn_match = re.search(r'([A-Z0-9]{16,})', sn_output)
sn = sn_match.group(1) if sn_match else 'unknown'
print(f"Device SN: {sn}\n")

logfile = f'dtest_ddrstress_{sn}.txt'
pass_count = 0
fail_count = 0

with open(logfile, 'w') as f:
    f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
    
    for i in range(1, args.r + 1):
        print(f"\n===== Starting round {i}/{args.r} =====")
        f.write(f"===== round {i} =====\n")
        output = run_cmd_live('adb shell dtest DDR_f_stress', f)
        f.write('\n')
        
        if 'PASS^_' in output:
            pass_count += 1
            print(f"Round {i}/{args.r} complete - PASS")
        else:
            fail_count += 1
            print(f"Round {i}/{args.r} complete - FAIL")
    
    f.write(f"===== Summary =====\n")
    f.write(f"PASS: {pass_count}\n")
    f.write(f"FAIL: {fail_count}\n")

print(f"\nDone. Log saved to {logfile}")

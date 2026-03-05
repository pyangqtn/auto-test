#!/usr/bin/env python3

import subprocess
import argparse
import sys
import time
from datetime import datetime

def run_command(cmd, timeout=60, show_output=False):
    """Run command and return output, return code"""
    try:
        if show_output:
            # Stream output in real-time for flashimage
            process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, 
                                     stderr=subprocess.STDOUT, text=True, bufsize=1)
            output_lines = []
            start_time = time.time()
            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                if line:
                    print(line.rstrip())  # Print to console immediately
                    output_lines.append(line.rstrip())
                # Check timeout
                if time.time() - start_time > timeout:
                    process.terminate()
                    return "", f"Command timed out after {timeout}s", 1
            
            stdout = '\n'.join(output_lines)
            return stdout, "", process.returncode
        else:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return result.stdout.strip(), result.stderr.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "", f"Command timed out after {timeout}s", 1

def log_message(log_file, message):
    """Write message to log file and print to console"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    with open(log_file, 'a') as f:
        f.write(log_entry + '\n')

def main():
    parser = argparse.ArgumentParser(description='Thermal stress test for Persimmon device')
    parser.add_argument('-r', '--rounds', type=int, default=0, 
                       help='Number of test rounds (0 = infinite)')
    args = parser.parse_args()

    # Create log file with timestamp
    timestamp = datetime.now().strftime("%m_%d_%y_%H_%M")
    log_file = f"Thermal_test_{timestamp}.log"
    
    log_message(log_file, "=== Thermal Stress Test Started ===")
    log_message(log_file, f"Rounds: {'Infinite' if args.rounds == 0 else args.rounds}")
    
    # Step 1: Get OS release info
    log_message(log_file, "Step 1: Getting OS release info...")
    stdout, stderr, rc = run_command('adb shell "cat /etc/os-release"')
    if rc != 0:
        log_message(log_file, f"ERROR: Failed to get OS release: {stderr}")
        sys.exit(1)
    log_message(log_file, f"OS Release:\n{stdout}")
    
    round_count = 0
    try:
        while True:
            round_count += 1
            if args.rounds > 0 and round_count > args.rounds:
                break
                
            log_message(log_file, f"\n--- Round {round_count} ---")
            
            # Step 3: Wait for device
            log_message(log_file, "Step 3: Waiting for device...")
            stdout, stderr, rc = run_command('adb wait-for-device', timeout=120)
            if rc != 0:
                log_message(log_file, f"ERROR: Device not ready: {stderr}")
                continue
            
            # Step 4: Flash image
            log_message(log_file, "Step 4: Flashing image...")
            stdout, stderr, rc = run_command('./flashimage.py', timeout=300, show_output=True)
            if rc != 0:
                log_message(log_file, f"ERROR: Flash failed: {stderr}")
                continue
            log_message(log_file, "Flash completed successfully")
            if stdout:
                log_message(log_file, f"Flash output: {stdout}")
            if stderr:
                log_message(log_file, f"Flash stderr: {stderr}")
            
            # Step 5: Wait for device again
            log_message(log_file, "Step 5: Waiting for device after flash...")
            stdout, stderr, rc = run_command('adb wait-for-device', timeout=240)
            if rc != 0:
                log_message(log_file, f"ERROR: Device not ready after flash: {stderr}")
                continue
            
            # Step 6: Check reboot reason
            log_message(log_file, "Step 6: Checking reboot reason...")
            stdout, stderr, rc = run_command('adb shell "cat /sys/kernel/reboot_reason"')
            if rc != 0:
                log_message(log_file, f"ERROR: Failed to read reboot reason: {stderr}")
                continue
                
            reboot_reason = stdout.strip()
            log_message(log_file, f"Reboot reason: {reboot_reason}")
            
            # Check for error condition
            if "Linux Software Reset" in reboot_reason:
                log_message(log_file, f"*** THERMAL ISSUE DETECTED: {reboot_reason} ***")
                log_message(log_file, "Test stopped due to thermal reset detection")
                break
            
            log_message(log_file, f"Round {round_count} completed successfully")
            time.sleep(2)  # Brief pause between rounds
            
    except KeyboardInterrupt:
        log_message(log_file, "\nTest interrupted by user")
    except Exception as e:
        log_message(log_file, f"Unexpected error: {e}")
    
    log_message(log_file, f"\n=== Test completed after {round_count} rounds ===")
    log_message(log_file, f"Log saved to: {log_file}")

if __name__ == "__main__":
    main()

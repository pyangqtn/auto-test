#!/usr/bin/env python3
import subprocess
import time
import threading
import sys
import signal
import serial
import re
import argparse

class MadeleineStressTest:
    def __init__(self, serial_port='/dev/ttyUSB0', baud_rate=921600, max_rounds=None):
        self.running = True
        self.panic_detected = False
        self.iteration = 0
        self.max_rounds = max_rounds
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.serial_conn = None
        self.panic_patterns = [
            r'Kernel panic',
            r'synchronous external abort',
            r'Internal error',
            r'Unable to handle kernel',
            r'Oops:',
            r'BUG:',
            r'Call trace:'
        ]
        
    def setup_serial(self):
        """Setup serial connection"""
        try:
            self.serial_conn = serial.Serial(
                self.serial_port, 
                self.baud_rate, 
                timeout=1
            )
            print(f"✓ Serial connected: {self.serial_port} @ {self.baud_rate}")
            return True
        except Exception as e:
            print(f"❌ Serial connection failed: {e}")
            print("Available serial ports:")
            import serial.tools.list_ports
            for port in serial.tools.list_ports.comports():
                print(f"  - {port.device}: {port.description}")
            return False
    
    def run_adb_command(self, command, timeout=10):
        """Run ADB command with timeout"""
        try:
            result = subprocess.run(
                ['adb', 'shell', command], 
                capture_output=True, 
                text=True, 
                timeout=timeout
            )
            return result.returncode == 0, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return False, "", "Command timed out"
        except Exception as e:
            return False, "", str(e)
    
    def check_device_alive(self):
        """Check if device is responsive"""
        success, _, _ = self.run_adb_command("echo 'alive'", timeout=5)
        return success
    
    def monitor_serial(self):
        """Monitor serial console for kernel panic"""
        print("🔍 Starting serial console monitoring...")
        
        while self.running and self.serial_conn:
            try:
                if self.serial_conn.in_waiting:
                    line = self.serial_conn.readline().decode('utf-8', errors='ignore').strip()
                    
                    if line:
                        # Print serial output with timestamp
                        timestamp = time.strftime("%H:%M:%S")
                        print(f"[{timestamp}] {line}")
                        
                        # Check for panic patterns
                        for pattern in self.panic_patterns:
                            if re.search(pattern, line, re.IGNORECASE):
                                print(f"\n🚨 KERNEL PANIC DETECTED!")
                                print(f"Pattern matched: {pattern}")
                                print(f"Line: {line}")
                                self.panic_detected = True
                                self.running = False
                                return
                                
            except Exception as e:
                print(f"Serial read error: {e}")
                break
                
            time.sleep(0.1)
    
    def start_stress(self):
        """Start stress-ng in background"""
        print("Starting system stress...")
        success, _, _ = self.run_adb_command(
            "stress-ng --cpu 8 --io 6 --vm 3 > /dev/null 2>&1 &"
        )
        if success:
            print("✓ Stress test started")
        else:
            print("✗ Failed to start stress test")
        return success
    
    def stop_stress(self):
        """Stop stress-ng"""
        print("Stopping stress test...")
        self.run_adb_command("pkill -f stress-ng")
    
    def touchscreen_cycle(self):
        """Unbind and bind touchscreen driver"""
        rounds_info = f" ({self.iteration}/{self.max_rounds})" if self.max_rounds else ""
        print(f"\n--- Iteration {self.iteration}{rounds_info} ---")
        print("Cycling touchscreen driver...")
        
        # Unbind
        success, _, _ = self.run_adb_command(
            "echo 'spi0.0' > /sys/bus/spi/drivers/fts_ts/unbind"
        )
        if not success:
            print("  ✗ Failed to unbind")
            return False
            
        time.sleep(1)
        
        # Bind (this triggers firmware loading)
        print("  → Binding driver (triggering firmware load)...")
        success, _, _ = self.run_adb_command(
            "echo 'spi0.0' > /sys/bus/spi/drivers/fts_ts/bind"
        )
        if not success:
            print("  ✗ Failed to bind")
            return False
            
        print("  ✓ Driver cycled successfully")
        return True
    
    def signal_handler(self, signum, frame):
        """Handle Ctrl+C gracefully"""
        print("\n🛑 Stopping test...")
        self.running = False
    
    def run_test(self):
        """Main test loop"""
        print("🔥 Madeleine Touchscreen Stress Test with Serial Monitoring")
        print("=" * 60)
        print(f"Serial: {self.serial_port} @ {self.baud_rate}")
        if self.max_rounds:
            print(f"Rounds: {self.max_rounds}")
        else:
            print("Rounds: Endless loop")
        print("=" * 60)
        
        # Setup signal handler
        signal.signal(signal.SIGINT, self.signal_handler)
        
        # Setup serial connection
        if not self.setup_serial():
            print("❌ Cannot continue without serial connection")
            return
        
        # Check initial device connection
        if not self.check_device_alive():
            print("❌ Device not connected via ADB")
            return
        
        # Start serial monitoring thread
        serial_thread = threading.Thread(target=self.monitor_serial)
        serial_thread.daemon = True
        serial_thread.start()
        
        # Start stress test
        if not self.start_stress():
            print("❌ Failed to start stress test")
            return
        
        try:
            # Main test loop
            while self.running and not self.panic_detected:
                self.iteration += 1
                
                # Check if we've reached max rounds
                if self.max_rounds and self.iteration > self.max_rounds:
                    print(f"\n✓ Completed {self.max_rounds} rounds without panic")
                    break
                
                if not self.touchscreen_cycle():
                    print("❌ Driver cycle failed")
                    break
                
                # Wait between iterations
                time.sleep(3)
                
                # Periodic device check
                if self.iteration % 5 == 0:
                    if not self.check_device_alive():
                        print("⚠ Device not responding via ADB")
                        time.sleep(2)
                        continue
                    else:
                        print(f"✓ Device responsive after {self.iteration} iterations")
                        
        except KeyboardInterrupt:
            print("\n🛑 Test interrupted by user")
        
        finally:
            self.running = False
            self.stop_stress()
            
            if self.serial_conn:
                self.serial_conn.close()
            
            if self.panic_detected:
                print("\n🎯 SUCCESS: Kernel panic reproduced!")
                print(f"Panic detected after {self.iteration} iterations")
                print("Check serial console output above for crash details")
            else:
                print(f"\n📊 Test completed: {self.iteration} iterations")
                print("No kernel panic detected")

def main():
    parser = argparse.ArgumentParser(description='Madeleine Touchscreen Stress Test')
    parser.add_argument('-r', '--rounds', type=int, default=None,
                        help='Number of test rounds (default: endless loop)')
    parser.add_argument('-s', '--serial', type=str, default='/dev/ttyUSB0',
                        help='UART device path (default: /dev/ttyUSB0)')
    parser.add_argument('-b', '--baudrate', type=int, default=921600,
                        help='UART baud rate (default: 921600)')
    
    args = parser.parse_args()
    
    test = MadeleineStressTest(
        serial_port=args.serial,
        baud_rate=args.baudrate,
        max_rounds=args.rounds
    )
    test.run_test()

if __name__ == "__main__":
    main()

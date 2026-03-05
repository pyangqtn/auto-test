import serial
import time
import argparse

def send_command(ser, command):
    ser.write((command + '\r').encode())  # Send command followed by carriage return
    time.sleep(0.5)  # Wait for the device to process the command
    response = ser.read_all().decode()  # Read the response
    return response

def main():
    # Argument parser setup
    parser = argparse.ArgumentParser(description='Send commands to a UART device.')
    parser.add_argument('device', type=str, help='The UART device path (e.g., /dev/ttyUSB0)')
    parser.add_argument('-b', '--baudrate', type=int, default=115200, help='Baud rate for the UART connection')
    args = parser.parse_args()

    # Commands to be sent
    commands = [
#        "command1",
#        "command2",
        # Add more commands as needed
        "scan",
    ]

    # Open serial port
    ser = serial.Serial(args.device, args.baudrate, timeout=1)

    try:
        for cmd in commands:
            response = send_command(ser, cmd)
            print(f"Sent: {cmd}")
            print(f"Received: {response}")

    finally:
        ser.close()  # Close the serial port

if __name__ == '__main__':
    main()


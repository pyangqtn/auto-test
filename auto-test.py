import sys
import xml.etree.ElementTree as ET
import serial
import time
import re
import subprocess
import datetime
import threading

# Define the default values for UART parameters
DEFAULT_BAUDRATE = 115200
DEFAULT_PARITY = 'N'
DEFAULT_DATA_BITS = 8
DEFAULT_STOP_BITS = 1
DEFAULT_RECV_INTERVAL = 0

DEFAULT_OWN_IP = "127.0.0.0"  # Variable to store extracted IP
DEFAULT_DST_IP = "0.0.0.0"
DEFAULT_PING_PARAM = None
DEFAULT_SAVELOG = "no"

class TestEnv:
    def __init__(self, own_ip, dst_ip, ping_param, savelog=False, extra_device=None):
        self.own_ip = own_ip
        self.dst_ip = dst_ip
        self.ping_param = ping_param
        self.savelog = savelog
        self.log_file = None
        self.extra_device = extra_device
        self.name = None 

    def setup_log_file(self):
        if self.savelog:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            #self.log_file = f"{config_file.rsplit('.', 1)[0]}_{timestamp}.log" 
            self.log_file = f"{self.name}_{timestamp}.log" 
            print(f"Logging enabled. Log file: {self.log_file}")

    def log(self, message):
        if self.savelog and self.log_file:
            with open(self.log_file, "a") as log_file:
                log_file.write(message.replace('\r', '') + '\n')

class UARTDevice:
    def __init__(self, name, baudrate=DEFAULT_BAUDRATE, parity=DEFAULT_PARITY, data_bits=DEFAULT_DATA_BITS, stop_bits=DEFAULT_STOP_BITS):
        self.name = name
        self.baudrate = baudrate
        self.parity = parity
        self.data_bits = data_bits
        self.stop_bits = stop_bits

class Command:
    def __init__(self, text, repeat, interval, recv_interval, parse_rslt, native, execute, prerun=False, reasm=False, attri=None):
        self.text = text
        self.repeat = repeat
        self.interval = interval
        self.recv_interval = recv_interval
        self.parse_rslt = parse_rslt
        self.native = native
        self.execute = execute
        self.prerun = prerun
        self.reasm = reasm
        self.attri = attri

    def run(self, ser, global_env):
        if not self.execute or self.text is None:
            return
        print(f"Start running {self.text}")
        if self.reasm and self.attri is not None:
            handler_name = f"{self.text}_attri_handler"
            if handler_name in globals():
                self.text = globals()[handler_name](self, global_env)
                self.reasm = False
            else:
                print(f"Warning: No handler found for command attributes in '{text}'")
    
        if not self.execute or self.text is None:
            return
        print(f"get execute for extlog is {self.execute}")

        for _ in range(self.repeat):
            if self.native:
                print(f"Executing native command: {self.text}")
                global_env.log(f"Executing native command: {self.text}")
                subprocess.run(self.text, shell=True)
            else:
                if ser is None:
                    return

                ser.write((self.text + '\n').encode())
                print(f"Sent: {self.text}")
                global_env.log(f"Sent: {self.text}")
                time.sleep(self.interval)
                
                response = ser.read_all().decode()
                print(f"Received: {response}")
                global_env.log(f"Received: {response}")
                
                if self.parse_rslt:
                    handler_name = f"{self.parse_rslt}_handler"
                    if handler_name in globals():
                        globals()[handler_name](response, global_env)
                    else:
                        print(f"Warning: No handler found for pattern '{self.parse_rslt}'")
                
                time.sleep(self.recv_interval)

def parse_device(device_elem):
    name = device_elem.find('name').text
    baudrate = int(device_elem.find('baudrate').text)
    parity = device_elem.find('parity').text or DEFAULT_PARITY
    data_bits = int(device_elem.find('data_bits').text)
    stop_bits = int(device_elem.find('stop_bits').text)
    return UARTDevice(name, baudrate, parity, data_bits, stop_bits)

def parse_extra_device(root):
    extra_device_elem = root.find('extra_device')
    if extra_device_elem is not None:
        return parse_device(extra_device_elem)
    return None

def extlog_attri_handler(instance, global_env):
    global extra_uart_thread, extra_uart_running
    print(f"get execute for extlog is {instance.execute}")
    extra_uart_running = instance.execute 
    if extra_uart_running:
        extra_uart_thread = threading.Thread(target=log_additional_uart, args=(global_env, instance.execute))
        extra_uart_thread.start()
    else:
        print("Skipping extra UART logging (execute flag is no).")      
    instance.execute = False
    print(f"get execute for extlog is {instance.execute}")
    return None  # Ensure command is ignored in main loop

def log_additional_uart(global_env, execute):
    global extra_uart_running
    if not execute:
        print("Skipping extra log as execute is set to no.")
        return

    extra_device = global_env.extra_device
    if extra_device is None or extra_uart_running is not True:
        print("No extra log needed.")
        return

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    extra_dev_name = extra_device.name.split('/')[-1] if extra_device.name else "unknown"
    log_filename = f"{global_env.name or 'default'}_{extra_dev_name}_{timestamp}.log"
    print(f"Extra log filename: {log_filename}.")

    try:
        ser = serial.Serial(extra_device.name, extra_device.baudrate, timeout=1)
        with open(log_filename, "a") as log_file:
            while extra_uart_running:
                data = ser.readline().decode().strip()
                if data:
                    log_file.write(data.replace('\r', '') + '\n')
    except Exception as e:
        print(f"Error logging extra UART: {e}")


def parse_env(env_elem):
    own_ip = env_elem.find('own_ip').text or DEFAULT_OWN_IP
    dst_ip = env_elem.find('dst_ip').text or DEFAULT_DST_IP
    ping_param = env_elem.find('ping_param').text or DEFAULT_PING_PARAM
    savelog = env_elem.find('savelog') is not None and env_elem.find('savelog').text.strip().lower() == 'yes'
    return TestEnv(own_ip, dst_ip, ping_param, savelog)

def parse_config(config_file):
    tree = ET.parse(config_file)
    root = tree.getroot()

    execution_count = int(root.find('execution_count').text)
    env_elem = root.find('global_env')
    global_env = parse_env(env_elem)
    global_env.name = f"{config_file.rsplit('.', 1)[0]}" 
    global_env.setup_log_file()

    extra_device = parse_extra_device(root)
    global_env.extra_device = extra_device

    device_elem = root.find('device')
    device = parse_device(device_elem)

    commands = []
    for cmd in root.findall('commands/command'):
        execute = cmd.get('execute', 'yes').lower() == 'yes'
        text = cmd.find('text').text
        print(f"find cmd name {text}")
        repeat = int(cmd.find('repeat').text)
        interval = float(cmd.find('interval').text)
        recv_interval = float(cmd.find('recv_interval').text) if cmd.find('recv_interval') is not None else DEFAULT_RECV_INTERVAL
        parse_rslt = cmd.find('parse_rslt').text if cmd.find('parse_rslt') is not None else None
        native = cmd.find('native') is not None and cmd.find('native').text.strip().lower() == 'yes'
        prerun = cmd.find('prerun') is not None and cmd.find('prerun').text.strip().lower() == 'yes'
        reasm = False
        attri = None

        cmd_attri_elem = cmd.find('cmd_attri')
        if cmd_attri_elem is not None:
            attri = cmd_attri_elem
            reasm = True
#            cmd_attris = cmd_attri_elem.text.split()
#            handler_name = f"{text}_attri_handler"
#            if handler_name in globals():
#                text = globals()[handler_name](text, cmd_attris, global_env, config_file, execute)
#            else:
#                print(f"Warning: No handler found for command attributes in '{text}'")
        
        commands.append(Command(text, repeat, interval, recv_interval, parse_rslt, native, execute, prerun, reasm, attri))

    return execution_count, global_env, device, commands

def execute_commands(execution_count, global_env, device, commands, readback, debug=False):
    if debug:
        print("Debug: Command list before filtering:")
        for cmd in commands:
            print(f"  Command: {cmd.text}, Prerun: {cmd.prerun}")
    prerun_commands = [cmd for cmd in commands if cmd.prerun]
    normal_commands = [cmd for cmd in commands if not cmd.prerun]

    if debug:
        print("Debug: Prerun commands:")
        for cmd in prerun_commands:
            print(f"  {cmd.text}")

        print("Debug: Normal commands:")
        for cmd in normal_commands:
            print(f"  {cmd.text}")

    try:
        ser = serial.Serial(device.name, device.baudrate, parity=device.parity, bytesize=device.data_bits, stopbits=device.stop_bits)
    except serial.SerialException as e:
        print(f"Warning: Failed to open serial device {device.name}: {e}")
        ser = None

    try:
        for command in prerun_commands:
            command.run(ser, global_env)

        for _ in range(execution_count):
            print(f"Starting execution {_ + 1} of {execution_count}")
            for command in normal_commands:
                command.run(ser, global_env)
    finally:
        global extra_uart_running
        extra_uart_running = False
        if ser:
            ser.close()

def IP_handler(response, global_env):
    match = re.search(r'ip=([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)', response)
    if match:
        global_env.own_ip = match.group(1)
        print(f"Extracted IP: {global_env.own_ip}")
    else:
        print("No IP address found in response.")

def ping_attri_handler(instance, global_env):
    assembled_command = instance.text
    #attributes = instance.attri
    attributes = instance.attri.text.split()
    print(f"attribute is {attributes}");
    if "DST_IP" in attributes:
        assembled_command += f" {global_env.dst_ip}"
    elif "DUT_IP" in attributes:
        assembled_command += f" {global_env.own_ip}"
        print(f"Get DUTIP and ping: {global_env.own_ip}, cmd {assembled_command}")
    if "PING_PARAM" in attributes:
        assembled_command += f" {global_env.ping_param}"
    print(f"Get DUTIP and ping: return cmd {assembled_command}")
    return assembled_command

def iperf_attri_handler(instance, global_env):
    assembled_command = instance.text
    attributes = instance.attri
    if "DST_IP" in attributes:
        assembled_command += f" {global_env.dst_ip}"
    if "PING_PARAM" in attributes:
        assembled_command += f" {global_env.ping_param}"

def print_usage():
    print("Usage: python auto-test.py <config_file>")
    print("Options:")
    print("  <config_file>: Path to the XML configuration file.")

def main():
    if len(sys.argv) != 2 or sys.argv[1] in ('-h', '--help'):
        print_usage()
        sys.exit(1)

    config_file = sys.argv[1]

    try:
        tree = ET.parse(config_file)
        root = tree.getroot()
        loop_count = int(root.get('loop_count', 1))
        readback = root.get('readback', 'no')

        config_files = [config.text for config in root.findall('config_file') if config.get('execute', 'yes').lower() == 'yes']

        for _ in range(loop_count):
            for config_file in config_files:
                execution_count, global_env, device, commands = parse_config(config_file)
                execute_commands(execution_count, global_env, device, commands, readback, False)
    except Exception as e:
        print(f"Error: {e}")
        print_usage()
        sys.exit(1)

if __name__ == "__main__":
    main()

# UART Command Sender
Author: "pyang \<caoerk@hotmail.com\>"
## Description

Send commands to a UART device using configurations from an XML file.

## Usage

```bash
python auto-test.py <master_config.xml>
- '<master_config.xml>': Path to the master configuration XML file.

### Master Config XML

The master XML configuration file should contain:
- `loop_count`: The number of times to execute the sequence of sub-configurations.
- `readback`: Attribute to indicate if the UART output should be read back during execution (yes/no).
- `config_file`: Path to sub-config XML file to execute.
  - `execute`: Attribute to indicate if the sub-config should be executed (yes/no).

### Example Master XML

```xml
<master_config loop_count="2" readback="yes">
    <config_file execute="yes">config1.xml</config_file>
    <config_file execute="no">config2.xml</config_file>
</master_config>

### Sub-Config XML

The sub XML configuration file should contain:
- 'execution_count': Number of times to execute the sequence of commands.
- 'device': Serial device path (e.g., /dev/ttyUSB0 for Linux or COM1 for Windows).
  - 'baudrate': Baud rate for serial communication (e.g., 115200).
  - 'parity': Parity setting (e.g., none, even, odd).
  - 'data_bits': Number of data bits (e.g., 8).
  - 'stop_bits': Number of stop bits (e.g., 1).
- 'commands': List of commands to send.
  - 'text': The command text to send.
  - 'repeat': Number of times to repeat the command.
  - 'interval': Time interval between command repeats (in seconds).
  - 'recv_interval': Time interval to wait for the response (in seconds, optional, defaults to interval).
  - 'execute': Attribute to indicate if the command should be executed (yes/no).

### Example sub-config XML

```xml
<config>
    <execution_count>1</execution_count>
    <device>
        <name>/dev/ttyUSB0</name>
        <baudrate>115200</baudrate>
        <parity>none</parity>
        <data_bits>8</data_bits>
        <stop_bits>1</stop_bits>
    </device>
    <commands>
        <command execute="yes">
            <text>AT+CMD1</text>
            <repeat>1</repeat>
            <interval>1.0</interval>
            <recv_interval>0.5</recv_interval>
        </command>
        <command execute="yes">
            <text>AT+CMD2</text>
            <repeat>2</repeat>
            <interval>0.5</interval>
            <recv_interval>0.2</recv_interval>
        </command>
    </commands>
</config>

### File list
1. auto-test.py
2. *.xml
3. mk_py_bin (script to make the python script a standalone executible bin) 

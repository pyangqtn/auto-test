# auto-iperf.py

class IperfHandler:
    def __init__(self, iperf_type, iperf_mode, iperf_itvl, iperf_port, iperf_time, iperf_dstip, iperf_rate):
        self.iperf_type = iperf_type
        self.iperf_mode = iperf_mode
        self.iperf_itvl = iperf_itvl
        self.iperf_port = iperf_port
        self.iperf_time = iperf_time
        self.iperf_dstip = iperf_dstip
        self.iperf_rate = iperf_rate

    def __str__(self):
        return (f"IperfHandler(iperf_type={self.iperf_type}, iperf_mode={self.iperf_mode}, "
                f"iperf_itvl={self.iperf_itvl}, iperf_port={self.iperf_port}, iperf_time={self.iperf_time}, "
                f"iperf_dstip={self.iperf_dstip}, iperf_rate={self.iperf_rate})")

def iperf_attri_handler(instance, global_env):
    iperf_elem = instance.attri

    if iperf_elem is None:
        return None

    iperf_type = iperf_elem.find('iperf_type').text if iperf_elem.find('iperf_type') is not None else "client"
    iperf_mode = iperf_elem.find('iperf_mode').text if iperf_elem.find('iperf_mode') is not None else "TCP"
    iperf_itvl = int(iperf_elem.find('iperf_itvl').text) if iperf_elem.find('iperf_itvl') is not None else 1
    iperf_port = int(iperf_elem.find('iperf_port').text) if iperf_elem.find('iperf_port') is not None else 5001
    iperf_time = int(iperf_elem.find('iperf_time').text) if iperf_elem.find('iperf_time') is not None else 10
    iperf_dstip = iperf_elem.find('iperf_dstip').text if iperf_elem.find('iperf_dstip') is not None else global_env.dst_ip
    iperf_rate = iperf_elem.find('iperf_rate').text if iperf_elem.find('iperf_rate') is not None else ""

    if iperf_type.lower() == "server":
        assembled_command = f"iperf -s -i {iperf_itvl} -p {iperf_port}"
    else:
        assembled_command = f"iperf -c {iperf_dstip} -i {iperf_itvl} -p {iperf_port} -t {iperf_time}"

    if iperf_mode.upper() == "UDP":
        assembled_command += " -u"
        if iperf_rate:
            assembled_command += f" -b {iperf_rate}"

    print(f"Iperf command: {assembled_command}")
    return assembled_command


import subprocess as sub
import re
import time
import enum
import datetime
import traceback
import logging


class CollectorException(Exception):
    pass



def unit_conversion(number: str) -> float:
    try:
        unit = number[-1]
        str_num = number[:-1]
        if ',' in str_num:
            str_num = str_num.replace(',', '.')
        num = float(str_num)
        case = {
            'K': 0.0009765625,
            'M': 1.0,
            'G': 1024.0,
            'T': 1048576.0,
        }
        return num * case[unit]
    except (KeyError, IndexError, ValueError):
        return -1.0


class JournalLogCollector:
    header = ('date', 'hostname', 'process', 'error_desc')

    class Priority(enum.Enum):
        EMERGENCY = 0
        ALERT = 1
        CRITICAL = 2
        ERROR = 3
        WARNING = 4
        NOTICE = 5
        INFO = 6
        DEBUG = 7

    def __init__(self):
        self.command = 'journalctl'
        self.args = {}
        self.__init_args()

    def __init_args(self):
        self.args['--no-pager'] = None
        self.set_output('short-iso')

    def clean(self):
        self.args.clear()
        self.__init_args()

    def __parse_lines(self, lines):
        out = []
        for line in lines:
            try:
                a = re.search('(\S+) (\S+) (\S+): (.*)', line)
                out.append((a.group(1), a.group(2), a.group(3), a.group(4)))
            except AttributeError:
                pass
        return out

    def __args_to_string(self):
        str_ = self.command
        for key, value in self.args.items():
            str_ += ' '
            if value is not None:
                str_ += '{} {}'.format(str(key), str(value))
            else:
                str_ += str(key)
        return str_

    def collect(self):
        raw_data, error = sub.Popen(self.__args_to_string(), stderr=sub.PIPE, stdout=sub.PIPE, shell=True).communicate()
        lines = str(raw_data, 'utf-8').split('\n')
        return self.__parse_lines(lines)

    def set_priority(self, priority: Priority):
        self.args['-p'] = priority.value

    def set_limit(self, n=10):
        self.args['-n'] = n

    def set_reverse(self):
        self.args['-r'] = None

    def set_utc(self):
        self.args['--utc'] = None

    def set_output(self, output='json'):
        self.args['--output'] = output

    def set_since_date(self, date):
        self.args['--since'] = "\'" + str(date) + "\'"

    def set_from_config(self, config):
        self.set_priority(self.Priority[config.priority])
        self.set_limit(config.limit)
        if config.reverse:
            self.set_reverse()


class SystemDataCollector:
    def get_hostname(self):
        try:
            return str(self.__exec_sys_command('hostname', '-s').stdout, 'utf-8')[:-1]
        except sub.CalledProcessError as ex:
            raise CollectorException('get_hostname', ex)

    def get_macs(self):
        try:
            data = str(self.__exec_sys_command('ip', 'link').stdout, 'utf-8')
            i_name = re.findall('\d: (\w+): ', data)[1:]
            i_mac = re.findall('((?:[0-9A-Fa-f]{2}[:-]){5}(?:[0-9A-Fa-f]{2})) brd', data)[1:]
            return list(zip(i_name, i_mac))
        except Exception as ex:
            raise CollectorException('get_macs', ex)

    def get_mac(self):
        return self.get_macs()[0][1]

    def get_temp(self):
        try:
            temp_file = open("/sys/class/thermal/thermal_zone0/temp", "r")
            temp = temp_file.read()
            temp_file.close()
            return float(int(temp) / 1000)
        except Exception as ex:
            raise CollectorException("error reading temp", ex)

    # unints - mb
    def ram_usage(self):
        try:
            ram_data = self.__exec_sys_command("free", "-m")
            ram_data = ram_data.stdout
            total_mem, used_mem = self.__format_total_and_used_ram(str(ram_data))
            return int(total_mem), int(used_mem)

        
        except Exception as ex:
            raise CollectorException("ram_usage error", ex)


    def __format_total_and_used_ram(self, raw_data):
        mem_data = raw_data.split("\\n")
        mem_data = mem_data[1]
        a = re.split(' ', mem_data)
        filter_data = list(filter(lambda x: x != '', a))
        return filter_data[1], filter_data[2]

    def drive_space(self):
        """:returns list of tuples with name,size,used_size [GB] eg. [(a1,200G,120G),(b2,30M,12M)]"""
        try:
            raw_drive_data = self.__exec_sys_command("df", "-h")
            raw_drive_data = raw_drive_data.stdout
            drive_data = self.__format_drive_space_data(raw_drive_data)
            return drive_data
        except Exception as ex:
            raise CollectorException("drive_space", ex)

    def __format_drive_space_data(self, raw_data_str):
        raw_data_list = raw_data_str.split(b"\n")
        sd_list = list(filter(lambda x: b"/dev/sd" in x, raw_data_list))
        ret_list = []
        for dt in sd_list:
            filtered_spaces = list(filter(lambda x: x != b'', re.split(b' ', dt)))
            name = filtered_spaces[0][5:]
            size = filtered_spaces[1]
            used = filtered_spaces[2]
            ret_list.append(
                (name.decode("utf-8"), unit_conversion(size.decode("utf-8")), unit_conversion(used.decode("utf-8"))))
        return ret_list

    def processor_usage(self):
        try:
            raw_data = self.__exec_sys_command("top", "-bn1")
            line = str(raw_data.stdout, 'utf-8').split('\n', 3)[2]
            cpu = re.search('%Cpu\(s\): {1,}(\S+).us, {1,}(\S+).sy, {1,}(\S+).ni', line)
            return tuple([float(cpu.group(i).replace(',', '.')) for i in range(1, 4)])
        except Exception as ex:
            raise CollectorException("processor_usage", ex)

    def drive_operations(self):
        """:returns list of tuples with name,read/sec,write/sec"""
        try:
            raw_data = self.__exec_sys_command("iostat", "-dx")
            raw_data = raw_data.stdout
            return self.__format_drive_operations(raw_data)
        except Exception as ex:
            raise CollectorException("drive_operations", ex)

    def __format_drive_operations(self, raw_str):
        split_data = raw_str.split(b"\n")[3:]
        ret_list = []
        split_data = list(filter(lambda x: x != b'', split_data))
        for x in split_data:
            filt_str = list(filter(lambda y: y != b'', re.split(b' ', x)))
            name = filt_str[0]
            r_sec = filt_str[3]
            w_sec = filt_str[4]
            ret_list.append((name.decode("utf-8"), float(r_sec.decode("utf-8").replace(',', '.')),
                             float(w_sec.decode("utf-8").replace(',', '.'))))
        return ret_list

    def interface_load(self):
        """:returns list of tuples with name,rec/sec,trans/sec"""

        def diff(el1, el2):
            n, r1, t1 = el1
            n, r2, t2 = el2
            return (n, r2 - r1, t2 - t1)

        try:
            first_list = self.__get_curr_intf_load()
            time.sleep(1)
            second_list = self.__get_curr_intf_load()
            return list(map(diff, first_list, second_list))
        except Exception as ex:
            raise CollectorException("interface_load", ex)

    def __get_curr_intf_load(self):
        try:
            raw_data = self.__exec_sys_command("netstat", "-i")
            raw_data = raw_data.stdout
            return self.__format_intf_load(raw_data)
        except:
            raise Exception

    def __format_intf_load(self, raw_str):
        split_data = raw_str.split(b"\n")[2:]
        split_data = list(filter(lambda x: x != b'', split_data))
        ret_list = []
        for x in split_data:
            filt_str = list(filter(lambda y: y != b'', re.split(b' ', x)))
            name = filt_str[0]
            received = filt_str[1]
            transmitted = filt_str[7]
            transmitted_int = int(transmitted.decode("utf-8"))
            received_int = int(received.decode("utf-8"))
            ret_list.append((name.decode("utf-8"), received_int, transmitted_int))
        return ret_list

    def __exec_sys_command(self, command, args):
        raw_data = sub.run([command, args], stdout=sub.PIPE)
        raw_data.check_returncode()
        return raw_data


if __name__ == '__main__':

    d = datetime.datetime(2009, 10, 5, 18, 00)
    collector = JournalLogCollector()
    collector.set_reverse()
    collector.set_limit(n=5)
    # collector.set_since_date(d)
    collector.set_priority(JournalLogCollector.Priority.WARNING)
    for log in collector.collect():
        print(log)

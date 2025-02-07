"""
Copyright (C) 2017 Sergej Schumilo

This file is part of kAFL Fuzzer (kAFL).

QEMU-PT is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 2 of the License, or
(at your option) any later version.

QEMU-PT is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with QEMU-PT.  If not, see <http://www.gnu.org/licenses/>.
"""

import mmap
import os
import sys
import random
import re
import resource
import select
import socket
import struct
import subprocess
import time
from socket import error as socket_error
import psutil
import mmh3

from common.debug import log_qemu, log_exception
from common.util import atomic_write

from common.util import Singleton
from multiprocessing import Process, Manager


def to_string_32(value):
    return chr((value >> 24) & 0xff) + \
           chr((value >> 16) & 0xff) + \
           chr((value >> 8) & 0xff) + \
           chr(value & 0xff)

class QemuLookupSet:
    __metaclass__ = Singleton

    def __init__(self):
        manager = Manager()
        self.non_finding = manager.dict()
        self.crash = manager.dict()
        self.timeout = manager.dict()
        self.kasan = manager.dict()

class ControlSocketDebugger(object) :
    """MITM the control socket for debugging"""
    def log(self, msg, *args) :
        s = "sock %r: %s" % (self.__hash__(), msg % args)
        log_qemu(s, 0)
    def __init__(self) :
        self.sock = socket.socket(socket.AF_UNIX)
        self.log("create")
    def connect(self, fn) :
        self.log("connect %s", fn)
        try :
            x = self.sock.connect(fn)
            self.log("connected")
            return x
        except Exception,e :
            self.log("connect error %r", e)
            raise e
    def recv(self, n) :
        #self.log("recv want %d", n)
        try :
            x = self.sock.recv(n)
            self.log("recv got %r", x)
            return x
        except Exception,e :
            self.log("recv exception %r", e)
            raise e
    def send(self, x) :
        self.log("sent %r", x)
        try :
            n = self.sock.send(x)
            #self.log("sent %d", n)
            return n
        except Exception,e :
            self.log("send exception %r", e)
            raise e
    def settimeout(self, *arg) :
        return self.sock.settimeout(*arg)
    def close(self, *arg) :
        return self.sock.close(*arg)

class qemu:
    SC_CLK_TCK = os.sysconf(os.sysconf_names['SC_CLK_TCK'])

    def __init__(self, qid, config):

        self.global_bitmap = None
        self.global_bitmap_fd = None

        self.lookup = QemuLookupSet()

        self.bitmap_size = config.config_values['BITMAP_SHM_SIZE']
        self.config = config
        self.qemu_id = str(qid)

        self.process = None
        self.intervm_tty_write = None
        self.control = None
        self.control_fileno = None

        self.payload_filename   = "/dev/shm/kafl_qemu_payload_" + self.qemu_id
        self.binary_filename    = "/dev/shm/kafl_qemu_binary_"  + self.qemu_id
        self.argv_filename      = "/dev/shm/kafl_argv_"         + self.qemu_id
        self.bitmap_filename    = "/dev/shm/kafl_bitmap_"       + self.qemu_id
        if self.config.argument_values.has_key('work_dir'):
            self.control_filename   = self.config.argument_values['work_dir'] + "/kafl_qemu_control_"  + self.qemu_id
        else:
            self.control_filename   = "/tmp/kafl_qemu_control_"  + self.qemu_id
        self.start_ticks = 0
        self.end_ticks = 0
        self.tick_timeout_treshold = self.config.config_values["TIMEOUT_TICK_FACTOR"]

        self.cmd =  self.config.config_values['QEMU_KAFL_LOCATION'] + " " \
                    "-hdb " + self.config.argument_values['ram_file'] + " " \
                    "-hda " + self.config.argument_values['overlay_dir'] +  "/overlay_" + self.qemu_id + ".qcow2 " \
                    "-serial mon:stdio " \
                    "-enable-kvm " \
                    "-k de " \
                    "-m " + str(config.argument_values['mem']) + " " \
                    "-nographic " \
                    "-net user " \
                    "-net nic " \
                    "-chardev socket,server,nowait,path=" + self.control_filename + \
                    ",id=kafl_interface " \
                    "-device kafl,chardev=kafl_interface,bitmap_size=" + str(self.bitmap_size) + ",shm0=" + self.binary_filename + \
                    ",shm1=" + self.payload_filename + \
                    ",bitmap=" + self.bitmap_filename

        for i in range(1):
            key = "ip" + str(i)
            if self.config.argument_values.has_key(key) and self.config.argument_values[key]:
                range_a = hex(self.config.argument_values[key][0]).replace("L", "")
                range_b = hex(self.config.argument_values[key][1]).replace("L", "") 
                self.cmd += ",ip" + str(i) + "_a=" + range_a + ",ip" + str(i) + "_b=" + range_b
                self.cmd += ",filter" + str(i) + "=/dev/shm/kafl_filter" + str(i)
                    
        self.cmd += " -loadvm " + self.config.argument_values["S"] + " "

        if self.config.argument_values["macOS"]:
            self.cmd = self.cmd.replace("-net user -net nic", "-netdev user,id=hub0port0 -device e1000-82545em,netdev=hub0port0,id=mac_vnet0 -cpu Penryn,kvm=off,vendor=GenuineIntel -device isa-applesmc,osk=\"" + self.config.config_values["APPLE-SMC-OSK"].replace("\"", "") + "\" -machine pc-q35-2.4")
            if qid == 0:
                self.cmd = self.cmd.replace("-machine pc-q35-2.4", "-machine pc-q35-2.4 -redir tcp:5901:0.0.0.0:5900 -redir tcp:10022:0.0.0.0:22")
        elif self.config.argument_values["zircon"]:
            self.cmd = re.sub("-hda [^ ]*", "", self.cmd)
            self.cmd += "-machine q35 " \
                        "-device isa-debug-exit,iobase=0xf4,iosize=0x04 " \
                        "-cpu Haswell,+smap,-check,-fsgsbase"
        else:
            self.cmd += " -machine pc-i440fx-2.6 "

        self.kafl_shm   = None
        self.fs_shm     = None

        self.e = select.epoll()
        self.crashed = False
        self.timeout = False
        self.kasan = False
        self.shm_problem = False
        self.initial_mem_usage = 0

        self.stat_fd = None

        if qid == 0:
            log_qemu("Launching Virtual Maschine...CMD:\n" + self.cmd, self.qemu_id)
        else:
            log_qemu("Launching Virtual Maschine...", self.qemu_id)
        self.virgin_bitmap = ''.join(chr(0xff) for x in range(self.bitmap_size))

        self.__set_binary(self.binary_filename, self.config.argument_values['executable'], (16 << 20))


    def __del__(self):
        log_qemu("kill qemu pid %d" % self.process.pid, self.qemu_id)
        os.system("kill -9 " + str(self.process.pid))

        try:
            if self.process:
                try:
                    self.process.kill()
                except:
                    log_exception()

            if self.e:
                if self.control_fileno:
                    self.e.unregister(self.control_fileno)

            if self.intervm_tty_write:
                self.intervm_tty_write.close()
            if self.control:
                self.control.close()
        except OSError:
            log_exception()
            pass

        try:
            self.kafl_shm.close()
        except:
            log_exception()
            pass

        try:
            self.fs_shm.close() 
        except:
            log_exception()
            pass

        try:
            if self.stat_fd:
                self.stat_fd.close()
        except:
            log_exception()
            pass

        if self.global_bitmap is not None :
            self.global_bitmap.close()
            self.global_bitmap = None

        if self.global_bitmap_fd is not None :
            os.close(self.global_bitmap_fd)
            self.global_bitmap_fd = None

    def __get_pid_guest_ticks(self):
        if self.stat_fd:
            self.stat_fd.seek(0)
            self.stat_fd.flush()
            return int(self.stat_fd.readline().split(" ")[42])
        return 0

    def __set_binary(self, filename, binaryfile, max_size):
        shm_fd = os.open(filename, os.O_RDWR | os.O_SYNC | os.O_CREAT)
        os.ftruncate(shm_fd, max_size)
        shm = mmap.mmap(shm_fd, max_size, mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ)
        shm.seek(0x0)
        shm.write('\x00' * max_size)
        shm.seek(0x0)

        f = open(binaryfile, "rb")
        bytes = f.read(1024)
        if bytes:
            shm.write(bytes)
        while bytes != "":
            bytes = f.read(1024)
            if bytes:
                shm.write(bytes)

        f.close()
        shm.flush()
        shm.close()
        os.close(shm_fd)

    def set_tick_timeout_treshold(self, treshold):
        self.tick_timeout_treshold = treshold

    def start(self, verbose=False, serout=None):
        if verbose:
            self.process = subprocess.Popen(filter(None, self.cmd.split(" ")),
                                            stdin=None,
                                            stdout=serout, #None,
                                            stderr=None)
        else:
            if serout is None :
                serout = subprocess.PIPE
            self.process = subprocess.Popen(filter(None, self.cmd.split(" ")),
                                            stdin=subprocess.PIPE,
                                            stdout=serout, #subprocess.PIPE,
                                            stderr=subprocess.PIPE)

        log_qemu("process pid %d" % self.process.pid, self.qemu_id)
        self.stat_fd = open("/proc/" + str(self.process.pid) + "/stat")
        self.init()
        try:
            self.set_init_state()
        except:
            log_exception()
            return False
        self.initial_mem_usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        #time.sleep(1)
        self.kafl_shm.seek(0x0)
        self.kafl_shm.write(self.virgin_bitmap)
        self.kafl_shm.flush()
        return True

    def set_init_state(self):
        # since we've reloaded the VM - let's assume there is no panic state...
        self.crashed = False
        self.timeout = False
        self.kasan = False
        self.start_ticks = 0
        self.end_ticks = 0

        # wait for initial hypercall_acquire from vm
        self.control.settimeout(10.0)
        v = self.control.recv(1)
        log_qemu("Initial stage 1 handshake ["+ str(v) + "] done...", self.qemu_id)
        self.__set_binary(self.binary_filename, self.config.argument_values['executable'], (16 << 20))
        if v != 'D':
            raise Exception("this better not happen! v = %r" % v)
            #self.control.send('R')
            #v = self.control.recv(1)
            #log_qemu("Initial stage 2 handshake ["+ str(v) + "] done...", self.qemu_id)

        # now wait for the hypercall_next_payload from vm before continuing
        v = self.control.recv(1)
        log_qemu("Initial stage 3 handshake ["+ str(v) + "] done...", self.qemu_id)
        self.control.settimeout(5.0)

    def init(self):
        self.control = socket.socket(socket.AF_UNIX)
        #self.control = ControlSocketDebugger()
        while True:
            try:
                self.control.connect(self.control_filename)
                break
            except socket_error:
                #log_exception()
                time.sleep(0.01)

        kafl_shm_f     = os.open(self.bitmap_filename, os.O_RDWR | os.O_SYNC | os.O_CREAT)
        fs_shm_f       = os.open(self.payload_filename, os.O_RDWR | os.O_SYNC | os.O_CREAT)
        #argv_fd             = os.open(self.argv_filename, os.O_RDWR | os.O_SYNC | os.O_CREAT)
        os.ftruncate(kafl_shm_f, self.bitmap_size)
        os.ftruncate(fs_shm_f, (128 << 10))
        #os.ftruncate(argv_fd, (4 << 10))

        self.kafl_shm       = mmap.mmap(kafl_shm_f, self.bitmap_size, mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ)
        self.fs_shm         = mmap.mmap(fs_shm_f, (128 << 10),  mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ)
        os.close(kafl_shm_f) #XXX
        os.close(fs_shm_f) #XXX

        return True

    def soft_reload(self):
        log_qemu("soft reload", self.qemu_id)
        self.crashed = False
        self.timeout = False
        self.kasan = False
        self.start_ticks = 0
        self.end_ticks = 0
        self.control.settimeout(10.0)

        # ask qemu to reload the VM and wait for acknowledgement
        self.control.send('L')
        while True:
            ch = self.control.recv(1)
            if ch == 'L' or ch == '' :
                break

        # wait for the initial handshake (acquire)
        v = self.control.recv(1)
        self.__set_binary(self.binary_filename, self.config.argument_values['executable'], (16 << 20))
        if v != 'D':
            raise Exception("this better not happen! v == %r" % v)
            #self.control.send('R')
            #v = self.control.recv(1)

        # now wait for the hypercall_next_payload from vm before continuing
        v = self.control.recv(1)
        self.control.settimeout(5.0)

    # Return Codes: OK, CRASH, TIMEOUT, KASAN, EOF
    def check_recv(self, timeout_detection=True):
        log_qemu("check recv", self.qemu_id)
        if timeout_detection:
            self.control.settimeout(1.25)
        try:
            result = self.control.recv(1)
        #except socket_error, e:
        except socket.timeout, e:
            log_exception()
            #raise Exception("XXX DEBUG TIMEOUT")
            return "TIMEOUT"

        log_qemu("check recv got %r" % result, self.qemu_id)
        if result == 'C':
            return "CRASH"
        elif result == 'K':
            return "KASAN"
        elif result == 'R':
            return "OK"
            log_qemu("Finding...Type is ["+ result + "]", self.qemu_id)
        elif result == '':
            return "EOF"
        log_qemu("unexpected value on controls ocket %r" % result)
        raise Exception("unexpected value on control socket %r" % result)
        #return 2

    def send_payload(self, timeout_detection=True):
        """Send a test case to qemu, wait for a response, and read back the coverage map"""
        log_qemu("send payload", self.qemu_id)
        self.start_ticks = self.__get_pid_guest_ticks()
        try:
            # vm was waiting for next payload, unlock them now that its been provided
            self.control.send("R")
        except OSError:
            log_exception()
            log_qemu("Failed to send payload...", self.qemu_id)
            return None

        self.crashed = False
        self.timeout = False
        self.kasan = False

        if timeout_detection:
            counter = 0
            while True:
                value = self.check_recv()
                if value == "TIMEOUT":
                    self.end_ticks = self.__get_pid_guest_ticks()
                    if (self.end_ticks-self.start_ticks) >= self.tick_timeout_treshold:
                        break
                    if counter >= 10:
                    	break
                    counter += 1
                else:
                    break
            self.end_ticks = self.__get_pid_guest_ticks()
        else:
            value = self.check_recv(timeout_detection=False)
        log_qemu("check_recv val %s" % value, self.qemu_id)
        if value == "CRASH":
            self.crashed = True
            self.finalize_iteration()
        elif value == "TIMEOUT":
            self.timeout = True
            self.finalize_iteration()
        elif value == "KASAN":
            self.kasan = True
            self.finalize_iteration()
        elif value == "EOF":
            log_qemu("QEMU control channel unexpectedly shut down!", self.qemu_id)
            self.timeout = True # XXX was really qemu shutting down!
            self.finalize_iteration()
        self.kafl_shm.seek(0x0)
        return self.kafl_shm.read(self.bitmap_size)

    def enable_sampling_mode(self):
        self.control.send("S")

    def disable_sampling_mode(self):
        self.control.send("O")

    def submit_sampling_run(self):
        self.control.send("T")

    def copy_master_payload(self, shm, num, size):
        self.fs_shm.seek(0)
        shm.seek(size * num)
        payload = shm.read(size)
        self.fs_shm.write(payload)
        self.fs_shm.write(''.join(chr(0x00) for x in range((64 << 10)-size)))
        self.fs_shm.flush()
        return payload, size

    def copy_mapserver_payload(self, shm, num, size):
        self.fs_shm.seek(0)
        shm.seek(size * num)
        shm.write(self.fs_shm.read(size))
        shm.flush()

    def open_global_bitmap(self):
        self.global_bitmap_fd = os.open(self.config.argument_values['work_dir'] + "/bitmap", os.O_RDWR | os.O_SYNC | os.O_CREAT)
        os.ftruncate(self.global_bitmap_fd, self.bitmap_size)
        self.global_bitmap = mmap.mmap(self.global_bitmap_fd, self.bitmap_size, mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ)

    def verifiy_input(self, payload, bitmap, payload_size, runs=3):
    	crashed = self.crashed
    	timeout = self.timeout
    	kasan = self.kasan
        failed = False
        try:
            self.enable_sampling_mode()
            init = True
            tmp_bitmap1 = bitmap
            for i in range(runs):
                if not init:
                    self.fs_shm.seek(0)
                    self.fs_shm.write(payload)
                    self.fs_shm.write(''.join(chr(0x00) for x in range((64 << 10)-payload_size)))
                    self.fs_shm.flush()
                    tmp_bitmap1 = self.send_payload(timeout_detection=False)
                    if (self.crashed or self.kasan or self.timeout):
                        break
                    else:
                        self.submit_sampling_run()

                self.fs_shm.seek(0)
                self.fs_shm.write(payload)
                self.fs_shm.write(''.join(chr(0x00) for x in range((64 << 10)-payload_size)))
                self.fs_shm.flush()
                tmp_bitmap2 = self.send_payload(timeout_detection=False)
                if (self.crashed or self.kasan or self.timeout):
                    break
                else:
                    self.submit_sampling_run()
                if tmp_bitmap1 == tmp_bitmap2:
                    break
                init = False
                
        except:
            log_exception()
            failed = True

        self.crashed = crashed or self.crashed
        self.timeout = timeout or self.timeout
        self.kasan = kasan or self.kasan

        try:
            if not self.timeout:
                self.submit_sampling_run()
            self.disable_sampling_mode()
            if not failed:
                return tmp_bitmap2
            else:
                return bitmap            
        except:
            log_exception()
            self.timeout = True
            return bitmap

    def check_for_unseen_bits(self, bitmap):
        if not self.global_bitmap:
            self.open_global_bitmap()

        for i in range(self.bitmap_size):
            if bitmap[i] != '\xff':
                if self.global_bitmap[i] == '\x00':
                    return True
                if (ord(bitmap[i]) | ord(self.global_bitmap[i])) != ord(self.global_bitmap[i]):
                    return True
        return False

    
    def copy_bitmap(self, shm, num, size, bitmap, payload, payload_size, effector_mode=False):
        new_hash = mmh3.hash64(bitmap)
        if not (self.crashed or self.kasan or self.timeout):
            if new_hash in self.lookup.non_finding:
                if effector_mode:
                    shm.seek(size * num)
                    shm.write(bitmap)
                    shm.flush()
                    return True
                else:
                    shm.seek((size * num) + len(bitmap))
                    return False

        if not (self.crashed or self.kasan or self.timeout) and not self.check_for_unseen_bits(bitmap):
            self.lookup.non_finding[new_hash] = None
            return False
        if not (self.timeout):
            bitmap = self.verifiy_input(payload, bitmap, payload_size)
            shm.seek(size * num)
            shm.write(bitmap)
            shm.flush()
        self.lookup.non_finding[new_hash] = None
        return True

    def set_payload(self, payload):
        #log_qemu("set payload", self.qemu_id)
        self.fs_shm.seek(0)
        self.fs_shm.write(struct.pack('<I', len(payload)))
        self.fs_shm.write(payload)
        self.fs_shm.flush()
        #log_qemu("done set payload: %s" % payload[:16].encode('hex'), self.qemu_id)

    def finalize_iteration(self):
        try:
            self.control.send('F')
            self.control.recv(1)
        except:
            log_exception()
            log_qemu("finalize_iteration failed...", self.qemu_id)


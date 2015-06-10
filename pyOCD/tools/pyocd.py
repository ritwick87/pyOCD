#!/usr/bin/env python
"""
 mbed CMSIS-DAP debugger
 Copyright (c) 2015 ARM Limited

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

     http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""

import argparse
import sys
import logging

import os
import pyOCD
from pyOCD.board import MbedBoard
from pyOCD.target import target_kinetis

# Make disasm optional.
try:
    import capstone

    isCapstoneAvailable = True
except ImportError:
    isCapstoneAvailable = False

LEVELS={'debug':logging.DEBUG,
        'info':logging.INFO,
        'warning':logging.WARNING,
        'error':logging.ERROR,
        'critical':logging.CRITICAL
        }

## Default SWD clock in kHz.
DEFAULT_CLOCK_FREQ_KHZ = 1000

def dumpHexData(data, startAddress=0, width=8):
    i = 0
    while i < len(data):
        print "%08x: " % (startAddress + i),

        while i < len(data):
            d = data[i]
            i += 1
            if width==8:
                print "%02x" % d,
                if i % 4 == 0:
                    print "",
                if i % 16 == 0:
                    break
            elif width==16:
                print "%04x" % d,
                if i % 8 == 0:
                    break
            elif width==32:
                print "%08x" % d,
                if i % 4 == 0:
                    break
        print

class ToolError(Exception):
    pass

class PyOCDConsole(object):
    PROMPT = '>>> '

    def __init__(self, tool):
        self.tool = tool
        self.last_command = ''

    def run(self):
        try:
            while True:
                line = raw_input(self.PROMPT)
                line = line.strip()
                if line:
                    self.process_command_line(line)
                    self.last_command = line
                elif self.last_command:
                    self.process_command(self.last_command)
        except EOFError:
            print

    def process_command_line(self, line):
        for cmd in line.split(';'):
            self.process_command(cmd)

    def process_command(self, cmd):
        try:
            args = cmd.split()
            cmd = args[0].lower()
            args = args[1:]

            # Handle help.
            if cmd in ['?', 'help']:
                self.show_help(args)
                return

            # Handle register name as command.
            if cmd in pyOCD.target.cortex_m.CORE_REGISTER:
                self.tool.handle_reg([cmd])
                return

            # Check for valid command.
            if cmd not in self.tool.command_list:
                print "Error: unrecognized command '%s'" % cmd
                return

            # Run command.
            handler = self.tool.command_list[cmd]
            handler(args)
        except pyOCD.transport.transport.TransferError:
            print "Error: transfer failed"
        except ToolError, e:
            print "Error:", e

    def show_help(self, args):
        if not args:
            self.list_commands()

    def list_commands(self):
        cmds = sorted(self.tool.command_list.keys())
        print "Commands:\n---------"
        print '\n'.join(cmds)

class PyOCDTool(object):
    def __init__(self):
        self.board = None
        self.exitCode = 0
        self.command_list = {
                'list' :    self.handle_list,
                'erase' :   self.handle_erase,
                'unlock' :  self.handle_unlock,
                'info' :    self.handle_info,
                'i' :       self.handle_info,
                'status' :  self.handle_status,
                'reg' :     self.handle_reg,
                'reset' :   self.handle_reset,
                'read' :    self.handle_read8,
                'read8' :   self.handle_read8,
                'read16' :  self.handle_read16,
                'read32' :  self.handle_read32,
                'r' :       self.handle_read8,
                'r8' :      self.handle_read8,
                'r16' :     self.handle_read16,
                'r32' :     self.handle_read32,
                'write' :   self.handle_write8,
                'write8' :  self.handle_write8,
                'write16' : self.handle_write16,
                'write32' : self.handle_write32,
                'w' :       self.handle_write8,
                'w8' :      self.handle_write8,
                'w16' :     self.handle_write16,
                'w32' :     self.handle_write32,
                'go' :      self.handle_go,
                'g' :       self.handle_go,
                'step' :    self.handle_step,
                's' :       self.handle_step,
                'halt' :    self.handle_halt,
                'h' :       self.handle_halt,
                'disasm' :  self.handle_disasm,
                'd' :       self.handle_disasm,
                'map' :     self.handle_memory_map,
                'log' :     self.handle_log,
                'clock' :   self.handle_clock
            }

    def get_args(self):
        debug_levels = LEVELS.keys()

        epi = "Available commands:\n" + ', '.join(sorted(self.command_list.keys()))

        parser = argparse.ArgumentParser(description='Target inspection utility', epilog=epi)
        parser.add_argument("-H", "--halt", action="store_true", help="Halt core upon connect.")
        parser.add_argument('-k', "--clock", metavar='KHZ', default=DEFAULT_CLOCK_FREQ_KHZ, type=int, help="Set SWD speed in kHz. (Default 1 MHz.)")
        parser.add_argument('-b', "--board", action='store', metavar='ID', help="Use the specified board. ")
        parser.add_argument('-t', "--target", action='store', metavar='TARGET', help="Override target.")
        parser.add_argument("-d", "--debug", dest="debug_level", choices=debug_levels, default='warning', help="Set the level of system logging output. Supported choices are: "+", ".join(debug_levels), metavar="LEVEL")
        parser.add_argument("cmd", nargs='?', default=None, help="Command")
        parser.add_argument("args", nargs='*', help="Arguments for the command.")
        return parser.parse_args()

    def configure_logging(self):
        level = LEVELS.get(self.args.debug_level, logging.WARNING)
        logging.basicConfig(level=level)

    def run(self):
        try:
            # Read command-line arguments.
            self.args = self.get_args()
            self.cmd = self.args.cmd
            if self.cmd:
                self.cmd = self.cmd.lower()

            # Set logging level
            self.configure_logging()

            # Check for a valid command.
            if self.cmd and self.cmd not in self.command_list:
                print "Error: unrecognized command '%s'" % self.cmd
                return 1

            # List command must be dealt with specially.
            if self.cmd == 'list':
                self.handle_list()
                return 0

            if self.args.clock != DEFAULT_CLOCK_FREQ_KHZ:
                print "Setting SWD clock to %d kHz" % self.args.clock

            # Connect to board.
            self.board = MbedBoard.chooseBoard(board_id=self.args.board, target_override=self.args.target, init_board=False, frequency=(self.args.clock * 1000))
            self.board.target.setAutoUnlock(False)
            self.board.target.setHaltOnConnect(False)
            try:
                self.board.init()
            except Exception, e:
                print "Exception while initing board:", e

            self.target = self.board.target
            self.transport = self.board.transport
            self.flash = self.board.flash

            # Halt if requested.
            if self.args.halt:
                self.handle_halt()

            # Handle a device with flash security enabled.
            self.didErase = False
            if self.target.isLocked() and self.cmd != 'unlock':
                print "Error: Target is locked, cannot complete operation. Use unlock command to mass erase and unlock."
                if self.cmd and self.cmd not in ['reset', 'info']:
                    return 1

            # If no command, enter interactive mode.
            if not self.cmd:
                console = PyOCDConsole(self)
                console.run()
            else:
                # Invoke action handler.
                result = self.command_list[self.cmd](self.args.args)
                if result is not None:
                    self.exitCode = result

        except pyOCD.transport.transport.TransferError:
            print "Error: transfer failed"
            self.exitCode = 2
        except ToolError, e:
            print "Error:", e
            self.exitCode = 1
        finally:
            if self.board != None:
                # Pass false to prevent target resume.
                self.board.uninit(False)

        return self.exitCode

    def handle_list(self, args):
        MbedBoard.listConnectedBoards()

    def handle_info(self, args):
        print "Target:    %s" % self.target.part_number
        print "CPU type:  %s" % pyOCD.target.cortex_m.CORE_TYPE_NAME[self.target.core_type]
        print "Unique ID: %s" % self.board.getUniqueID()
        print "Core ID:   0x%08x" % self.target.readIDCode()

    def handle_status(self, args):
        if self.target.isLocked():
            print "Security:       Locked"
        else:
            print "Security:       Unlocked"
        if isinstance(self.target, pyOCD.target.target_kinetis.Kinetis):
            print "MDM-AP Control: 0x%08x" % self.transport.readAP(target_kinetis.MDM_CTRL)
            print "MDM-AP Status:  0x%08x" % self.transport.readAP(target_kinetis.MDM_STATUS)
        status = self.target.getState()
        if status == pyOCD.target.cortex_m.TARGET_HALTED:
            print "Core status:    Halted"
            self.dump_registers()
        elif status == pyOCD.target.cortex_m.TARGET_RUNNING:
            print "Core status:    Running"

    def handle_reg(self, args):
        # If there are no args, print all register values.
        if len(args) < 1:
            self.dump_registers()
            return

        reg = args[0].lower()
        value = self.target.readCoreRegister(reg)
        if type(value) is int:
            print "%s = 0x%08x (%d)" % (reg, value, value)
        elif type(value) is float:
            print "%s = %g" % (reg, value)
        else:
            raise ToolError("Unknown register value type")

    def handle_reset(self, args):
        print "Resetting target"
        if len(args) and args[0].lower() in ['-halt', '-h']:
            self.target.resetStopOnReset()

            status = self.target.getState()
            if status != pyOCD.target.cortex_m.TARGET_HALTED:
                print "Failed to halt device on reset"
            else:
                print "Successfully halted device on reset"
        else:
            self.target.reset()

    def handle_disasm(self, args):
        if len(args) == 0:
            print "Error: no address specified"
            return 1
        addr = self.convert_value(args[0])
        if len(args) < 2:
            count = 6
        else:
            count = self.convert_value(args[1])
        if len(args) < 3:
            center = False
        else:
            center = args[2].lower() in ('-c', '-center')

        if center:
            addr -= count // 2

        # Since we're disassembling, make sure the Thumb bit is cleared.
        addr &= ~1

        # Print disasm of data.
        data = self.target.readBlockMemoryUnaligned8(addr, count)
        self.print_disasm(str(bytearray(data)), addr)

    def handle_read8(self, args):
        self.args.width = 8
        return self.do_read(args)

    def handle_read16(self, args):
        self.args.width = 16
        return self.do_read(args)

    def handle_read32(self, args):
        self.args.width = 32
        return self.do_read(args)

    def handle_write8(self, args):
        self.args.width = 8
        return self.do_write(args)

    def handle_write16(self, args):
        self.args.width = 16
        return self.do_write(args)

    def handle_write32(self, args):
        self.args.width = 32
        return self.do_write(args)

    def do_read(self, args):
        if len(args) == 0:
            print "Error: no address specified"
            return 1
        addr = self.convert_value(args[0])
        if len(args) < 2:
            count = 4
        else:
            count = self.convert_value(args[1])

        if self.args.width == 8:
            data = self.target.readBlockMemoryUnaligned8(addr, count)
            byteData = data
        elif self.args.width == 16:
            byteData = self.target.readBlockMemoryUnaligned8(addr, count)
            data = pyOCD.utility.conversion.byte2half(byteData)
        elif self.args.width == 32:
            byteData = self.target.readBlockMemoryUnaligned8(addr, count)
            data = pyOCD.utility.conversion.byte2word(byteData)

        # Print hex dump of output.
        dumpHexData(data, addr, width=self.args.width)

    def do_write(self, args):
        if len(args) == 0:
            print "Error: no address specified"
            return 1
        addr = self.convert_value(args[0])
        if len(args) <= 1:
            print "Error: no data for write"
            return 1
        else:
            data = [self.convert_value(d) for d in args[1:]]

        if self.args.width == 8:
            pass
        elif self.args.width == 16:
            data = pyOCD.utility.conversion.half2byte(data)
        elif self.args.width == 32:
            data = pyOCD.utility.conversion.word2byte(data)

        self.target.writeBlockMemoryUnaligned8(addr, data)

    def handle_erase(self, args):
        self.flash.init()
        self.flash.eraseAll()

    def handle_unlock(self, args):
        # Currently the same as erase.
        if not self.didErase:
            self.target.massErase()

    def handle_go(self, args):
        self.target.resume()
        status = self.target.getState()
        if status == pyOCD.target.cortex_m.TARGET_RUNNING:
            print "Successfully resumed device"
        else:
            print "Failed to resume device"

    def handle_step(self, args):
        self.target.step()
        print "Successfully stepped device"

    def handle_halt(self, args):
        self.target.halt()

        status = self.target.getState()
        if status != pyOCD.target.cortex_m.TARGET_HALTED:
            print "Failed to halt device"
            return 1
        else:
            print "Successfully halted device"

    def handle_memory_map(self, args):
        self.print_memory_map()

    def handle_log(self, args):
        if len(args) < 1:
            print "Error: no log level provided"
            return 1
        if args[0].lower() not in LEVELS:
            print "Error: log level must be one of {%s}" % ','.join(LEVELS.keys())
            return 1
        logging.getLogger().setLevel(LEVELS[args[0].lower()])

    def handle_clock(self, args):
        if len(args) < 1:
            print "Error: no clock frequency provided"
            return 1
        freq_Hz = int(args[0]) * 1000
        self.transport.setClock(freq_Hz)

        if self.transport.mode == pyOCD.transport.cmsis_dap.DAP_MODE_SWD:
            swd_jtag = 'SWD'
        else:
            swd_jtag = 'JTAG'

        if freq_Hz >= 1000000:
            nice_freq = "%.2f MHz" % (freq_Hz / 1000000)
        elif freq_Hz > 1000:
            nice_freq = "%.2f kHz" % (freq_Hz / 1000)
        else:
            nice_frq = "%d Hz" % freq_Hz

        print "Changed %s frequency to %s" % (swd_jtag, nice_freq)

    ## @brief Convert an argument to a 32-bit integer.
    #
    # Handles the usual decimal, binary, and hex numbers with the appropriate prefix.
    # Also recognizes register names and address dereferencing. Dereferencing using the
    # ARM assembler syntax. To dereference, put the value in brackets, i.e. '[r0]' or
    # '[0x1040]'. You can also use put an offset in the brackets after a comma, such as
    # '[r3,8]'. The offset can be positive or negative, and any supported base.
    def convert_value(self, arg):
        arg = arg.lower()
        deref = (arg[0] == '[')
        if deref:
            arg = arg[1:-1]
            offset = 0
            if ',' in arg:
                arg, offset = arg.split(',')
                arg = arg.strip()
                offset = int(offset.strip(), base=0)

        if arg in pyOCD.target.cortex_m.CORE_REGISTER:
            value = self.target.readCoreRegister(arg)
            print "%s = 0x%08x" % (arg, value)
        else:
            value = int(arg, base=0)

        if deref:
            value = pyOCD.utility.conversion.byte2word(self.target.readBlockMemoryUnaligned8(value + offset, 4))[0]
            print "[%s,%d] = 0x%08x" % (arg, offset, value)

        return value

    def dump_registers(self):
        # Registers organized into columns for display.
        regs = ['r0', 'r6', 'r12',
                'r1', 'r7', 'sp',
                'r2', 'r8', 'lr',
                'r3', 'r9', 'pc',
                'r4', 'r10', 'xpsr',
                'r5', 'r11', 'primask']

        for i, reg in enumerate(regs):
            regValue = self.target.readCoreRegister(reg)
            print "{:>8} {:#010x} ".format(reg + ':', regValue),
            if i % 3 == 2:
                print

    def print_memory_map(self):
        print "Region          Start         End           Blocksize"
        for region in self.target.getMemoryMap():
            print "{:<15} {:#010x}    {:#010x}    {}".format(region.name, region.start, region.end, region.blocksize if region.isFlash else '-')

    def print_disasm(self, code, startAddr):
        if not isCapstoneAvailable:
            print "Warning: Disassembly is not available because the Capstone library is not installed"
            return

        pc = self.target.readCoreRegister('pc') & ~1
        md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)

        addrLine = 0
        text = ''
        for i in md.disasm(code, startAddr):
            hexBytes = ''
            for b in i.bytes:
                hexBytes += '%02x' % b
            pc_marker = '*' if (pc==i.address) else ' '
            text += "{addr:#010x}:{pc_marker} {bytes:<10}{mnemonic:<8}{args}\n".format(addr=i.address, pc_marker=pc_marker, bytes=hexBytes, mnemonic=i.mnemonic, args=i.op_str)

        print text


def main():
    sys.exit(PyOCDTool().run())


if __name__ == '__main__':
    main()

# TMC2130 configuration
#
# Copyright (C) 2018  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import math, logging, pins
import tmc2130_extra
import bus

TMC_FREQUENCY = 13200000.
GCONF_EN_PWM_MODE = 1<<2
GCONF_DIAG1_STALL = 1<<8

Registers = tmc2130_extra.Registers

ReadRegisters = [
    "GCONF", "GSTAT", "IOIN", "TSTEP", "XDIRECT", "MSCNT", "MSCURACT",
    "CHOPCONF", "DRV_STATUS", "PWM_SCALE", "LOST_STEPS",
]

Fields = {}


######################################################################
# Field helpers
######################################################################

# Return the position of the first bit set in a mask
def ffs(mask):
    return (mask & -mask).bit_length() - 1

# Decode two's complement signed integer
def decode_signed_int(val, bits):
    if ((val >> (bits - 1)) & 1):
        return val - (1 << bits)
    return val

class FieldHelper:
    def __init__(self, all_fields, field_formatters={}, registers=None):
        self.all_fields = all_fields
        self.field_formatters = field_formatters
        self.registers = registers
        if self.registers is None:
            self.registers = {}
        self.field_to_register = { f: r for r, fields in self.all_fields.items()
                                   for f in fields }
    def get_field(self, field_name, reg_value=None, reg_name=None):
        # Returns value of the register field
        if reg_name is None:
            reg_name = self.field_to_register[field_name]
        if reg_value is None:
            reg_value = self.registers[reg_name]
        mask = self.all_fields[reg_name][field_name]
        return (reg_value & mask) >> ffs(mask)
    def set_field(self, field_name, field_value, reg_value=None, reg_name=None):
        # Returns register value with field bits filled with supplied value
        if reg_name is None:
            reg_name = self.field_to_register[field_name]
        if reg_value is None:
            reg_value = self.registers.get(reg_name, 0)
        mask = self.all_fields[reg_name][field_name]
        new_value = (reg_value & ~mask) | ((field_value << ffs(mask)) & mask)
        self.registers[reg_name] = new_value
        return new_value
    def set_config_field(self, config, field_name, default, config_name=None):
        # Allow a field to be set from the config file
        if config_name is None:
            config_name = "driver_" + field_name.upper()
        reg_name = self.field_to_register[field_name]
        mask = self.all_fields[reg_name][field_name]
        maxval = mask >> ffs(mask)
        if maxval == 1:
            val = config.getboolean(config_name, default)
        else:
            val = config.getint(config_name, default, minval=0, maxval=maxval)
        return self.set_field(field_name, val)
    def pretty_format(self, reg_name, value):
        # Provide a string description of a register
        reg_fields = self.all_fields.get(reg_name, {})
        reg_fields = sorted([(mask, name) for name, mask in reg_fields.items()])
        fields = []
        for mask, field_name in reg_fields:
            fval = (value & mask) >> ffs(mask)
            sval = self.field_formatters.get(field_name, str)(fval)
            if sval and sval != "0":
                fields.append(" %s=%s" % (field_name, sval))
        return "%-11s %08x%s" % (reg_name + ":", value, "".join(fields))


######################################################################
# Config reading helpers
######################################################################

def current_bits(current, sense_resistor, vsense_on):
    sense_resistor += 0.020
    vsense = 0.32
    if vsense_on:
        vsense = 0.18
    cs = int(32. * current * sense_resistor * math.sqrt(2.) / vsense - 1. + .5)
    return max(0, min(31, cs))

def get_config_current(config):
    vsense = False
    run_current = config.getfloat('run_current', above=0., maxval=2.)
    hold_current = config.getfloat('hold_current', run_current,
                                   above=0., maxval=2.)
    sense_resistor = config.getfloat('sense_resistor', 0.110, above=0.)
    irun = current_bits(run_current, sense_resistor, vsense)
    ihold = current_bits(hold_current, sense_resistor, vsense)
    if irun < 16 and ihold < 16:
        vsense = True
        irun = current_bits(run_current, sense_resistor, vsense)
        ihold = current_bits(hold_current, sense_resistor, vsense)
    return vsense, irun, ihold

def get_config_microsteps(config):
    steps = {'256': 0, '128': 1, '64': 2, '32': 3, '16': 4,
             '8': 5, '4': 6, '2': 7, '1': 8}
    return config.getchoice('microsteps', steps)

def get_config_stealthchop(config, tmc_freq):
    mres = get_config_microsteps(config)
    velocity = config.getfloat('stealthchop_threshold', 0., minval=0.)
    if not velocity:
        return mres, False, 0
    stepper_name = config.get_name().split()[1]
    stepper_config = config.getsection(stepper_name)
    step_dist = stepper_config.getfloat('step_distance')
    step_dist_256 = step_dist / (1 << mres)
    threshold = int(tmc_freq * step_dist_256 / velocity + .5)
    return mres, True, max(0, min(0xfffff, threshold))


######################################################################
# TMC2130 printer object
######################################################################

class TMC2130:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[1]
        self.spi = bus.MCU_SPI_from_config(config, 3, default_speed=4000000)
        # Allow virtual endstop to be created
        self.diag1_pin = config.get('diag1_pin', None)
        ppins = self.printer.lookup_object("pins")
        ppins.register_chip("tmc2130_" + self.name, self)
        # Add DUMP_TMC command
        gcode = self.printer.lookup_object("gcode")
        step_dist = config.getsection(self.name).getfloat('step_distance')
        gcode.register_mux_command(
            "DUMP_TMC", "STEPPER", self.name,
            self.cmd_DUMP_TMC, desc=self.cmd_DUMP_TMC_help)
        # Get config for initial driver settings
        self.fields = FieldHelper(Fields)
        interpolate = config.getboolean('interpolate', True)
        self.mres, en_pwm, sc_threshold = get_config_stealthchop(
            config, TMC_FREQUENCY)
        iholddelay = config.getint('driver_IHOLDDELAY', 8, minval=0, maxval=15)
        tpowerdown = config.getint('driver_TPOWERDOWN', 0, minval=0, maxval=255)
        blank_time_select = config.getint('driver_BLANK_TIME_SELECT', 1,
                                          minval=0, maxval=3)
        toff = config.getint('driver_TOFF', 4, minval=1, maxval=15)
        hend = config.getint('driver_HEND', 7, minval=0, maxval=15)
        hstrt = config.getint('driver_HSTRT', 0, minval=0, maxval=7)
        sgt = config.getint('driver_SGT', 0, minval=-64, maxval=63) & 0x7f
        pwm_scale = config.getboolean('driver_PWM_AUTOSCALE', True)
        pwm_freq = config.getint('driver_PWM_FREQ', 1, minval=0, maxval=3)
        pwm_grad = config.getint('driver_PWM_GRAD', 4, minval=0, maxval=255)
        pwm_ampl = config.getint('driver_PWM_AMPL', 128, minval=0, maxval=255)
            raise pins.error("Can not pullup/invert tmc2130 virtual endstop")
        return TMC2130VirtualEndstop(self)
    def get_register(self, reg_name):
        self.spi.spi_send([reg, 0x00, 0x00, 0x00, 0x00])
        params = self.spi.spi_transfer([reg, 0x00, 0x00, 0x00, 0x00])
        pr = bytearray(params['response'])
        return (pr[1] << 24) | (pr[2] << 16) | (pr[3] << 8) | pr[4]
    def set_register(self, reg_name, val):
        reg = Registers[reg_name]
        data = [(reg | 0x80) & 0xff, (val >> 24) & 0xff, (val >> 16) & 0xff,
                (val >> 8) & 0xff, val & 0xff]
        self.spi.spi_send(data)
    def get_microsteps(self):
        return 256 >> self.mres
    def get_phase(self):
        return (self.get_register("MSCNT") & 0x3ff) >> self.mres
    cmd_DUMP_TMC_help = "Read and display TMC stepper driver registers"
    def cmd_DUMP_TMC(self, params):
        self.printer.lookup_object('toolhead').get_last_move_time()
        gcode = self.printer.lookup_object('gcode')
        logging.info("DUMP_TMC %s", self.name)
        for reg_name in ReadRegisters:
            val = self.get_register(reg_name)
            msg = self.fields.pretty_format(reg_name, val)
            logging.info(msg)
            gcode.respond_info(msg)

# Endstop wrapper that enables tmc2130 "sensorless homing"
class TMC2130VirtualEndstop:
    def __init__(self, tmc2130):
        self.tmc2130 = tmc2130
        if tmc2130.diag1_pin is None:
            raise pins.error("tmc2130 virtual endstop requires diag1_pin")
        ppins = tmc2130.printer.lookup_object('pins')
        self.mcu_endstop = ppins.setup_pin('endstop', tmc2130.diag1_pin)
        if self.mcu_endstop.get_mcu() is not tmc2130.spi.get_mcu():
            raise pins.error("tmc2130 virtual endstop must be on same mcu")
        # Wrappers
        self.get_mcu = self.mcu_endstop.get_mcu
        self.add_stepper = self.mcu_endstop.add_stepper
        self.get_steppers = self.mcu_endstop.get_steppers
        self.home_start = self.mcu_endstop.home_start
        self.home_wait = self.mcu_endstop.home_wait
        self.query_endstop = self.mcu_endstop.query_endstop
        self.query_endstop_wait = self.mcu_endstop.query_endstop_wait
        self.TimeoutError = self.mcu_endstop.TimeoutError
    def home_prepare(self):
        gconf = self.tmc2130.reg_GCONF
        gconf &= ~GCONF_EN_PWM_MODE
        gconf |= GCONF_DIAG1_STALL
        self.tmc2130.set_register("GCONF", gconf)
        self.tmc2130.set_current_regs(
            self.tmc2130.homing_current, self.tmc2130.hold_current)
        self.tmc2130.set_register("TCOOLTHRS", 0xfffff)
        self.mcu_endstop.home_prepare()
    def home_finalize(self):
        self.tmc2130.set_register("GCONF", self.tmc2130.reg_GCONF)
        self.tmc2130.set_current_regs(
            self.tmc2130.run_current, self.tmc2130.hold_current)
        self.tmc2130.set_register(
            "TCOOLTHRS", max(0, min(0xfffff, self.tmc2130.tcoolthrs)))
        self.mcu_endstop.home_finalize()

def load_config_prefix(config):
    return TMC2130(config)

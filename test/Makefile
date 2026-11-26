# Makefile
# See https://docs.cocotb.org/en/stable/quickstart.html for more info

# defaults
SIM ?= verilator
FST ?= -fst # Use more efficient FST format (Icarus)
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(PWD)/../src
PROJECT_SOURCES = tt_um_proppy_megabytebeat.v pwm_audio.v megabytebeat.sv sc_adc_ramp_1ch_4b.v sc_r2r_dac_8bit.v r2r_ladder_4bit_sum.v r2r_ladder_8bit.v

ifneq ($(GATES),yes)

# RTL simulation:
SIM_BUILD				= sim_build/rtl
WAVES					?= 1
COMPILE_ARGS    += -DSIM
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))

else

# Gate level simulation:
SIM_BUILD				= sim_build/gl
WAVES					?= 0
COMPILE_ARGS    += -DGL_TEST
COMPILE_ARGS    += -DFUNCTIONAL
COMPILE_ARGS    += -DUSE_POWER_PINS
COMPILE_ARGS    += -DSIM
ifneq ($(SIM),verilator)
COMPILE_ARGS    += -DUNIT_DELAY=\#1
endif
VERILOG_SOURCES += $(PDK_ROOT)/gf180mcuD/libs.ref/gf180mcu_fd_sc_mcu7t5v0/verilog/primitives.v
VERILOG_SOURCES += $(PDK_ROOT)/gf180mcuD/libs.ref/gf180mcu_fd_sc_mcu7t5v0/verilog/gf180mcu_fd_sc_mcu7t5v0.v
VERILOG_SOURCES += $(SRC_DIR)/r2r_ladder_4bit_sum.v
VERILOG_SOURCES += $(SRC_DIR)/r2r_ladder_8bit.v

# this gets copied in by the GDS action workflow
VERILOG_SOURCES += $(PWD)/gate_level_netlist.v

endif

# Allow sharing configuration between design and testbench via `include`:
COMPILE_ARGS 		+= -I$(SRC_DIR)

# Include the testbench sources:
ifeq ($(SIM),verilator)
VERILOG_SOURCES += $(PWD)/verilator.vlt
endif
VERILOG_SOURCES += $(PWD)/tb.v
TOPLEVEL = tb

# List test modules to run, separated by commas and without the .py suffix:
COCOTB_TEST_MODULES = test

# include cocotb's make rules to take care of the simulator setup
include $(shell cocotb-config --makefiles)/Makefile.sim

# Override cocotb's global --public-flat-rw after Makefile.sim includes it, using scoped verilator.vlt.
ifeq ($(SIM),verilator)
COMPILE_ARGS += --no-public-flat-rw --no-timing
BUILD_ARGS   += -j$(shell nproc)
ifeq ($(GATES),yes)
BUILD_ARGS   += OPT_FAST=-O1 OPT_GLOBAL=-O1
endif
ifeq ($(WAVES),1)
ifneq ($(wildcard /usr/include/lz4.h),)
COMPILE_ARGS += --trace-fst --trace-structs
SIM_ARGS     += --trace --trace-file $(PWD)/tb.fst
else
COMPILE_ARGS += --trace --trace-structs
SIM_ARGS     += --trace --trace-file $(PWD)/tb.vcd
endif
endif
endif

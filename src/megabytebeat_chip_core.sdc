# Copyright 2026 Google LLC.
# SPDX-License-Identifier: Apache-2.0
#
# Timing constraints for wafer.space 5-track mixed-signal `megabytebeat_chip_core` (32.768 kHz).
# Mirrors LibreLane's base.sdc for digital I/O while excluding passive analog ports
# `adc_in[*]` (4x ramp ADC sum_in) and `dac_out[*]` (5x R-2R DAC out) from digital delay,
# driving-cell, and load checks.

create_clock [get_ports clk] -name clk -period $::env(CLOCK_PERIOD)

set input_delay_value [expr $::env(CLOCK_PERIOD) * $::env(IO_DELAY_CONSTRAINT) / 100.0]
set output_delay_value [expr $::env(CLOCK_PERIOD) * $::env(IO_DELAY_CONSTRAINT) / 100.0]
puts "\[INFO\] Setting input/output delay to: $input_delay_value / $output_delay_value"

set_max_fanout $::env(MAX_FANOUT_CONSTRAINT) [current_design]
if { [info exists ::env(MAX_TRANSITION_CONSTRAINT)] } {
    set_max_transition $::env(MAX_TRANSITION_CONSTRAINT) [current_design]
}
if { [info exists ::env(MAX_CAPACITANCE_CONSTRAINT)] } {
    set_max_capacitance $::env(MAX_CAPACITANCE_CONSTRAINT) [current_design]
}

set clocks [get_clocks clk]
set dig_inputs [get_ports {rst_n bpm_in}]
set ana_ports [get_ports {adc_in[*] dac_out[*]}]

set_input_delay $input_delay_value -clock $clocks $dig_inputs

set_driving_cell \
    -lib_cell [lindex [split $::env(SYNTH_DRIVING_CELL) "/"] 0] \
    -pin [lindex [split $::env(SYNTH_DRIVING_CELL) "/"] 1] \
    $dig_inputs

if { ![info exists ::env(SYNTH_CLK_DRIVING_CELL)] } {
    set ::env(SYNTH_CLK_DRIVING_CELL) $::env(SYNTH_DRIVING_CELL)
}
set_driving_cell \
    -lib_cell [lindex [split $::env(SYNTH_CLK_DRIVING_CELL) "/"] 0] \
    -pin [lindex [split $::env(SYNTH_CLK_DRIVING_CELL) "/"] 1] \
    [get_ports clk]

# Analog pins adc_in[3:0] and dac_out[4:0] connect directly to passive R-2R ladders.
set_false_path -from $ana_ports
set_false_path -to $ana_ports

set_clock_uncertainty $::env(CLOCK_UNCERTAINTY_CONSTRAINT) $clocks
set_clock_transition $::env(CLOCK_TRANSITION_CONSTRAINT) $clocks
set_timing_derate -early [expr 1.0 - $::env(TIME_DERATING_CONSTRAINT) / 100.0]
set_timing_derate -late  [expr 1.0 + $::env(TIME_DERATING_CONSTRAINT) / 100.0]

if { [info exists ::env(OPENLANE_SDC_IDEAL_CLOCKS)] && $::env(OPENLANE_SDC_IDEAL_CLOCKS) } {
    unset_propagated_clock [all_clocks]
} else {
    set_propagated_clock [all_clocks]
}

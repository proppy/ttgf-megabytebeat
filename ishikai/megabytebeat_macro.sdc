# SPDX-FileCopyrightText: © 2026 GNSS AFE + megabytebeat Authors
# SPDX-License-Identifier: Apache-2.0
#
# Timing constraints for ISHI-KAI 5.0 V VDD_BB `megabytebeat_macro` (2.048 MHz, 5x 8-bit R-2R DAC).
# Excludes passive analog inout ports `ua[4:0]` from digital delay/load/driving-cell checks
# and constrains `u_tt.u_megabytebeat`'s 8 kHz sample-rate registers (`____t`, `____pcm`) as
# a 256-cycle multicycle path while keeping `clk_8[*]`, `pwm_audio[*]`, and `ena_q` at 1 cycle.

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
set dig_inputs [get_ports {ui_in[*] uio_in[*] ena rst_n}]
set dig_outputs [get_ports {uo_pwm[*] vdd_bb_hi_s vss_lo_s vss_lo_w vss_lo_e}]
set ana_ports [get_ports {ua[*]}]

set_input_delay $input_delay_value -clock $clocks $dig_inputs
set_output_delay $output_delay_value -clock $clocks $dig_outputs

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

set cap_load [expr $::env(OUTPUT_CAP_LOAD) / 1000.0]
puts "\[INFO\] Setting digital output load to: $cap_load"
set_load $cap_load $dig_outputs

# Analog pins ua[4:0] (5x passive R-2R DAC outputs) are non-timed.
set_false_path -from $ana_ports
set_false_path -to $ana_ports

# u_tt.u_megabytebeat state flops (32x ____t + 40x ____pcm) only clock-enable on
# sample_rdy = (clk_8 == 8'hff) -> 256 cycles (125 us).
set sample_regs {}
foreach pin [all_registers -output_pins] {
    set net [get_nets -quiet -of_objects $pin]
    if { $net ne "" } {
        set net_name [get_full_name [lindex $net 0]]
        if {
            [string match {*____t*} $net_name]
            || [string match {*____pcm*} $net_name]
            || [string match {*pcm\[*} $net_name]
            || [string match {*pcm_bus*} $net_name]
        } {
            set cell [lindex [get_cells -quiet -of_objects $pin] 0]
            if { $cell ne "" && [lsearch -exact $sample_regs $cell] < 0 } {
                lappend sample_regs $cell
            }
        }
    }
}
if { [llength $sample_regs] > 0 } {
    puts "\[INFO\] Applying 256-cycle multicycle path to [llength $sample_regs] sample-rate registers."
    set_multicycle_path -setup 256 -from $sample_regs -to $sample_regs
    set_multicycle_path -hold  255 -from $sample_regs -to $sample_regs
    set_multicycle_path -setup 256 -from [get_ports {ui_in[*] uio_in[*]}] -to $sample_regs
    set_multicycle_path -hold  255 -from [get_ports {ui_in[*] uio_in[*]}] -to $sample_regs
}

set_clock_uncertainty $::env(CLOCK_UNCERTAINTY_CONSTRAINT) $clocks
set_clock_transition $::env(CLOCK_TRANSITION_CONSTRAINT) $clocks
set_timing_derate -early [expr 1.0 - $::env(TIME_DERATING_CONSTRAINT) / 100.0]
set_timing_derate -late  [expr 1.0 + $::env(TIME_DERATING_CONSTRAINT) / 100.0]

if { [info exists ::env(OPENLANE_SDC_IDEAL_CLOCKS)] && $::env(OPENLANE_SDC_IDEAL_CLOCKS) } {
    unset_propagated_clock [all_clocks]
} else {
    set_propagated_clock [all_clocks]
}

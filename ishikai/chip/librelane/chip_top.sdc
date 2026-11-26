current_design $::env(DESIGN_NAME)
set_units -time ns

set clock_port __VIRTUAL_CLK__
if { [info exists ::env(CLOCK_PORT)] } {
    set port_count [llength $::env(CLOCK_PORT)]

    if { $port_count == "0" } {
        puts "\[WARNING] No CLOCK_PORT found. A dummy clock will be used."
    } elseif { $port_count != "1" } {
        puts "\[WARNING] Multi-clock files are not currently supported by the base SDC file. Only the first clock will be constrained."
    }

    if { $port_count > "0" } {
        set ::clock_port [lindex $::env(CLOCK_PORT) 0]
    }
}

if { $::env(CLOCK_PORT) == $::env(CLOCK_NET) } {
    set port_args [get_ports $clock_port]
} else {
    set port_args [get_pins [lindex $::env(CLOCK_NET) 0]]
}

puts "\[INFO] Using clock $clock_port…"
create_clock {*}$port_args -name $clock_port -period $::env(CLOCK_PERIOD)

set input_delay_value [expr $::env(CLOCK_PERIOD) * $::env(IO_DELAY_CONSTRAINT) / 100]
set output_delay_value [expr $::env(CLOCK_PERIOD) * $::env(IO_DELAY_CONSTRAINT) / 100]
puts "\[INFO] Setting output delay to: $output_delay_value"
puts "\[INFO] Setting input delay to: $input_delay_value"

set_max_fanout $::env(MAX_FANOUT_CONSTRAINT) [current_design]
if { [info exists ::env(MAX_TRANSITION_CONSTRAINT)] } {
    set_max_transition $::env(MAX_TRANSITION_CONSTRAINT) [current_design]
}
if { [info exists ::env(MAX_CAPACITANCE_CONSTRAINT)] } {
    set_max_capacitance $::env(MAX_CAPACITANCE_CONSTRAINT) [current_design]
}

set clocks [get_clocks $clock_port]

# GNSS Bidirectional pads
set clk_core_inout_ports [get_ports {
    bidir_PAD[*]
}]

set_input_delay -min 0 -clock $clocks $clk_core_inout_ports
set_input_delay -max $input_delay_value -clock $clocks $clk_core_inout_ports
set_output_delay $output_delay_value -clock $clocks $clk_core_inout_ports

# GNSS AFE: SDO (bidir_PAD[5]) launched on falling edge of SCLK (specified <= 5 MHz)
set_multicycle_path -setup 2 -to [get_ports {bidir_PAD[5]}]
set_multicycle_path -hold 1 -to [get_ports {bidir_PAD[5]}]

# GNSS Input-only pads
set clk_core_input_ports [get_ports {
    rst_n_PAD
    input_PAD[*]
}]

set_input_delay -min 0 -clock $clocks $clk_core_input_ports
set_input_delay -max $input_delay_value -clock $clocks $clk_core_input_ports

# megabytebeat asynchronous clock domain (2.048 MHz = 488 ns, VDD_BB domain)
set bb_clk_pins [get_pins -quiet bb_clk_pad/Y]
if { [llength $bb_clk_pins] > 0 } {
    puts "\[INFO] Constraining megabytebeat clock bb_clk (488 ns = 2.048 MHz)…"
    create_clock $bb_clk_pins -name bb_clk -period 488.0
    set_clock_groups -asynchronous -group [get_clocks $clock_port] -group [get_clocks bb_clk]

    set bb_in_ports [get_ports {bb_rst_n_PAD bb_ena_PAD bb_ui_PAD[*] bb_uio_PAD[*]}]
    set bb_out_ports [get_ports {bb_uo_PAD[*]}]
    set_input_delay -min 5.0 -clock [get_clocks bb_clk] $bb_in_ports
    set_input_delay -max 97.6 -clock [get_clocks bb_clk] $bb_in_ports
    set_output_delay 97.6 -clock [get_clocks bb_clk] $bb_out_ports

    set bb_ana_ports [get_ports -quiet {bb_ua_PAD[*]}]
    if { [llength $bb_ana_ports] > 0 } {
        set_false_path -from $bb_ana_ports
        set_false_path -to $bb_ana_ports
    }

    set_clock_uncertainty $::env(CLOCK_UNCERTAINTY_CONSTRAINT) [get_clocks bb_clk]
    set_clock_transition $::env(CLOCK_TRANSITION_CONSTRAINT) [get_clocks bb_clk]
}

# Output load
set cap_load [expr $::env(OUTPUT_CAP_LOAD) / 1000.0]
puts "\[INFO] Setting load to: $cap_load"
set_load $cap_load [all_outputs]

puts "\[INFO] Setting clock uncertainty to: $::env(CLOCK_UNCERTAINTY_CONSTRAINT)"
set_clock_uncertainty $::env(CLOCK_UNCERTAINTY_CONSTRAINT) $clocks

puts "\[INFO] Setting clock transition to: $::env(CLOCK_TRANSITION_CONSTRAINT)"
set_clock_transition $::env(CLOCK_TRANSITION_CONSTRAINT) $clocks

puts "\[INFO] Setting timing derate to: $::env(TIME_DERATING_CONSTRAINT)%"
set_timing_derate -early [expr 1-[expr $::env(TIME_DERATING_CONSTRAINT) / 100]]
set_timing_derate -late [expr 1+[expr $::env(TIME_DERATING_CONSTRAINT) / 100]]

if { [info exists ::env(OPENLANE_SDC_IDEAL_CLOCKS)] && $::env(OPENLANE_SDC_IDEAL_CLOCKS) } {
    unset_propagated_clock [all_clocks]
} else {
    set_propagated_clock [all_clocks]
}

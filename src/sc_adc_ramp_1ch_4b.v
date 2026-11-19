// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

`default_nettype none
`ifdef SIM
module sc_adc_ramp_1ch_4b (
`ifdef USE_POWER_PINS
    inout  wire       VDD,
    inout  wire       VSS,
`endif
    input  wire [3:0] step,
    input  wire       adc_in,
    output wire       cmp
);
    // In digital cocotb sim, testbench drives comparator PWM pulse width (0..15 clocks) on adc_in
    assign cmp = adc_in;
endmodule
`else
(* blackbox, keep *)
module sc_adc_ramp_1ch_4b (
`ifdef USE_POWER_PINS
    inout  wire       VDD,
    inout  wire       VSS,
`endif
    input  wire [3:0] step,
    input  wire       adc_in,
    output wire       cmp
);
endmodule
`endif
`default_nettype wire

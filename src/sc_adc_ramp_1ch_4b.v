// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0
//
// Structural 4-bit resistive-summing ramp ADC channel wrapper: 5x (* keep *) clkinv_4
// drivers (term + step[3:0]), passive `r2r_ladder_4bit_sum` macro, and (* keep *) clkbuf_1 comparator.

`default_nettype none

module sc_adc_ramp_1ch_4b (
    input  wire [3:0] step,
    input  wire       adc_in,
    output wire       cmp
);
`ifdef SIM
    // In digital cocotb sim, testbench drives comparator PWM pulse width (0..15 clocks) on adc_in
    assign cmp = adc_in;
    wire _unused = &{step, 1'b0};
`else
    wire       term_tie;
    wire       term_drv;
    wire [3:0] pre_drv;
    wire [3:0] in_drv;
    wire       vsum;
    wire       cmp_raw;

    /* verilator lint_off PINMISSING */
    (* keep *)
    gf180mcu_fd_sc_mcu7t5v0__tieh u_tieh (
        .Z(term_tie)
    );

    (* keep *)
    gf180mcu_fd_sc_mcu7t5v0__clkinv_4 u_term (
        .I(term_tie),
        .ZN(term_drv)
    );

    genvar b;
    generate
        for (b = 0; b < 4; b = b + 1) begin : gen_drv
            // Non-dont_touch pre-buffer lets OpenROAD Resizer freely buffer upstream digital nets.
            (* keep *)
            gf180mcu_fd_sc_mcu7t5v0__clkbuf_1 u_pre (
                .I(step[b]),
                .Z(pre_drv[b])
            );

            (* keep *)
            gf180mcu_fd_sc_mcu7t5v0__clkinv_4 u_inv (
                .I(pre_drv[b]),
                .ZN(in_drv[b])
            );
        end
    endgenerate

    r2r_ladder_4bit_sum u_r2r (
        .term(term_drv),
        .in(in_drv),
        .out(vsum),
        .sum_in(adc_in)
    );

    (* keep *)
    gf180mcu_fd_sc_mcu7t5v0__clkbuf_1 u_cmp (
        .I(vsum),
        .Z(cmp_raw)
    );

    // Non-dont_touch post-buffer isolates comparator output from downstream digital fanout/buffering.
    (* keep *)
    gf180mcu_fd_sc_mcu7t5v0__clkbuf_1 u_post (
        .I(cmp_raw),
        .Z(cmp)
    );
    /* verilator lint_on PINMISSING */
`endif
endmodule
`default_nettype wire

// SPDX-FileCopyrightText: © 2026 GNSS AFE + megabytebeat Authors
// SPDX-License-Identifier: Apache-2.0
//
// Hardened mixed-signal macro wrapper around proppy/ttgf-megabytebeat
// (tt_um_proppy_megabytebeat + megabytebeat.sv + pwm_audio.v + 5x sc_r2r_dac_8bit /
// r2r_ladder_8bit) in a dedicated VDD_BB power domain (shared VSS).
//
// Enable / quiet-by-default behavior:
//   * The chip_top pad for `ena` (bb_ena_pad) has internal pull-down (PD=1 driven by
//     VDD_BB-domain `vdd_bb_hi_s`), so when unconnected `ena` defaults to 0.
//   * While `ena == 0` and `rst_n == 1`, the integrated clock gate shuts off `gclk`
//     completely (zero dynamic switching during GNSS operation), all PWM outputs are
//     clamped to 0, and `tt_um_proppy_megabytebeat` combinationally clamps all 5 R-2R
//     DAC ladders to 0x00 (0 V, < 2 nA/ch standby leakage).
//   * On a 1 -> 0 transition of `ena`, `ena_q` keeps `gclk` alive for 1 cycle so the
//     synchronous resets inside XLS `megabytebeat` and `pwm_audio` clear all FFs cleanly
//     before the clock tree stops.
//   * Dedicated VDD_BB-domain constant outputs (vdd_bb_hi_s, vss_lo_*) drive all static
//     control pins (PD, PU, OE, IE, CS, SL, PDRV0, PDRV1) of the megabytebeat I/O pads,
//     guaranteeing ZERO GNSS-VDD cells on any megabytebeat net.

`default_nettype none

module megabytebeat_macro (
`ifdef USE_POWER_PINS
    inout  wire       VDD_BB,
    inout  wire       VSS,
`endif
    input  wire       clk,
    input  wire       rst_n,
    input  wire       ena,
    input  wire [7:0] ui_in,
    input  wire [7:0] uio_in,
    output wire [2:0] uo_pwm,
    inout  wire [4:0] ua,
    output wire       vdd_bb_hi_s,
    output wire       vss_lo_s,
    output wire       vss_lo_w,
    output wire       vss_lo_e
);

    // 1-cycle flush register so synchronous reset clears all FFs when ena drops 1 -> 0
    reg ena_q;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            ena_q <= 1'b0;
        else
            ena_q <= ena;
    end

    wire clk_en = ena | (~rst_n) | ena_q;
    wire gclk;

`ifdef SYNTHESIS
    /* verilator lint_off PINMISSING */
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__icgtp_4 u_icg (
        .CLK (clk),
        .E   (clk_en),
        .TE  (1'b0),
        .Q   (gclk)
    );

    wire tieh_s_raw, tiel_s_raw, tiel_w_raw, tiel_e_raw;
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__tieh  u_tieh_s (.Z(tieh_s_raw));
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__buf_8 u_bufh_s (.I(tieh_s_raw), .Z(vdd_bb_hi_s));

    (* keep *) gf180mcu_fd_sc_mcu7t5v0__tiel  u_tiel_s (.ZN(tiel_s_raw));
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__buf_8 u_bufl_s (.I(tiel_s_raw), .Z(vss_lo_s));
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__tiel  u_tiel_w (.ZN(tiel_w_raw));
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__buf_8 u_bufl_w (.I(tiel_w_raw), .Z(vss_lo_w));
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__tiel  u_tiel_e (.ZN(tiel_e_raw));
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__buf_8 u_bufl_e (.I(tiel_e_raw), .Z(vss_lo_e));
    /* verilator lint_on PINMISSING */
`else
    // Behavioral equivalent of positive-edge integrated clock gate (low-phase latch)
    /* verilator lint_off LATCH */
    reg clk_en_lat;
    always @(*) begin
        if (!clk)
            clk_en_lat = clk_en;
    end
    /* verilator lint_on LATCH */
    assign gclk = clk & clk_en_lat;

    assign vdd_bb_hi_s = 1'b1;
    assign vss_lo_s    = 1'b0;
    assign vss_lo_w    = 1'b0;
    assign vss_lo_e    = 1'b0;
`endif

    wire eff_rst_n = rst_n & ena;

    wire [7:0] uo_raw;
    wire [7:0] uio_out_unused;
    wire [7:0] uio_oe_unused;
    /* verilator lint_off UNDRIVEN */
    /* verilator lint_off UNUSEDSIGNAL */
    wire [2:0] ua_unused;
    /* verilator lint_on UNUSEDSIGNAL */
    /* verilator lint_on UNDRIVEN */

    tt_um_proppy_megabytebeat u_tt (
        .ui_in   (ui_in),
        .uo_out  (uo_raw),
        .uio_in  (uio_in),
        .uio_out (uio_out_unused),
        .uio_oe  (uio_oe_unused),
        .ua      ({ua_unused, ua}),
        .ena     (ena),
        .clk     (gclk),
        .rst_n   (eff_rst_n)
    );

    // Map tracks 0..2 to South PWM pads (tracks 0..3 & 7 are all output on analog DACs ua[4:0])
    // Clamped to 0 whenever ena == 0
    assign uo_pwm = ena ? uo_raw[2:0] : 3'b000;

    wire _unused = &{uo_raw[7:3], uio_out_unused, uio_oe_unused, 1'b0};

endmodule

`default_nettype wire

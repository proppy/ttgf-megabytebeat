// SPDX-FileCopyrightText: © 2026 GNSS AFE + megabytebeat Authors
// SPDX-License-Identifier: Apache-2.0
//
// Hardened mixed-signal macro wrapper around XLS `megabytebeat`
// (megabytebeat.v + 5x sc_r2r_dac_8bit / r2r_ladder_8bit) in a dedicated
// VDD_BB power domain (shared VSS), clocked at 32.768 kHz (/4 = 8.192 kHz DAC rate).
//
// Enable / quiet-by-default behavior:
//   * The chip_top pad for `ena` (bb_ena_pad) has internal pull-down (PD=1 driven by
//     VDD_BB-domain `vdd_bb_hi_s`), so when unconnected `ena` defaults to 0.
//   * While `ena == 0` and `rst_n == 1`, the integrated clock gate shuts off `gclk`
//     completely (zero dynamic switching during GNSS operation) and `analog_en == 0`
//     combinationally clamps all 5 R-2R DAC ladders to 0x00 (0 V, < 2 nA/ch standby leakage).
//   * On a 1 -> 0 transition of `ena`, `ena_q` keeps `gclk` alive for 1 cycle so the
//     synchronous resets inside `clk_div` and XLS `megabytebeat` clear all FFs cleanly
//     before the clock tree stops.
//   * Dedicated VDD_BB-domain constant outputs (vdd_bb_hi_s, vss_lo_*) drive all static
//     control pins (PD, PU) of the megabytebeat I/O pads, guaranteeing ZERO GNSS-VDD cells
//     on any megabytebeat net.

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
    wire rst       = !eff_rst_n;
    wire analog_en = eff_rst_n;

    // 2-bit divider: 32.768 kHz / 4 = 8.192 kHz DAC audio sample rate (4 clocks per frame)
    reg [1:0] clk_div;
    wire      sample_rdy = (clk_div == 2'd3);
    always @(posedge gclk) begin
        if (!eff_rst_n)
            clk_div <= 2'd0;
        else
            clk_div <= clk_div + 2'd1;
    end

    wire [15:0] packed_params = {uio_in, ui_in};  // {d, c, b, a}
    wire [39:0] pcm_bus;
    wire        pcm_vld;
    wire        dac_vld;
    wire        en_rdy;
    wire        params_rdy;

    megabytebeat u_megabytebeat (
        .clk          (gclk),
        .reset        (rst),
        ._en_r        (analog_en),
        ._en_r_vld    (1'b1),
        ._en_r_rdy    (en_rdy),
        ._params_r    (packed_params),
        ._params_r_vld(1'b1),
        ._params_r_rdy(params_rdy),
        ._pcm_s       (pcm_bus),
        ._pcm_s_vld   (pcm_vld),
        ._pcm_s_rdy   (sample_rdy),
        ._dac_s       (ua),
        ._dac_s_vld   (dac_vld),
        ._dac_s_rdy   (sample_rdy)
    );

    wire _unused = &{pcm_bus, pcm_vld, dac_vld, en_rdy, params_rdy, 1'b0};

endmodule

`default_nettype wire

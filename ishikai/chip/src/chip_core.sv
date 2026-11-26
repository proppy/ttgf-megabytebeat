// SPDX-FileCopyrightText: © 2026 GNSS AFE Authors
// SPDX-License-Identifier: Apache-2.0
//
// GNSS AFE (GPS L1 C/A + Galileo E1) + proppy/ttgf-megabytebeat (slot 0p5x1 dual-VDD).
//
//   * GNSS SPI control registers (standard cells, GNSS VDD domain): 24-bit frame, mode 0, MSB first.
//   * gnss_afe_analog: analog hard macro (VDDA / VDDV / VSSA, north pad block).
//   * megabytebeat_macro (when HAS_MEGABYTEBEAT is defined): mixed-signal 5-track bytebeat
//     synthesizer with 5x 8-bit R-2R DAC analog outputs (bb_ua[4:0]) in a dedicated VDD_BB
//     power domain (shared VSS), isolated at the padring by fill10_noVDD and supplied by
//     fill10_supplyVDD. Zero signals cross between VDD and VDD_BB.

`default_nettype none

`include "generated_defines.svh"
`include "slot_defines.svh"

module chip_core #(
    parameter NUM_INPUT_PADS,
    parameter NUM_BIDIR_PADS,
    parameter NUM_ANALOG_PADS
`ifdef HAS_MEGABYTEBEAT
    ,
    parameter NUM_BB_UO_PADS = `NUM_BB_UO_PADS,
    parameter NUM_BB_ANALOG_PADS = `NUM_BB_ANALOG_PADS
`endif
    )(
    `ifdef USE_POWER_PINS
    inout  wire VDD,
    inout  wire VSS,
    inout  wire VDDA,      // analog supply (VDDA pads)
    inout  wire VDDV,      // VCO supply (VDDV pad)
    inout  wire VSSA,      // isolated analog ground (north analog VSS pads)
    `ifdef HAS_MEGABYTEBEAT
    inout  wire VDD_BB,    // isolated megabytebeat supply (fill10_noVDD + fill10_supplyVDD)
    `endif
    `endif

    input  wire clk,       // GNSS SCLK
    input  wire rst_n,     // GNSS reset (active low)

    input  wire [NUM_INPUT_PADS-1:0] input_in,
    output wire [NUM_INPUT_PADS-1:0] input_pu,
    output wire [NUM_INPUT_PADS-1:0] input_pd,

    input  wire [NUM_BIDIR_PADS-1:0] bidir_in,
    output wire [NUM_BIDIR_PADS-1:0] bidir_out,
    output wire [NUM_BIDIR_PADS-1:0] bidir_oe,
    output wire [NUM_BIDIR_PADS-1:0] bidir_cs,
    output wire [NUM_BIDIR_PADS-1:0] bidir_sl,
    output wire [NUM_BIDIR_PADS-1:0] bidir_ie,
    output wire [NUM_BIDIR_PADS-1:0] bidir_pu,
    output wire [NUM_BIDIR_PADS-1:0] bidir_pd,
    output wire [NUM_BIDIR_PADS-1:0] bidir_pdrv0,
    output wire [NUM_BIDIR_PADS-1:0] bidir_pdrv1,

    inout  wire [NUM_ANALOG_PADS-1:0] analog,
    inout  wire rfin                       // RF input (analog pad block)
`ifdef HAS_MEGABYTEBEAT
    ,
    input  wire       bb_clk,
    input  wire       bb_rst_n,
    input  wire       bb_ena,
    input  wire [7:0] bb_ui_in,
    input  wire [7:0] bb_uio_in,
    output wire [NUM_BB_UO_PADS-1:0]     bb_uo_out,
    inout  wire [NUM_BB_ANALOG_PADS-1:0] bb_ua,
    output wire       bb_vdd_hi_s,
    output wire       bb_vss_lo_s,
    output wire       bb_vss_lo_w,
    output wire       bb_vss_lo_e
`endif
);

    // ------------------------------------------------------------ GNSS pads
    // inputs: CS_N pulled up so that an open pin keeps the registers latched
    assign input_pu = 2'b10;
    assign input_pd = 2'b00;

    // bidir[4:0] are outputs, bidir[5] is SDO (driven while CS_N is low)
    localparam NOUT = 5;
    wire sdo, sdo_oe;
    assign bidir_oe = {{(NUM_BIDIR_PADS-NOUT-1){1'b0}}, sdo_oe, {NOUT{1'b1}}};
    assign bidir_cs = '0;
    assign bidir_sl = {{(NUM_BIDIR_PADS-NOUT-1){1'b0}}, {(NOUT+1){1'b1}}};
    assign bidir_ie = {{(NUM_BIDIR_PADS-NOUT-1){1'b1}}, {(NOUT+1){1'b0}}};
    assign bidir_pu = '0;
    assign bidir_pd = {{(NUM_BIDIR_PADS-NOUT){1'b1}}, {NOUT{1'b0}}};
    assign bidir_pdrv0 = '0;
    assign bidir_pdrv1 = '0;

    logic _unused;
    assign _unused = &{bidir_in};

    wire sdi  = input_in[0];
    wire cs_n = input_in[1];

    // ------------------------------------------------------------ GNSS SPI registers
    logic [23:0] sr;          // shift stages
    logic [18:0] ctl;         // IG1A IG1B IG2A IG2B IG3A IG3B B0 B1 AFC0..7 ICP2 BWW BWN

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            sr <= '0;
        else if (!cs_n)
            sr <= {sr[22:0], sdi};
    end

    always_ff @(posedge cs_n or negedge rst_n) begin
        if (!rst_n)
            ctl <= 19'h0_8000;    // AFC7 = 1
        else
            ctl <= sr[18:0];
    end

    // readback: bit counter cleared while CS_N is high, advanced on the falling edge of SCLK
    localparam logic [4:0] ID = 5'b10101;
    wire  [23:0] rd = {ID, ctl};
    logic [4:0]  cnt;
    wire         cnt_rst_n = rst_n & ~cs_n;

    always_ff @(negedge clk or negedge cnt_rst_n) begin
        if (!cnt_rst_n)
            cnt <= '0;
        else if (cnt != 5'd24)
            cnt <= cnt + 5'd1;
    end

    assign sdo    = (cnt < 5'd24) ? rd[5'd23 - cnt] : 1'b0;
    assign sdo_oe = ~cs_n;

    // ------------------------------------------------------------ GNSS analog macro
    wire i_sign, i_mag, q_sign, q_mag, clkout;

    gnss_afe_analog i_afe (
        `ifdef USE_POWER_PINS
        .VDDA   (VDDA),
        .VDDV   (VDDV),
        .VSSA   (VSSA),
        `endif
        .RFIN   (rfin),
        .REFIN  (analog[0]),
        .IG1A   (ctl[0]),  .IG1B (ctl[1]),
        .IG2A   (ctl[2]),  .IG2B (ctl[3]),
        .IG3A   (ctl[4]),  .IG3B (ctl[5]),
        .B0     (ctl[6]),  .B1   (ctl[7]),
        .AFC0   (ctl[8]),  .AFC1 (ctl[9]),  .AFC2 (ctl[10]), .AFC3 (ctl[11]),
        .AFC4   (ctl[12]), .AFC5 (ctl[13]), .AFC6 (ctl[14]), .AFC7 (ctl[15]),
        .ICP2   (ctl[16]),
        .BWK    (~ctl[17]), .BWN (ctl[18]),      // BWK = not BWW (IF filter: 0 = wider)
        .I_SIGN (i_sign),
        .I_MAG  (i_mag),
        .Q_SIGN (q_sign),
        .Q_MAG  (q_mag),
        .CLKOUT (clkout),
        .AFCOUT (analog[1]),
        .VCTRL  (analog[2])
    );

    assign bidir_out = {{(NUM_BIDIR_PADS-NOUT-1){1'b0}}, sdo, clkout, q_mag, ~q_sign, i_mag, i_sign};

`ifdef HAS_MEGABYTEBEAT
    // ------------------------------------------------------------ megabytebeat macro (VDD_BB domain)
    megabytebeat_macro i_megabytebeat (
        `ifdef USE_POWER_PINS
        .VDD_BB      (VDD_BB),
        .VSS         (VSS),
        `endif
        .clk         (bb_clk),
        .rst_n       (bb_rst_n),
        .ena         (bb_ena),
        .ui_in       (bb_ui_in),
        .uio_in      (bb_uio_in),
        .uo_pwm      (bb_uo_out),
        .ua          (bb_ua),
        .vdd_bb_hi_s (bb_vdd_hi_s),
        .vss_lo_s    (bb_vss_lo_s),
        .vss_lo_w    (bb_vss_lo_w),
        .vss_lo_e    (bb_vss_lo_e)
    );
`endif

endmodule

`default_nettype wire

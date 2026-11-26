// SPDX-FileCopyrightText: © 2025 Project Template Contributors
// SPDX-License-Identifier: Apache-2.0

`default_nettype none

`include "generated_defines.svh"
`include "slot_defines.svh"

`ifdef SRAM_gf180mcu_ocd_ip_sram
`define gf180mcu_xxx_ip_sram__sram512x8m8wm1 gf180mcu_ocd_ip_sram__sram512x8m8wm1
`else
`define gf180mcu_xxx_ip_sram__sram512x8m8wm1 gf180mcu_fd_ip_sram__sram512x8m8wm1
`endif

`ifdef PAD_gf180mcu_ocd_io
`define gf180mcu_xxx_io__vdd gf180mcu_ocd_io__vdd
`define gf180mcu_xxx_io__vss gf180mcu_ocd_io__vss
`define gf180mcu_xxx_io__dvdd gf180mcu_ocd_io__dvdd
`define gf180mcu_xxx_io__dvss gf180mcu_ocd_io__dvss
`define gf180mcu_xxx_io__in_s gf180mcu_ocd_io__in_s
`define gf180mcu_xxx_io__in_c gf180mcu_ocd_io__in_c
`define gf180mcu_xxx_io__bi_t gf180mcu_ocd_io__bi_t
`define gf180mcu_xxx_io__asig_5p0 gf180mcu_ocd_io__asig_5p0
`else
`define gf180mcu_xxx_io__vdd gf180mcu_fd_io__dvdd
`define gf180mcu_xxx_io__vss gf180mcu_fd_io__dvss
`define gf180mcu_xxx_io__dvdd gf180mcu_fd_io__dvdd
`define gf180mcu_xxx_io__dvss gf180mcu_fd_io__dvss
`define gf180mcu_xxx_io__in_s gf180mcu_fd_io__in_s
`define gf180mcu_xxx_io__in_c gf180mcu_fd_io__in_c
`define gf180mcu_xxx_io__bi_t gf180mcu_fd_io__bi_t
`define gf180mcu_xxx_io__asig_5p0 gf180mcu_fd_io__asig_5p0
`endif

module chip_top #(
    // Power/ground pads for I/O
    parameter NUM_DVDD_PADS = `NUM_DVDD_PADS,
    parameter NUM_DVSS_PADS = `NUM_DVSS_PADS,
`ifdef HAS_MEGABYTEBEAT
    parameter NUM_DVDD_BB_PADS = `NUM_DVDD_BB_PADS,
    parameter NUM_DVSS_BB_PADS = `NUM_DVSS_BB_PADS,
    parameter NUM_BB_UO_PADS = `NUM_BB_UO_PADS,
    parameter NUM_BB_ANALOG_PADS = `NUM_BB_ANALOG_PADS,
`ifdef HAS_NC_BB_PADS
    parameter NUM_NC_BB_PADS = `NUM_NC_BB_PADS,
`endif
`endif

    // Power/ground pads for core
    parameter NUM_VDD_PADS = `NUM_VDD_PADS,
    parameter NUM_VSS_PADS = `NUM_VSS_PADS,

    // Signal pads
    parameter NUM_INPUT_PADS = `NUM_INPUT_PADS,
    parameter NUM_BIDIR_PADS = `NUM_BIDIR_PADS,
    parameter NUM_ANALOG_PADS = `NUM_ANALOG_PADS,
    parameter NUM_NC_PADS = `NUM_NC_PADS
    )(
    `ifdef USE_POWER_PINS
    inout  wire VDD,
    inout  wire VSS,
    inout  wire VDDA,       // isolated analog supply (VDDA pad + ring breaks)
    inout  wire VDDV,       // VCO supply (VDDV pad, its own ring section)
    inout  wire VSSA,       // isolated analog ground (north analog VSS pads + fill10_noPWR breaks)
    inout  wire DVDD,
    inout  wire DVSS,
    `ifdef HAS_MEGABYTEBEAT
    inout  wire VDD_BB,     // isolated megabytebeat supply (fill10_noVDD + fill10_supplyVDD)
    inout  wire DVDD_BB,    // megabytebeat padring supply (shorted to VDD_BB in gf180mcu_fd_io)
    `endif
    `endif

    inout  wire clk_PAD,
    inout  wire rst_n_PAD,

    inout  wire [NUM_INPUT_PADS-1:0] input_PAD,
    inout  wire [NUM_BIDIR_PADS-1:0] bidir_PAD,

    inout  wire [NUM_ANALOG_PADS-1:0] analog_PAD,
    inout  wire rf_PAD,         // GNSS RF input (50 ohm)
    inout  wire [NUM_NC_PADS-1:0] nc_PAD
`ifdef HAS_MEGABYTEBEAT
    ,
    inout  wire        bb_clk_PAD,    // megabytebeat 2.048 MHz clock (VDD_BB domain)
    inout  wire        bb_rst_n_PAD,  // megabytebeat active-low reset (VDD_BB domain)
    inout  wire        bb_ena_PAD,    // megabytebeat enable, internal pull-down (VDD_BB domain)
    inout  wire [7:0]  bb_ui_PAD,     // megabytebeat param {b[3:0], a[3:0]} (VDD_BB domain)
    inout  wire [7:0]  bb_uio_PAD,    // megabytebeat param {d[3:0], c[3:0]} (VDD_BB domain)
    inout  wire [NUM_BB_UO_PADS-1:0]     bb_uo_PAD, // megabytebeat PWM audio {trk2, trk1, trk0}
    inout  wire [NUM_BB_ANALOG_PADS-1:0] bb_ua_PAD  // megabytebeat 5x 8-bit R-2R DAC analog out
`ifdef HAS_NC_BB_PADS
    ,
    inout  wire [NUM_NC_BB_PADS-1:0] nc_bb_PAD
`endif
`endif
);

    wire clk_PAD2CORE;
    wire rst_n_PAD2CORE;

    wire [NUM_INPUT_PADS-1:0] input_PAD2CORE;
    wire [NUM_INPUT_PADS-1:0] input_CORE2PAD_PU;
    wire [NUM_INPUT_PADS-1:0] input_CORE2PAD_PD;

    wire [NUM_BIDIR_PADS-1:0] bidir_PAD2CORE;
    wire [NUM_BIDIR_PADS-1:0] bidir_CORE2PAD;
    wire [NUM_BIDIR_PADS-1:0] bidir_CORE2PAD_OE;
    wire [NUM_BIDIR_PADS-1:0] bidir_CORE2PAD_CS;
    wire [NUM_BIDIR_PADS-1:0] bidir_CORE2PAD_SL;
    wire [NUM_BIDIR_PADS-1:0] bidir_CORE2PAD_IE;
    wire [NUM_BIDIR_PADS-1:0] bidir_CORE2PAD_PU;
    wire [NUM_BIDIR_PADS-1:0] bidir_CORE2PAD_PD;
    wire [NUM_BIDIR_PADS-1:0] bidir_CORE2PAD_PDRV0;
    wire [NUM_BIDIR_PADS-1:0] bidir_CORE2PAD_PDRV1;

`ifdef HAS_MEGABYTEBEAT
    wire        bb_clk_PAD2CORE;
    wire        bb_rst_n_PAD2CORE;
    wire        bb_ena_PAD2CORE;
    wire [7:0]  bb_ui_PAD2CORE;
    wire [7:0]  bb_uio_PAD2CORE;
    wire [NUM_BB_UO_PADS-1:0] bb_uo_CORE2PAD;
    wire [NUM_BB_UO_PADS-1:0] bb_uo_PAD2CORE_unused;
    wire        bb_vdd_hi_s;
    wire        bb_vss_lo_s;
    wire        bb_vss_lo_w;
    wire        bb_vss_lo_e;
`endif

    // In the foundry pads, the I/O and core voltage domains are shorted per domain
    `ifdef USE_POWER_PINS
    `ifdef PAD_gf180mcu_fd_io
    assign VDD = DVDD;
    assign VSS = DVSS;
    `ifdef HAS_MEGABYTEBEAT
    assign VDD_BB = DVDD_BB;
    `endif
    `endif
    `endif

    // Power/ground pad instances
    generate
    for (genvar i=0; i<NUM_DVDD_PADS; i++) begin : dvdd_pads
        (* keep *)
        `gf180mcu_xxx_io__dvdd pad (
            `ifdef USE_POWER_PINS
            .DVDD   (DVDD),
            .DVSS   (DVSS),
            `ifdef PAD_gf180mcu_ocd_io
            .VDD    (VDD),
            `endif
            .VSS    (VSS)
            `endif
        );
    end
    for (genvar i=0; i<NUM_DVSS_PADS; i++) begin : dvss_pads
        (* keep *)
        `gf180mcu_xxx_io__dvss pad (
            `ifdef USE_POWER_PINS
            .DVDD   (DVDD),
            .DVSS   (DVSS),
            `ifdef PAD_gf180mcu_ocd_io
            .VSS    (VSS),
            `endif
            .VDD    (VDD)
            `endif
        );
    end
    for (genvar i=0; i<NUM_VDD_PADS; i++) begin : vdd_pads
        (* keep *)
        `gf180mcu_xxx_io__vdd pad (
            `ifdef USE_POWER_PINS
            .DVDD   (DVDD),
            .DVSS   (DVSS),
            `ifdef PAD_gf180mcu_ocd_io
            .VDD    (VDD),
            `endif
            .VSS    (VSS)
            `endif
        );
    end
    for (genvar i=0; i<NUM_VSS_PADS; i++) begin : vss_pads
        (* keep *)
        `gf180mcu_xxx_io__vss pad (
            `ifdef USE_POWER_PINS
            .DVDD   (DVDD),
            .DVSS   (DVSS),
            `ifdef PAD_gf180mcu_ocd_io
            .VSS    (VSS),
            `endif
            .VDD    (VDD)
            `endif
        );
    end
    endgenerate

`ifdef HAS_MEGABYTEBEAT
    // megabytebeat dedicated VDD_BB power pads & shared-VSS ground pads in VDD_BB ring section
    generate
    for (genvar i=0; i<NUM_DVDD_BB_PADS; i++) begin : dvdd_bb_pads
        (* keep *)
        `gf180mcu_xxx_io__dvdd pad (
            `ifdef USE_POWER_PINS
            .DVDD   (DVDD_BB),
            .DVSS   (DVSS),
            `ifdef PAD_gf180mcu_ocd_io
            .VDD    (VDD_BB),
            `endif
            .VSS    (VSS)
            `endif
        );
    end
    for (genvar i=0; i<NUM_DVSS_BB_PADS; i++) begin : dvss_bb_pads
        (* keep *)
        `gf180mcu_xxx_io__dvss pad (
            `ifdef USE_POWER_PINS
            .DVDD   (DVDD_BB),
            .DVSS   (DVSS),
            `ifdef PAD_gf180mcu_ocd_io
            .VSS    (VSS),
            `endif
            .VDD    (VDD_BB)
            `endif
        );
    end
    endgenerate

    // VDD domain separation breaks on East & West padring (cuts VDD/DVDD, keeps VSS/DVSS continuous)
    (* keep *)
    fill10_noVDD brk_vdd_e (
        `ifdef USE_POWER_PINS
        .DVSS (DVSS),
        .VSS  (VSS)
        `endif
    );
    (* keep *)
    fill10_noVDD brk_vdd_w (
        `ifdef USE_POWER_PINS
        .DVSS (DVSS),
        .VSS  (VSS)
        `endif
    );

    // VDD_BB supply taps (6x fill10_supplyVDD: 3 East + 3 West) feeding Metal5 VDD_BB straps
    (* keep *)
    fill10_supplyVDD sup_vdd_e0 (
        `ifdef USE_POWER_PINS
        .DVDD (DVDD_BB), .DVSS (DVSS), .VSS (VSS)
        `endif
    );
    (* keep *)
    fill10_supplyVDD sup_vdd_e1 (
        `ifdef USE_POWER_PINS
        .DVDD (DVDD_BB), .DVSS (DVSS), .VSS (VSS)
        `endif
    );
    (* keep *)
    fill10_supplyVDD sup_vdd_e2 (
        `ifdef USE_POWER_PINS
        .DVDD (DVDD_BB), .DVSS (DVSS), .VSS (VSS)
        `endif
    );
    (* keep *)
    fill10_supplyVDD sup_vdd_w0 (
        `ifdef USE_POWER_PINS
        .DVDD (DVDD_BB), .DVSS (DVSS), .VSS (VSS)
        `endif
    );
    (* keep *)
    fill10_supplyVDD sup_vdd_w1 (
        `ifdef USE_POWER_PINS
        .DVDD (DVDD_BB), .DVSS (DVSS), .VSS (VSS)
        `endif
    );
    (* keep *)
    fill10_supplyVDD sup_vdd_w2 (
        `ifdef USE_POWER_PINS
        .DVDD (DVDD_BB), .DVSS (DVSS), .VSS (VSS)
        `endif
    );
`endif

    // Signal IO pad instances (GNSS digital VDD domain)

    // Schmitt trigger on SCLK
    `gf180mcu_xxx_io__in_s clk_pad (
        `ifdef USE_POWER_PINS
        .DVDD   (DVDD),
        .DVSS   (DVSS),
        .VDD    (VDD),
        .VSS    (VSS),
        `endif

        .Y      (clk_PAD2CORE),
        .PAD    (clk_PAD),

        .PU     (1'b0),
        .PD     (1'b0)
    );

    // Schmitt trigger on RST_N (safe with external RC reset)
    `gf180mcu_xxx_io__in_s rst_n_pad (
        `ifdef USE_POWER_PINS
        .DVDD   (DVDD),
        .DVSS   (DVSS),
        .VDD    (VDD),
        .VSS    (VSS),
        `endif

        .Y      (rst_n_PAD2CORE),
        .PAD    (rst_n_PAD),

        .PU     (1'b0),
        .PD     (1'b0)
    );

    // Schmitt trigger on SDI and CS_N
    generate
    for (genvar i=0; i<NUM_INPUT_PADS; i++) begin : inputs
        (* keep *)
        `gf180mcu_xxx_io__in_s pad (
            `ifdef USE_POWER_PINS
            .DVDD   (DVDD),
            .DVSS   (DVSS),
            .VDD    (VDD),
            .VSS    (VSS),
            `endif

            .Y      (input_PAD2CORE[i]),
            .PAD    (input_PAD[i]),

            .PU     (input_CORE2PAD_PU[i]),
            .PD     (input_CORE2PAD_PD[i])
        );
    end
    endgenerate

    // Low-drive (4 mA, PDRV0=0, PDRV1=0, SL=1) bidirectional pads for GNSS digital I/O
    generate
    for (genvar i=0; i<NUM_BIDIR_PADS; i++) begin : bidir
        (* keep *)
        `gf180mcu_xxx_io__bi_t pad (
            `ifdef USE_POWER_PINS
            .DVDD   (DVDD),
            .DVSS   (DVSS),
            .VDD    (VDD),
            .VSS    (VSS),
            `endif

            .A      (bidir_CORE2PAD[i]),
            .OE     (bidir_CORE2PAD_OE[i]),
            .Y      (bidir_PAD2CORE[i]),
            .PAD    (bidir_PAD[i]),

            .CS     (bidir_CORE2PAD_CS[i]),
            .SL     (bidir_CORE2PAD_SL[i]),
            .IE     (bidir_CORE2PAD_IE[i]),

            .PU     (bidir_CORE2PAD_PU[i]),
            .PD     (bidir_CORE2PAD_PD[i]),
            .PDRV0  (bidir_CORE2PAD_PDRV0[i]),
            .PDRV1  (bidir_CORE2PAD_PDRV1[i])
        );
    end
    endgenerate

`ifdef HAS_MEGABYTEBEAT
    // megabytebeat signal IO pads (100% in VDD_BB domain, control pins driven by VDD_BB-domain ties)
    (* keep *)
    `gf180mcu_xxx_io__in_s bb_clk_pad (
        `ifdef USE_POWER_PINS
        .DVDD (DVDD_BB), .DVSS (DVSS), .VDD (VDD_BB), .VSS (VSS),
        `endif
        .Y    (bb_clk_PAD2CORE),
        .PAD  (bb_clk_PAD),
        .PU   (bb_vss_lo_s),
        .PD   (bb_vss_lo_s)
    );

    (* keep *)
    `gf180mcu_xxx_io__in_s bb_rst_n_pad (
        `ifdef USE_POWER_PINS
        .DVDD (DVDD_BB), .DVSS (DVSS), .VDD (VDD_BB), .VSS (VSS),
        `endif
        .Y    (bb_rst_n_PAD2CORE),
        .PAD  (bb_rst_n_PAD),
        .PU   (bb_vss_lo_s),
        .PD   (bb_vss_lo_s)
    );

    // bb_ena has internal pull-down (PD=1 from VDD_BB) -> defaults to 0 (stopped) when unconnected
    (* keep *)
    `gf180mcu_xxx_io__in_s bb_ena_pad (
        `ifdef USE_POWER_PINS
        .DVDD (DVDD_BB), .DVSS (DVSS), .VDD (VDD_BB), .VSS (VSS),
        `endif
        .Y    (bb_ena_PAD2CORE),
        .PAD  (bb_ena_PAD),
        .PU   (bb_vss_lo_s),
        .PD   (bb_vdd_hi_s)
    );

    generate
    for (genvar i=0; i<8; i++) begin : bb_ui_pads
        (* keep *)
        `gf180mcu_xxx_io__in_s pad (
            `ifdef USE_POWER_PINS
            .DVDD (DVDD_BB), .DVSS (DVSS), .VDD (VDD_BB), .VSS (VSS),
            `endif
            .Y    (bb_ui_PAD2CORE[i]),
            .PAD  (bb_ui_PAD[i]),
            .PU   (bb_vss_lo_w),
            .PD   (bb_vss_lo_w)
        );
    end
    for (genvar i=0; i<8; i++) begin : bb_uio_pads
        (* keep *)
        `gf180mcu_xxx_io__in_s pad (
            `ifdef USE_POWER_PINS
            .DVDD (DVDD_BB), .DVSS (DVSS), .VDD (VDD_BB), .VSS (VSS),
            `endif
            .Y    (bb_uio_PAD2CORE[i]),
            .PAD  (bb_uio_PAD[i]),
            .PU   (bb_vss_lo_e),
            .PD   (bb_vss_lo_e)
        );
    end
    for (genvar i=0; i<NUM_BB_UO_PADS; i++) begin : bb_uo_pads
        (* keep *)
        `gf180mcu_xxx_io__bi_t pad (
            `ifdef USE_POWER_PINS
            .DVDD  (DVDD_BB), .DVSS (DVSS), .VDD (VDD_BB), .VSS (VSS),
            `endif
            .A     (bb_uo_CORE2PAD[i]),
            .OE    (bb_vdd_hi_s),
            .Y     (bb_uo_PAD2CORE_unused[i]),
            .PAD   (bb_uo_PAD[i]),
            .CS    (bb_vss_lo_s),
            .SL    (bb_vdd_hi_s),
            .IE    (bb_vss_lo_s),
            .PU    (bb_vss_lo_s),
            .PD    (bb_vss_lo_s),
            .PDRV0 (bb_vss_lo_s),
            .PDRV1 (bb_vss_lo_s)
        );
    end
    for (genvar i=0; i<NUM_BB_ANALOG_PADS; i++) begin : bb_ua_pads
        (* keep *)
        `gf180mcu_xxx_io__asig_5p0 pad (
            `ifdef USE_POWER_PINS
            .DVDD   (DVDD_BB),
            .DVSS   (DVSS),
            .VDD    (VDD_BB),
            .VSS    (VSS),
            `endif
            .ASIG5V (bb_ua_PAD[i])
        );
    end
`ifdef HAS_NC_BB_PADS
    for (genvar i=0; i<NUM_NC_BB_PADS; i++) begin : nc_bb
        (* keep *)
        `gf180mcu_xxx_io__asig_5p0 pad (
            `ifdef USE_POWER_PINS
            .DVDD   (DVDD_BB),
            .DVSS   (DVSS),
            .VDD    (VDD_BB),
            .VSS    (VSS),
            `endif
            .ASIG5V (nc_bb_PAD[i])
        );
    end
`endif
    endgenerate
`endif

    generate
    for (genvar i=0; i<NUM_ANALOG_PADS; i++) begin : analog
        (* keep *)
        `gf180mcu_xxx_io__asig_5p0 pad (
            `ifdef USE_POWER_PINS
            .DVDD   (DVDD),
            .DVSS   (DVSS),
            .VDD    (VDD),
            .VSS    (VSS),
            `endif
            .ASIG5V (analog_PAD[i])
        );
    end
    endgenerate

    // Unused pad positions in GNSS VDD ring (fixed bonding frame)
    generate
    for (genvar i=0; i<NUM_NC_PADS; i++) begin : nc
        (* keep *)
        `gf180mcu_xxx_io__asig_5p0 pad (
            `ifdef USE_POWER_PINS
            .DVDD   (DVDD),
            .DVSS   (DVSS),
            .VDD    (VDD),
            .VSS    (VSS),
            `endif
            .ASIG5V (nc_PAD[i])
        );
    end
    endgenerate

    // GNSS AFE analog pad block (six pads on the template pitch)
    (* keep *)
    fill10_noPWR brk_w ();
    (* keep *)
    fill10_noPWR brk_e ();
    (* keep *)
    gf180mcu_gnss_io__afe_rf afe_rf_pad (
        `ifdef USE_POWER_PINS
        .VDDA   (VDDA),
        .VDDV   (VDDV),
        .VSSA   (VSSA),
        `endif
        .RF     (rf_PAD)
    );

    // Core design
    chip_core #(
        .NUM_INPUT_PADS  (NUM_INPUT_PADS),
        .NUM_BIDIR_PADS  (NUM_BIDIR_PADS),
        .NUM_ANALOG_PADS (NUM_ANALOG_PADS)
`ifdef HAS_MEGABYTEBEAT
        ,
        .NUM_BB_UO_PADS     (NUM_BB_UO_PADS),
        .NUM_BB_ANALOG_PADS (NUM_BB_ANALOG_PADS)
`endif
    ) i_chip_core (
        `ifdef USE_POWER_PINS
        .VDD        (VDD),
        .VSS        (VSS),
        .VDDA       (VDDA),
        .VDDV       (VDDV),
        .VSSA       (VSSA),
        `ifdef HAS_MEGABYTEBEAT
        .VDD_BB     (VDD_BB),
        `endif
        `endif

        .clk        (clk_PAD2CORE),
        .rst_n      (rst_n_PAD2CORE),

        .input_in   (input_PAD2CORE),
        .input_pu   (input_CORE2PAD_PU),
        .input_pd   (input_CORE2PAD_PD),

        .bidir_in    (bidir_PAD2CORE),
        .bidir_out   (bidir_CORE2PAD),
        .bidir_oe    (bidir_CORE2PAD_OE),
        .bidir_cs    (bidir_CORE2PAD_CS),
        .bidir_sl    (bidir_CORE2PAD_SL),
        .bidir_ie    (bidir_CORE2PAD_IE),
        .bidir_pu    (bidir_CORE2PAD_PU),
        .bidir_pd    (bidir_CORE2PAD_PD),
        .bidir_pdrv0 (bidir_CORE2PAD_PDRV0),
        .bidir_pdrv1 (bidir_CORE2PAD_PDRV1),

        .analog     (analog_PAD),
        .rfin       (rf_PAD)
`ifdef HAS_MEGABYTEBEAT
        ,
        .bb_clk       (bb_clk_PAD2CORE),
        .bb_rst_n     (bb_rst_n_PAD2CORE),
        .bb_ena       (bb_ena_PAD2CORE),
        .bb_ui_in     (bb_ui_PAD2CORE),
        .bb_uio_in    (bb_uio_PAD2CORE),
        .bb_uo_out    (bb_uo_CORE2PAD),
        .bb_ua        (bb_ua_PAD),
        .bb_vdd_hi_s  (bb_vdd_hi_s),
        .bb_vss_lo_s  (bb_vss_lo_s),
        .bb_vss_lo_w  (bb_vss_lo_w),
        .bb_vss_lo_e  (bb_vss_lo_e)
`endif
    );

    // Do not remove, necessary for tapeout
    (* keep *) gf180mcu_ws_ip__qrcode_id qrcode_id ();
    (* keep *) gf180mcu_ws_ip__shuttle_id shuttle_id ();
    (* keep *) gf180mcu_ws_ip__project_id project_id ();
    (* keep *) gf180mcu_ws_ip__marker marker ();

    // wafer.space logo - can be removed if desired
    (* keep *) gf180mcu_ws_ip__logo wafer_space_logo ();

endmodule

`default_nettype wire

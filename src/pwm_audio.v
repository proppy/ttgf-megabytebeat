// from https://github.com/MichaelBell/tt08-pwm-example/blob/main/src/pwm.v
// SPDX-License-Identifier: Apache-2.0

`default_nettype none

// PWM audio output.
// The output is high for sample clocks out of every 256 clocks.
// This means a sample of 0 is always off.
module pwm_audio (
    input wire clk,
    input wire rst_n,

    input wire [7:0] sample,

    output reg pwm
);

    reg [7:0] count;

    always @(posedge clk) begin
        if (!rst_n) begin
            count <= 0;
            pwm <= 0;
        end else begin
            // PWM output high for sample clocks out of every 256 clocks
            pwm <= count < sample;

            // Wrap the 8-bit counter every 256 clocks
            count <= count + 1;
        end
    end

endmodule

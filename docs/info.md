```
░█▀▄░█░█░▀█▀░█▀▀░█▀▄░█▀▀░█▀█░▀█▀░▀░█▀▀
░█▀▄░░█░░░█░░█▀▀░█▀▄░█▀▀░█▀█░░█░░░░▀▀█
░▀▀░░░▀░░░▀░░▀▀▀░▀▀░░▀▀▀░▀░▀░░▀░░░░▀▀▀
       • ▌ ▄ ·. ▄▄▄ . ▄▄ •  ▄▄▄· 
       ·██ ▐███▪▀▄.▀·▐█ ▀ ▪▐█ ▀█ 
       ▐█ ▌▐▌▐█·▐▀▀▪▄▄█ ▀█▄▄█▀▀█ 
       ██ ██▌▐█▌▐█▄▄▌▐█▄▪▐█▐█ ▪▐▌
       ▀▀  █▪▀▀▀ ▀▀▀ ·▀▀▀▀  ▀  ▀ 
 █████   █████ █████ ███████████  █████████ 
░░███   ░░███ ░░███ ░█░░░███░░░█ ███░░░░░███
 ░███    ░███  ░███ ░   ░███  ░ ░███    ░░░ 
 ░███████████  ░███     ░███    ░░█████████ 
 ░███░░░░░███  ░███     ░███     ░░░░░░░░███
 ░███    ░███  ░███     ░███     ███    ░███
 █████   █████ █████    █████   ░░█████████ 
░░░░░   ░░░░░ ░░░░░    ░░░░░     ░░░░░░░░░  
      ,----,     ,----..        ,---,     ,---, 
    .'   .' \   /   /   \    ,`--.' |  ,`--.' | 
  ,----,'    | /   .     :  /    /  : /    /  : 
  |    :  .  ;.   /   ;.  \:    |.' ':    |.' ' 
  ;    |.'  /.   ;   /  ` ;`----':  |`----':  | 
  `----'/  ; ;   |  ; \ ; |   '   ' ;   '   ' ; 
    /  ;  /  |   :  | ; | '   |   | |   |   | | 
   ;  /  /-, .   |  ' ' ' :   '   : ;   '   : ; 
  /  /  /.`| '   ;  \; /  |   |   | '   |   | ' 
./__;      :  \   \  ',  /    '   : |   '   : | 
|   :    .'    ;   :    /     ;   |.'   ;   |.' 
;   | .'        \   \ .'      '---'     '---'   
`---'            `---`                          
```

Extended Play of some of the 2011's BYTEBEAT greatest hits on-a-chip:

| PWM Pin | DAC Pin | Track | Artist |
| ------- | ------- | ----- | ------ |
| `uo[0]` | `ua[0]` | the 42 melody | people on irc |
| `uo[1]` | `ua[1]` | fractal trees | danharaj |
| `uo[2]` | `ua[2]` | untitled | droid |
| `uo[3]` | `ua[3]` | a tune to share | Niklas_Roy |
| `uo[7]` | `ua[4]` | sierpinski harmony | miiro |

## How it works

The unified XLS core reads 4× 4-bit parameter buses (`a = ui[3:0]`, `b = ui[7:4]`, `c = uio[3:0]`, `d = uio[7:4]`) and computes 5 concurrent 8-bit PCM bytebeat streams at `8 kHz` (`2.048 MHz / 256`):

| PWM Pin | DAC Pin | Formula | Original Params |
| ------- | ------- | ------- | --------------- |
| `uo[0]` | `ua[0]` | `t*({b,a}&t>>{d,c})` | `a=0xa,b=0x2,c=0xa,d=0x0` |
| `uo[1]` | `ua[1]` | `t\|t%{b,a}\|t%(2+{d,c})` | `a=0xf,b=0xf,c=0xf,d=0xf` |
| `uo[2]` | `ua[2]` | `t>>a&b?t>>c:-t>>d` | `a=0x6,b=0x1,c=0x5,d=0x4` |
| `uo[3]` | `ua[3]` | `t*(t>>a\|t>>b)&{d,c}` | `a=0x9,b=0xd,c=0x0,d=0x1` |
| `uo[7]` | `ua[4]` | `t*a&(t>>b)\|t*c&(t>>d)` | `a=0x5,b=0x7,c=0x3,d=0xa` |

All 5 tracks are output simultaneously as:
- **Digital 8-bit PWM** on `uo[0..3]` and `uo[7]` (`256`-clock frames at `2.048 MHz`).
- **Analog 8-bit R-2R DACs** on `ua[0..4]` via five on-chip passive poly-resistor R-2R ladders (`r2r_ladder_8bit`, `~20.5 kΩ` output impedance, `0..VDD` rail-to-rail swing).

PCM frames are registered inside the XLS core for glitch-free updates at frame boundaries, and holding reset (`rst_n = 0`) or deselecting the project (`ena = 0`) combinationally clamps all 5 DAC ladders to `0 V` for near-zero standby leakage.

## How to test

- **Analog R-2R DACs (`ua[0..4]`):** Connect `ua[0..4]` to an op-amp buffer, audio amplifier, oscilloscope, or high-impedance line-in (`>= 100 kΩ` input impedance; do not drive a low-impedance speaker directly from the `~20.5 kΩ` passive R-2R ladder).
- **Digital PWM (`uo[0..3]`, `uo[7]`):** Connect a speaker or Tiny Tapeout Audio PMOD to the PWM pin you want to play.
- **Parameters (`ui[7:0]`, `uio[7:0]`):** Tweak `a`, `b`, `c`, `d` using binary rotary switches or DIP switches.

## External hardware

- [Rotary switches](https://akizukidenshi.com/catalog/g/g102276/) w/ 16 positions for `ui[7:0]` (`a`, `b`) and `uio[7:0]` (`c`, `d`).
- Op-amp buffer / audio amplifier / line-in for the 5× `ua[0..4]` analog DAC outputs.
- Speakers or [TinyTapeout Audio PMOD](https://github.com/MichaelBell/tt-audio-pmod) if you want to hear `uo[7]`.

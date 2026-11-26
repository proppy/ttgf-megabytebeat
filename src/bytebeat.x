// Copyright 2025 Google LLC.
// SPDX-License-Identifier: Apache-2.0

// Canonical 8-bit bytebeat synthesizer formulas shared across tracks.

pub fn the42melody(t: u32, a: u4, b: u4, c: u4, d: u4) -> u8 {
    let ab = (b ++ a) as u32;
    let cd = (d ++ c) as u32;
    let s = t * (ab & t >> cd);
    s as u8
}

fn umod_u32_u9(n: u32, d: u9) -> u9 {
    let d10 = d as u10;
    for (i, rem): (u32, u9) in u32:0..u32:32 {
        let bit = n[(u32:31 - i)+:u1];
        let cand: u10 = rem ++ bit;
        if cand >= d10 { (cand - d10) as u9 } else { cand as u9 }
    }(u9:0)
}

pub fn fractaltrees(t: u32, a: u4, b: u4, c: u4, d: u4) -> u8 {
    let ab8 = b ++ a;
    let ab: u9 = if ab8 == u8:0 { u9:1 } else { ab8 as u9 };
    let cd_257: u9 = ((d ++ c) as u9) + u9:2;
    let r0 = umod_u32_u9(t, ab) as u8;
    let r1 = umod_u32_u9(t, cd_257) as u8;
    (t as u8) | r0 | r1
}

#[test]
fn umod_u32_u9_test() {
    assert_eq(umod_u32_u9(u32:0x12345678, u9:42), (u32:0x12345678 % u32:42) as u9);
    assert_eq(umod_u32_u9(u32:0xffffffff, u9:257), (u32:0xffffffff % u32:257) as u9);
    assert_eq(umod_u32_u9(u32:100000, u9:1), u9:0);
    assert_eq(umod_u32_u9(u32:987654321, u9:255), (u32:987654321 % u32:255) as u9);
}

pub fn untitleddroid(t: u32, a: u4, b: u4, c: u4, d: u4) -> u8 {
    let cond0 = t >> (a as u32) & (b as u32);
    let p0 = t >> (c as u32);
    let p1 = ((!t) + u32:1) >> (d as u32);
    let s = if cond0 != u32:0 { p0 } else { p1 };
    s as u8
}

pub fn atunetoshare(t: u32, a: u4, b: u4, c: u4, d: u4) -> u8 {
    let cd = (d ++ c) as u32;
    let s = t * (t >> (a as u32) | t >> (b as u32)) & cd;
    s as u8
}

pub fn sierpinskiharmony(t: u32, a: u4, b: u4, c: u4, d: u4) -> u8 {
    let s = ((t * a as u32) & (t >> b as u32)) | ((t * c as u32) & (t >> d as u32));
    s as u8
}

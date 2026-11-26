// Copyright 2025 Google LLC.
// SPDX-License-Identifier: Apache-2.0

// Canonical 8-bit bytebeat synthesizer formulas shared across tracks.

import std;

pub fn the42melody(t: u32, a: u4, b: u4, c: u4, d: u4) -> u8 {
    let ab = (b ++ a) as u32;
    let cd = (d ++ c) as u32;
    let s = t * (ab & t >> cd);
    s as u8
}

pub fn fractaltrees(t: u32, a: u4, b: u4, c: u4, d: u4) -> u8 {
    let ab = b ++ a;
    let ab = if ab == u8:0 { u8:1 } else { ab };
    let cd_257 = ((d ++ c) as u9) + u9:2;
    let (_, r0) = std::iterative_div_mod(t, ab);
    let (_, r1) = std::iterative_div_mod(t, cd_257);
    (t as u8) | r0 | (r1 as u8)
}

#[test]
fn fractaltrees_mod_test() {
    for (t, _): (u32, ()) in u32:0..u32:64 {
        for (ab, _): (u8, ()) in u8:0..u8:8 {
            let a = ab[0+:u4];
            let b = ab[4+:u4];
            let c = u4:0xf;
            let d = u4:0xf;
            let ab_nz = if ab == u8:0 { u32:1 } else { ab as u32 };
            let cd_257 = ((d ++ c) as u32) + u32:2;
            let expected = (t | (t % ab_nz) | (t % cd_257)) as u8;
            assert_eq(fractaltrees(t, a, b, c, d), expected);
        }(());
    }(());
    // Also check large t with max divisors (ab=255, cd+2=257).
    let t_hi = u32:0x12345678;
    assert_eq(
        fractaltrees(t_hi, u4:0xf, u4:0xf, u4:0xf, u4:0xf),
        (t_hi | (t_hi % u32:255) | (t_hi % u32:257)) as u8,
    );
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

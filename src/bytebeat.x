// Copyright 2025 Google LLC.
// SPDX-License-Identifier: Apache-2.0

// Canonical 8-bit bytebeat synthesizer formulas shared across tracks.

pub fn the42melody(t: u32, a: u4, b: u4, c: u4, d: u4) -> u8 {
    let ab = (b ++ a) as u32;
    let cd = (d ++ c) as u32;
    let s = t * (ab & t >> cd);
    s as u8
}

pub fn fractaltrees(t: u32, a: u4, b: u4, c: u4, d: u4) -> u8 {
    let ab = (b ++ a) as u32;
    let ab = if ab == u32:0 { u32:1 } else { ab };
    let cd = (d ++ c) as u32;
    let cd_257 = cd + u32:2;
    let s = t | t % ab | t % cd_257;
    s as u8
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

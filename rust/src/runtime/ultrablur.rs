//! The four UltraBlur corner colours of an image, as PMS 1.43.4 works them out (read off its
//! binary and checked against plexav's `/services/ultrablur/colors?url=`, docs/findings.md
//! "Artwork BlurHash and UltraBlur colours").
//!
//! Each quadrant's palette is color-thief's MMCQ (every fifth pixel, near-white left out, eight
//! boxes by count then ten by count times volume); each colour is scored in Okhsl -- rank, a dark
//! lightness, a strong saturation, its distance from the colour it sits beside, yellow-greens and
//! skin tones and washed-out ones penalised -- and the best is kept, its lightness capped at 0.2
//! top left and 0.4 elsewhere and a saturation over 0.2 raised to 0.6. Top right and bottom left
//! are scored against top left, bottom right against bottom left. Ties fall as libc++'s
//! `std::sort` leaves them, and `cbrtf`, `powf`, `cosf`, `sinf` and `atan2f` are musl's (PMS
//! ships musl 1.2.2) bit for bit: a grey's hue is their rounding noise, which moves its score.

/// TL, TR, BR, BL as lowercase hex, or nothing where PMS answers no palette (a quadrant of
/// near-white alone: plexav's 500 for a white image).
pub(super) fn corners(width: usize, height: usize, rgb: &[u8]) -> Option<[String; 4]> {
    let capped;
    let (width, height, rgb) = if width * height > 8_294_400 {
        // PMS caps the decode at 3840x2160's pixel count, bilinearly.
        let k = (8_294_400.0 / (width * height) as f64).sqrt();
        let (to_width, to_height) = ((width as f64 * k) as usize, (height as f64 * k) as usize);
        capped = bilinear(width, height, rgb, to_width, to_height);
        (to_width, to_height, capped.as_slice())
    } else {
        (width, height, rgb)
    };
    let (half_width, half_height) = (width / 2, height / 2);
    let quadrant = |x0: usize, y0: usize| -> Vec<[u8; 3]> {
        let mut pixels = Vec::with_capacity(half_width * half_height);
        for y in y0..y0 + half_height {
            for x in x0..x0 + half_width {
                let at = (y * width + x) * 3;
                pixels.push([rgb[at], rgb[at + 1], rgb[at + 2]]);
            }
        }
        pixels
    };
    let top_left = pick(&quadrant(0, 0), 0.2, None)?;
    let top_right = pick(&quadrant(half_width, 0), 0.4, Some(&top_left))?;
    let bottom_left = pick(&quadrant(0, half_height), 0.4, Some(&top_left))?;
    let bottom_right = pick(&quadrant(half_width, half_height), 0.4, Some(&bottom_left))?;
    Some([&top_left, &top_right, &bottom_right, &bottom_left].map(|color| okhsl_to_hex(color.h, color.s, color.l)))
}

fn bilinear(width: usize, height: usize, rgb: &[u8], to_width: usize, to_height: usize) -> Vec<u8> {
    let (scale_x, scale_y) = (width as f32 / to_width as f32, height as f32 / to_height as f32);
    let mut out = Vec::with_capacity(to_width * to_height * 3);
    for y in 0..to_height {
        let fy = ((y as f32 + 0.5) * scale_y - 0.5).max(0.0);
        let (y0, wy) = ((fy as usize).min(height - 1), fy.fract());
        let y1 = (y0 + 1).min(height - 1);
        for x in 0..to_width {
            let fx = ((x as f32 + 0.5) * scale_x - 0.5).max(0.0);
            let (x0, wx) = ((fx as usize).min(width - 1), fx.fract());
            let x1 = (x0 + 1).min(width - 1);
            for channel in 0..3 {
                let at = |x: usize, y: usize| rgb[(y * width + x) * 3 + channel] as f32;
                let top = at(x0, y0) * (1.0 - wx) + at(x1, y0) * wx;
                let bottom = at(x0, y1) * (1.0 - wx) + at(x1, y1) * wx;
                out.push((top * (1.0 - wy) + bottom * wy).round() as u8);
            }
        }
    }
    out
}

// -- the palette: color-thief's MMCQ ---------------------------------------------------------

const RSHIFT: u32 = 3;
const MAX_ITERATIONS: usize = 1000;

#[derive(Clone, Copy)]
struct VBox {
    bounds: [i32; 6],
    count: i64,
}

struct Histogram(Vec<i64>);

impl Histogram {
    fn at(&self, r: i32, g: i32, b: i32) -> i64 {
        self.0[((r << 10) | (g << 5) | b) as usize]
    }

    fn vbox(&self, bounds: [i32; 6]) -> VBox {
        let [r1, r2, g1, g2, b1, b2] = bounds;
        let mut count = 0;
        for r in r1..=r2 {
            for g in g1..=g2 {
                for b in b1..=b2 {
                    count += self.at(r, g, b);
                }
            }
        }
        VBox { bounds, count }
    }

    fn average(&self, vbox: &VBox) -> [u8; 3] {
        let [r1, r2, g1, g2, b1, b2] = vbox.bounds;
        let mult = (1 << RSHIFT) as f64;
        let (mut total, mut sums) = (0i64, [0f64; 3]);
        for r in r1..=r2 {
            for g in g1..=g2 {
                for b in b1..=b2 {
                    let value = self.at(r, g, b);
                    total += value;
                    sums[0] += (r as f64 + 0.5) * value as f64 * mult;
                    sums[1] += value as f64 * (g as f64 + 0.5) * mult;
                    sums[2] += value as f64 * (b as f64 + 0.5) * mult;
                }
            }
        }
        let color = if total > 0 {
            sums.map(|sum| (sum / total as f64) as i64)
        } else {
            [(r1, r2), (g1, g2), (b1, b2)].map(|(low, high)| ((mult * (low + high) as f64 + mult) * 0.5) as i64)
        };
        // Kept in a uint8: 256 wraps to 0.
        color.map(|value| (value & 0xff) as u8)
    }

    fn median_cut(&self, vbox: &VBox) -> (VBox, Option<VBox>) {
        if vbox.count == 1 {
            return (*vbox, None);
        }
        let [r1, r2, g1, g2, b1, b2] = vbox.bounds;
        let (rw, gw, bw) = (r2 - r1 + 1, g2 - g1 + 1, b2 - b1 + 1);
        let dim = if rw >= gw.max(bw) {
            0
        } else if gw == rw.max(gw).max(bw) {
            1
        } else {
            2
        };
        let (low, high) = (vbox.bounds[2 * dim], vbox.bounds[2 * dim + 1]);
        let mut partial = Vec::with_capacity((high - low + 1) as usize);
        let mut total = 0;
        for index in low..=high {
            let mut bounds = vbox.bounds;
            bounds[2 * dim] = index;
            bounds[2 * dim + 1] = index;
            total += self.vbox(bounds).count;
            partial.push(total);
        }
        let partial_at = |index: i32| if (low..=high).contains(&index) { partial[(index - low) as usize] } else { 0 };
        let look_at = |index: i32| if (low..=high).contains(&index) { total - partial[(index - low) as usize] } else { 0 };
        for index in low..=high {
            if partial_at(index) > total / 2 {
                let (left, right) = (index - low, high - index);
                let mut cut = if left <= right {
                    (high - 1).min((index as f64 + right as f64 * 0.5) as i32)
                } else {
                    low.max(((index - 1) as f64 + left as f64 * -0.5) as i32)
                };
                while partial_at(cut) == 0 {
                    cut += 1;
                }
                // PMS's port never reads the count again while it backs off, so an empty
                // lookahead walks the cut down to the first empty slice.
                if look_at(cut) == 0 {
                    while partial_at(cut - 1) > 0 {
                        cut -= 1;
                    }
                }
                let (mut first, mut second) = (vbox.bounds, vbox.bounds);
                first[2 * dim + 1] = cut;
                second[2 * dim] = cut + 1;
                return (self.vbox(first), Some(self.vbox(second)));
            }
        }
        (*vbox, None)
    }

    fn iterate(&self, queue: &mut Vec<VBox>, target: i64, less: fn(&VBox, &VBox) -> bool) {
        let (mut colors, mut iterations) = (1, 0);
        loop {
            cxx_sort(queue, less);
            let Some(vbox) = queue.pop() else { return };
            if vbox.count == 0 {
                queue.push(vbox);
                iterations += 1;
                if iterations == MAX_ITERATIONS {
                    return;
                }
                continue;
            }
            let (first, second) = self.median_cut(&vbox);
            queue.push(first);
            if let Some(second) = second {
                queue.push(second);
                colors += 1;
            }
            if colors >= target {
                return;
            }
            iterations += 1;
            if iterations == MAX_ITERATIONS {
                return;
            }
        }
    }
}

fn by_count(a: &VBox, b: &VBox) -> bool {
    a.count < b.count
}

fn volume(vbox: &VBox) -> i64 {
    let [r1, r2, g1, g2, b1, b2] = vbox.bounds;
    ((r2 - r1 + 1) * (g2 - g1 + 1) * (b2 - b1 + 1)) as i64
}

fn by_count_volume(a: &VBox, b: &VBox) -> bool {
    a.count * volume(a) < b.count * volume(b)
}

fn palette(pixels: &[[u8; 3]]) -> Option<Vec<[u8; 3]>> {
    let mut histogram = Histogram(vec![0; 1 << 15]);
    let (mut low, mut high) = ([i32::MAX; 3], [i32::MIN; 3]);
    let mut any = false;
    for pixel in pixels.iter().step_by(5) {
        if pixel.iter().all(|value| *value > 250) {
            continue;
        }
        let bins = pixel.map(|value| (value >> RSHIFT) as i32);
        histogram.0[((bins[0] << 10) | (bins[1] << 5) | bins[2]) as usize] += 1;
        for channel in 0..3 {
            low[channel] = low[channel].min(bins[channel]);
            high[channel] = high[channel].max(bins[channel]);
        }
        any = true;
    }
    if !any {
        return None;
    }
    let mut queue = vec![histogram.vbox([low[0], high[0], low[1], high[1], low[2], high[2]])];
    histogram.iterate(&mut queue, 8, by_count);
    cxx_sort(&mut queue, by_count);
    let mut second: Vec<VBox> = queue.into_iter().rev().collect();
    let target = 10 - second.len() as i64;
    histogram.iterate(&mut second, target, by_count_volume);
    cxx_sort(&mut second, by_count_volume);
    Some(second.iter().rev().map(|vbox| histogram.average(vbox)).collect())
}

// -- libc++'s std::sort (LLVM <= 15), for its tie order ----------------------------------------

fn sort3<T>(a: &mut [T], x: usize, y: usize, z: usize, less: &impl Fn(&T, &T) -> bool) -> u32 {
    if !less(&a[y], &a[x]) {
        if !less(&a[z], &a[y]) {
            return 0;
        }
        a.swap(y, z);
        if less(&a[y], &a[x]) {
            a.swap(x, y);
            return 2;
        }
        return 1;
    }
    if less(&a[z], &a[y]) {
        a.swap(x, z);
        return 1;
    }
    a.swap(x, y);
    if less(&a[z], &a[y]) {
        a.swap(y, z);
        return 2;
    }
    1
}

fn sort4<T>(a: &mut [T], x1: usize, x2: usize, x3: usize, x4: usize, less: &impl Fn(&T, &T) -> bool) -> u32 {
    let mut swaps = sort3(a, x1, x2, x3, less);
    if less(&a[x4], &a[x3]) {
        a.swap(x3, x4);
        swaps += 1;
        if less(&a[x3], &a[x2]) {
            a.swap(x2, x3);
            swaps += 1;
            if less(&a[x2], &a[x1]) {
                a.swap(x1, x2);
                swaps += 1;
            }
        }
    }
    swaps
}

fn sort5<T>(a: &mut [T], x: [usize; 5], less: &impl Fn(&T, &T) -> bool) -> u32 {
    let mut swaps = sort4(a, x[0], x[1], x[2], x[3], less);
    if less(&a[x[4]], &a[x[3]]) {
        a.swap(x[3], x[4]);
        swaps += 1;
        if less(&a[x[3]], &a[x[2]]) {
            a.swap(x[2], x[3]);
            swaps += 1;
            if less(&a[x[2]], &a[x[1]]) {
                a.swap(x[1], x[2]);
                swaps += 1;
                if less(&a[x[1]], &a[x[0]]) {
                    a.swap(x[0], x[1]);
                    swaps += 1;
                }
            }
        }
    }
    swaps
}

/// The lengths libc++ sorts with a network; `true` when `first..last` was one of them.
fn sort_small<T>(a: &mut [T], first: usize, last: usize, less: &impl Fn(&T, &T) -> bool) -> bool {
    match last - first {
        0 | 1 => {}
        2 => {
            if less(&a[last - 1], &a[first]) {
                a.swap(first, last - 1);
            }
        }
        3 => {
            sort3(a, first, first + 1, last - 1, less);
        }
        4 => {
            sort4(a, first, first + 1, first + 2, last - 1, less);
        }
        5 => {
            sort5(a, [first, first + 1, first + 2, first + 3, last - 1], less);
        }
        _ => return false,
    }
    true
}

/// `__insertion_sort_3` (no `limit`) and `__insertion_sort_incomplete` (a limit of 8 moves).
fn insertion_sort3<T: Copy>(a: &mut [T], first: usize, last: usize, less: &impl Fn(&T, &T) -> bool, limit: Option<u32>) -> bool {
    let mut j = first + 2;
    sort3(a, first, first + 1, j, less);
    let mut moved = 0;
    let mut i = j + 1;
    while i != last {
        if less(&a[i], &a[j]) {
            let t = a[i];
            let mut k = j;
            j = i;
            loop {
                a[j] = a[k];
                j = k;
                if j == first {
                    break;
                }
                k -= 1;
                if !less(&t, &a[k]) {
                    break;
                }
            }
            a[j] = t;
            moved += 1;
            if limit == Some(moved) {
                return i + 1 == last;
            }
        }
        j = i;
        i += 1;
    }
    true
}

fn insertion_incomplete<T: Copy>(a: &mut [T], first: usize, last: usize, less: &impl Fn(&T, &T) -> bool) -> bool {
    sort_small(a, first, last, less) || insertion_sort3(a, first, last, less, Some(8))
}

pub(super) fn cxx_sort<T: Copy>(a: &mut [T], less: impl Fn(&T, &T) -> bool) {
    let last = a.len();
    sort_range(a, 0, last, &less);
}

fn sort_range<T: Copy>(a: &mut [T], mut first: usize, mut last: usize, less: &impl Fn(&T, &T) -> bool) {
    // Six, not 30: PMS sorts objects that are not trivially copyable.
    const LIMIT: usize = 6;
    'restart: loop {
        let length = last - first;
        if sort_small(a, first, last, less) {
            return;
        }
        if length <= LIMIT {
            insertion_sort3(a, first, last, less, None);
            return;
        }
        let mut m = first;
        let lm1 = last - 1;
        let mut swaps = if length >= 1000 {
            let mut delta = length / 2;
            m += delta;
            delta /= 2;
            sort5(a, [first, first + delta, m, m + delta, lm1], less)
        } else {
            m += length / 2;
            sort3(a, first, m, lm1, less)
        };
        let mut i = first;
        let mut j = lm1;
        if !less(&a[i], &a[m]) {
            loop {
                j -= 1;
                if i == j {
                    // Every element is >= the pivot: partition by == against `first` instead.
                    i += 1;
                    j = last - 1;
                    if !less(&a[first], &a[j]) {
                        loop {
                            if i == j {
                                return;
                            }
                            if less(&a[first], &a[i]) {
                                a.swap(i, j);
                                i += 1;
                                break;
                            }
                            i += 1;
                        }
                    }
                    if i == j {
                        return;
                    }
                    loop {
                        while !less(&a[first], &a[i]) {
                            i += 1;
                        }
                        j -= 1;
                        while less(&a[first], &a[j]) {
                            j -= 1;
                        }
                        if i >= j {
                            break;
                        }
                        a.swap(i, j);
                        i += 1;
                    }
                    first = i;
                    continue 'restart;
                }
                if less(&a[j], &a[m]) {
                    a.swap(i, j);
                    swaps += 1;
                    break;
                }
            }
        }
        i += 1;
        if i < j {
            loop {
                while less(&a[i], &a[m]) {
                    i += 1;
                }
                j -= 1;
                while !less(&a[j], &a[m]) {
                    j -= 1;
                }
                if i > j {
                    break;
                }
                a.swap(i, j);
                swaps += 1;
                if m == i {
                    m = j;
                }
                i += 1;
            }
        }
        if i != m && less(&a[m], &a[i]) {
            a.swap(i, m);
            swaps += 1;
        }
        if swaps == 0 {
            let sorted_first = insertion_incomplete(a, first, i, less);
            if insertion_incomplete(a, i + 1, last, less) {
                if sorted_first {
                    return;
                }
                last = i;
                continue;
            } else if sorted_first {
                first = i + 1;
                continue;
            }
        }
        if i - first < last - i {
            sort_range(a, first, i, less);
            first = i + 1;
        } else {
            sort_range(a, i + 1, last, less);
            last = i;
        }
    }
}

// -- musl 1.2.2's cbrtf and powf, bit for bit -----------------------------------------------

fn cbrtf(x: f32) -> f32 {
    let mut hx = x.to_bits() & 0x7fff_ffff;
    if hx >= 0x7f80_0000 {
        return x + x;
    }
    if hx < 0x0080_0000 {
        if hx == 0 {
            return x;
        }
        hx = ((x * 16_777_216.0).to_bits() & 0x7fff_ffff) / 3 + 642_849_266;
    } else {
        hx = hx / 3 + 709_958_130;
    }
    let x = x as f64;
    let mut t = f32::from_bits((x as f32).to_bits() & 0x8000_0000 | hx) as f64;
    let mut r = t * t * t;
    t = t * (x + x + r) / (x + r + r);
    r = t * t * t;
    t = t * (x + x + r) / (x + r + r);
    t as f32
}

const LOG2_TABLE: [(u64, u64); 16] = [
    (0x3ff661ec79f8f3be, 0xbfdefec65b963019), (0x3ff571ed4aaf883d, 0xbfdb0b6832d4fca4),
    (0x3ff49539f0f010b0, 0xbfd7418b0a1fb77b), (0x3ff3c995b0b80385, 0xbfd39de91a6dcf7b),
    (0x3ff30d190c8864a5, 0xbfd01d9bf3f2b631), (0x3ff25e227b0b8ea0, 0xbfc97c1d1b3b7af0),
    (0x3ff1bb4a4a1a343f, 0xbfc2f9e393af3c9f), (0x3ff12358f08ae5ba, 0xbfb960cbbf788d5c),
    (0x3ff0953f419900a7, 0xbfaa6f9db6475fce), (0x3ff0000000000000, 0x0000000000000000),
    (0x3fee608cfd9a47ac, 0x3fb338ca9f24f53d), (0x3feca4b31f026aa0, 0x3fc476a9543891ba),
    (0x3feb2036576afce6, 0x3fce840b4ac4e4d2), (0x3fe9c2d163a1aa2d, 0x3fd40645f0c6651c),
    (0x3fe886e6037841ed, 0x3fd88e9c2c1b9ff8), (0x3fe767dcf5534862, 0x3fdce0a44eb17bcc),
];
const LOG2_POLY: [u64; 5] = [0x3fd27616c9496e0b, 0xbfd71969a075c67a, 0x3fdec70a6ca7badd, 0xbfe7154748bef6c8, 0x3ff71547652ab82b];
const EXP2_TABLE: [u64; 32] = [
    0x3ff0000000000000, 0x3fefd9b0d3158574, 0x3fefb5586cf9890f, 0x3fef9301d0125b51,
    0x3fef72b83c7d517b, 0x3fef54873168b9aa, 0x3fef387a6e756238, 0x3fef1e9df51fdee1,
    0x3fef06fe0a31b715, 0x3feef1a7373aa9cb, 0x3feedea64c123422, 0x3feece086061892d,
    0x3feebfdad5362a27, 0x3feeb42b569d4f82, 0x3feeab07dd485429, 0x3feea47eb03a5585,
    0x3feea09e667f3bcd, 0x3fee9f75e8ec5f74, 0x3feea11473eb0187, 0x3feea589994cce13,
    0x3feeace5422aa0db, 0x3feeb737b0cdc5e5, 0x3feec49182a3f090, 0x3feed503b23e255d,
    0x3feee89f995ad3ad, 0x3feeff76f2fb5e47, 0x3fef199bdd85529c, 0x3fef3720dcef9069,
    0x3fef5818dcfba487, 0x3fef7c97337b9b5f, 0x3fefa4afa2a490da, 0x3fefd0765b6e4540,
];
const EXP2_POLY: [u64; 3] = [0x3fac6af84b912394, 0x3fcebfce50fac4f3, 0x3fe62e42ff0c52d6];

/// musl's `powf` for a finite positive normal `x` (the only kind this module raises).
fn powf(x: f32, y: f32) -> f32 {
    let ix = x.to_bits();
    if !(0x0080_0000..0x7f80_0000).contains(&ix) {
        return x.powf(y);
    }
    let d = f64::from_bits;
    let tmp = ix.wrapping_sub(0x3f33_0000);
    let i = ((tmp >> 19) % 16) as usize;
    let top = tmp & 0xff80_0000;
    let iz = ix.wrapping_sub(top);
    let k = (top as i32) >> 23;
    let (invc, logc) = (d(LOG2_TABLE[i].0), d(LOG2_TABLE[i].1));
    let z = f32::from_bits(iz) as f64;
    let r = z * invc - 1.0;
    let y0 = logc + k as f64;
    let a = LOG2_POLY.map(d);
    let r2 = r * r;
    let mut yy = a[0] * r + a[1];
    let p = a[2] * r + a[3];
    let r4 = r2 * r2;
    let mut q = a[4] * r + y0;
    q = p * r2 + q;
    yy = yy * r4 + q;
    let xd = y as f64 * yy;
    let shift = d(0x4338_0000_0000_0000) / 32.0;
    let kd = xd + shift;
    let ki = kd.to_bits();
    let kd = kd - shift;
    let r = xd - kd;
    let t = EXP2_TABLE[(ki % 32) as usize].wrapping_add(ki << 47);
    let s = d(t);
    let c = EXP2_POLY.map(d);
    let z = c[0] * r + c[1];
    let r2 = r * r;
    let mut yy = c[2] * r + 1.0;
    yy = z * r2 + yy;
    (yy * s) as f32
}

// musl's cosf, sinf and atan2f, for the arguments they get here: 2 pi h with h in 0..=1, and
// finite Oklab components.
const C: [f64; 4] = [-0.499999997251031003120, 0.0416666233237390631894, -0.00138867637746099294692, 0.0000243904487962774090654];
const S: [f64; 4] = [-0.166666666416265235595, 0.0083333293858894631756, -0.000198393348360966317347, 0.0000027183114939898219064];
const PIO2: [f64; 4] = [std::f64::consts::FRAC_PI_2, 2.0 * std::f64::consts::FRAC_PI_2, 3.0 * std::f64::consts::FRAC_PI_2, 4.0 * std::f64::consts::FRAC_PI_2];

fn cosdf(x: f64) -> f32 {
    let z = x * x;
    let w = z * z;
    let r = C[2] + z * C[3];
    (((1.0 + z * C[0]) + w * C[1]) + (w * z) * r) as f32
}

fn sindf(x: f64) -> f32 {
    let z = x * x;
    let w = z * z;
    let r = S[2] + z * S[3];
    let s = z * x;
    ((x + s * (S[0] + z * S[1])) + s * w * r) as f32
}

fn cosf(x: f32) -> f32 {
    let (bits, xd) = (x.to_bits(), x as f64);
    let (sign, ix) = (bits >> 31 == 1, bits & 0x7fff_ffff);
    if ix <= 0x3f49_0fda {
        return if ix < 0x3980_0000 { 1.0 } else { cosdf(xd) };
    }
    if ix <= 0x407b_53d1 {
        if ix > 0x4016_cbe3 {
            return -cosdf(if sign { xd + PIO2[1] } else { xd - PIO2[1] });
        }
        return if sign { sindf(xd + PIO2[0]) } else { sindf(PIO2[0] - xd) };
    }
    if ix > 0x40e2_31d5 {
        return x.cos();
    }
    if ix > 0x40af_eddf {
        return cosdf(if sign { xd + PIO2[3] } else { xd - PIO2[3] });
    }
    if sign { sindf(-xd - PIO2[2]) } else { sindf(xd - PIO2[2]) }
}

fn sinf(x: f32) -> f32 {
    let (bits, xd) = (x.to_bits(), x as f64);
    let (sign, ix) = (bits >> 31 == 1, bits & 0x7fff_ffff);
    if ix <= 0x3f49_0fda {
        return if ix < 0x3980_0000 { x } else { sindf(xd) };
    }
    if ix <= 0x407b_53d1 {
        if ix <= 0x4016_cbe3 {
            return if sign { -cosdf(xd + PIO2[0]) } else { cosdf(xd - PIO2[0]) };
        }
        return sindf(if sign { -(xd + PIO2[1]) } else { -(xd - PIO2[1]) });
    }
    if ix > 0x40e2_31d5 {
        return x.sin();
    }
    if ix <= 0x40af_eddf {
        return if sign { cosdf(xd + PIO2[2]) } else { -cosdf(xd - PIO2[2]) };
    }
    sindf(if sign { xd + PIO2[3] } else { xd - PIO2[3] })
}

const ATAN_HI: [f32; 4] = [4.6364760399e-01, 7.8539812565e-01, 9.8279368877e-01, 1.5707962513e+00];
const ATAN_LO: [f32; 4] = [5.0121582440e-09, 3.7748947079e-08, 3.4473217170e-08, 7.5497894159e-08];
const AT: [f32; 5] = [3.3333328366e-01, -1.9999158382e-01, 1.4253635705e-01, -1.0648017377e-01, 6.1687607318e-02];
const PI: f32 = 3.1415927410e+00;
const PI_LO: f32 = -8.7422776573e-08;

fn atanf(x: f32) -> f32 {
    let bits = x.to_bits();
    let (sign, ix) = (bits >> 31 == 1, bits & 0x7fff_ffff);
    if ix >= 0x4c80_0000 {
        if x.is_nan() {
            return x;
        }
        let z = ATAN_HI[3] + f32::from_bits(0x0380_0000);
        return if sign { -z } else { z };
    }
    let (index, x) = if ix < 0x3ee0_0000 {
        if ix < 0x3980_0000 {
            return x;
        }
        (None, x)
    } else {
        let x = x.abs();
        if ix < 0x3f98_0000 {
            if ix < 0x3f30_0000 {
                (Some(0), (2.0 * x - 1.0) / (2.0 + x))
            } else {
                (Some(1), (x - 1.0) / (x + 1.0))
            }
        } else if ix < 0x401c_0000 {
            (Some(2), (x - 1.5) / (1.0 + 1.5 * x))
        } else {
            (Some(3), -1.0 / x)
        }
    };
    let z = x * x;
    let w = z * z;
    let s1 = z * (AT[0] + w * (AT[2] + w * AT[4]));
    let s2 = w * (AT[1] + w * AT[3]);
    let Some(index) = index else { return x - x * (s1 + s2) };
    let z = ATAN_HI[index] - ((x * (s1 + s2) - ATAN_LO[index]) - x);
    if sign { -z } else { z }
}

fn atan2f(y: f32, x: f32) -> f32 {
    if x.is_nan() || y.is_nan() {
        return x + y;
    }
    let (ix, iy) = (x.to_bits(), y.to_bits());
    if ix == 0x3f80_0000 {
        return atanf(y);
    }
    let m = ((iy >> 31) & 1) | ((ix >> 30) & 2);
    let (ix, iy) = (ix & 0x7fff_ffff, iy & 0x7fff_ffff);
    if iy == 0 {
        return match m {
            0 | 1 => y,
            2 => PI,
            _ => -PI,
        };
    }
    if ix == 0 {
        return if m & 1 == 1 { -PI / 2.0 } else { PI / 2.0 };
    }
    if ix == 0x7f80_0000 || iy == 0x7f80_0000 {
        return y.atan2(x);
    }
    if ix + (26 << 23) < iy {
        return if m & 1 == 1 { -PI / 2.0 } else { PI / 2.0 };
    }
    let z = if m & 2 == 2 && iy + (26 << 23) < ix { 0.0 } else { atanf((y / x).abs()) };
    match m {
        0 => z,
        1 => -z,
        2 => PI - (z - PI_LO),
        _ => (z - PI_LO) - PI,
    }
}

// -- Ottosson's Oklab and Okhsl, in f32 ------------------------------------------------------

fn to_linear(x: f32) -> f32 {
    if x > 0.04045 {
        powf((x + 0.055) / 1.055, 2.4)
    } else {
        x / 12.92
    }
}

fn to_gamma(x: f32) -> f32 {
    if x > 0.0031308 {
        1.055 * powf(x, (1.0f64 / 2.4) as f32) + -0.055
    } else {
        x * 12.92
    }
}

fn linear_to_oklab(r: f32, g: f32, b: f32) -> [f32; 3] {
    let l = r * 0.4122214615 + g * 0.5363325477 + b * 0.0514459945;
    let m = r * 0.2119034982 + g * 0.6806995451 + b * 0.1073969566;
    let s = r * 0.0883024619 + g * 0.2817188376 + b * 0.6299787005;
    let (l, m, s) = (cbrtf(l), cbrtf(m), cbrtf(s));
    [
        l * 0.2104542553 + m * 0.7936177850 + s * -0.0040720468,
        l * 1.9779984951 + m * -2.4285922050 + s * 0.4505937099,
        l * 0.0259040371 + m * 0.7827717662 + s * -0.8086757660,
    ]
}

fn oklab_to_linear(lightness: f32, a: f32, b: f32) -> [f32; 3] {
    let l = lightness + 0.3963377774 * a + 0.2158037573 * b;
    let m = lightness - 0.1055613458 * a - 0.0638541728 * b;
    let s = lightness - 0.0894841775 * a - 1.2914855480 * b;
    let (l, m, s) = (l * l * l, m * m * m, s * s * s);
    [
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    ]
}

const RED: [f32; 3] = [4.0767416621, -3.3077115913, 0.2309699292];
const GREEN: [f32; 3] = [-1.2684380046, 2.6097574011, -0.3413193965];
const BLUE: [f32; 3] = [-0.0041960863, -0.7034186147, 1.7076147010];

fn lms_slopes(a: f32, b: f32) -> (f32, f32, f32) {
    (0.3963377774 * a + 0.2158037573 * b, -0.1055613458 * a - 0.0638541728 * b, -0.0894841775 * a - 1.2914855480 * b)
}

fn max_saturation(a: f32, b: f32) -> f32 {
    let (k, w) = if -1.88170328 * a - 0.80936493 * b > 1.0 {
        ([1.19086277, 1.76576728, 0.59662641, 0.75515197, 0.56771245], RED)
    } else if 1.81444104 * a - 1.19445276 * b > 1.0 {
        ([0.73956515, -0.45954404, 0.08285427, 0.12541070, 0.14503204], GREEN)
    } else {
        ([1.35733652, -0.00915799, -1.15130210, -0.50559606, 0.00692167], BLUE)
    };
    let saturation = k[0] + k[1] * a + k[2] * b + k[3] * a * a + k[4] * a * b;
    let (kl, km, ks) = lms_slopes(a, b);
    let (l_, m_, s_) = (1.0 + saturation * kl, 1.0 + saturation * km, 1.0 + saturation * ks);
    let (l, m, s) = (l_ * l_ * l_, m_ * m_ * m_, s_ * s_ * s_);
    let (l1, m1, s1) = (3.0 * kl * l_ * l_, 3.0 * km * m_ * m_, 3.0 * ks * s_ * s_);
    let (l2, m2, s2) = (6.0 * kl * kl * l_, 6.0 * km * km * m_, 6.0 * ks * ks * s_);
    let f = w[0] * l + w[1] * m + w[2] * s;
    let f1 = w[0] * l1 + w[1] * m1 + w[2] * s1;
    let f2 = w[0] * l2 + w[1] * m2 + w[2] * s2;
    saturation - f * f1 / (f1 * f1 - 0.5 * f * f2)
}

fn find_cusp(a: f32, b: f32) -> (f32, f32) {
    let saturation = max_saturation(a, b);
    let [r, g, b] = oklab_to_linear(1.0, saturation * a, saturation * b);
    let lightness = cbrtf(1.0 / r.max(g).max(b));
    (lightness, lightness * saturation)
}

fn gamut_intersection(a: f32, b: f32, l1: f32, c1: f32, l0: f32, cusp: (f32, f32)) -> f32 {
    let (cusp_l, cusp_c) = cusp;
    if (l1 - l0) * cusp_c - (cusp_l - l0) * c1 <= 0.0 {
        return cusp_c * l0 / (c1 * cusp_l + cusp_c * (l0 - l1));
    }
    let t = cusp_c * (l0 - 1.0) / (c1 * (cusp_l - 1.0) + cusp_c * (l0 - l1));
    let (dl, dc) = (l1 - l0, c1);
    let (kl, km, ks) = lms_slopes(a, b);
    let (ldt, mdt, sdt) = (dl + dc * kl, dl + dc * km, dl + dc * ks);
    let lightness = l0 * (1.0 - t) + t * l1;
    let chroma = t * c1;
    let (l_, m_, s_) = (lightness + chroma * kl, lightness + chroma * km, lightness + chroma * ks);
    let (l, m, s) = (l_ * l_ * l_, m_ * m_ * m_, s_ * s_ * s_);
    let (lt, mt, st) = (3.0 * ldt * l_ * l_, 3.0 * mdt * m_ * m_, 3.0 * sdt * s_ * s_);
    let (lt2, mt2, st2) = (6.0 * ldt * ldt * l_, 6.0 * mdt * mdt * m_, 6.0 * sdt * sdt * s_);
    let mut best = f32::MAX;
    for w in [RED, GREEN, BLUE] {
        let x = w[0] * l + w[1] * m + w[2] * s - 1.0;
        let x1 = w[0] * lt + w[1] * mt + w[2] * st;
        let x2 = w[0] * lt2 + w[1] * mt2 + w[2] * st2;
        let u = x1 / (x1 * x1 - 0.5 * x * x2);
        let step = if u >= 0.0 { -x * u } else { f32::MAX };
        best = best.min(step);
    }
    t + best
}

fn st_mid(a: f32, b: f32) -> (f32, f32) {
    let s = 0.11516993
        + 1.0 / (7.44778970 + 4.15901240 * b
            + a * (-2.19557347 + 1.75198401 * b + a * (-2.13704948 - 10.02301043 * b + a * (-4.24894561 + 5.38770819 * b + 4.69891013 * a))));
    let t = 0.11239642
        + 1.0 / (1.61320320 - 0.68124379 * b
            + a * (0.40370612 + 0.90148123 * b + a * (-0.27087943 + 0.61223990 * b + a * (0.00299215 - 0.45399568 * b - 0.14661872 * a))));
    (s, t)
}

fn cs(lightness: f32, a: f32, b: f32) -> (f32, f32, f32) {
    let cusp = find_cusp(a, b);
    let c_max = gamut_intersection(a, b, lightness, 1.0, lightness, cusp);
    let (s_max, t_max) = (cusp.1 / cusp.0, cusp.1 / (1.0 - cusp.0));
    let k = c_max / (lightness * s_max).min((1.0 - lightness) * t_max);
    let (s_mid, t_mid) = st_mid(a, b);
    let (ca, cb) = (lightness * s_mid, (1.0 - lightness) * t_mid);
    let c_mid = 0.9 * k * (1.0 / (1.0 / (ca * ca * ca * ca) + 1.0 / (cb * cb * cb * cb))).sqrt().sqrt();
    let (ca, cb) = (lightness * 0.4, (1.0 - lightness) * 0.8);
    let c0 = (1.0 / (1.0 / (ca * ca) + 1.0 / (cb * cb))).sqrt();
    (c0, c_mid, c_max)
}

const MID: f32 = 0.8;
const ONE_MINUS_MID: f32 = 1.0 - MID;
const TOE_K1: f32 = 0.206;
const TOE_K2: f32 = 0.03;

fn toe(x: f32) -> f32 {
    let k3 = (1.0 + TOE_K1) / (1.0 + TOE_K2);
    let y = k3 * x - TOE_K1;
    0.5 * (y + (y * y + 4.0 * TOE_K2 * k3 * x).sqrt())
}

fn toe_inv(x: f32) -> f32 {
    let k3 = (1.0 + TOE_K1) / (1.0 + TOE_K2);
    (x * x + TOE_K1 * x) / (k3 * (x + TOE_K2))
}

/// One palette colour as PMS scores it: Okhsl, and the Oklab of its gamma-encoded channels (what
/// PMS measures distances in, which is not Oklab proper).
#[derive(Clone, Copy)]
struct Swatch {
    h: f32,
    s: f32,
    l: f32,
    lab: [f32; 3],
}

fn swatch(rgb: [u8; 3]) -> Swatch {
    let channel = |value: u8| value as f32 / 255.0;
    let [lightness, a, b] = linear_to_oklab(to_linear(channel(rgb[0])), to_linear(channel(rgb[1])), to_linear(channel(rgb[2])));
    let chroma = (a * a + b * b).sqrt();
    let (a_, b_) = (a / chroma, b / chroma);
    let h = atan2f(-b, -a) * 0.5 / PI + 0.5;
    let (c0, c_mid, c_max) = cs(lightness, a_, b_);
    // A NaN (a grey's zero chroma) takes the second branch, as `ucomiss`/`jbe` does.
    let s = if c_mid > chroma {
        let k1 = MID * c0;
        let k2 = 1.0 - k1 / c_mid;
        chroma / (k1 + k2 * chroma) * MID
    } else {
        let k1 = c_mid * ONE_MINUS_MID * c_mid * 1.25 * 1.25 / c0;
        let k2 = 1.0 - k1 / (c_max - c_mid);
        let t = (chroma - c_mid) / (k1 + k2 * (chroma - c_mid));
        t * ONE_MINUS_MID + MID
    };
    Swatch { h, s, l: toe(lightness), lab: linear_to_oklab(channel(rgb[0]), channel(rgb[1]), channel(rgb[2])) }
}

fn okhsl_to_hex(h: f32, s: f32, l: f32) -> String {
    let s = if s.is_nan() { 0.0 } else { s };
    let rgb = if l == 1.0 {
        [1.0; 3]
    } else if l == 0.0 {
        [0.0; 3]
    } else {
        let (a_, b_) = (cosf(6.28318548 * h), sinf(6.28318548 * h));
        let lightness = toe_inv(l);
        let (c0, c_mid, c_max) = cs(lightness, a_, b_);
        let chroma = if MID > s {
            let t = s * 1.25;
            let k1 = MID * c0;
            let k2 = 1.0 - k1 / c_mid;
            t * k1 / (1.0 - k2 * t)
        } else {
            let t = (s - MID) / ONE_MINUS_MID;
            let k1 = ONE_MINUS_MID * c_mid * c_mid * 1.25 * 1.25 / c0;
            let k2 = 1.0 - k1 / (c_max - c_mid);
            t * k1 / (1.0 - k2 * t) + c_mid
        };
        oklab_to_linear(lightness, chroma * a_, chroma * b_).map(to_gamma)
    };
    let byte = |x: f32| if x.is_nan() { 0 } else { (x.clamp(0.0, 1.0) * 255.0) as u8 };
    format!("{:02x}{:02x}{:02x}", byte(rgb[0]), byte(rgb[1]), byte(rgb[2]))
}

// -- the score (0x1148953) -------------------------------------------------------------------

/// 100 inside `low..=high`, falling linearly to 0 over `below_width` under it and `above_width`
/// over it; `below`/`above` replace a side's fall.
fn ramp(x: f32, low: f32, high: f32, below_width: f32, above_width: f32, below: Option<f32>, above: Option<f32>) -> f32 {
    if low <= x && x <= high {
        return 100.0;
    }
    if x < low {
        return below.unwrap_or_else(|| 0f32.max((low - x) / -below_width * 100.0 + 100.0));
    }
    above.unwrap_or_else(|| 0f32.max((x - high) / -above_width * 100.0 + 100.0))
}

fn score(color: &Swatch, reference: Option<&Swatch>, index: usize, count: usize) -> f32 {
    let rank = if index == 0 { 100.0 } else { 0f32.max(100.0 - index as f32 / (count - 1) as f32 * 100.0) };
    let l = color.l;
    let light = if (0.0..=0.4).contains(&l) {
        150.0
    } else if l < 0.0 {
        0.0
    } else {
        0f32.max((l + -0.4) / -0.299999982 * 100.0 + 100.0) * 1.5
    };
    let s = if color.s.is_nan() { 0.0 } else { color.s };
    let saturation = if (0.6..=1.0).contains(&s) {
        300.0
    } else if s < 0.6 {
        0f32.max((0.6 - s) / -0.400000036 * 100.0 + 100.0) * 3.0
    } else {
        0.0
    };
    let distance = match reference {
        None => 300.0,
        Some(reference) => {
            let d = [0, 1, 2].map(|i| color.lab[i] - reference.lab[i]);
            let dd = (d[1] * d[1] + d[0] * d[0] + d[2] * d[2]).sqrt();
            let distance = if (0.05..=0.1).contains(&dd) {
                100.0
            } else if dd < 0.05 {
                0f32.max(100.0 - (0.05 - dd) / 0.05 * 100.0)
            } else {
                0f32.max((dd + -0.1) / -0.9 * 100.0 + 100.0)
            };
            distance * 3.0
        }
    };
    let hue = color.h * 360.0;
    let yellow_green = ramp(hue, 80.0, 120.0, 10.0, 10.0, None, None);
    let orange = ramp(hue, 40.0, 70.0, 10.0, 10.0, None, None);
    let s_mid = ramp(s, 0.2, 0.6, 0.1, 0.0999999642, None, None);
    let s_low = ramp(s, 0.0, 0.2, 0.0, 0.2, Some(0.0), None);
    let l_high = ramp(l, 0.5, 1.0, 0.199999988, 0.0, None, Some(0.0));
    let base = rank + light + saturation + distance - (yellow_green + yellow_green);
    let skin = if s_mid == 0.0 || orange == 0.0 { -0.0 } else { (orange + s_mid) * 0.5 * -2.0 };
    let washed = if l_high == 0.0 || s_low == 0.0 { -0.0 } else { (s_low + l_high) * 0.5 * -2.0 };
    washed + (skin + base)
}

fn pick(pixels: &[[u8; 3]], max_lightness: f32, reference: Option<&Swatch>) -> Option<Swatch> {
    let colors = palette(pixels)?;
    let count = colors.len();
    let mut scored: Vec<(f32, Swatch)> = colors
        .iter()
        .enumerate()
        .map(|(index, rgb)| {
            let color = swatch(*rgb);
            (score(&color, reference, index, count), color)
        })
        .collect();
    cxx_sort(&mut scored, |a, b| a.0 > b.0);
    let mut best = scored.first()?.1;
    if 0.2 < best.s {
        best.s = best.s.max(0.6);
    }
    if best.l as f64 > max_lightness as f64 {
        best.l = max_lightness;
    }
    Some(best)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn libcxx_sort_orders_like_any_sort() {
        let mut seed = 1u32;
        for length in 0..60 {
            let mut values: Vec<u32> = (0..length).map(|_| {
                seed = seed.wrapping_mul(1_103_515_245).wrapping_add(12345);
                (seed >> 16) % 6
            }).collect();
            let mut expected = values.clone();
            expected.sort();
            cxx_sort(&mut values, |a, b| a < b);
            assert_eq!(values, expected);
        }
    }

    #[test]
    fn musl_powf_and_cbrtf_agree_with_the_platform_within_an_ulp() {
        for x in [0.001f32, 0.04, 0.2, 0.5, 0.9, 1.0, 1.7] {
            assert!((powf(x, 2.4) - x.powf(2.4)).abs() <= x.powf(2.4) * 2e-7, "{x}");
            assert!((cbrtf(x) - x.cbrt()).abs() <= x.cbrt() * 2e-7, "{x}");
        }
    }
}

#[cfg(test)]
mod oracle {
    /// `REX_UB_ORACLE=<dir>:<tsv>` (plexav's answers per image, `probes/record_blur.py`'s solids
    /// step or any `name<TAB>json` table): how far rex's corners are from them. Not run by default.
    #[test]
    #[ignore]
    fn against_recorded_oracle() {
        let spec = std::env::var("REX_UB_ORACLE").unwrap();
        for pair in spec.split(',') {
            let (dir, table) = pair.split_once(':').unwrap();
            for line in std::fs::read_to_string(table).unwrap().lines() {
                let (name, json) = line.split_once('\t').unwrap();
                let bytes = std::fs::read(format!("{dir}/{name}")).unwrap();
                let theirs: serde_json::Value = serde_json::from_str(json).unwrap_or_default();
                let theirs = theirs["MediaContainer"]["UltraBlurColors"][0].as_object().map(|colors| {
                    ["topLeft", "topRight", "bottomRight", "bottomLeft"].map(|corner| colors[corner].as_str().unwrap().to_string())
                });
                let ours = super::super::blur::decode(&bytes).and_then(|(w, h, rgb)| super::corners(w, h, &rgb));
                let distance = match (&ours, &theirs) {
                    (Some(a), Some(b)) => (0..4).flat_map(|c| [0, 2, 4].map(|i| {
                        u8::from_str_radix(&a[c][i..i + 2], 16).unwrap().abs_diff(u8::from_str_radix(&b[c][i..i + 2], 16).unwrap())
                    })).max().unwrap() as i32,
                    (None, None) => 0,
                    _ => 999,
                };
                println!("ORACLE {distance} {name}");
            }
        }
    }
}

//! The measured SQLite spellfix1 subset Plex universal search falls back on --
//! a port of `spellfix.py`, whose docstrings hold the measurements (cycles 37-39).
//! Unicode decomposition is the Latin table below rather than a full NFKD: the
//! catalog's text surface is Latin, and every other code point is its own base.

use std::collections::{BTreeMap, HashSet};

/// NFKD of the Latin letters, marks dropped: generated with Python's `unicodedata`
/// (U+00C0-U+024F and U+1E00-U+1EFF), which is what `spellfix.py` decomposes with.
pub(super) const LATIN_NFKD: &[(char, &str)] = &[
    ('\u{00c0}', "A"),
    ('\u{00c1}', "A"),
    ('\u{00c2}', "A"),
    ('\u{00c3}', "A"),
    ('\u{00c4}', "A"),
    ('\u{00c5}', "A"),
    ('\u{00c7}', "C"),
    ('\u{00c8}', "E"),
    ('\u{00c9}', "E"),
    ('\u{00ca}', "E"),
    ('\u{00cb}', "E"),
    ('\u{00cc}', "I"),
    ('\u{00cd}', "I"),
    ('\u{00ce}', "I"),
    ('\u{00cf}', "I"),
    ('\u{00d1}', "N"),
    ('\u{00d2}', "O"),
    ('\u{00d3}', "O"),
    ('\u{00d4}', "O"),
    ('\u{00d5}', "O"),
    ('\u{00d6}', "O"),
    ('\u{00d9}', "U"),
    ('\u{00da}', "U"),
    ('\u{00db}', "U"),
    ('\u{00dc}', "U"),
    ('\u{00dd}', "Y"),
    ('\u{00e0}', "a"),
    ('\u{00e1}', "a"),
    ('\u{00e2}', "a"),
    ('\u{00e3}', "a"),
    ('\u{00e4}', "a"),
    ('\u{00e5}', "a"),
    ('\u{00e7}', "c"),
    ('\u{00e8}', "e"),
    ('\u{00e9}', "e"),
    ('\u{00ea}', "e"),
    ('\u{00eb}', "e"),
    ('\u{00ec}', "i"),
    ('\u{00ed}', "i"),
    ('\u{00ee}', "i"),
    ('\u{00ef}', "i"),
    ('\u{00f1}', "n"),
    ('\u{00f2}', "o"),
    ('\u{00f3}', "o"),
    ('\u{00f4}', "o"),
    ('\u{00f5}', "o"),
    ('\u{00f6}', "o"),
    ('\u{00f9}', "u"),
    ('\u{00fa}', "u"),
    ('\u{00fb}', "u"),
    ('\u{00fc}', "u"),
    ('\u{00fd}', "y"),
    ('\u{00ff}', "y"),
    ('\u{0100}', "A"),
    ('\u{0101}', "a"),
    ('\u{0102}', "A"),
    ('\u{0103}', "a"),
    ('\u{0104}', "A"),
    ('\u{0105}', "a"),
    ('\u{0106}', "C"),
    ('\u{0107}', "c"),
    ('\u{0108}', "C"),
    ('\u{0109}', "c"),
    ('\u{010a}', "C"),
    ('\u{010b}', "c"),
    ('\u{010c}', "C"),
    ('\u{010d}', "c"),
    ('\u{010e}', "D"),
    ('\u{010f}', "d"),
    ('\u{0112}', "E"),
    ('\u{0113}', "e"),
    ('\u{0114}', "E"),
    ('\u{0115}', "e"),
    ('\u{0116}', "E"),
    ('\u{0117}', "e"),
    ('\u{0118}', "E"),
    ('\u{0119}', "e"),
    ('\u{011a}', "E"),
    ('\u{011b}', "e"),
    ('\u{011c}', "G"),
    ('\u{011d}', "g"),
    ('\u{011e}', "G"),
    ('\u{011f}', "g"),
    ('\u{0120}', "G"),
    ('\u{0121}', "g"),
    ('\u{0122}', "G"),
    ('\u{0123}', "g"),
    ('\u{0124}', "H"),
    ('\u{0125}', "h"),
    ('\u{0128}', "I"),
    ('\u{0129}', "i"),
    ('\u{012a}', "I"),
    ('\u{012b}', "i"),
    ('\u{012c}', "I"),
    ('\u{012d}', "i"),
    ('\u{012e}', "I"),
    ('\u{012f}', "i"),
    ('\u{0130}', "I"),
    ('\u{0132}', "IJ"),
    ('\u{0133}', "ij"),
    ('\u{0134}', "J"),
    ('\u{0135}', "j"),
    ('\u{0136}', "K"),
    ('\u{0137}', "k"),
    ('\u{0139}', "L"),
    ('\u{013a}', "l"),
    ('\u{013b}', "L"),
    ('\u{013c}', "l"),
    ('\u{013d}', "L"),
    ('\u{013e}', "l"),
    ('\u{013f}', "L·"),
    ('\u{0140}', "l·"),
    ('\u{0143}', "N"),
    ('\u{0144}', "n"),
    ('\u{0145}', "N"),
    ('\u{0146}', "n"),
    ('\u{0147}', "N"),
    ('\u{0148}', "n"),
    ('\u{0149}', "ʼn"),
    ('\u{014c}', "O"),
    ('\u{014d}', "o"),
    ('\u{014e}', "O"),
    ('\u{014f}', "o"),
    ('\u{0150}', "O"),
    ('\u{0151}', "o"),
    ('\u{0154}', "R"),
    ('\u{0155}', "r"),
    ('\u{0156}', "R"),
    ('\u{0157}', "r"),
    ('\u{0158}', "R"),
    ('\u{0159}', "r"),
    ('\u{015a}', "S"),
    ('\u{015b}', "s"),
    ('\u{015c}', "S"),
    ('\u{015d}', "s"),
    ('\u{015e}', "S"),
    ('\u{015f}', "s"),
    ('\u{0160}', "S"),
    ('\u{0161}', "s"),
    ('\u{0162}', "T"),
    ('\u{0163}', "t"),
    ('\u{0164}', "T"),
    ('\u{0165}', "t"),
    ('\u{0168}', "U"),
    ('\u{0169}', "u"),
    ('\u{016a}', "U"),
    ('\u{016b}', "u"),
    ('\u{016c}', "U"),
    ('\u{016d}', "u"),
    ('\u{016e}', "U"),
    ('\u{016f}', "u"),
    ('\u{0170}', "U"),
    ('\u{0171}', "u"),
    ('\u{0172}', "U"),
    ('\u{0173}', "u"),
    ('\u{0174}', "W"),
    ('\u{0175}', "w"),
    ('\u{0176}', "Y"),
    ('\u{0177}', "y"),
    ('\u{0178}', "Y"),
    ('\u{0179}', "Z"),
    ('\u{017a}', "z"),
    ('\u{017b}', "Z"),
    ('\u{017c}', "z"),
    ('\u{017d}', "Z"),
    ('\u{017e}', "z"),
    ('\u{017f}', "s"),
    ('\u{01a0}', "O"),
    ('\u{01a1}', "o"),
    ('\u{01af}', "U"),
    ('\u{01b0}', "u"),
    ('\u{01c4}', "DZ"),
    ('\u{01c5}', "Dz"),
    ('\u{01c6}', "dz"),
    ('\u{01c7}', "LJ"),
    ('\u{01c8}', "Lj"),
    ('\u{01c9}', "lj"),
    ('\u{01ca}', "NJ"),
    ('\u{01cb}', "Nj"),
    ('\u{01cc}', "nj"),
    ('\u{01cd}', "A"),
    ('\u{01ce}', "a"),
    ('\u{01cf}', "I"),
    ('\u{01d0}', "i"),
    ('\u{01d1}', "O"),
    ('\u{01d2}', "o"),
    ('\u{01d3}', "U"),
    ('\u{01d4}', "u"),
    ('\u{01d5}', "U"),
    ('\u{01d6}', "u"),
    ('\u{01d7}', "U"),
    ('\u{01d8}', "u"),
    ('\u{01d9}', "U"),
    ('\u{01da}', "u"),
    ('\u{01db}', "U"),
    ('\u{01dc}', "u"),
    ('\u{01de}', "A"),
    ('\u{01df}', "a"),
    ('\u{01e0}', "A"),
    ('\u{01e1}', "a"),
    ('\u{01e2}', "Æ"),
    ('\u{01e3}', "æ"),
    ('\u{01e6}', "G"),
    ('\u{01e7}', "g"),
    ('\u{01e8}', "K"),
    ('\u{01e9}', "k"),
    ('\u{01ea}', "O"),
    ('\u{01eb}', "o"),
    ('\u{01ec}', "O"),
    ('\u{01ed}', "o"),
    ('\u{01ee}', "Ʒ"),
    ('\u{01ef}', "ʒ"),
    ('\u{01f0}', "j"),
    ('\u{01f1}', "DZ"),
    ('\u{01f2}', "Dz"),
    ('\u{01f3}', "dz"),
    ('\u{01f4}', "G"),
    ('\u{01f5}', "g"),
    ('\u{01f8}', "N"),
    ('\u{01f9}', "n"),
    ('\u{01fa}', "A"),
    ('\u{01fb}', "a"),
    ('\u{01fc}', "Æ"),
    ('\u{01fd}', "æ"),
    ('\u{01fe}', "Ø"),
    ('\u{01ff}', "ø"),
    ('\u{0200}', "A"),
    ('\u{0201}', "a"),
    ('\u{0202}', "A"),
    ('\u{0203}', "a"),
    ('\u{0204}', "E"),
    ('\u{0205}', "e"),
    ('\u{0206}', "E"),
    ('\u{0207}', "e"),
    ('\u{0208}', "I"),
    ('\u{0209}', "i"),
    ('\u{020a}', "I"),
    ('\u{020b}', "i"),
    ('\u{020c}', "O"),
    ('\u{020d}', "o"),
    ('\u{020e}', "O"),
    ('\u{020f}', "o"),
    ('\u{0210}', "R"),
    ('\u{0211}', "r"),
    ('\u{0212}', "R"),
    ('\u{0213}', "r"),
    ('\u{0214}', "U"),
    ('\u{0215}', "u"),
    ('\u{0216}', "U"),
    ('\u{0217}', "u"),
    ('\u{0218}', "S"),
    ('\u{0219}', "s"),
    ('\u{021a}', "T"),
    ('\u{021b}', "t"),
    ('\u{021e}', "H"),
    ('\u{021f}', "h"),
    ('\u{0226}', "A"),
    ('\u{0227}', "a"),
    ('\u{0228}', "E"),
    ('\u{0229}', "e"),
    ('\u{022a}', "O"),
    ('\u{022b}', "o"),
    ('\u{022c}', "O"),
    ('\u{022d}', "o"),
    ('\u{022e}', "O"),
    ('\u{022f}', "o"),
    ('\u{0230}', "O"),
    ('\u{0231}', "o"),
    ('\u{0232}', "Y"),
    ('\u{0233}', "y"),
    ('\u{1e00}', "A"),
    ('\u{1e01}', "a"),
    ('\u{1e02}', "B"),
    ('\u{1e03}', "b"),
    ('\u{1e04}', "B"),
    ('\u{1e05}', "b"),
    ('\u{1e06}', "B"),
    ('\u{1e07}', "b"),
    ('\u{1e08}', "C"),
    ('\u{1e09}', "c"),
    ('\u{1e0a}', "D"),
    ('\u{1e0b}', "d"),
    ('\u{1e0c}', "D"),
    ('\u{1e0d}', "d"),
    ('\u{1e0e}', "D"),
    ('\u{1e0f}', "d"),
    ('\u{1e10}', "D"),
    ('\u{1e11}', "d"),
    ('\u{1e12}', "D"),
    ('\u{1e13}', "d"),
    ('\u{1e14}', "E"),
    ('\u{1e15}', "e"),
    ('\u{1e16}', "E"),
    ('\u{1e17}', "e"),
    ('\u{1e18}', "E"),
    ('\u{1e19}', "e"),
    ('\u{1e1a}', "E"),
    ('\u{1e1b}', "e"),
    ('\u{1e1c}', "E"),
    ('\u{1e1d}', "e"),
    ('\u{1e1e}', "F"),
    ('\u{1e1f}', "f"),
    ('\u{1e20}', "G"),
    ('\u{1e21}', "g"),
    ('\u{1e22}', "H"),
    ('\u{1e23}', "h"),
    ('\u{1e24}', "H"),
    ('\u{1e25}', "h"),
    ('\u{1e26}', "H"),
    ('\u{1e27}', "h"),
    ('\u{1e28}', "H"),
    ('\u{1e29}', "h"),
    ('\u{1e2a}', "H"),
    ('\u{1e2b}', "h"),
    ('\u{1e2c}', "I"),
    ('\u{1e2d}', "i"),
    ('\u{1e2e}', "I"),
    ('\u{1e2f}', "i"),
    ('\u{1e30}', "K"),
    ('\u{1e31}', "k"),
    ('\u{1e32}', "K"),
    ('\u{1e33}', "k"),
    ('\u{1e34}', "K"),
    ('\u{1e35}', "k"),
    ('\u{1e36}', "L"),
    ('\u{1e37}', "l"),
    ('\u{1e38}', "L"),
    ('\u{1e39}', "l"),
    ('\u{1e3a}', "L"),
    ('\u{1e3b}', "l"),
    ('\u{1e3c}', "L"),
    ('\u{1e3d}', "l"),
    ('\u{1e3e}', "M"),
    ('\u{1e3f}', "m"),
    ('\u{1e40}', "M"),
    ('\u{1e41}', "m"),
    ('\u{1e42}', "M"),
    ('\u{1e43}', "m"),
    ('\u{1e44}', "N"),
    ('\u{1e45}', "n"),
    ('\u{1e46}', "N"),
    ('\u{1e47}', "n"),
    ('\u{1e48}', "N"),
    ('\u{1e49}', "n"),
    ('\u{1e4a}', "N"),
    ('\u{1e4b}', "n"),
    ('\u{1e4c}', "O"),
    ('\u{1e4d}', "o"),
    ('\u{1e4e}', "O"),
    ('\u{1e4f}', "o"),
    ('\u{1e50}', "O"),
    ('\u{1e51}', "o"),
    ('\u{1e52}', "O"),
    ('\u{1e53}', "o"),
    ('\u{1e54}', "P"),
    ('\u{1e55}', "p"),
    ('\u{1e56}', "P"),
    ('\u{1e57}', "p"),
    ('\u{1e58}', "R"),
    ('\u{1e59}', "r"),
    ('\u{1e5a}', "R"),
    ('\u{1e5b}', "r"),
    ('\u{1e5c}', "R"),
    ('\u{1e5d}', "r"),
    ('\u{1e5e}', "R"),
    ('\u{1e5f}', "r"),
    ('\u{1e60}', "S"),
    ('\u{1e61}', "s"),
    ('\u{1e62}', "S"),
    ('\u{1e63}', "s"),
    ('\u{1e64}', "S"),
    ('\u{1e65}', "s"),
    ('\u{1e66}', "S"),
    ('\u{1e67}', "s"),
    ('\u{1e68}', "S"),
    ('\u{1e69}', "s"),
    ('\u{1e6a}', "T"),
    ('\u{1e6b}', "t"),
    ('\u{1e6c}', "T"),
    ('\u{1e6d}', "t"),
    ('\u{1e6e}', "T"),
    ('\u{1e6f}', "t"),
    ('\u{1e70}', "T"),
    ('\u{1e71}', "t"),
    ('\u{1e72}', "U"),
    ('\u{1e73}', "u"),
    ('\u{1e74}', "U"),
    ('\u{1e75}', "u"),
    ('\u{1e76}', "U"),
    ('\u{1e77}', "u"),
    ('\u{1e78}', "U"),
    ('\u{1e79}', "u"),
    ('\u{1e7a}', "U"),
    ('\u{1e7b}', "u"),
    ('\u{1e7c}', "V"),
    ('\u{1e7d}', "v"),
    ('\u{1e7e}', "V"),
    ('\u{1e7f}', "v"),
    ('\u{1e80}', "W"),
    ('\u{1e81}', "w"),
    ('\u{1e82}', "W"),
    ('\u{1e83}', "w"),
    ('\u{1e84}', "W"),
    ('\u{1e85}', "w"),
    ('\u{1e86}', "W"),
    ('\u{1e87}', "w"),
    ('\u{1e88}', "W"),
    ('\u{1e89}', "w"),
    ('\u{1e8a}', "X"),
    ('\u{1e8b}', "x"),
    ('\u{1e8c}', "X"),
    ('\u{1e8d}', "x"),
    ('\u{1e8e}', "Y"),
    ('\u{1e8f}', "y"),
    ('\u{1e90}', "Z"),
    ('\u{1e91}', "z"),
    ('\u{1e92}', "Z"),
    ('\u{1e93}', "z"),
    ('\u{1e94}', "Z"),
    ('\u{1e95}', "z"),
    ('\u{1e96}', "h"),
    ('\u{1e97}', "t"),
    ('\u{1e98}', "w"),
    ('\u{1e99}', "y"),
    ('\u{1e9a}', "aʾ"),
    ('\u{1e9b}', "s"),
    ('\u{1ea0}', "A"),
    ('\u{1ea1}', "a"),
    ('\u{1ea2}', "A"),
    ('\u{1ea3}', "a"),
    ('\u{1ea4}', "A"),
    ('\u{1ea5}', "a"),
    ('\u{1ea6}', "A"),
    ('\u{1ea7}', "a"),
    ('\u{1ea8}', "A"),
    ('\u{1ea9}', "a"),
    ('\u{1eaa}', "A"),
    ('\u{1eab}', "a"),
    ('\u{1eac}', "A"),
    ('\u{1ead}', "a"),
    ('\u{1eae}', "A"),
    ('\u{1eaf}', "a"),
    ('\u{1eb0}', "A"),
    ('\u{1eb1}', "a"),
    ('\u{1eb2}', "A"),
    ('\u{1eb3}', "a"),
    ('\u{1eb4}', "A"),
    ('\u{1eb5}', "a"),
    ('\u{1eb6}', "A"),
    ('\u{1eb7}', "a"),
    ('\u{1eb8}', "E"),
    ('\u{1eb9}', "e"),
    ('\u{1eba}', "E"),
    ('\u{1ebb}', "e"),
    ('\u{1ebc}', "E"),
    ('\u{1ebd}', "e"),
    ('\u{1ebe}', "E"),
    ('\u{1ebf}', "e"),
    ('\u{1ec0}', "E"),
    ('\u{1ec1}', "e"),
    ('\u{1ec2}', "E"),
    ('\u{1ec3}', "e"),
    ('\u{1ec4}', "E"),
    ('\u{1ec5}', "e"),
    ('\u{1ec6}', "E"),
    ('\u{1ec7}', "e"),
    ('\u{1ec8}', "I"),
    ('\u{1ec9}', "i"),
    ('\u{1eca}', "I"),
    ('\u{1ecb}', "i"),
    ('\u{1ecc}', "O"),
    ('\u{1ecd}', "o"),
    ('\u{1ece}', "O"),
    ('\u{1ecf}', "o"),
    ('\u{1ed0}', "O"),
    ('\u{1ed1}', "o"),
    ('\u{1ed2}', "O"),
    ('\u{1ed3}', "o"),
    ('\u{1ed4}', "O"),
    ('\u{1ed5}', "o"),
    ('\u{1ed6}', "O"),
    ('\u{1ed7}', "o"),
    ('\u{1ed8}', "O"),
    ('\u{1ed9}', "o"),
    ('\u{1eda}', "O"),
    ('\u{1edb}', "o"),
    ('\u{1edc}', "O"),
    ('\u{1edd}', "o"),
    ('\u{1ede}', "O"),
    ('\u{1edf}', "o"),
    ('\u{1ee0}', "O"),
    ('\u{1ee1}', "o"),
    ('\u{1ee2}', "O"),
    ('\u{1ee3}', "o"),
    ('\u{1ee4}', "U"),
    ('\u{1ee5}', "u"),
    ('\u{1ee6}', "U"),
    ('\u{1ee7}', "u"),
    ('\u{1ee8}', "U"),
    ('\u{1ee9}', "u"),
    ('\u{1eea}', "U"),
    ('\u{1eeb}', "u"),
    ('\u{1eec}', "U"),
    ('\u{1eed}', "u"),
    ('\u{1eee}', "U"),
    ('\u{1eef}', "u"),
    ('\u{1ef0}', "U"),
    ('\u{1ef1}', "u"),
    ('\u{1ef2}', "Y"),
    ('\u{1ef3}', "y"),
    ('\u{1ef4}', "Y"),
    ('\u{1ef5}', "y"),
    ('\u{1ef6}', "Y"),
    ('\u{1ef7}', "y"),
    ('\u{1ef8}', "Y"),
    ('\u{1ef9}', "y"),
];

const SILENT: u8 = 0;
const VOWEL: u8 = 1;
const B: u8 = 2;
const C: u8 = 3;
const D: u8 = 4;
const H: u8 = 5;
const L: u8 = 6;
const R: u8 = 7;
const M: u8 = 8;
const Y: u8 = 9;
const DIGIT: u8 = 10;
const SPACE: u8 = 11;
const OTHER: u8 = 12;
const CLASSNAME: &[u8] = b".ABCDHLRMY9 ?";

fn mid_class(ch: char) -> u8 {
    let _ = H;
    match ch.to_ascii_lowercase() {
        'a' | 'e' | 'i' | 'o' | 'u' | 'y' => VOWEL,
        'b' | 'f' | 'p' | 'v' | 'w' => B,
        'c' | 'g' | 'j' | 'k' | 'q' | 's' | 'x' | 'z' => C,
        'd' | 't' => D,
        'h' => SILENT,
        'l' => L,
        'm' | 'n' => M,
        'r' => R,
        '0'..='9' => DIGIT,
        ' ' | '\t' | '\n' | '\r' | '\u{b}' | '\u{c}' => SPACE,
        '\'' => SILENT,
        _ => OTHER,
    }
}

fn init_class(ch: char) -> u8 {
    match ch {
        'y' | 'Y' => Y,
        '\'' => OTHER,
        _ => mid_class(ch),
    }
}

fn is_mark(ch: char) -> bool {
    matches!(ch as u32, 0x0300..=0x036f | 0x1ab0..=0x1aff | 0x1dc0..=0x1dff | 0x20d0..=0x20ff | 0xfe20..=0xfe2f)
}

fn is_word_char(ch: char) -> bool {
    ch.is_alphanumeric() || is_mark(ch)
}

fn simple_fold(text: &str) -> String {
    crate::hierarchy::fold_case(text)
}

fn nfkd_base(ch: char) -> String {
    if let Ok(index) = LATIN_NFKD.binary_search_by(|(from, _)| from.cmp(&ch)) {
        return LATIN_NFKD[index].1.to_string();
    }
    if is_mark(ch) {
        return String::new();
    }
    ch.to_string()
}

/// The ICU word-break shapes present in the measured title/tag tables.
pub(super) fn tokens(text: &str) -> Vec<String> {
    let chars = text.chars().collect::<Vec<_>>();
    let mut result = Vec::new();
    let mut word = String::new();
    for (index, &ch) in chars.iter().enumerate() {
        if is_word_char(ch) {
            word.push(ch);
            continue;
        }
        if (ch == '\'' || ch == '\u{2019}')
            && !word.is_empty()
            && chars.get(index + 1).is_some_and(|next| is_word_char(*next))
        {
            word.push(ch);
            continue;
        }
        if !word.is_empty() {
            result.push(simple_fold(&word));
            word.clear();
        }
        if !ch.is_whitespace() {
            result.push(simple_fold(&ch.to_string()));
        }
    }
    if !word.is_empty() {
        result.push(simple_fold(&word));
    }
    result
}

fn transliterate(text: &str) -> String {
    let mut out = String::new();
    for ch in text.chars() {
        if (ch as u32) < 128 {
            out.push(ch);
            continue;
        }
        let special = match ch {
            'ß' => Some("ss"),
            'Æ' => Some("AE"),
            'æ' => Some("ae"),
            'Œ' => Some("OE"),
            'œ' => Some("oe"),
            'Ð' | 'Đ' => Some("D"),
            'ð' | 'đ' => Some("d"),
            'Þ' => Some("Th"),
            'þ' => Some("th"),
            'Ł' => Some("L"),
            'ł' => Some("l"),
            'Ø' => Some("O"),
            'ø' => Some("o"),
            _ => None,
        };
        if let Some(special) = special {
            out.push_str(special);
            continue;
        }
        let decomposed = nfkd_base(ch)
            .chars()
            .filter(|c| (*c as u32) < 128)
            .collect::<String>();
        if decomposed.is_empty() {
            out.push('?');
        } else {
            out.push_str(&decomposed);
        }
    }
    out
}

/// SQLite ext/misc/spellfix.c's `phoneticHash` for ASCII input.
fn phonehash(value: &str) -> String {
    let mut chars = value.chars().collect::<Vec<_>>();
    if chars.len() > 2 && matches!(chars[0], 'g' | 'k') && chars[1] == 'n' {
        chars.remove(0);
    }
    let mut result: Vec<u8> = Vec::new();
    let mut previous = 0x77_u8;
    let mut previous_non_silent = 0x77_u8;
    let mut initial = true;
    let mut index = 0;
    while index < chars.len() {
        let ch = chars[index];
        if index + 1 < chars.len() {
            if ch == 'w' && chars[index + 1] == 'r' {
                index += 1;
                continue;
            }
            if ch == 'd' && matches!(chars[index + 1], 'j' | 'g') {
                index += 1;
                continue;
            }
            if index + 2 < chars.len()
                && ch == 't'
                && chars[index + 1] == 'c'
                && chars[index + 2] == 'h'
            {
                index += 1;
                continue;
            }
        }
        let current = if initial {
            init_class(ch)
        } else {
            mid_class(ch)
        };
        index += 1;
        if current == SPACE {
            continue;
        }
        if current == OTHER && previous != DIGIT {
            continue;
        }
        initial = false;
        if current == VOWEL && (previous_non_silent == R || previous_non_silent == L) {
            continue;
        }
        if (current == R || current == L) && previous_non_silent == VOWEL && !result.is_empty() {
            result.pop();
        }
        previous = current;
        if current == SILENT {
            continue;
        }
        previous_non_silent = current;
        let symbol = CLASSNAME[current as usize];
        if result.last() != Some(&symbol) {
            result.push(symbol);
        }
    }
    String::from_utf8(result).unwrap_or_default()
}

fn character_class(previous: Option<char>, ch: char) -> u8 {
    if previous.is_some() {
        mid_class(ch)
    } else {
        init_class(ch)
    }
}

fn insert_delete_cost(previous: Option<char>, ch: char, following: Option<char>) -> i64 {
    let current = character_class(previous, ch);
    if current == SILENT {
        return 1;
    }
    if previous == Some(ch) {
        return 10;
    }
    if current == VOWEL && (previous == Some('r') || following == Some('r')) {
        return 20;
    }
    let previous_class = match previous {
        Some(p) => character_class(Some(p), p),
        None => character_class(None, '\0'),
    };
    if current == previous_class {
        return if current == VOWEL { 15 } else { 50 };
    }
    100
}

fn substitute_cost(previous: Option<char>, source: char, target: char) -> i64 {
    if source == target {
        return 0;
    }
    if source.is_alphabetic()
        && target.is_alphabetic()
        && source.to_lowercase().eq(target.to_lowercase())
    {
        return 0;
    }
    let source_class = character_class(previous, source);
    let target_class = character_class(previous, target);
    if source_class == target_class {
        return 40;
    }
    if (B..=Y).contains(&source_class) && (B..=Y).contains(&target_class) {
        return 75;
    }
    100
}

/// SQLite spellfix1's built-in `editdist1` cost.
fn edit_distance(source: &str, target: &str) -> i64 {
    let source = source.chars().collect::<Vec<_>>();
    let target = target.chars().collect::<Vec<_>>();
    let mut prefix: Option<char> = None;
    let mut index = 0;
    while index < source.len() && index < target.len() && source[index] == target[index] {
        prefix = Some(source[index]);
        index += 1;
    }
    let source = &source[index..];
    let target = &target[index..];
    if source.is_empty() && target.is_empty() {
        return 0;
    }
    if source.is_empty() {
        let mut total = 0;
        let mut previous = prefix;
        for (i, &ch) in target.iter().enumerate() {
            total += insert_delete_cost(previous, ch, target.get(i + 1).copied()) / 4;
            previous = Some(ch);
        }
        return total;
    }
    if target.is_empty() {
        let mut total = 0;
        let mut previous = prefix;
        for (i, &ch) in source.iter().enumerate() {
            total += insert_delete_cost(previous, ch, source.get(i + 1).copied());
            previous = Some(ch);
        }
        return total;
    }
    let mut matrix = vec![0_i64; target.len() + 1];
    let mut context: Vec<Option<char>> = vec![None; target.len() + 1];
    context[0] = prefix;
    let mut previous = prefix;
    for column in 1..=target.len() {
        let ch = target[column - 1];
        let following = target.get(column).copied();
        context[column] = Some(ch);
        matrix[column] = matrix[column - 1] + insert_delete_cost(previous, ch, following);
        previous = Some(ch);
    }
    let mut previous_source = prefix;
    for row in 1..=source.len() {
        let last_source = row == source.len();
        let source_char = source[row - 1];
        let source_next = source.get(row).copied();
        let mut diagonal = matrix[0];
        matrix[0] = diagonal + insert_delete_cost(previous_source, source_char, source_next);
        for column in 1..=target.len() {
            let target_char = target[column - 1];
            let target_next = target.get(column).copied();
            let mut insertion = insert_delete_cost(context[column - 1], target_char, target_next);
            if last_source {
                insertion /= 4;
            }
            let deletion = insert_delete_cost(context[column], source_char, target_next);
            let substitution = substitute_cost(context[column - 1], source_char, target_char);
            let mut total = insertion + matrix[column - 1];
            let mut next_context = Some(target_char);
            if deletion + matrix[column] < total {
                total = deletion + matrix[column];
                next_context = Some(source_char);
            }
            if substitution + diagonal < total {
                total = substitution + diagonal;
            }
            diagonal = matrix[column];
            matrix[column] = total;
            context[column] = next_context;
        }
        previous_source = Some(source_char);
    }
    matrix[target.len()]
}

/// ICU primary/shifted equivalence used by PMS's title FTS table.
fn collation_key(value: &str) -> String {
    let folded = simple_fold(value);
    let mut out = String::new();
    for ch in folded.chars() {
        for base in nfkd_base(ch).chars() {
            for lower in crate::hierarchy::fold_case(&base.to_string()).chars() {
                if !is_mark(lower) && lower.is_alphanumeric() {
                    out.push(lower);
                }
            }
        }
    }
    out
}

#[derive(Clone, Debug)]
struct Word {
    rowid: usize,
    word: String,
    rank: usize,
    k1: String,
    k2: String,
}

/// One measured spellfix vocabulary, including its bounded candidate scan.
#[derive(Clone, Default, Debug)]
pub struct Vocabulary {
    rows: Vec<Word>,
}

impl Vocabulary {
    fn new<'a>(inputs: impl Iterator<Item = String>) -> Self {
        let distinct = inputs.collect::<HashSet<_>>();
        let mut ranks: BTreeMap<String, usize> = BTreeMap::new();
        for value in distinct {
            for token in tokens(&value) {
                *ranks.entry(token).or_insert(0) += 1;
            }
        }
        let mut rows = ranks
            .into_iter()
            .enumerate()
            .map(|(index, (word, rank))| {
                let k1 = transliterate(&word).to_lowercase();
                let k2 = phonehash(&k1);
                Word {
                    rowid: index + 1,
                    word,
                    rank,
                    k1,
                    k2,
                }
            })
            .collect::<Vec<_>>();
        rows.sort_by(|a, b| a.k2.cmp(&b.k2).then(a.rowid.cmp(&b.rowid)));
        Self { rows }
    }

    fn candidates(&self, query: &str) -> Vec<(i64, String)> {
        let (top, cutoff) = (10_usize, 200_i64);
        let pattern = transliterate(query);
        let hashed = phonehash(&pattern);
        let mut scope = 3;
        if hashed.len() <= scope {
            scope = if hashed.len() > 2 {
                hashed.len() - 1
            } else {
                hashed.len()
            };
        }
        let prefix = &hashed[..scope];
        let mut selected: Vec<(i64, String)> = Vec::new();
        let mut worst_score = 0;
        let mut worst_index = 0;
        for row in &self.rows {
            if !row.k2.starts_with(prefix) {
                continue;
            }
            let distance = edit_distance(&pattern, &row.k1);
            let bits = (usize::BITS - row.rank.leading_zeros()) as i64;
            let score = distance + 32 - bits;
            if score > cutoff {
                continue;
            }
            let index = if selected.len() < top {
                selected.len()
            } else if score < worst_score {
                worst_index
            } else {
                continue;
            };
            if index == selected.len() {
                selected.push((score, row.word.clone()));
            } else {
                selected[index] = (score, row.word.clone());
            }
            if selected.len() == top {
                worst_index = 0;
                worst_score = selected[0].0;
                for (candidate, entry) in selected.iter().enumerate().skip(1) {
                    if worst_score < entry.0 {
                        worst_score = entry.0;
                        worst_index = candidate;
                    }
                }
            }
        }
        selected.sort_by_key(|entry| entry.0);
        selected
    }

    fn groups(&self, query: &str) -> Groups {
        query
            .split(' ')
            .filter(|original| original.len() >= 3)
            .map(|original| {
                let candidates = self
                    .candidates(original)
                    .into_iter()
                    .map(|(_, word)| collation_key(&word))
                    .collect::<HashSet<_>>();
                (candidates, collation_key(original))
            })
            .collect()
    }
}

/// Each query word's candidate keys and its own, as `Vocabulary::groups` makes them.
type Groups = Vec<(HashSet<String>, String)>;

fn hits(groups: &Groups, text: &str) -> bool {
    if groups.is_empty() {
        return false;
    }
    let haystack = tokens(text)
        .into_iter()
        .map(|word| collation_key(&word))
        .collect::<Vec<_>>();
    groups.iter().all(|(candidates, prefix)| {
        haystack
            .iter()
            .any(|word| candidates.contains(word) || word.starts_with(prefix.as_str()))
    })
}

/// One query's candidates, scanned once for every title and name a search tests: scanned
/// again for each, a misspelt query held the server for seconds on a 100k-row library.
pub struct Fuzzy {
    metadata: Groups,
    tags: Groups,
}

impl Fuzzy {
    pub fn metadata_matches<'v>(&self, get: impl Fn(&str) -> Option<crate::hotrow::Field<'v>>) -> bool {
        let text = ["title", "titleSort", "originalTitle"]
            .iter()
            .map(|field| get(field).and_then(crate::hotrow::Field::as_str).unwrap_or(""))
            .collect::<Vec<_>>()
            .join(" ");
        hits(&self.metadata, &text)
    }

    pub fn tag_matches(&self, name: &str) -> bool {
        hits(&self.tags, name)
    }
}

/// Where the server's vocabulary is (`Runtime::spelling_fuzzy`).
#[derive(Debug)]
pub enum Spelling {
    /// Nowhere: a start builds it from the rows.
    Unbuilt,
    /// Only in the state file, read back by the next search that falls back on it.
    Saved,
    Loaded(SearchIndex),
}

impl Spelling {
    /// The saved document: `null` for one not in memory, which a save never writes.
    pub fn to_value(&self) -> serde_json::Value {
        match self {
            Spelling::Loaded(index) => index.to_value(),
            _ => serde_json::Value::Null,
        }
    }
}

/// One snapshot of the title and tag spellfix tables.
#[derive(Clone, Default, Debug)]
pub struct SearchIndex {
    metadata: Vocabulary,
    tags: Vocabulary,
}

const TAG_FIELDS: &[&str] = &[
    "Genre",
    "Collection",
    "Director",
    "Writer",
    "Producer",
    "Role",
    "Country",
    "Label",
    "Similar",
    "Mood",
    "Style",
];
const TAG_CHILDREN: &[&str] = &["Chapter", "Review", "Marker", "CommonSenseMedia", "Network"];

impl SearchIndex {
    pub fn build(rows: &dyn Fn(&mut dyn FnMut(&serde_json::Value)), static_tags: &[String]) -> Self {
        use serde_json::Value;
        let text = |value: Option<&Value>| value.and_then(Value::as_str).map(str::to_string);
        let mut titles = Vec::new();
        let mut tags: Vec<String> = static_tags.to_vec();
        // Every row once, read one at a time (`Catalog::each_row`).
        rows(&mut |row: &Value| {
            if let Some(title) = text(row.get("title")) {
                let sort = text(row.get("titleSort"))
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| title.clone());
                titles.push(format!("{title} {sort}"));
            }
            for field in TAG_FIELDS.iter().chain(TAG_CHILDREN) {
                for child in row
                    .get(*field)
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                {
                    tags.extend(text(child.get("tag")));
                }
            }
            for child in row
                .get("Guid")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                tags.extend(text(child.get("id")));
            }
            for child in row
                .get("Rating")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                tags.extend(text(child.get("image")));
            }
            tags.extend(text(row.get("studio")));
            for (field, label) in [("thumb", "Thumb"), ("art", "Art"), ("theme", "Theme")] {
                if row.get(field).is_some_and(|v| !v.is_null() && v != "") {
                    tags.push(label.to_string());
                }
            }
            if row.get("_clearLogo").is_some_and(|v| !v.is_null()) {
                tags.push("ClearLogo".to_string());
            }
        });
        Self {
            metadata: Vocabulary::new(titles.into_iter()),
            tags: Vocabulary::new(tags.into_iter()),
        }
    }

    pub fn fuzzy(&self, query: &str) -> Fuzzy {
        Fuzzy { metadata: self.metadata.groups(query), tags: self.tags.groups(query) }
    }

    /// `persist.py` keeps `(spelling.metadata.rows, spelling.tags.rows)`: cycle 73
    /// measured that a restart does not refresh the vocabulary (PMS rebuilds its
    /// spellfix tables at database optimization only), so the rows are saved as they
    /// stand rather than rebuilt from the restored catalog.
    pub fn to_value(&self) -> serde_json::Value {
        let rows = |vocabulary: &Vocabulary| {
            serde_json::Value::Array(
                vocabulary
                    .rows
                    .iter()
                    .map(|w| serde_json::json!([w.rowid, w.word, w.rank, w.k1, w.k2]))
                    .collect(),
            )
        };
        serde_json::json!({"metadata": rows(&self.metadata), "tags": rows(&self.tags)})
    }

    /// The inverse of `to_value`; `None` for anything it did not write.
    pub fn from_value(value: &serde_json::Value) -> Option<Self> {
        let rows = |field: &str| -> Option<Vocabulary> {
            let mut out = Vec::new();
            for row in value.get(field)?.as_array()? {
                let row = row.as_array()?;
                out.push(Word {
                    rowid: row.first()?.as_u64()? as usize,
                    word: row.get(1)?.as_str()?.to_string(),
                    rank: row.get(2)?.as_u64()? as usize,
                    k1: row.get(3)?.as_str()?.to_string(),
                    k2: row.get(4)?.as_str()?.to_string(),
                });
            }
            Some(Vocabulary { rows: out })
        };
        Some(Self {
            metadata: rows("metadata")?,
            tags: rows("tags")?,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};
    use std::collections::HashMap;

    fn static_tags() -> Vec<String> {
        serde_json::from_str(include_str!("../../../data/spellfix_static_tags.json"))
            .expect("the static vocabulary parses")
    }

    /// `tests/catalog_features.json`'s index: the tiny measured plexlab corpus, where
    /// `zzz` has the same two words.
    fn features_index() -> SearchIndex {
        let document: Value = serde_json::from_str(include_str!("../../../tests/catalog_features.json"))
            .expect("the features fixture parses");
        let catalog = crate::catalog::Catalog::from_value(document).expect("the features fixture loads");
        SearchIndex::build(&|visit| catalog.each_row(|_, row| visit(row)), &static_tags())
    }

    fn words(vocabulary: &Vocabulary, query: &str) -> Vec<String> {
        vocabulary.candidates(query).into_iter().map(|(_, word)| word).collect()
    }

    /// Cycle 37 reconstructed both PMS spellfix tables: the exact editdist1/rank score
    /// (`job` at distance 110, `steel` at 137), the top-ten cap and the <=200 cutoff.
    #[test]
    fn candidates_use_the_measured_cost_and_bounded_result_set() {
        let zzz = features_index().metadata.candidates("zzz");
        assert_eq!(zzz, vec![(140, "job".to_string()), (167, "steel".to_string())]);
    }

    /// Cycle 39: every row-backed source feeds the tag vocabulary, each with its own field
    /// shape -- GUIDs by `id`, ratings by `image`, a scalar studio, and a constant naming
    /// each populated artwork column.
    #[test]
    fn the_tag_vocabulary_reads_every_row_backed_source() {
        let row = json!({
            "title": "Vocabulary Sources",
            "Genre": [{"tag": "Measured Genre"}],
            "Chapter": [{"tag": "The Grey Havens"}],
            "Review": [{"tag": "Reviewer One"}],
            "Marker": [{"tag": "Intro Marker"}],
            "CommonSenseMedia": [{"tag": "Age Twelve"}],
            "Guid": [{"id": "imdb://tt1234567"}],
            "Rating": [{"image": "rottentomatoes://image.rating.upright"}],
            "studio": "Studio Canal",
            "Network": [{"tag": "HBO Max"}],
            "thumb": "/thumb/path",
            "art": "/art/path",
            "theme": "/theme/path",
            "_clearLogo": "/logo/path",
        });
        let rows = [row];
        let index = SearchIndex::build(&|visit| rows.iter().for_each(|row| visit(row)), &[]);
        let ranks = index.tags.rows.iter().map(|word| (word.word.as_str(), word.rank)).collect::<HashMap<_, _>>();
        for word in [
            "measured", "grey", "reviewer", "marker", "twelve", "imdb", "rottentomatoes", "studio",
            "canal", "hbo", "max", "thumb", "art", "theme", "clearlogo",
        ] {
            assert_eq!(ranks.get(word), Some(&1), "{word} in {ranks:?}");
        }
    }

    /// Cycle 39's versioned 28-string server-owned vocabulary takes part in the bounded
    /// candidate vectors.
    #[test]
    fn the_static_tags_reach_the_candidate_vectors() {
        let index = features_index();
        assert_eq!(static_tags().len(), 28);
        assert_eq!(words(&index.tags, "optmized"), vec!["optimized".to_string()]);
        assert!(words(&index.tags, "tft").contains(&"dvd".to_string()));
        assert!(words(&index.tags, "flr").contains(&"bluray".to_string()));
    }

    /// A compact reconstruction of the live `emtz` witness: 5,785 distinct imdb GUID strings
    /// give that token its measured rank and keep Imtiaz out of the ten-candidate vector,
    /// exactly as the 1.42.2 reference's visible people hubs do.
    #[test]
    fn guid_rank_keeps_the_emtz_people_candidate_outside_the_cap() {
        let ranks = [
            ("ents", 2), ("hintze", 1), ("ends", 2), ("entre", 1), ("hunts", 2),
            ("enter", 4), ("amter", 1), ("ent", 3), ("emde", 1), ("imtiaz", 1),
        ];
        let roles = ranks
            .iter()
            .flat_map(|(word, count)| (0..*count).map(move |index| json!({"tag": format!("{word}{}", " ".repeat(index))})))
            .collect::<Vec<_>>();
        let guids = (0..5785).map(|index| json!({"id": format!("imdb://tt{index:07}")})).collect::<Vec<_>>();
        let rows = [json!({"title": "Vocabulary Witness", "Role": roles, "Guid": guids})];
        let index = SearchIndex::build(&|visit| rows.iter().for_each(|row| visit(row)), &[]);
        let emtz = words(&index.tags, "emtz");
        assert_eq!(emtz.last().map(String::as_str), Some("imdb"), "{emtz:?}");
        assert!(!emtz.contains(&"imtiaz".to_string()), "{emtz:?}");
        assert!(!index.fuzzy("emtz").tag_matches("Imtiaz Ali"));
    }
}

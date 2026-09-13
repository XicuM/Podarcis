//! Terminal key encoding for the embedded pane.
//!
//! When the sidebar has focus every keystroke is handed to the child process,
//! so it has to be encoded the way a real terminal emulator would. Getting this
//! wrong is how embedded panes end up feeling "almost right" — arrows that
//! insert letters, a Ctrl-C that does nothing.

use crossterm::event::{
    KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};

/// Encode a key event as the bytes a terminal would send.
///
/// `application_cursor` reflects the child's DECCKM state: full-screen programs
/// set it and then expect `ESC O A` for Up rather than `ESC [ A`.
pub fn encode(key: &KeyEvent, application_cursor: bool) -> Vec<u8> {
    let m = key.modifiers;
    let ctrl = m.contains(KeyModifiers::CONTROL);
    let alt = m.contains(KeyModifiers::ALT);
    let shift = m.contains(KeyModifiers::SHIFT);

    let body: Vec<u8> = match key.code {
        KeyCode::Char(c) => return encode_char(c, ctrl, alt, shift),
        KeyCode::Enter => vec![b'\r'],
        KeyCode::Tab => vec![b'\t'],
        KeyCode::BackTab => b"\x1b[Z".to_vec(),
        KeyCode::Backspace => vec![0x7f],
        KeyCode::Esc => vec![0x1b],
        KeyCode::Up => cursor_key(b'A', m, application_cursor),
        KeyCode::Down => cursor_key(b'B', m, application_cursor),
        KeyCode::Right => cursor_key(b'C', m, application_cursor),
        KeyCode::Left => cursor_key(b'D', m, application_cursor),
        KeyCode::Home => cursor_key(b'H', m, application_cursor),
        KeyCode::End => cursor_key(b'F', m, application_cursor),
        KeyCode::Insert => csi_tilde(2, m),
        KeyCode::Delete => csi_tilde(3, m),
        KeyCode::PageUp => csi_tilde(5, m),
        KeyCode::PageDown => csi_tilde(6, m),
        KeyCode::F(n) => return encode_function(n, m),
        _ => return Vec::new(),
    };
    if alt && !body.starts_with(b"\x1b") {
        let mut out = vec![0x1b];
        out.extend(body);
        return out;
    }
    body
}

fn encode_char(c: char, ctrl: bool, alt: bool, _shift: bool) -> Vec<u8> {
    let mut out = Vec::new();
    if alt {
        out.push(0x1b);
    }
    if ctrl {
        // The classic control-byte table. Anything outside it has no control
        // form, so the plain character is the honest answer.
        let byte = match c.to_ascii_lowercase() {
            c @ 'a'..='z' => (c as u8) - b'a' + 1,
            ' ' | '@' => 0,
            '[' => 0x1b,
            '\\' => 0x1c,
            ']' => 0x1d,
            '^' => 0x1e,
            '_' | '?' => 0x1f,
            _ => {
                out.extend(c.to_string().into_bytes());
                return out;
            }
        };
        out.push(byte);
        return out;
    }
    out.extend(c.to_string().into_bytes());
    out
}

/// `1 + shift + 2*alt + 4*ctrl`, the standard xterm modifier parameter.
fn modifier_param(m: KeyModifiers) -> u8 {
    1 + u8::from(m.contains(KeyModifiers::SHIFT))
        + 2 * u8::from(m.contains(KeyModifiers::ALT))
        + 4 * u8::from(m.contains(KeyModifiers::CONTROL))
}

/// A modified cursor key is always CSI, even in application mode — that is what
/// every terminal emulator does, and what xterm's own table says.
fn cursor_key(letter: u8, m: KeyModifiers, application: bool) -> Vec<u8> {
    let param = modifier_param(m);
    match (param, application) {
        (1, true) => vec![0x1b, b'O', letter],
        (1, false) => vec![0x1b, b'[', letter],
        _ => format!("\x1b[1;{param}{}", letter as char).into_bytes(),
    }
}

fn csi_tilde(number: u8, m: KeyModifiers) -> Vec<u8> {
    let param = modifier_param(m);
    if param == 1 {
        format!("\x1b[{number}~").into_bytes()
    } else {
        format!("\x1b[{number};{param}~").into_bytes()
    }
}

fn encode_function(n: u8, m: KeyModifiers) -> Vec<u8> {
    let param = modifier_param(m);
    match n {
        1..=4 if param == 1 => vec![0x1b, b'O', b'P' + (n - 1)],
        1..=4 => format!("\x1b[1;{param}{}", (b'P' + (n - 1)) as char).into_bytes(),
        _ => {
            // F5..F12 map to 15,17,18,19,20,21,23,24 — 16 and 22 are skipped.
            let code = match n {
                5 => 15,
                6..=10 => 17 + (n - 6),
                11 => 23,
                12 => 24,
                _ => return Vec::new(),
            };
            csi_tilde(code, m)
        }
    }
}

/// Cell size advertised on the herdr PTY when the host ioctl has no pixels.
/// Herdr's SGR-pixels mode (1016) interprets reports in these units.
pub const PTY_CELL_PX: (u16, u16) = (8, 16);

/// Encode a mouse event in SGR (1006) format for the child PTY.
/// `col` and `row` are 1-based cell coordinates within the child's screen.
/// When `pixel` is set, they are converted to 1-based pixel coordinates (DEC 1016).
pub fn encode_mouse(mouse: &MouseEvent, col: u16, row: u16, pixel: Option<(u16, u16)>) -> Vec<u8> {
    let m = mouse.modifiers;
    let mut modifier_bits = 0u8;
    if m.contains(KeyModifiers::SHIFT) {
        modifier_bits |= 4;
    }
    if m.contains(KeyModifiers::ALT) {
        modifier_bits |= 8;
    }
    if m.contains(KeyModifiers::CONTROL) {
        modifier_bits |= 16;
    }

    let (base, release) = match mouse.kind {
        MouseEventKind::Down(btn) => {
            let b = match btn {
                MouseButton::Left => 0,
                MouseButton::Middle => 1,
                MouseButton::Right => 2,
            };
            (b, false)
        }
        MouseEventKind::Up(btn) => {
            let b = match btn {
                MouseButton::Left => 0,
                MouseButton::Middle => 1,
                MouseButton::Right => 2,
            };
            (b, true)
        }
        MouseEventKind::Drag(btn) => {
            let b = match btn {
                MouseButton::Left => 0,
                MouseButton::Middle => 1,
                MouseButton::Right => 2,
            };
            (b + 32, false)
        }
        MouseEventKind::ScrollDown => (65, false),
        MouseEventKind::ScrollUp => (64, false),
        MouseEventKind::ScrollLeft => (66, false),
        MouseEventKind::ScrollRight => (67, false),
        MouseEventKind::Moved => (35, false),
    };

    let (col, row) = if let Some((cw, ch)) = pixel {
        let cw = u32::from(cw.max(1));
        let ch = u32::from(ch.max(1));
        (
            u32::from(col.saturating_sub(1)) * cw + 1,
            u32::from(row.saturating_sub(1)) * ch + 1,
        )
    } else {
        (u32::from(col), u32::from(row))
    };

    let code = base + modifier_bits;
    let suffix = if release { 'm' } else { 'M' };
    format!("\x1b[<{code};{col};{row}{suffix}").into_bytes()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(code: KeyCode, m: KeyModifiers) -> Vec<u8> {
        encode(&KeyEvent::new(code, m), false)
    }

    fn app_key(code: KeyCode, m: KeyModifiers) -> Vec<u8> {
        encode(&KeyEvent::new(code, m), true)
    }

    const NONE: KeyModifiers = KeyModifiers::NONE;

    #[test]
    fn plain_characters_pass_through_as_utf8() {
        assert_eq!(key(KeyCode::Char('a'), NONE), b"a");
        assert_eq!(key(KeyCode::Char('é'), NONE), "é".as_bytes());
        assert_eq!(key(KeyCode::Char('A'), KeyModifiers::SHIFT), b"A");
    }

    #[test]
    fn control_characters_use_the_classic_table() {
        assert_eq!(key(KeyCode::Char('c'), KeyModifiers::CONTROL), vec![3]);
        assert_eq!(key(KeyCode::Char('C'), KeyModifiers::CONTROL), vec![3], "case does not matter");
        assert_eq!(key(KeyCode::Char('a'), KeyModifiers::CONTROL), vec![1]);
        assert_eq!(key(KeyCode::Char('['), KeyModifiers::CONTROL), vec![0x1b]);
    }

    #[test]
    fn ctrl_space_reaches_herdr_as_nul_because_it_is_its_prefix() {
        assert_eq!(key(KeyCode::Char(' '), KeyModifiers::CONTROL), vec![0]);
    }

    #[test]
    fn alt_prefixes_with_escape() {
        assert_eq!(key(KeyCode::Char('x'), KeyModifiers::ALT), vec![0x1b, b'x']);
        assert_eq!(key(KeyCode::Enter, KeyModifiers::ALT), vec![0x1b, b'\r']);
    }

    #[test]
    fn ctrl_alt_combines_both() {
        assert_eq!(key(KeyCode::Char('c'), KeyModifiers::CONTROL | KeyModifiers::ALT), vec![0x1b, 3]);
    }

    #[test]
    fn a_control_character_with_no_control_form_sends_itself() {
        assert_eq!(key(KeyCode::Char('1'), KeyModifiers::CONTROL), b"1");
    }

    #[test]
    fn editing_keys_use_the_conventional_bytes() {
        assert_eq!(key(KeyCode::Enter, NONE), b"\r");
        assert_eq!(key(KeyCode::Backspace, NONE), vec![0x7f]);
        assert_eq!(key(KeyCode::Tab, NONE), b"\t");
        assert_eq!(key(KeyCode::BackTab, NONE), b"\x1b[Z");
        assert_eq!(key(KeyCode::Esc, NONE), vec![0x1b]);
    }

    #[test]
    fn arrows_are_csi_sequences_and_gain_a_modifier_parameter() {
        assert_eq!(key(KeyCode::Up, NONE), b"\x1b[A");
        assert_eq!(key(KeyCode::Left, NONE), b"\x1b[D");
        // herdr binds ctrl+alt+arrows to pane focus: 1 + 2(alt) + 4(ctrl) = 7
        assert_eq!(
            key(KeyCode::Left, KeyModifiers::CONTROL | KeyModifiers::ALT),
            b"\x1b[1;7D"
        );
        // and ctrl+alt+shift+arrows to resize: 1 + 1 + 2 + 4 = 8
        assert_eq!(
            key(KeyCode::Right, KeyModifiers::CONTROL | KeyModifiers::ALT | KeyModifiers::SHIFT),
            b"\x1b[1;8C"
        );
    }

    #[test]
    fn application_cursor_mode_switches_arrows_to_ss3() {
        assert_eq!(app_key(KeyCode::Up, NONE), b"\x1bOA");
        assert_eq!(app_key(KeyCode::Home, NONE), b"\x1bOH");
        // A modified arrow stays CSI in both modes.
        assert_eq!(app_key(KeyCode::Up, KeyModifiers::CONTROL), b"\x1b[1;5A");
    }

    #[test]
    fn navigation_keys_use_tilde_or_letter_forms() {
        assert_eq!(key(KeyCode::Home, NONE), b"\x1b[H");
        assert_eq!(key(KeyCode::End, NONE), b"\x1b[F");
        assert_eq!(key(KeyCode::PageUp, NONE), b"\x1b[5~");
        assert_eq!(key(KeyCode::Delete, NONE), b"\x1b[3~");
        assert_eq!(key(KeyCode::Delete, KeyModifiers::SHIFT), b"\x1b[3;2~");
    }

    #[test]
    fn function_keys_switch_encoding_at_f5() {
        assert_eq!(key(KeyCode::F(1), NONE), b"\x1bOP");
        assert_eq!(key(KeyCode::F(4), NONE), b"\x1bOS");
        assert_eq!(key(KeyCode::F(5), NONE), b"\x1b[15~");
        assert_eq!(key(KeyCode::F(6), NONE), b"\x1b[17~", "16 is skipped");
        assert_eq!(key(KeyCode::F(12), NONE), b"\x1b[24~");
    }

    #[test]
    fn unknown_keys_send_nothing_rather_than_garbage() {
        assert!(key(KeyCode::CapsLock, NONE).is_empty());
        assert!(key(KeyCode::F(25), NONE).is_empty());
    }

    #[test]
    fn mouse_left_click_encodes_sgr_press_and_release() {
        let down = MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: 11,
            row: 1,
            modifiers: KeyModifiers::NONE,
        };
        assert_eq!(encode_mouse(&down, 11, 1, None), b"\x1b[<0;11;1M");

        let up = MouseEvent {
            kind: MouseEventKind::Up(MouseButton::Left),
            column: 11,
            row: 1,
            modifiers: KeyModifiers::NONE,
        };
        assert_eq!(encode_mouse(&up, 11, 1, None), b"\x1b[<0;11;1m");
    }

    #[test]
    fn mouse_right_click_and_modifiers() {
        let right_down = MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Right),
            column: 5,
            row: 10,
            modifiers: KeyModifiers::CONTROL | KeyModifiers::SHIFT,
        };
        // Right button (2) + Shift (4) + Ctrl (16) = 22
        assert_eq!(encode_mouse(&right_down, 5, 10, None), b"\x1b[<22;5;10M");
    }

    #[test]
    fn mouse_scroll_and_drag() {
        let scroll_up = MouseEvent {
            kind: MouseEventKind::ScrollUp,
            column: 20,
            row: 15,
            modifiers: KeyModifiers::NONE,
        };
        assert_eq!(encode_mouse(&scroll_up, 20, 15, None), b"\x1b[<64;20;15M");

        let scroll_down = MouseEvent {
            kind: MouseEventKind::ScrollDown,
            column: 20,
            row: 15,
            modifiers: KeyModifiers::NONE,
        };
        assert_eq!(encode_mouse(&scroll_down, 20, 15, None), b"\x1b[<65;20;15M");

        let drag = MouseEvent {
            kind: MouseEventKind::Drag(MouseButton::Left),
            column: 8,
            row: 12,
            modifiers: KeyModifiers::NONE,
        };
        // Left button (0) + 32 = 32
        assert_eq!(encode_mouse(&drag, 8, 12, None), b"\x1b[<32;8;12M");
    }

    #[test]
    fn mouse_pixel_mode_scales_cells_to_pty_pixels() {
        let down = MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: 3,
            row: 2,
            modifiers: KeyModifiers::NONE,
        };
        // cell (3,2) with 8×16 cells → pixel (17, 17)
        assert_eq!(
            encode_mouse(&down, 3, 2, Some(PTY_CELL_PX)),
            b"\x1b[<0;17;17M"
        );
    }

    #[test]
    fn mouse_motion_encodes_sgr_any_event() {
        let moved = MouseEvent {
            kind: MouseEventKind::Moved,
            column: 4,
            row: 5,
            modifiers: KeyModifiers::NONE,
        };
        assert_eq!(encode_mouse(&moved, 4, 5, None), b"\x1b[<35;4;5M");
    }
}

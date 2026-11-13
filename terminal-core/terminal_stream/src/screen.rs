//! The screen model: pyte's `HubTerminalScreen`/`HistoryScreen` semantics over the `vte` state machine.
//!
//! Why this is not `alacritty_terminal::Term` (doc §7.2's first choice): the corpus, not the crate list,
//! decides. Two of its behaviours cannot reproduce the goldens:
//!
//! * `Term::resize` reflows the grid (`grid.resize(!alt, lines, cols)` with reflow enabled), while pyte's
//!   `Screen.resize` anchors content top-left, pads on the right and bottom, and drops the top rows on a
//!   shrink. The live cases resize 120x36 -> 140x44 mid-stream, and `screen_at_resize.txt` is the screen
//!   right after it; a reflow moves text between rows by design.
//! * `Term` keeps a separate inactive grid for the alternate screen (`?1049h`/`?1049l`), while pyte has a
//!   single buffer: `?1049` only records a mode bit. Four of the five live recordings switch to `?1049`,
//!   so alacritty would render a different buffer after the switch.
//!
//! What is kept from the crate is its parser: `vte` 0.15 (the one `alacritty_terminal` 0.26 builds on),
//! driven through `Perform` rather than `ansi::Handler`, because the handler's semantics are alacritty's.
//! Everything below mirrors pyte 0.8.2 line by line, including its quirks, so a diff against the oracle is
//! a bug here (DECISIONS.md `oracle-bugs`).
//!
//! Deliberately not reproduced: pyte stores cells in sparse dicts, so `delete_lines` over a row that was
//! never touched keeps the target row's old content; this model keeps dense rows (blank = one space) and
//! clears the target. `get_clean_screen_text` renders `Screen.display` on every read, which materialises
//! every row anyway, and no corpus case runs IL/DL at all.
//!
//! Combining marks are appended to the previous cell as pyte does, but without its NFC pass
//! (`unicodedata.normalize`): no recording in the corpus carries a combining mark, and the normalisation
//! tables would be a new dependency. A composed pair therefore renders as base + mark here.

use std::collections::{BTreeSet, VecDeque};

use vte::{Params, Perform};

use crate::width::{char_width, is_combining};

/// A cell's text: `'\0'` marks a wide character's stub, which renders as nothing in a history row and
/// as one space in the screen renderer (pyte's `HubTerminalScreen.display` papers the stub over).
const STUB: char = '\0';

#[derive(Clone, Copy, PartialEq, Eq)]
struct Cell {
    ch: char,
    /// 0 = none; otherwise `index + 1` into `Screen::marks`.
    marks: u32,
}

const BLANK: Cell = Cell { ch: ' ', marks: 0 };

impl Cell {
    fn is_stub(&self) -> bool {
        self.ch == STUB
    }
}

/// pyte's `buffer[y]`; `cells.len()` is the width the row had when it was created, which can differ from
/// the current column count after a resize (pyte keeps history rows at their old width).
#[derive(Clone)]
struct Row {
    cells: Vec<Cell>,
}

impl Row {
    fn blank(cols: usize) -> Self {
        Self {
            cells: vec![BLANK; cols],
        }
    }
}

#[derive(Clone, Copy)]
struct Savepoint {
    x: usize,
    y: usize,
    origin: bool,
    wrap: bool,
}

pub struct Screen {
    cols: usize,
    rows: usize,
    lines: Vec<Row>,
    /// `HistoryScreen.history.top`: the newest line is at the back.
    history: VecDeque<Row>,
    history_limit: usize,
    /// `cursor.x` can equal `cols`: pyte's pending-wrap position.
    x: usize,
    y: usize,
    margins: Option<(usize, usize)>,
    /// Private mode 7 (`DECAWM`), private 6 (`DECOM`), 4 (`IRM`), 20 (`LNM`), private 3 (`DECCOLM`).
    wrap: bool,
    origin: bool,
    insert: bool,
    newline: bool,
    saved_columns: Option<usize>,
    savepoints: Vec<Savepoint>,
    tabstops: BTreeSet<usize>,
    /// Combining marks folded into a cell, addressed by `Cell::marks`.
    marks: Vec<Box<str>>,
}

impl Screen {
    pub fn new(cols: usize, rows: usize, history_limit: usize) -> Self {
        let mut screen = Self {
            cols: cols.max(1),
            rows: rows.max(1),
            lines: Vec::new(),
            history: VecDeque::new(),
            history_limit,
            x: 0,
            y: 0,
            margins: None,
            wrap: true,
            origin: false,
            insert: false,
            newline: false,
            saved_columns: None,
            savepoints: Vec::new(),
            tabstops: BTreeSet::new(),
            marks: Vec::new(),
        };
        screen.reset();
        screen
    }

    /// `Screen.reset` plus `HistoryScreen._reset_history`.
    fn reset(&mut self) {
        self.lines = (0..self.rows).map(|_| Row::blank(self.cols)).collect();
        self.history.clear();
        self.margins = None;
        self.wrap = true;
        self.origin = false;
        self.insert = false;
        self.newline = false;
        self.saved_columns = None;
        self.tabstops = (8..self.cols).step_by(8).collect();
        self.x = 0;
        self.y = 0;
        self.ensure_hbounds();
        self.ensure_vbounds(false);
    }

    pub fn cols(&self) -> usize {
        self.cols
    }

    pub fn rows(&self) -> usize {
        self.rows
    }

    pub fn cursor(&self) -> (usize, usize) {
        (self.x, self.y)
    }

    /// `Screen.resize`: content stays anchored top-left; a shrink drops the top rows (not into history),
    /// a grow pads; history rows keep their own width. Margins go back to the screen bounds.
    pub fn resize(&mut self, cols: usize, rows: usize) {
        let cols = cols.max(1);
        let rows = rows.max(1);
        if cols == self.cols && rows == self.rows {
            return;
        }
        if rows < self.rows {
            // `Screen.resize`: drop the top rows through `delete_lines`, cursor parked at home.
            self.save_cursor();
            self.x = 0;
            self.y = 0;
            self.delete_lines(self.rows - rows);
            self.restore_cursor();
        }
        if cols < self.cols {
            for row in &mut self.lines {
                row.cells.truncate(cols);
            }
        }
        if rows > self.lines.len() {
            for _ in self.lines.len()..rows {
                self.lines.push(Row::blank(cols));
            }
        }
        for row in &mut self.lines {
            while row.cells.len() < cols {
                row.cells.push(BLANK);
            }
        }
        self.lines.truncate(rows);
        self.cols = cols;
        self.rows = rows;
        // `set_margins()` at the end of `Screen.resize` only resets the scrolling region; pyte keeps its
        // tab stops across a resize.
        self.margins = None;
    }

    /// `get_clean_screen_text`: `display` (with the stub papered over), each row `rstrip`ped, trailing
    /// blank rows dropped, then the last `max_lines` rows.
    pub fn screen_text(&self, max_lines: usize) -> String {
        let mut lines: Vec<String> = (0..self.rows).map(|y| self.display_row(y)).collect();
        while lines.last().map(|line| line.is_empty()).unwrap_or(false) {
            lines.pop();
        }
        if max_lines > 0 && lines.len() > max_lines {
            lines.drain(..lines.len() - max_lines);
        }
        lines.join("\n")
    }

    /// `get_full_scrollback_text`: newest history rows first, each rendered over the *current* column
    /// count and `rstrip`ped, then the screen rows, capped at `max_lines`.
    pub fn scrollback_text(&self, max_lines: usize) -> String {
        let mut current: Vec<String> = (0..self.lines.len().min(self.rows))
            .map(|y| self.display_row(y))
            .collect();
        while current.last().map(|line| line.is_empty()).unwrap_or(false) {
            current.pop();
        }
        let mut newest: Vec<String> = Vec::new();
        for row in self.history.iter().rev() {
            if max_lines > 0 && newest.len() + current.len() >= max_lines {
                break;
            }
            let rendered = self.raw_row(row);
            let text = rstrip_python(&rendered);
            if !text.is_empty() || !current.is_empty() || !newest.is_empty() {
                newest.push(text.to_string());
            }
        }
        newest.reverse();
        newest.extend(current);
        if max_lines > 0 && newest.len() > max_lines {
            newest.drain(..newest.len() - max_lines);
        }
        newest.join("\n")
    }

    /// `HubTerminalScreen.display` for one row, `rstrip`ped.
    fn display_row(&self, y: usize) -> String {
        let Some(row) = self.lines.get(y) else {
            return String::new();
        };
        let mut out = String::with_capacity(self.cols);
        let mut skip = false;
        for x in 0..self.cols {
            if skip {
                skip = false;
                continue;
            }
            let cell = row.cells.get(x).copied().unwrap_or(BLANK);
            if cell.is_stub() {
                // pyte's safe renderer yields one space for an empty cell, so the stub occupies a column.
                out.push(' ');
                continue;
            }
            self.push_cell_text(&mut out, cell);
            if char_width(cell.ch) == 2 {
                skip = true;
            }
        }
        let end = rstrip_python(&out).len();
        out.truncate(end);
        out
    }

    /// A history row as `get_full_scrollback_text` renders it: every column contributes its cell's text,
    /// so a wide character's stub contributes nothing at all.
    fn raw_row(&self, row: &Row) -> String {
        let mut out = String::with_capacity(self.cols);
        for x in 0..self.cols {
            let cell = row.cells.get(x).copied().unwrap_or(BLANK);
            if cell.is_stub() {
                continue;
            }
            self.push_cell_text(&mut out, cell);
        }
        out
    }

    fn push_cell_text(&self, out: &mut String, cell: Cell) {
        out.push(cell.ch);
        if cell.marks != 0 {
            if let Some(marks) = self.marks.get((cell.marks - 1) as usize) {
                out.push_str(marks);
            }
        }
    }

    fn row_mut(&mut self, y: usize) -> Option<&mut Row> {
        self.lines.get_mut(y)
    }

    fn set_cell(&mut self, x: usize, y: usize, ch: char) {
        if let Some(row) = self.lines.get_mut(y) {
            if let Some(cell) = row.cells.get_mut(x) {
                *cell = Cell { ch, marks: 0 };
            }
        }
    }

    fn fill(&mut self, x: usize, y: usize, end_x: usize) {
        if let Some(row) = self.lines.get_mut(y) {
            for x in x..end_x.min(row.cells.len()) {
                row.cells[x] = BLANK;
            }
        }
    }

    fn blank_row(&self) -> Row {
        Row::blank(self.cols)
    }

    fn ensure_hbounds(&mut self) {
        self.x = self.x.min(self.cols.saturating_sub(1));
    }

    fn ensure_vbounds(&mut self, use_margins: bool) {
        let (top, bottom) = match (self.margins, use_margins || self.origin) {
            (Some(margins), true) => margins,
            _ => (0, self.rows.saturating_sub(1)),
        };
        let bottom = bottom.min(self.rows.saturating_sub(1));
        self.y = self.y.max(top).min(bottom);
    }

    fn scroll_bounds(&self) -> (usize, usize) {
        let (top, bottom) = self.margins.unwrap_or((0, self.rows.saturating_sub(1)));
        (top.min(self.rows.saturating_sub(1)), bottom.min(self.rows.saturating_sub(1)))
    }

    fn carriage_return(&mut self) {
        self.x = 0;
    }

    fn index(&mut self) {
        let (top, bottom) = self.scroll_bounds();
        if self.y == bottom {
            if top < self.lines.len() {
                let removed = self.lines.remove(top);
                if self.history_limit > 0 {
                    self.history.push_back(removed);
                    while self.history.len() > self.history_limit {
                        self.history.pop_front();
                    }
                }
            }
            let blank = self.blank_row();
            if bottom <= self.lines.len() {
                self.lines.insert(bottom, blank);
            }
            self.lines.truncate(self.rows);
        } else {
            self.y = (self.y + 1).min(bottom);
        }
    }

    fn reverse_index(&mut self) {
        let (top, bottom) = self.scroll_bounds();
        if self.y == top {
            self.lines.insert(top, self.blank_row());
            self.lines.truncate(self.rows);
        } else {
            self.y = self.y.saturating_sub(1).max(top);
        }
        let _ = bottom;
    }

    fn linefeed(&mut self) {
        self.index();
        if self.newline {
            self.carriage_return();
        }
    }

    fn tab(&mut self) {
        let mut column = self.cols.saturating_sub(1);
        for stop in &self.tabstops {
            if self.x < *stop {
                column = *stop;
                break;
            }
        }
        self.x = column;
    }

    fn cursor_back(&mut self, count: usize) {
        if self.x == self.cols {
            self.x -= 1;
        }
        self.x = self.x.saturating_sub(count.max(1));
        self.ensure_hbounds();
    }

    fn cursor_forward(&mut self, count: usize) {
        self.x += count.max(1);
        self.ensure_hbounds();
    }

    fn cursor_up(&mut self, count: usize) {
        let (top, _) = self.scroll_bounds();
        self.y = self.y.saturating_sub(count.max(1)).max(top);
    }

    fn cursor_down(&mut self, count: usize) {
        let (_, bottom) = self.scroll_bounds();
        self.y = (self.y + count.max(1)).min(bottom);
    }

    fn cursor_position(&mut self, line: Option<usize>, column: Option<usize>) {
        let column = column.unwrap_or(1).saturating_sub(1);
        let mut line = line.unwrap_or(1).saturating_sub(1);
        if let Some((top, bottom)) = self.margins {
            if self.origin {
                line += top;
                if line < top || line > bottom {
                    return;
                }
            }
        }
        self.x = column;
        self.y = line;
        self.ensure_hbounds();
        self.ensure_vbounds(false);
    }

    fn cursor_to_column(&mut self, column: Option<usize>) {
        self.x = column.unwrap_or(1).saturating_sub(1);
        self.ensure_hbounds();
    }

    fn cursor_to_line(&mut self, line: Option<usize>) {
        self.y = line.unwrap_or(1).saturating_sub(1);
        if self.origin {
            if let Some((top, _)) = self.margins {
                self.y += top;
            }
        }
        self.ensure_vbounds(false);
    }

    fn save_cursor(&mut self) {
        self.savepoints.push(Savepoint {
            x: self.x,
            y: self.y,
            origin: self.origin,
            wrap: self.wrap,
        });
    }

    fn restore_cursor(&mut self) {
        match self.savepoints.pop() {
            Some(savepoint) => {
                // pyte's `restore_cursor` only *sets* the modes the savepoint had on; a mode turned on
                // after the save survives the restore.
                if savepoint.origin {
                    self.set_mode(6, true);
                }
                if savepoint.wrap {
                    self.set_mode(7, true);
                }
                self.x = savepoint.x;
                self.y = savepoint.y;
                self.ensure_hbounds();
                self.ensure_vbounds(true);
            }
            None => {
                self.reset_mode(6, true);
                self.cursor_position(None, None);
            }
        }
    }

    fn insert_lines(&mut self, count: usize) {
        let count = count.max(1);
        let (top, bottom) = self.scroll_bounds();
        if top <= self.y && self.y <= bottom {
            let blank = self.blank_row();
            for _ in 0..count.min(bottom - self.y + 1) {
                self.lines.insert(self.y, blank.clone());
                self.lines.truncate(self.rows);
            }
            self.carriage_return();
        }
    }

    fn delete_lines(&mut self, count: usize) {
        let count = count.max(1);
        let (top, bottom) = self.scroll_bounds();
        if top <= self.y && self.y <= bottom {
            // `buffer[y] = buffer[y + count]` walking up, rows past `bottom - count` become blank; the
            // increasing order is safe because the source lies ahead of the write.
            let last = bottom.min(self.lines.len().saturating_sub(1));
            for y in self.y..=last {
                if y + count <= bottom && y + count < self.lines.len() {
                    let source = self.lines[y + count].cells.clone();
                    self.lines[y].cells = source;
                } else {
                    self.lines[y] = self.blank_row();
                }
            }
            self.carriage_return();
        }
    }

    fn insert_characters(&mut self, count: usize) {
        let count = count.max(1);
        let x = self.x.min(self.cols);
        if x >= self.cols {
            return;
        }
        if let Some(row) = self.row_mut(self.y) {
            let width = row.cells.len();
            let blanks = count.min(width - x);
            for _ in 0..blanks {
                row.cells.insert(x, BLANK);
            }
            row.cells.truncate(width);
        }
    }

    fn delete_characters(&mut self, count: usize) {
        let count = count.max(1);
        let x = self.x.min(self.cols);
        if let Some(row) = self.row_mut(self.y) {
            let width = row.cells.len();
            let end = (x + count).min(width);
            if x < end {
                row.cells.drain(x..end);
                for _ in 0..(end - x) {
                    row.cells.push(BLANK);
                }
            }
        }
    }

    fn erase_characters(&mut self, count: usize) {
        let count = count.max(1);
        let x = self.x.min(self.cols);
        self.fill(x, self.y, x + count);
    }

    fn erase_in_line(&mut self, how: usize) {
        match how {
            0 => self.fill(self.x.min(self.cols), self.y, self.cols),
            1 => self.fill(0, self.y, (self.x + 1).min(self.cols)),
            2 => self.fill(0, self.y, self.cols),
            _ => {}
        }
    }

    fn erase_in_display(&mut self, how: usize) {
        match how {
            0 => {
                for y in self.y + 1..self.rows {
                    self.fill(0, y, self.cols);
                }
                self.erase_in_line(0);
            }
            1 => {
                for y in 0..self.y {
                    self.fill(0, y, self.cols);
                }
                self.erase_in_line(1);
            }
            2 | 3 => {
                for y in 0..self.rows {
                    self.fill(0, y, self.cols);
                }
                if how == 3 {
                    self.history.clear();
                }
            }
            _ => {}
        }
    }

    fn set_margins(&mut self, top: Option<usize>, bottom: Option<usize>) {
        if (top.is_none() || top == Some(0)) && bottom.is_none() {
            self.margins = None;
            return;
        }
        // pyte's `set_margins`: the arguments are 1-based, so each is decremented and clamped into
        // `[0, rows - 1]`; a missing argument keeps the current region's bound (so `ESC[3r` and
        // `ESC[2;r` both count). A region of fewer than two lines is ignored, and changing it homes the
        // cursor through `cursor_position`, which is origin-relative.
        let last = self.rows.saturating_sub(1);
        let (current_top, current_bottom) = self.margins.unwrap_or((0, last));
        let top = top.map_or(current_top, |value| value.saturating_sub(1).min(last));
        let bottom = bottom.map_or(current_bottom, |value| value.saturating_sub(1).min(last));
        if bottom >= top + 1 {
            self.margins = Some((top, bottom));
            self.cursor_position(None, None);
        }
    }

    fn set_mode(&mut self, mode: u16, private: bool) {
        match (private, mode) {
            (true, 7) => self.wrap = true,
            (true, 6) => {
                self.origin = true;
                // pyte: "According to VT520 manual, DECOM should also home the cursor."
                self.cursor_position(None, None);
            }
            (true, 3) => {
                self.saved_columns = Some(self.cols);
                self.resize(132, self.rows);
                self.erase_in_display(2);
                self.cursor_position(None, None);
            }
            (false, 4) => self.insert = true,
            (false, 20) => self.newline = true,
            _ => {}
        }
    }

    fn reset_mode(&mut self, mode: u16, private: bool) {
        match (private, mode) {
            (true, 7) => self.wrap = false,
            (true, 6) => {
                self.origin = false;
                // Clearing DECOM homes the cursor too (`reset_mode` follows `set_mode`'s logic).
                self.cursor_position(None, None);
            }
            (true, 3) => {
                if let Some(columns) = self.saved_columns.take() {
                    self.resize(columns, self.rows);
                }
                self.erase_in_display(2);
                self.cursor_position(None, None);
            }
            (false, 4) => self.insert = false,
            (false, 20) => self.newline = false,
            _ => {}
        }
    }

    /// pyte's `draw` for one character, with the hub's fix: a zero-width character that is not
    /// combining, or an unprintable one, is skipped — it takes no cell, does not trigger a pending wrap
    /// and changes nothing — and the text after it is still drawn (`HubTerminalScreen.draw`).
    fn draw(&mut self, ch: char) {
        let width = char_width(ch);
        if width < 0 || (width == 0 && !is_combining(ch)) {
            return;
        }
        if self.x == self.cols {
            if self.wrap {
                self.carriage_return();
                self.index();
            } else if width > 0 {
                self.x -= width as usize;
            }
        }
        if self.insert && width > 0 {
            self.insert_characters(width as usize);
        }
        match width {
            1 => self.set_cell(self.x, self.y, ch),
            2 => {
                self.set_cell(self.x, self.y, ch);
                if self.x + 1 < self.cols {
                    self.set_cell(self.x + 1, self.y, STUB);
                }
            }
            // Zero width, and combining: the only other case left.
            _ => self.combine(ch),
        }
        if width > 0 {
            self.x = (self.x + width as usize).min(self.cols);
        }
    }

    /// pyte folds a combining mark into the cell to the left, or the previous row's last cell.
    ///
    /// Only the marks are stored: the cell's own character is printed once by `push_cell_text`, and pyte
    /// holds the base and the marks in one string. pyte additionally NFC-composes that string, which
    /// needs `unicodedata.normalize`; the marks are kept unnormalised (`screen_exceptions.jsonl`).
    fn combine(&mut self, ch: char) {
        let (x, y) = if self.x > 0 {
            (self.x - 1, self.y)
        } else if self.y > 0 {
            (self.cols.saturating_sub(1), self.y - 1)
        } else {
            return;
        };
        let Some(row) = self.lines.get_mut(y) else {
            return;
        };
        let Some(cell) = row.cells.get_mut(x) else {
            return;
        };
        let mut text = String::new();
        if cell.marks != 0 {
            if let Some(marks) = self.marks.get((cell.marks - 1) as usize) {
                text.push_str(marks);
            }
        }
        text.push(ch);
        self.marks.push(text.into_boxed_str());
        cell.marks = self.marks.len() as u32;
    }
}

/// Python's `str.rstrip()`: whitespace as `str.isspace()` defines it, which includes the C0 separators
/// `\x1c..\x1f` that `char::is_whitespace` does not.
fn rstrip_python(text: &str) -> &str {
    text.trim_end_matches(python_isspace)
}

fn python_isspace(ch: char) -> bool {
    matches!(ch, '\u{1c}'..='\u{20}') || ch.is_whitespace()
}

fn param(params: &Params, index: usize) -> Option<usize> {
    params
        .iter()
        .nth(index)
        .and_then(|values| values.first().copied())
        .map(|value| value as usize)
}

/// pyte's `csi_dispatch` argument list: a parameter written empty (`,4` in `ESC[;4r`, `2;` in `ESC[2;r`)
/// is a 0 to pyte, while a parameter that was not written at all is absent. `param` collapses those two,
/// which is harmless for every handler but `set_margins` (`ESC[2;r` means top 2, bottom 0 to pyte).
fn param_written(params: &Params, index: usize) -> Option<usize> {
    params
        .iter()
        .nth(index)
        .map(|values| values.first().copied().unwrap_or(0) as usize)
}

fn private_marker(intermediates: &[u8]) -> bool {
    intermediates.contains(&b'?')
}

impl Perform for Screen {
    fn print(&mut self, c: char) {
        // pyte's `Stream` never draws NUL or DEL (`NUL_OR_DEL`); vte hands DEL to `print`. Neither takes
        // a cell nor changes the cursor.
        if c == '\0' || c == '\x7f' {
            return;
        }
        self.draw(c);
    }

    fn execute(&mut self, byte: u8) {
        match byte {
            0x07 => {}
            0x08 => self.cursor_back(1),
            0x09 => self.tab(),
            0x0A | 0x0B | 0x0C => self.linefeed(),
            0x0D => self.carriage_return(),
            // SO/SI only select charsets, which pyte ignores in UTF-8 mode; every other C0 control is a
            // no-op in its `basic` table (`Stream` dispatches unknown ones to `debug`).
            _ => {}
        }
    }

    fn csi_dispatch(&mut self, params: &Params, intermediates: &[u8], _ignore: bool, action: char) {
        let private = private_marker(intermediates);
        let first = param(params, 0);
        let second = param(params, 1);
        let count = |value: Option<usize>| value.unwrap_or(1).max(1);
        match action {
            '@' => self.insert_characters(count(first)),
            'A' => self.cursor_up(count(first)),
            'B' => self.cursor_down(count(first)),
            'C' => self.cursor_forward(count(first)),
            'D' => self.cursor_back(count(first)),
            'E' => {
                self.cursor_down(count(first));
                self.carriage_return();
            }
            'F' => {
                self.cursor_up(count(first));
                self.carriage_return();
            }
            'G' => self.cursor_to_column(first),
            'H' | 'f' => self.cursor_position(first, second),
            'J' => self.erase_in_display(first.unwrap_or(0)),
            'K' => self.erase_in_line(first.unwrap_or(0)),
            'L' => self.insert_lines(count(first)),
            'M' => self.delete_lines(count(first)),
            'P' => self.delete_characters(count(first)),
            'X' => self.erase_characters(count(first)),
            'a' => self.cursor_forward(count(first)),
            'd' => self.cursor_to_line(first),
            'e' => self.cursor_down(count(first)),
            'g' => match first.unwrap_or(0) {
                0 => {
                    self.tabstops.remove(&self.x);
                }
                3 => self.tabstops.clear(),
                _ => {}
            },
            'h' | 'l' => {
                let values: Vec<u16> = params
                    .iter()
                    .filter_map(|values| values.first().copied())
                    .collect();
                let values = if values.is_empty() { vec![0] } else { values };
                for mode in values {
                    if action == 'h' {
                        self.set_mode(mode, private);
                    } else {
                        self.reset_mode(mode, private);
                    }
                }
            }
            'm' => {}
            'r' => self.set_margins(param_written(params, 0), param_written(params, 1)),
            '\'' => self.cursor_to_column(first),
            // pyte has no handler for these (`Stream` dispatches them to `debug`): DA, DSR, window ops
            // and the rest must draw nothing and change nothing.
            _ => {}
        }
    }

    fn esc_dispatch(&mut self, intermediates: &[u8], _ignore: bool, byte: u8) {
        if !intermediates.is_empty() {
            // `ESC ( B`, `ESC # 8`, `ESC % G`: pyte ignores the charset selections and only DECALN draws.
            if intermediates == [b'#'] && byte == b'8' {
                for y in 0..self.rows {
                    for x in 0..self.cols {
                        self.set_cell(x, y, 'E');
                    }
                }
            }
            return;
        }
        match byte {
            b'c' => self.reset(),
            b'D' => self.index(),
            b'E' => self.linefeed(),
            b'H' => {
                self.tabstops.insert(self.x);
            }
            b'M' => self.reverse_index(),
            b'7' => self.save_cursor(),
            b'8' => self.restore_cursor(),
            _ => {}
        }
    }

    fn osc_dispatch(&mut self, _params: &[&[u8]], _bell_terminated: bool) {
        // Titles and the like: this model draws nothing and keeps no state for them.
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use vte::Parser;

    fn screen(cols: usize, rows: usize, history: usize, text: &str) -> Screen {
        let mut parser = Parser::new();
        let mut screen = Screen::new(cols, rows, history);
        parser.advance(&mut screen, text.as_bytes());
        screen
    }

    // Expected values below are pyte's, computed through `HubTerminalScreen`/`HubTerminalStream` and the
    // same renderers the hub uses (`get_clean_screen_text`, `get_full_scrollback_text`).

    #[test]
    fn text_wraps_at_the_column_count() {
        let screen = screen(5, 2, 10, "abcdefgh");
        assert_eq!(screen.screen_text(0), "abcde\nfgh");
        assert_eq!(screen.scrollback_text(10), "abcde\nfgh");
        assert_eq!(screen.cursor(), (3, 1));
    }

    #[test]
    fn scrolled_lines_go_to_history_and_render_above_the_screen() {
        let screen = screen(3, 2, 10, "abc\r\ndef\r\nghi\r\n");
        assert_eq!(screen.screen_text(0), "ghi");
        assert_eq!(screen.scrollback_text(10), "abc\ndef\nghi");
        assert_eq!(screen.cursor(), (0, 1));
    }

    #[test]
    fn a_wide_cell_renders_once_on_screen_and_its_stub_not_at_all_in_history() {
        let screen = screen(4, 1, 10, "中\r\nab");
        assert_eq!(screen.screen_text(0), "ab");
        // The history row keeps the wide character and drops the stub cell, as the scrollback renderer
        // indexes every column and appends `""` for it.
        assert_eq!(screen.scrollback_text(10), "中\nab");
    }

    #[test]
    fn erase_and_cursor_moves_follow_pyte() {
        let cleared = screen(6, 2, 10, "abc\r\n\x1b[2J\x1b[H");
        assert_eq!(cleared.screen_text(0), "");
        assert_eq!(cleared.cursor(), (0, 0));
        let screen = screen(6, 2, 10, "abcdef\x1b[1;3Hxy");
        assert_eq!(screen.screen_text(0), "abxyef");
        assert_eq!(screen.cursor(), (4, 0));
    }

    #[test]
    fn resize_keeps_content_anchored_without_reflow() {
        let mut screen = screen(10, 2, 10, "aaaaaaaaaa\r\nbbbbbbbbbb");
        screen.resize(4, 3);
        assert_eq!(screen.screen_text(0), "aaaa\nbbbb");
        assert_eq!(screen.rows(), 3);
        assert_eq!(screen.cols(), 4);
    }

    #[test]
    fn history_is_bounded_by_the_requested_rows() {
        let text = (0..5).map(|index| format!("r{index}\r\n")).collect::<String>();
        let screen = screen(3, 1, 2, &text);
        assert_eq!(screen.scrollback_text(10), "r3\nr4");
    }

    #[test]
    fn an_alternate_screen_switch_changes_nothing() {
        // pyte has one buffer: `?1049` only sets a mode bit, so the text after it lands where the cursor
        // already is.
        let screen = screen(4, 1, 10, "ab\x1b[?1049hcd\x1b[?1049l");
        assert_eq!(screen.screen_text(0), "abcd");
    }
}

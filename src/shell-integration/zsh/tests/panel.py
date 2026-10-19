#!/usr/bin/env python3
"""Exercise the production GtkTextView/socket component with real local zsh.

Compile panel_harness.c with editor.c and GTK4, then run under Xvfb:
GHOSTTY_PANEL_HARNESS=/tmp/ghostty-panel-harness python3 .../tests/panel.py
The harness hosts the production panel, not Ghostty's terminal renderer.
"""
import os
from pathlib import Path
import signal
import sys
import subprocess
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
from command_editor import Shell


@unittest.skipUnless(os.environ.get("DISPLAY") and os.environ.get("GHOSTTY_PANEL_HARNESS"),
                     "X11 display and GHOSTTY_PANEL_HARNESS required")
class PanelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.log = (self.path / "gtk.log").open("w+")
        self.addCleanup(self.log.close)
        self.host = subprocess.Popen([os.environ["GHOSTTY_PANEL_HARNESS"], str(self.path)],
                                     stdout=subprocess.PIPE, stderr=self.log, text=True)
        self.addCleanup(self.close_host)
        path = self.host.stdout.readline().strip()
        self.assertTrue(path, "GTK harness failed to create a channel")
        self.assertEqual(Path(path).parent.stat().st_mode & 0o777, 0o700)
        self.shell = Shell(external_socket=path)
        self.addCleanup(self.shell.close)
        self.window = self.xdo("search", "--sync", "--onlyvisible", "--name", "^Ghostty Command Editor Test$").splitlines()[-1]
        self.xdo("windowfocus", "--sync", self.window)
        self.wait_text("")

    def close_host(self):
        if self.host.poll() is None:
            self.host.terminate()
        self.host.wait(timeout=5)
        self.host.stdout.close()

    def xdo(self, *args):
        return subprocess.check_output(["xdotool", *args], text=True, timeout=5).strip()

    def key(self, *keys):
        self.xdo("key", "--clearmodifiers", *keys)

    def type(self, text):
        self.xdo("type", "--clearmodifiers", "--delay", "1", "--", text)

    def paste(self, text, replace=True):
        subprocess.run(["/usr/bin/xclip", "-selection", "clipboard"], input=text.encode(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        if replace:
            self.key("ctrl+a")
        self.key("ctrl+v")

    def clipboard(self):
        return subprocess.check_output(["/usr/bin/xclip", "-selection", "clipboard", "-o"], timeout=5).decode()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except (FileNotFoundError, ValueError):
                pass
            time.sleep(.02)
        self.log.flush()
        self.fail(f"GTK state did not converge; buffer={self.text()!r}; state={self.state()}; "
                  f"log={(self.path / 'gtk.log').read_text()}")

    def text(self):
        return (self.path / "buffer").read_text()

    def state(self):
        return tuple(map(int, (self.path / "state").read_text().split()))

    def wait_text(self, text):
        self.wait_for(lambda: self.text() == text and self.state()[:2] == (1, 1))

    def test_keyboard_selection_system_clipboard_and_replace(self):
        self.paste("abc")
        self.wait_text("abc")
        self.key("ctrl+Home", "Right", "shift+Right", "shift+Right")
        self.wait_for(lambda: self.state()[2:] == (3, 1))
        self.key("ctrl+c")
        self.assertEqual(self.clipboard(), "bc")
        self.key("ctrl+x")
        self.wait_text("a")
        self.key("ctrl+v")
        self.wait_text("abc")
        self.key("shift+Left", "shift+Left")
        self.type("Z")
        self.wait_text("aZ")
        self.key("ctrl+a", "ctrl+shift+c")
        self.assertEqual(self.clipboard(), "aZ")

    def test_click_and_drag_edit_command_selection(self):
        self.paste("abcdef")
        self.wait_text("abcdef")
        self.wait_for(lambda: len((self.path / "positions").read_text().splitlines()) == 7)
        positions = [line.split() for line in (self.path / "positions").read_text().splitlines()]
        self.xdo("mousemove", "--window", self.window, *positions[2], "click", "1")
        self.type("X")
        self.wait_text("abXcdef")
        self.paste("abcdef")
        self.wait_text("abcdef")
        time.sleep(.35)  # Separate the drag press from GTK's double-click interval.
        positions = [line.split() for line in (self.path / "positions").read_text().splitlines()]
        self.xdo("mousemove", "--window", self.window, *positions[1], "mousedown", "1",
                  "mousemove", "--window", self.window, *positions[4], "mouseup", "1")
        self.wait_for(lambda: set(self.state()[2:]) == {1, 4})
        self.type("Z")
        self.wait_text("aZef")

    def test_multiline_paste_and_enter_execute_once(self):
        command = "print -r -- '雪\n$HOME ; second  ' >> \"$ZDOTDIR/result\""
        self.paste(command)
        self.wait_text(command)
        self.assertFalse((self.shell.path / "result").exists())
        self.key("Return", "Return")
        self.shell.wait(b"READY> ")
        self.wait_text("")
        self.assertEqual((self.shell.path / "result").read_text(), "雪\n$HOME ; second  \n")

    def test_shift_enter_and_resize_preserve_unicode_draft(self):
        text = "雪🙂e\u0301" * 30
        self.paste(text)
        self.wait_text(text)
        self.key("ctrl+End", "shift+Return")
        self.wait_text(text + "\n")
        self.xdo("windowsize", self.window, "400", "240")
        self.wait_text(text + "\n")
        self.key("ctrl+a", "ctrl+c")
        self.assertEqual(self.clipboard(), text + "\n")

    def test_native_completion_and_history_return_to_panel(self):
        command = 'cd "$ZDOTDIR"'
        self.paste(command)
        self.key("Return")
        self.shell.wait(b"READY> ")
        self.wait_text("")
        self.key("Up")
        self.wait_text(command)
        (self.shell.path / "unique-completion-file").touch()
        prefix = "cat " + str(self.shell.path / "unique-comp")
        self.paste(prefix)
        self.wait_text(prefix)
        self.key("Tab")
        self.wait_for(lambda: "unique-completion-file" in self.text() and self.state()[:2] == (1, 1))

    def test_ctrl_c_cancels_and_escape_hands_draft_to_shell(self):
        command = 'print should-not-run > "$ZDOTDIR/result"'
        self.paste(command)
        self.wait_text(command)
        self.key("ctrl+End", "ctrl+c")
        self.shell.wait(b"READY> ")
        self.wait_text("")
        self.assertFalse((self.shell.path / "result").exists())
        self.paste("abc雪")
        self.wait_text("abc雪")
        self.key("Escape")
        self.wait_for(lambda: self.state()[0] == 0)
        self.assertEqual(self.shell.snapshot()[0], "abc雪")
        self.shell.send(b"\x18\x07")
        self.wait_text("abc雪")

    def test_shell_disconnect_retains_read_only_recovery_draft(self):
        self.paste("unsent 雪\ncommand")
        self.wait_text("unsent 雪\ncommand")
        os.kill(self.shell.pid, signal.SIGKILL)
        self.wait_for(lambda: self.state()[:2] == (1, 0))
        self.assertEqual(self.text(), "unsent 雪\ncommand")
        # The draft remains selectable and copyable, without a live accept path.
        self.xdo("mousemove", "--window", self.window, "30", "290", "click", "1")
        self.key("ctrl+a", "ctrl+c")
        self.assertEqual(self.clipboard(), "unsent 雪\ncommand")

    def test_ctrl_d_deletes_or_exits_empty_input(self):
        self.paste("abc")
        self.wait_text("abc")
        self.key("ctrl+Home", "Right", "ctrl+d")
        self.wait_text("ac")
        self.key("ctrl+a", "BackSpace")
        self.wait_text("")
        self.key("ctrl+d")
        self.wait_for(lambda: self.state()[0] == 0)
        deadline = time.monotonic() + 5
        import select
        while time.monotonic() < deadline:
            if select.select([self.shell.fd], [], [], .1)[0]:
                try:
                    if not os.read(self.shell.fd, 65536):
                        return
                except OSError:
                    return
        self.fail("Empty input Ctrl+D did not close zsh")

    def test_readonly_preserves_copy_and_blocks_editing_and_acceptance(self):
        self.paste("read-only draft")
        self.wait_text("read-only draft")
        (self.path / "readonly").touch()
        self.wait_for(lambda: self.state()[:2] == (1, 0))
        self.key("ctrl+a", "ctrl+c", "Return")
        self.assertEqual(self.clipboard(), "read-only draft")
        self.assertEqual(self.text(), "read-only draft")
        self.assertEqual(self.state()[:2], (1, 0))
        (self.path / "readonly").unlink()
        self.wait_text("read-only draft")

    def test_terminal_keystrokes_forward_to_active_editor(self):
        self.paste("abc")
        self.wait_text("abc")
        self.xdo("mousemove", "--window", self.window, "40", "40", "click", "1")
        self.type("X")
        self.wait_text("abcX")


if __name__ == "__main__":
    unittest.main()

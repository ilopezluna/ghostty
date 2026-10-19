#!/usr/bin/env python3
"""Integration tests against real ZLE/PTY, without a display or Python packages.

Run: python3 src/shell-integration/zsh/tests/command_editor.py
Requires Linux, zsh 5.1+, and a UTF-8 locale.
"""

import fcntl
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import tempfile
import termios
import time
import unittest

INTEGRATION = Path(__file__).resolve().parents[1] / "ghostty-integration"
SNAPSHOT = b"\x1b]999;snapshot\x07"
LEFT = b"\x1b[D"
SELECT_LEFT = b"\x1b[1;2D"
PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"


class Shell:
    def __init__(self, features="command-editor", extra="", ssh=False):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)
        self.output = bytearray()
        self.pending = bytearray()
        (self.path / ".zshrc").write_text(
            "bindkey -e\nPS1='READY> '\nPS2='MORE> '\n"
            # Deterministic completion, but retain the standard ZLE widgets.
            "autoload -Uz compinit; compinit -D\n"
            + extra
            + "\nsource "
            + str(INTEGRATION)
            + """
_test_snapshot() {
    print -rn -- "$BUFFER" > "$ZDOTDIR/buffer"
    print -r -- "$CURSOR $MARK $REGION_ACTIVE" > "$ZDOTDIR/state"
    print -r -- "${(k)widgets}" > "$ZDOTDIR/widgets"
    print -rn -- $'\\e]999;snapshot\\a'
}
zle -N _test_snapshot
bindkey '^X^T' _test_snapshot
"""
        )
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            env = os.environ.copy()
            for name in ("SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY", "GHOSTTY_ZSH_ZDOTDIR"):
                env.pop(name, None)
            env.update(
                ZDOTDIR=str(self.path),
                TERM="xterm",
                LC_ALL="C.UTF-8",
                GHOSTTY_SHELL_FEATURES=features,
            )
            if ssh:
                env["SSH_CONNECTION"] = "127.0.0.1 1234 127.0.0.1 22"
            os.execvpe("zsh", ["zsh", "-d"], env)
        self.wait(b"READY> ")
        self.snapshot()  # Synchronize with ZLE, not just prompt output.

    def send(self, data):
        os.write(self.fd, data.encode() if isinstance(data, str) else data)

    def wait(self, marker):
        deadline = time.monotonic() + 5
        while marker not in self.pending:
            if time.monotonic() >= deadline:
                raise AssertionError(f"Timed out waiting for {marker!r}: {bytes(self.pending)!r}")
            if select.select([self.fd], [], [], 0.1)[0]:
                chunk = os.read(self.fd, 65536)
                self.pending.extend(chunk)
                self.output.extend(chunk)
        end = self.pending.index(marker) + len(marker)
        data = bytes(self.pending[:end])
        del self.pending[:end]
        if marker in (b"READY> ", b"MORE> "):
            # A printed prompt precedes ZLE's tty setup. Wait until bracketed
            # paste is enabled before injecting keys, otherwise tcsetattr can
            # flush input arriving between the prompt and ZLE activation.
            data += self.wait(b"\x1b[?2004h")
        return data

    def snapshot(self, keys=b""):
        self.send(keys + b"\x18\x14")
        self.wait(SNAPSHOT)
        buffer = (self.path / "buffer").read_text()
        cursor, mark, active = map(int, (self.path / "state").read_text().split())
        return buffer, cursor, mark, active

    def paste(self, text):
        self.send(PASTE_START + text.encode() + PASTE_END)

    def close(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(self.pid, 0)
        os.close(self.fd)
        self.directory.cleanup()


@unittest.skipUnless(shutil.which("zsh") and os.uname().sysname == "Linux", "Linux zsh required")
class CommandEditorTests(unittest.TestCase):
    def shell(self, **kwargs):
        shell = Shell(**kwargs)
        self.addCleanup(shell.close)
        return shell

    def test_disabled_and_ssh_keep_normal_widgets(self):
        for kwargs in ({"features": ""}, {"ssh": True}, {"extra": "bindkey -v\n"}):
            with self.subTest(**kwargs):
                shell = self.shell(**kwargs)
                widgets = (shell.path / "widgets").read_text()
                self.assertNotIn("_ghostty_editor_select_backward-char", widgets)

    def test_backward_and_forward_selection_replace(self):
        shell = self.shell()
        self.assertEqual(shell.snapshot(b"abc" + SELECT_LEFT * 2), ("abc", 1, 3, 1))
        self.assertEqual(shell.snapshot(b"Z")[:2], ("aZ", 2))
        self.assertEqual(shell.snapshot(LEFT * 2 + b"\x1b[1;2C")[:4], ("aZ", 1, 0, 1))
        self.assertEqual(shell.snapshot(b"Q")[:2], ("QZ", 1))

    def test_selection_delete_does_not_delete_an_extra_character(self):
        for delete in (b"\x7f", b"\x1b[3~"):
            with self.subTest(delete=delete):
                shell = self.shell()
                self.assertEqual(shell.snapshot(b"abc" + SELECT_LEFT * 2 + delete)[:2], ("a", 1))

    def test_copy_cut_yank_and_replace(self):
        shell = self.shell()
        # Alt+w copies into the ZLE kill ring. Ctrl+w cuts, Ctrl+y yanks.
        self.assertEqual(shell.snapshot(b"abc" + SELECT_LEFT * 2 + b"\x1bw")[:2], ("abc", 1))
        self.assertEqual(shell.snapshot(b"\x05\x19")[:2], ("abcbc", 5))
        self.assertEqual(shell.snapshot(SELECT_LEFT * 2 + b"\x17")[:2], ("abc", 3))
        self.assertEqual(shell.snapshot(SELECT_LEFT + b"\x19")[:2], ("abbc", 4))

    def test_multiline_paste_is_literal_and_executes_once(self):
        shell = self.shell()
        command = "print -r -- 'one\n雪 $HOME ; two' >> \"$ZDOTDIR/result\""
        shell.paste(command)
        self.assertEqual(shell.snapshot()[0], command)
        self.assertFalse((shell.path / "result").exists())
        shell.send(b"\r")
        shell.wait(b"READY> ")
        self.assertEqual(shell.snapshot()[0], "")
        self.assertEqual((shell.path / "result").read_text(), "one\n雪 $HOME ; two\n")
        # Another prompt/snapshot must not resubmit the accepted buffer.
        shell.snapshot()
        self.assertEqual((shell.path / "result").read_text(), "one\n雪 $HOME ; two\n")

    def test_multiline_paste_replaces_selection(self):
        shell = self.shell()
        shell.snapshot(b"abc" + SELECT_LEFT * 2)
        shell.paste("x\ny\n")
        self.assertEqual(shell.snapshot()[:2], ("ax\ny\n", 5))

    def test_newline_replaces_selection_without_accepting(self):
        for newline in (b"\x1b[27;2;13~", b"\x1b\r", b"\x1b[13;2u"):
            with self.subTest(newline=newline):
                shell = self.shell()
                self.assertEqual(shell.snapshot(b"abc" + SELECT_LEFT * 2 + newline)[:2], ("a\n", 2))

    def test_unicode_wrap_and_resize_preserve_buffer(self):
        shell = self.shell()
        text = "a雪🙂e\u0301" * 30
        shell.paste(text)
        self.assertEqual(shell.snapshot()[0], text)
        fcntl.ioctl(shell.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 20, 0, 0))
        os.kill(shell.pid, signal.SIGWINCH)
        self.assertEqual(shell.snapshot()[0], text)
        # ZLE character offsets, independent of double-width cells/wrapping.
        self.assertEqual(shell.snapshot(SELECT_LEFT * 2 + b"Z")[0], text[:-2] + "Z")

    def test_application_mode_home_end(self):
        shell = self.shell()
        self.assertEqual(shell.snapshot(b"abc\x1bOH")[:2], ("abc", 0))
        self.assertEqual(shell.snapshot(b"\x1bOF")[:2], ("abc", 3))

    def test_word_and_buffer_navigation(self):
        shell = self.shell()
        self.assertEqual(shell.snapshot(b"one two\x1b[1;6D")[:4], ("one two", 4, 7, 1))
        self.assertEqual(shell.snapshot(b"X")[:2], ("one X", 5))
        self.assertEqual(shell.snapshot(b"\x1b[1;5H")[:2], ("one X", 0))
        self.assertEqual(shell.snapshot(b"\x1b[1;6FQ")[:2], ("Q", 1))

    def test_completion_and_history_remain_zle_widgets(self):
        shell = self.shell()
        (shell.path / "unique-completion-file").touch()
        shell.send('cd "$ZDOTDIR"\r')
        shell.wait(b"READY> ")
        shell.snapshot()
        self.assertIn("unique-completion-file", shell.snapshot(b"cat unique-comp\t")[0])
        shell.send(b"\x03")
        shell.wait(b"READY> ")
        self.assertIn("cd ", shell.snapshot(b"\x1b[A")[0])

    def test_cancel_incomplete_quote_and_ctrl_d(self):
        shell = self.shell()
        shell.send(b"print -r -- 'open\r")
        shell.wait(b"MORE> ")
        shell.send(b"\x03")
        shell.wait(b"READY> ")
        self.assertEqual(shell.snapshot()[0], "")
        # Ctrl+D keeps normal delete-char-or-list behavior on a nonempty line.
        self.assertEqual(shell.snapshot(b"abc" + LEFT + b"\x04")[:2], ("ab", 2))
        shell.send(b"\x03")
        shell.wait(b"READY> ")
        shell.snapshot()
        shell.send(b"\x04")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if select.select([shell.fd], [], [], 0.1)[0] and not os.read(shell.fd, 65536):
                    return
            except OSError:
                return  # PTY slave closed on EOF.
        self.fail("Ctrl+D on empty input did not close the shell")

    def test_open_quote_can_be_completed_and_runs_once(self):
        shell = self.shell()
        shell.send(b"print -r -- 'first\r")
        shell.wait(b"MORE> ")
        shell.send("second' >> \"$ZDOTDIR/result\"\r")
        shell.wait(b"READY> ")
        self.assertEqual(shell.snapshot()[0], "")
        self.assertEqual((shell.path / "result").read_text(), "first\nsecond\n")

    def test_user_insertion_widget_keeps_its_widget_name(self):
        shell = self.shell(extra="""
_user_insert() {
    print -r -- "$WIDGET" >> "$ZDOTDIR/widget-calls"
    zle .self-insert
}
zle -N self-insert _user_insert
""")
        self.assertEqual(shell.snapshot(b"abc" + SELECT_LEFT + b"Z")[0], "abZ")
        self.assertEqual((shell.path / "widget-calls").read_text().splitlines(), ["self-insert"] * 4)

    def test_external_process_receives_raw_input_then_returns_to_prompt(self):
        shell = self.shell()
        # Models an interactive application that changes tty modes inside ZLE's
        # shell child. No mouse/keyboard protocol changes should leak into it.
        command = "python3 -c 'import os,tty; tty.setraw(0); os.write(1,b\"APP_READY\"); d=os.read(0,8); open(os.environ[\"ZDOTDIR\"]+\"/raw\",\"wb\").write(d)'"
        shell.paste(command)
        shell.snapshot()
        shell.send(b"\r")
        shell.wait(b"APP_READY")
        shell.send(SELECT_LEFT)
        shell.wait(b"READY> ")
        self.assertEqual((shell.path / "raw").read_bytes(), SELECT_LEFT)
        self.assertEqual(shell.snapshot(b"abc" + SELECT_LEFT + b"Q")[0], "abQ")


if __name__ == "__main__":
    unittest.main()

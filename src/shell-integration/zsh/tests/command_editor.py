#!/usr/bin/env python3
"""Real ZLE/PTY tests of the native panel's ownership-transfer protocol.

The peer here exercises transport/state behavior; GTK editing is tested by
panel.py using the actual native component under Xvfb.
"""
import fcntl
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import socket
import struct
import tempfile
import termios
import time
import unittest

INTEGRATION = Path(__file__).resolve().parents[1] / "ghostty-integration"


class Shell:
    def __init__(self, enabled=True, ssh=False, extra="", channel=True, external_socket=None):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)
        self.pending = bytearray()
        self.output = bytearray()
        self.connection = None
        self.native_ready = not enabled or ssh or not channel or "bindkey -v" in extra or "beep" in extra
        self.server = socket.socket(socket.AF_UNIX)
        if not external_socket:
            self.server.bind(str(self.path / "zle.sock"))
            self.server.listen()
        self.server.settimeout(5)
        (self.path / ".zshrc").write_text(
            "bindkey -e\nPS1='READY> '\nPS2='MORE> '\n"
            "autoload -Uz compinit; compinit -D\n" + extra + "\nsource " + str(INTEGRATION) + """
_test_snapshot() {
    print -rn -- "$BUFFER" > "$ZDOTDIR/buffer"
    print -r -- "$CURSOR $MARK $REGION_ACTIVE" > "$ZDOTDIR/state"
    print -r -- "${(k)widgets}" > "$ZDOTDIR/widgets"
    print -rn -- $'\\e]999;snapshot\\a'
}
zle -N _test_snapshot
bindkey '^X^T' _test_snapshot
""")
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            env = os.environ.copy()
            for name in ("SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY", "GHOSTTY_COMMAND_EDITOR_SOCKET"):
                env.pop(name, None)
            env.update(ZDOTDIR=str(self.path), TERM="xterm", LC_ALL="C.UTF-8",
                       GHOSTTY_SHELL_FEATURES="command-editor" if enabled else "")
            if ssh:
                env["SSH_CONNECTION"] = "127.0.0.1 1234 127.0.0.1 22"
            if channel:
                env["GHOSTTY_COMMAND_EDITOR_SOCKET"] = external_socket or str(self.path / "zle.sock")
            os.execvpe("zsh", ["zsh", "-d"], env)
        self.wait(b"READY> ")

    def send(self, data):
        os.write(self.fd, data.encode() if isinstance(data, str) else data)

    def wait(self, marker):
        deadline = time.monotonic() + 5
        while marker not in self.pending:
            if time.monotonic() > deadline:
                raise AssertionError(f"Missing {marker!r}: {bytes(self.pending)!r}")
            if select.select([self.fd], [], [], .1)[0]:
                chunk = os.read(self.fd, 65536)
                self.pending.extend(chunk)
                self.output.extend(chunk)
        end = self.pending.index(marker) + len(marker)
        result = bytes(self.pending[:end])
        del self.pending[:end]
        if self.native_ready and marker in (b"READY> ", b"MORE> "):
            result += self.wait(b"\x1b[?2004h")
        return result

    def open(self):
        self.connection, _ = self.server.accept()
        self.connection.settimeout(5)
        self.stream = self.connection.makefile("rb", buffering=0)
        return self.frame()

    def frame(self):
        header = self.stream.readline(97)
        fields = header.decode().split()
        if len(fields) != 5:
            raise AssertionError(f"Invalid frame {header!r}; terminal output={bytes(self.output)!r}")
        version, op, revision, cursor, size = fields
        assert version == "GCE1"
        data = bytearray()
        while len(data) < int(size):
            chunk = self.stream.read(int(size) - len(data))
            if not chunk:
                raise AssertionError("Truncated frame")
            data.extend(chunk)
        return op, int(revision), int(cursor), data.decode()

    def request(self, op, text="", cursor=None, revision=1):
        body = text.encode()
        if cursor is None:
            cursor = len(text)
        self.connection.sendall(f"GCE1 {op} {revision} {cursor} {len(body)}\n".encode() + body)
        self.assert_done()
        self.release()

    def assert_done(self):
        if self.frame() != ("done", 1, 0, ""):
            raise AssertionError("Transfer was not acknowledged")

    def release(self):
        self.stream.close()
        self.connection.close()
        self.connection = None

    def snapshot(self, keys=b""):
        self.send(keys + b"\x18\x14")
        self.wait(b"\x1b]999;snapshot\x07")
        return (self.path / "buffer").read_text(), tuple(map(int, (self.path / "state").read_text().split()))

    def close(self):
        if self.connection:
            self.release()
        self.server.close()
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

    def test_local_channel_opens_with_authoritative_snapshot(self):
        shell = self.shell()
        self.assertEqual(shell.open(), ("state", 1, 0, ""))
        shell.request("terminal", "雪🙂e\u0301\n  ", 2)
        self.assertEqual(shell.snapshot()[0:2], ("雪🙂e\u0301\n  ", (2, 0, 0)))
        shell.send(b"\x18\x07")
        self.assertEqual(shell.open(), ("state", 1, 2, "雪🙂e\u0301\n  "))

    def test_disabled_ssh_vi_and_missing_channel_fall_back(self):
        for kwargs in ({"enabled": False}, {"ssh": True}, {"extra": "bindkey -v\n"}, {"extra": "bindkey '^X^G' beep\n"}, {"channel": False}):
            with self.subTest(**kwargs):
                shell = self.shell(**kwargs)
                shell.snapshot()
                self.assertNotIn("_ghostty_panel", (shell.path / "widgets").read_text())

    def test_accept_multiline_exactly_once_and_never_before_accept(self):
        shell = self.shell()
        shell.open()
        command = "print -r -- 'first\n雪 $HOME ; second  ' >> \"$ZDOTDIR/result\""
        self.assertFalse((shell.path / "result").exists())
        shell.request("accept", command)
        shell.wait(b"READY> ")
        self.assertEqual(shell.open()[3], "")
        self.assertEqual((shell.path / "result").read_text(), "first\n雪 $HOME ; second  \n")

    def test_accepted_command_is_recorded_in_terminal_output(self):
        shell = self.shell()
        shell.open()
        shell.request("accept", "print output-check")
        shell.wait(b"READY> ")
        self.assertIn(b"print output-check", shell.output)
        self.assertEqual(shell.open()[3], "")

    def test_fragmented_frame_preserves_unicode_and_newlines(self):
        shell = self.shell()
        shell.open()
        text = "a雪🙂\n ".encode()
        wire = f"GCE1 terminal 1 3 {len(text)}\n".encode() + text
        for byte in wire:
            shell.connection.sendall(bytes([byte]))
        shell.assert_done()
        shell.release()
        self.assertEqual(shell.snapshot()[0], text.decode())

    def test_bad_revision_and_oversize_leave_zle_unchanged(self):
        for header in (b"GCE1 accept 2 0 0\n", b"GCE1 accept 1 0 1048577\n", b"GCE1 unknown 1 0 0\n"):
            with self.subTest(header=header):
                shell = self.shell()
                shell.open()
                shell.request("terminal", "original draft")
                shell.send(b"\x18\x07")
                shell.open()
                shell.connection.sendall(header)
                self.assertEqual(shell.connection.recv(1), b"")
                shell.release()
                self.assertEqual(shell.snapshot()[0], "original draft")

    def test_disconnection_returns_to_native_zle_without_accepting(self):
        shell = self.shell()
        shell.open()
        shell.release()
        self.assertEqual(shell.snapshot(b"abc")[0], "abc")
        self.assertFalse((shell.path / "result").exists())

    def test_cancel_opens_a_fresh_empty_prompt(self):
        shell = self.shell()
        shell.open()
        shell.request("cancel", 'print x > "$ZDOTDIR/result"')
        shell.wait(b"READY> ")
        self.assertEqual(shell.open()[3], "")
        self.assertFalse((shell.path / "result").exists())

    def test_incomplete_quote_continues_through_zsh(self):
        shell = self.shell()
        shell.open()
        shell.request("accept", "print -r -- 'first")
        shell.wait(b"MORE> ")
        shell.open()
        shell.request("accept", 'second\' > "$ZDOTDIR/result"')
        shell.wait(b"READY> ")
        shell.open()
        self.assertEqual((shell.path / "result").read_text(), "first\nsecond\n")

    def test_completion_and_history_transfer_through_native_widgets(self):
        shell = self.shell()
        (shell.path / "unique-completion-file").touch()
        shell.open()
        shell.request("accept", 'cd "$ZDOTDIR"')
        shell.wait(b"READY> ")
        shell.open()
        shell.request("complete", "cat " + str(shell.path / "unique-comp"))
        completed = shell.open()[3]
        self.assertIn("unique-completion-file", completed)
        shell.request("previous", "")
        self.assertIn('cd ', shell.open()[3])

    def test_resize_does_not_change_transferred_buffer(self):
        shell = self.shell()
        shell.open()
        text = "雪🙂e\u0301" * 100 + "\n  "
        fcntl.ioctl(shell.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 20, 0, 0))
        os.kill(shell.pid, signal.SIGWINCH)
        shell.request("terminal", text)
        self.assertEqual(shell.snapshot()[0], text)

    def test_socket_path_is_not_exported_to_child_processes(self):
        shell = self.shell()
        shell.open()
        shell.request("accept", 'print -r -- ${GHOSTTY_COMMAND_EDITOR_SOCKET-unset} > "$ZDOTDIR/result"')
        shell.wait(b"READY> ")
        shell.open()
        self.assertEqual((shell.path / "result").read_text(), "unset\n")

    def test_eof_closes_empty_shell(self):
        shell = self.shell()
        shell.open()
        shell.request("eof", "")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if select.select([shell.fd], [], [], .1)[0]:
                try:
                    if not os.read(shell.fd, 65536):
                        return
                except OSError:
                    return
        self.fail("ZLE did not process EOF")

    def test_external_widget_gets_terminal_control_and_no_socket(self):
        shell = self.shell(extra='''
_test_complete() {
    python3 -c 'import os,tty; tty.setraw(0); os.write(1,b"APP_READY"); d=os.read(0,8); open(os.environ["ZDOTDIR"]+"/raw","wb").write(d)' </dev/tty
    BUFFER+=' done'
}
zle -N _test_complete
bindkey '^I' _test_complete
''')
        shell.open()
        shell.request("complete", "abc")
        shell.wait(b"APP_READY")
        shell.send(b"\x1b[1;2D")
        self.assertEqual(shell.open()[3], "abc done")
        self.assertEqual((shell.path / "raw").read_bytes(), b"\x1b[1;2D")


if __name__ == "__main__":
    unittest.main()

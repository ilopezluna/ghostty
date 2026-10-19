# Experimental ZLE command editor

This is a **partial keyboard editing prototype**, for local Linux zsh using
`bindkey -e`. It is not a Warp-style GUI editor. It adds editable keyboard
selection to the existing ZLE command buffer. No new runtime dependencies are
required. The default configuration does not enable it.

## Enable

Build/install this checkout and add to Ghostty's configuration:

```ini
shell-integration = zsh
shell-integration-features = command-editor
```

Open a new terminal surface. The integration installs after `.zshrc`, at the
first prompt. It skips installation on other operating systems, in SSH sessions
(`SSH_CONNECTION`, `SSH_CLIENT`, or `SSH_TTY` set), and when `main` is not an alias
of the emacs keymap. Other shells ignore this feature. Remove the option or use
`no-command-editor` and open a new surface to disable it. As with other feature
flags, `shell-integration-features = true` enables it too.

For development without building Ghostty, run an interactive local Linux zsh,
use `bindkey -e`, set `GHOSTTY_SHELL_FEATURES=command-editor`, and source
`src/shell-integration/zsh/ghostty-integration`. This exercises the same ZLE
implementation but does not validate Ghostty configuration parsing or the GUI.
Do not source `ghostty-command-editor` directly.

| Input | Behavior |
| --- | --- |
| Left / Right | Move and clear command selection |
| Shift + arrows | Extend command selection (Up / Down move by logical line) |
| Ctrl + Left / Right | Move by ZLE word |
| Ctrl + Shift + Left / Right | Extend selection by ZLE word |
| Home / End | Move to beginning / end of logical line |
| Ctrl + Home / End | Move to beginning / end of entire buffer |
| Shift with Home / End or Ctrl + Home / End | Extend selection to that boundary |
| Type, Backspace, Delete | Replace / remove command selection |
| Alt + W | Copy command selection to ZLE's kill ring |
| Ctrl + W | Cut selection to kill ring; otherwise backward-kill-word |
| Ctrl + Y | Yank kill ring, replacing selection |
| Terminal paste | Standard bracketed paste, replacing selection |
| Shift + Enter (also Alt + Enter) | Insert literal newline, replacing selection |
| Enter | Normal ZLE accept-line, with zsh parsing and continuation prompts |
| Ctrl + C / Ctrl + D | Normal ZLE cancel / delete-or-list / EOF behavior |

Ghostty's existing legacy encoder in `src/input/function_keys.zig` sends
`ESC [27;2;13~` for Shift+Enter. The editor binds that sequence, as well as
`CSI 13;2u` and Alt+Enter, without changing terminal keyboard protocols. Enter
continues to reach the shell's accept-line widget. No global terminal binding
is added, so other applications retain their existing Enter/Shift+Enter input.

## Architecture investigation and decision

The relevant existing path is:

1. `src/apprt/gtk/class/surface.zig` translates GTK input and calls the shared
   `src/Surface.zig:keyCallback`. The shared surface processes bindings, then
   encodes keys with `src/input/key_encode.zig` and queues terminal IO messages.
2. `src/termio/Exec.zig:queueWrite` writes those bytes to the PTY. The subprocess
   owns the PTY slave as stdin/stdout/stderr. The read/parse pipeline feeds
   output into `Termio.processOutput` and the terminal model for rendering.
3. `src/termio/shell_integration.zig` injects integration resources and serializes
   feature flags in `GHOSTTY_SHELL_FEATURES`. `src/config/Config.zig` defines the
   flags and their defaults. `src/build/GhosttyResources.zig` installs the entire
   integration directory, including the new script.
4. `zsh/.zshenv` restores the user's startup environment. `ghostty-integration`
   defers hooks until after user rc files. It emits OSC 133 prompt/command
   markers and tracks `_ghostty_state` for those markers, not the input buffer.
   Its existing prompt code explicitly describes async-theme and hook-order
   limitations. Prompt redraws are not buffer snapshots.
5. `src/Surface.zig:maybePromptClick` uses semantic prompt positions and, for
   the `cl` path used by zsh, sends repeated arrow keys. It cannot tell whether
   ZLE consumed them as cursor motion, a plugin widget, or something else.
   `src/input/paste.zig` already frames bracketed paste and sanitizes control
   bytes. These mechanisms are reused without changing their semantics.

Three approaches were considered:

- A GUI-owned draft sent back as text would need to reconcile completion,
  history, ZLE widgets and suggestions. Replaying text through the PTY invokes
  key bindings and paste hooks; it is not an assignment to `BUFFER`. Clearing a
  line and replaying it can lose edits, change meaning or accept it twice.
- A GUI editor synchronized with ZLE could support mouse selection and a
  platform clipboard, but needs an explicit buffer protocol: shell identity,
  editing lifecycle, revisions, character offsets, replacement acknowledgments
  and an accept operation. OSC 133 alone supplies none of these guarantees.
- Editing inside ZLE uses its authoritative `BUFFER`, `CURSOR`, `MARK` and
  `REGION_ACTIVE`. Completion/history and shell parsing already operate on that
  buffer. This is the chosen, smaller prototype.

`ghostty-command-editor` copies the emacs keymap, adds selection/navigation
widgets, and interposes insertion/deletion widgets while retaining the original
widgets. Selection replacement changes `BUFFER` directly, using ZLE character
offsets rather than terminal columns. Enter is not intercepted and the draft is
never serialized back to the PTY for execution. There is no duplicate GUI draft
to synchronize and no retry/second accept operation.

There is no terminal-side editor activation based on detecting a prompt. The
widgets run only while ZLE dispatches input. Accepted commands, cancelled lines,
continuation prompts and the next ZLE session follow zsh's lifecycle. When zsh
or a ZLE widget runs Vim, less, top or fzf, that program receives the usual PTY
input. The prototype enables no mouse reporting or keyboard protocol and adds
no terminal key bindings. Existing OSC 133 hooks remain unchanged.

## Edge cases and limits

- **Completion/history:** Tab and history widgets are retained. The PTY tests
  cover ordinary completion and history. There is no asynchronous synchronization
  with another draft. Native completion and history selection behavior remains
  ZLE's, rather than promising desktop-editor replacement semantics for Tab.
- **ZLE plugins/suggestions:** original insertion, paste and yank widgets are
  called with their original `WIDGET` name. The new arrow bindings deliberately
  use native motions; custom arrow bindings such as right-arrow accepting an
  autosuggestion are superseded in this keymap. Autosuggestion, syntax
  highlighting, async prompt themes and fzf plugin combinations have not been
  manually validated. Plugins that replace widgets/keymaps after installation
  can bypass the prototype. Vi mode and custom main keymaps are not supported.
- **Cancellation/EOF:** Ctrl+C does not accept a draft. Ctrl+D retains the
  user's delete-char-or-list behavior, including EOF on empty input; it is not
  redefined as selection deletion. Enter also retains any custom accept widget.
- **Incomplete commands:** Enter lets zsh request PS2 for open quotes or other
  incomplete syntax. This preserves zsh semantics, but editing the entire
  already-submitted PS1+PS2 command as one draft is not implemented. Build a
  single multiline draft with Shift+Enter when that is needed.
- **Paste:** bracketed paste inserts literal newlines without accepting them.
  Ordinary printable command content and LF are preserved and executed once on
  acceptance. Ghostty's existing control-byte sanitization still applies; this
  does not promise binary/NUL command buffers or fidelity for unsafe controls.
- **Unicode/wrapping/resize:** ZLE owns display width, prompt layout and redraw.
  Tests compare exact UTF-8 text through narrow resize, including wide characters,
  emoji and combining characters. Motion uses ZLE character semantics, not a new
  grapheme-cluster implementation. Visual alignment has not been checked in GTK.
- **Mouse and clipboard:** command selection is `REGION_ACTIVE`, separate from
  Ghostty's visible scrollback selection. Mouse drag still selects terminal
  output; Ctrl+Shift+C copies that terminal selection. It does **not** copy the
  ZLE region. The existing prompt-click behavior remains approximate and is not
  promoted to authoritative buffer positioning. Editable mouse drag/Shift-click
  and system clipboard copy/cut of the ZLE region are not implemented. Paste
  from the system clipboard uses the terminal's existing bracketed-paste path.
- **Remote/context fallback:** no new bindings are sent to child applications
  or remote hosts. Automatic integration is only injected into the spawned
  shell, and SSH environment checks also reject manual loading remotely. Nested
  shells and multiplexers are not newly integrated. Contexts without this ZLE
  integration continue with the usual terminal input.

## Validation

Run from the repository root:

```sh
python3 src/shell-integration/zsh/tests/command_editor.py
zsh -n src/shell-integration/zsh/ghostty-integration
zsh -n src/shell-integration/zsh/ghostty-command-editor
zig fmt --check src/config/Config.zig src/termio/shell_integration.zig
zig build test -Dtest-filter='setup features'
```

The 15-test Python suite uses real zsh 5.9 under a Linux PTY, without a graphical server
or external Python packages. It checks buffer/selection offsets, replacement,
kill-ring operations, multiline acceptance exactly once, safe bracketed paste,
completion/history, quote continuation/cancellation, EOF, Unicode, resize, and
handoff to a raw-mode subprocess and back. It also checks opt-out, SSH and vi
fallback and preservation of a user insertion widget. The raw subprocess test
is not a manual test of Vim, less, top or fzf.

In the implementation environment (Linux ARM64), the PTY tests, shell syntax,
Zig formatting and `git diff --check` passed. Zig 0.16.0 was obtained for checks,
but the targeted Zig build could not fetch `translate-c` from Codeberg (HTTP
403, network approval required). The full project test target and GTK app
were therefore not compiled or run. As a narrower check, the exact
`ShellIntegrationFeatures` type, `setupFeatures` function and `setup features`
test were extracted into a temporary standard-library-only Zig module;
`zig test` passed that test, including default-off serialization, explicit
activation and removal of inherited flags when all features are disabled.
This does not verify the full config parser or the GTK build. No manual
graphical interaction was performed.

## Next experiment

Before building a GUI overlay, add a local, non-forwarded ZLE communication
channel and a small protocol test: read a versioned `BUFFER`/`CURSOR` snapshot,
request a replacement only for that revision, and receive an acknowledgment.
Exercise Tab, history, Ctrl+C, async prompt redraw, fzf and process transitions
between snapshot and replacement; stale operations must be rejected. Then test
an acknowledged accept operation that cannot execute twice. Do not use terminal
screen coordinates as buffer offsets. After that, a GTK editor can supply mouse
selection, system clipboard and grapheme-aware movement with an
explicit ownership handoff. Other platforms and shells need separate adapters.

This prototype and its tests/documentation were generated with OpenAI Codex;
any upstream contribution needs human review and disclosure per `AI_POLICY.md`.

# Experimental GTK command input panel

This prototype provides a **native graphical command editor**, with its own
`GtkTextView` below the terminal. It replaces the earlier keyboard-only ZLE
experiment. It targets local Linux zsh with the emacs keymap, using the existing
GTK/GLib runtime. It is disabled by default. Full application integration still
needs a successful Ghostty build; see the validation limits below.

## Enable

Build/install this checkout, then configure:

```ini
shell-integration = zsh
shell-integration-features = command-editor
```

Open a new surface. Shell integration installs after `.zshrc` and opens the
panel when ZLE starts accepting a new command. Remove the flag or use
`no-command-editor` and open a new surface to disable it. As with other shell
features, `shell-integration-features = true` enables this feature too.

Installation requires a local channel created by the GTK surface, Linux,
multibyte ZLE, the emacs keymap, and an unused Ctrl+X Ctrl+G binding. SSH sessions,
vi/custom main keymaps, Flatpak, other shells and other app runtimes keep normal
terminal input. Changing the main keymap to vi later stops automatic reopening.

| Input                                | Behavior                                                             |
| ------------------------------------ | -------------------------------------------------------------------- |
| Click / drag / Shift+arrows          | Native editable cursor and selection                                 |
| Ctrl+arrows, Home/End, Ctrl+Home/End | Native GTK text/word navigation                                      |
| Ctrl+C / Ctrl+X / Ctrl+V             | System clipboard copy/cut/paste                                      |
| Ctrl+Shift+C/X/V in the panel        | Also copy/cut/paste the command selection                            |
| Type / Backspace / Delete / paste    | Native selection replacement                                         |
| Shift+Enter                          | Insert a newline without executing                                   |
| Enter                                | Transfer the whole draft to ZLE and accept once                      |
| Tab                                  | Transfer to the configured completion widget, then reopen its result |
| Up / Down at first/last logical line | Native ZLE history, then reopen its result                           |
| Up / Down elsewhere                  | Move within the graphical draft                                      |
| Ctrl+C with no selection / Ctrl+G    | Cancel through ZLE, without executing                                |
| Ctrl+D                               | Native text deletion; EOF on an empty draft                          |
| Esc                                  | Return the draft and cursor to normal ZLE input                      |
| Ctrl+X Ctrl+G in ZLE                 | Reopen the panel with the current native buffer                      |

Ctrl+C with a selection copies it, as in a text editor. Ctrl+G cancels regardless
of selection. Clicking terminal output still permits scrollback selection;
Ctrl+Shift+C there copies the terminal selection. Other keys in the terminal
are forwarded to the active panel instead of accumulating in the blocked PTY.
The command selection and scrollback selection are separate.

## Architecture investigation and decision

The existing input path is
`src/apprt/gtk/class/surface.zig` → `src/Surface.zig:keyCallback` →
`src/input/key_encode.zig` → `src/termio/Exec.zig:queueWrite` → PTY → shell or
foreground program. The reverse PTY read/parse path feeds `Termio.processOutput`
and the terminal model for rendering.

`src/termio/shell_integration.zig` injects resources and serializes feature
flags. `zsh/.zshenv` restores user startup state; `ghostty-integration` defers its
hooks until after user rc files. Its OSC 133 prompt markers describe terminal
regions, not a reliable snapshot of ZLE's input buffer. The existing
`src/Surface.zig:maybePromptClick` zsh path sends arrow keys from approximate
screen positions. `src/input/paste.zig` frames/sanitizes terminal pastes; that is
not an assignment to `BUFFER` either. Neither mechanism supplies editor ownership
or acknowledged execution.

Three architectures were considered:

- **ZLE-only editing:** preserves native shell operation and can add keyboard
  selection, but cannot deliver a separate graphical editor. That was the earlier
  experiment and did not satisfy the intended interaction.
- **GUI draft replayed into the PTY:** completion, history, bindings and paste
  hooks can reinterpret replayed text. Prompt detection cannot make this a safe
  buffer synchronization mechanism. This approach is not used.
- **GUI draft with explicit local ZLE transfers:** the chosen design. GTK owns
  editing between transfers. ZLE owns completion, history, acceptance and all
  native terminal input after a handoff.

`src/apprt/gtk/command_editor/editor.c` implements the real GTK widget and a
nonblocking GLib socket service. A small C ABI in `command_editor.zig` connects
it to the existing Zig surface. This GTK-only helper introduces no new runtime
libraries and can be compiled/tested independently while the project's Zig
build dependencies are unavailable. `src/build/SharedDeps.zig` compiles it with
the existing GTK dependency. `ui/1.0/surface.blp` reserves a sibling slot below
the terminal, leaving terminal mouse/controllers on their existing branch.

The surface creates a private temporary directory (0700) and a Unix socket per
surface, removes any inherited channel path, and injects its own path as
`GHOSTTY_COMMAND_EDITOR_SOCKET`. `src/termio/Exec.zig` strips that path when
automatic integration identifies a different shell or cannot load integration.
No terminal escape sequence can open the panel
or select a socket. Incoming connections require same-user credentials. After
installation zsh unsets the exported path so child shells and SSH commands do
not inherit it. The socket is a same-user interface, not a security boundary
against arbitrary processes already running as that user.

`ghostty-command-editor` queues its widget after line-init hooks. Each widget
transaction has one socket connection and this framing:

```text
GCE1 operation revision unicode_cursor utf8_bytes\n
<exactly utf8_bytes bytes, including embedded newlines/trailing spaces>
```

The shell sends an authoritative `state` snapshot. A single request can return
`accept`, `complete`, `previous`, `next`, `terminal`, `cancel` or `eof`, together
with the complete draft and cursor. The revision is scoped to that connection
(currently one snapshot, revision 1). Closed connections cannot submit to a new
transaction. ZLE also compares its current buffer/cursor with the original
snapshot, rejecting the request if an asynchronous hook changed them.

After validation, ZLE assigns `BUFFER`/`CURSOR` directly and acknowledges with
`done`. GTK closes the connection; ZLE waits for that release and closes its fd
**before invoking any native widget or external command**. Completion/history
then open a fresh transaction with their resulting buffer. The GTK editor is
hidden and terminal focus restored during these operations, so interactive
widgets can use the terminal normally. Acceptance invokes native `accept-line`
once. The GUI disables sending while a transfer is pending; no timeout retries
or command-text replay are used. This is an ownership-transfer model, not
per-keystroke mirroring of two editable buffers.

## Edge cases and remaining limits

- **Completion/history:** tested with real ZLE completion, history, and a custom
  completion widget that runs a raw-mode subprocess. Menu completion can remain
  in the terminal until its widget returns. The configured Tab/arrow widgets
  are retained when they are ordinary widget bindings; macros fall back to
  native widgets. Esc gives access to all other native ZLE/plugin shortcuts.
- **Suggestions/highlighting:** live shell suggestions and shell syntax
  highlighting are not rendered in the panel. Arbitrary ZLE chords are not
  interpreted by the GTK editor. Async prompt/suggestion and fzf plugin
  combinations still need manual validation. Some custom Ghostty shortcuts
  whose handlers live only on the terminal branch are not available while the
  panel has keyboard focus; window/application accelerators retain their own
  dispatch. This prototype does not claim complete shortcut/plugin parity.
- **Cancellation/EOF:** GUI cancellation calls ZLE's send-break. Ctrl+D on an
  empty draft queues the normal Ctrl+D action in ZLE's top-level dispatch;
  directly invoking delete-char-or-list inside another widget is insufficient
  for EOF. Nonempty Ctrl+D uses GTK's native text deletion.
- **Incomplete commands:** Enter delegates parsing to zsh. Open quotes and
  incomplete syntax produce PS2 and another draft. Editing the previously
  accepted PS1 portion together with PS2 is not implemented. Shift+Enter keeps
  the entire multiline command in one graphical draft until acceptance.
- **Paste/execution:** clipboard text goes into `GtkTextBuffer`, never through
  the PTY. Embedded newlines cannot execute during paste. The accepted UTF-8
  draft is assigned directly to ZLE and accepted once, with normal shell
  parsing. Frames are bounded to 1 MiB; binary/NUL buffers are unsupported.
  If a connection fails during acceptance, execution outcome is unknown and
  the command is never retried automatically.
- **Unicode/resize:** GTK owns selection, wrapping, text layout and IME. Cursor
  positions use Unicode character offsets, matching multibyte ZLE, not display
  columns or UTF-8 bytes. Tests include wide characters, emoji, combining text,
  multiline paste and resize. Enter is first offered to the text view's IM
  context before being treated as acceptance. Complex IMEs still need manual
  validation. Changing GTK panel height resizes the terminal normally.
- **Prompt/process transitions:** only explicit local ZLE connections activate
  the panel. Prompt text/redraws do not activate it. On handoff to Vim, less,
  top, fzf or another process it is hidden and no editor fd remains open.
  No terminal mouse or keyboard protocols are changed. The raw-mode subprocess
  test verifies this path; those individual applications were not manually run
  inside a complete Ghostty build.
- **Failures/readonly:** loss of connection leaves the draft visible and
  read-only for recovery/copy, without automatic resubmission. Reopening a new
  transaction can replace that recovery view, so copy it first if needed.
  The surface's readonly action disables editing and acceptance while retaining
  selection/copy. Normal input remains the fallback if integration cannot load.

## Validation

The following commands compile the actual GTK component and exercise it with
real zsh. The test host is a minimal GTK window containing the production panel;
it does **not** substitute for verification of the complete Ghostty app.
Tests use Python's standard library, GTK4 development files, Xvfb, xdotool and
an X11 xclip executable. The tests deliberately use `/usr/bin/xclip` so a host
clipboard bridge earlier on PATH cannot replace the isolated X11 clipboard.

```sh
cc -Wall -Wextra -Werror \
  src/apprt/gtk/command_editor/editor.c \
  src/apprt/gtk/command_editor/panel_harness.c \
  $(pkg-config --cflags --libs gtk4) -o /tmp/ghostty-panel-harness
python3 src/shell-integration/zsh/tests/command_editor.py
xvfb-run -a env GDK_BACKEND=x11 GSK_RENDERER=cairo GSETTINGS_BACKEND=memory \
  GHOSTTY_PANEL_HARNESS=/tmp/ghostty-panel-harness \
  python3 src/shell-integration/zsh/tests/panel.py
blueprint-compiler compile src/apprt/gtk/ui/1.0/surface.blp \
  --output /tmp/ghostty-surface-panel.ui
zig build test -Dtest-filter='setup features'
```

On Linux ARM64 with zsh 5.9 and GTK 4.22, all 14 protocol/PTY tests and all
10 native GTK/X11 tests pass. They cover exact text/cursor transfers, fragmented frames, invalid
revisions, bounded sizes, missing-channel/SSH/vi fallback, Unicode/resize,
completion/history, subprocess handoff, cancellation, quote continuation, EOF,
mouse and keyboard selection, system clipboard, multiline acceptance exactly
once, readonly mode, recovery and forwarding keys from the terminal branch.
The GTK suite also runs with AddressSanitizer and UndefinedBehaviorSanitizer;
leak detection is disabled for toolkit allocations. Blueprint compilation,
shell syntax, Zig/C formatting and whitespace checks pass.

**Full-build limit:** the project Zig build is still blocked by HTTP 403 while
fetching `translate-c` from Codeberg (network approval required) and `uucode`
from GitHub. Consequently the Zig surface adapter, resource assembly and
complete Ghostty app have not been compiled or run together. The panel and ZLE
bridge are tested real components, but that integration gap must be closed
before treating this as a finished Ghostty feature. No manual full-application
GUI validation has been performed.

Next steps are to unblock the full GTK build, verify application shortcuts and
focus across tabs/splits and clipboard paths in the actual terminal, validate
real completion/fzf/theme/IME combinations, then add live suggestions and
highlighting if desired. macOS, other shells and SSH require explicit adapters
and lifecycle validation rather than relying on prompt markers.

Implementation, tests and documentation were generated with OpenAI Codex;
upstream contributions require human review and disclosure per `AI_POLICY.md`.

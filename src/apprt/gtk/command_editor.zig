//! Native GTK command panel and local ZLE transport. Kept behind a small C ABI
//! so the GTK/GLib component can also be compiled and exercised independently.
const gtk = @import("gtk");
const gdk = @import("gdk");

pub const Editor = opaque {
    pub const create = ghostty_command_editor_new;
    pub const deinit = ghostty_command_editor_free;
    pub const setReadonly = ghostty_command_editor_set_readonly;
    pub const socketPath = ghostty_command_editor_socket;
    pub const grabFocus = ghostty_command_editor_focus;
    pub const forwardKey = ghostty_command_editor_forward_key;

    extern fn ghostty_command_editor_new(*gtk.Box, *gtk.Widget) ?*Editor;
    extern fn ghostty_command_editor_free(*Editor) void;
    extern fn ghostty_command_editor_set_readonly(*Editor, c_int) void;
    extern fn ghostty_command_editor_socket(*Editor) [*:0]const u8;
    extern fn ghostty_command_editor_focus(*Editor) c_int;
    extern fn ghostty_command_editor_forward_key(*Editor, *gtk.EventControllerKey, c_uint, gdk.ModifierType) c_int;
};

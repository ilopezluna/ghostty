#pragma once
#include <gtk/gtk.h>

typedef struct GhosttyCommandEditor GhosttyCommandEditor;
GhosttyCommandEditor* ghostty_command_editor_new(GtkBox*, GtkWidget*);
void ghostty_command_editor_free(GhosttyCommandEditor*);
void ghostty_command_editor_set_readonly(GhosttyCommandEditor*, gboolean);
const char* ghostty_command_editor_socket(GhosttyCommandEditor*);
gboolean ghostty_command_editor_focus(GhosttyCommandEditor*);
gboolean ghostty_command_editor_forward_key(GhosttyCommandEditor*,
                                            GtkEventControllerKey*,
                                            guint,
                                            GdkModifierType);

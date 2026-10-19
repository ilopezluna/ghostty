/* Test host for the production GTK component, not a replacement terminal.
 * PTY and zsh are started by panel.py; snapshots only observe GTK widget state.
 */
#include <glib-unix.h>
#include <signal.h>
#include <stdio.h>
#include "editor.h"

static const char* directory;
static GtkWidget *window, *slot, *terminal;
static GtkTextView* view;
static GhosttyCommandEditor* editor;

static gboolean observe(gpointer unused) {
  (void)unused;
  char* readonly_path = g_build_filename(directory, "readonly", NULL);
  gboolean readonly = g_file_test(readonly_path, G_FILE_TEST_EXISTS);
  static gboolean last_readonly = FALSE;
  if (readonly != last_readonly) {
    ghostty_command_editor_set_readonly(editor, readonly);
    last_readonly = readonly;
  }
  g_free(readonly_path);
  GtkTextBuffer* buffer = gtk_text_view_get_buffer(view);
  GtkTextIter start, end, insert, bound;
  gtk_text_buffer_get_bounds(buffer, &start, &end);
  gtk_text_buffer_get_iter_at_mark(buffer, &insert,
                                   gtk_text_buffer_get_insert(buffer));
  gtk_text_buffer_get_iter_at_mark(buffer, &bound,
                                   gtk_text_buffer_get_selection_bound(buffer));
  char* text = gtk_text_buffer_get_text(buffer, &start, &end, TRUE);
  char* path = g_build_filename(directory, "buffer", NULL);
  g_file_set_contents(path, text, -1, NULL);
  g_free(path);
  g_free(text);
  char* state = g_strdup_printf("%d %d %d %d\n", gtk_widget_get_visible(slot),
                                gtk_text_view_get_editable(view),
                                gtk_text_iter_get_offset(&insert),
                                gtk_text_iter_get_offset(&bound));
  path = g_build_filename(directory, "state", NULL);
  g_file_set_contents(path, state, -1, NULL);
  g_free(path);
  g_free(state);
  if (gtk_widget_get_mapped(GTK_WIDGET(view))) {
    GString* positions = g_string_new(NULL);
    for (int i = 0; i <= gtk_text_buffer_get_char_count(buffer) && i < 500;
         i++) {
      GtkTextIter iter;
      GdkRectangle rect;
      int x, y;
      gtk_text_buffer_get_iter_at_offset(buffer, &iter, i);
      gtk_text_view_get_iter_location(view, &iter, &rect);
      gtk_text_view_buffer_to_window_coords(view, GTK_TEXT_WINDOW_WIDGET,
                                            rect.x, rect.y + rect.height / 2,
                                            &x, &y);
      graphene_point_t point = GRAPHENE_POINT_INIT(x, y), result;
      if (gtk_widget_compute_point(GTK_WIDGET(view), window, &point, &result))
        g_string_append_printf(positions, "%d %d\n", (int)result.x,
                               (int)result.y);
    }
    path = g_build_filename(directory, "positions", NULL);
    g_file_set_contents(path, positions->str, positions->len, NULL);
    g_free(path);
    g_string_free(positions, TRUE);
  }
  return G_SOURCE_CONTINUE;
}

static gboolean keys(GtkEventControllerKey* controller,
                     guint key,
                     guint code,
                     GdkModifierType mods,
                     gpointer unused) {
  (void)code;
  (void)unused;
  return ghostty_command_editor_forward_key(editor, controller, key, mods);
}

static gboolean close_window(GtkWindow* widget, gpointer loop) {
  (void)widget;
  g_main_loop_quit(loop);
  return TRUE;
}

static gboolean stop_loop(gpointer loop) {
  g_main_loop_quit(loop);
  return G_SOURCE_REMOVE;
}

int main(int argc, char** argv) {
  if (argc != 2)
    return 2;
  directory = argv[1];
  gtk_init();
  window = gtk_window_new();
  gtk_window_set_title(GTK_WINDOW(window), "Ghostty Command Editor Test");
  gtk_window_set_default_size(GTK_WINDOW(window), 640, 360);
  GtkWidget* root = gtk_box_new(GTK_ORIENTATION_VERTICAL, 0);
  gtk_window_set_child(GTK_WINDOW(window), root);
  terminal = gtk_text_view_new();
  gtk_widget_set_vexpand(terminal, TRUE);
  gtk_box_append(GTK_BOX(root), terminal);
  slot = gtk_box_new(GTK_ORIENTATION_VERTICAL, 0);
  gtk_widget_set_visible(slot, FALSE);
  gtk_box_append(GTK_BOX(root), slot);
  editor = ghostty_command_editor_new(GTK_BOX(slot), terminal);
  if (!editor)
    return 1;
  GtkWidget* scroll = gtk_widget_get_first_child(slot);
  view =
      GTK_TEXT_VIEW(gtk_scrolled_window_get_child(GTK_SCROLLED_WINDOW(scroll)));
  GtkEventController* controller = gtk_event_controller_key_new();
  g_signal_connect(controller, "key-pressed", G_CALLBACK(keys), NULL);
  gtk_widget_add_controller(terminal, controller);
  GMainLoop* loop = g_main_loop_new(NULL, FALSE);
  g_unix_signal_add(SIGTERM, stop_loop, loop);
  g_signal_connect(window, "close-request", G_CALLBACK(close_window), loop);
  gtk_window_present(GTK_WINDOW(window));
  guint source = g_timeout_add(20, observe, NULL);
  puts(ghostty_command_editor_socket(editor));
  fflush(stdout);
  g_main_loop_run(loop);
  g_source_remove(source);
  ghostty_command_editor_free(editor);
  gtk_window_destroy(GTK_WINDOW(window));
  g_main_loop_unref(loop);
  return 0;
}

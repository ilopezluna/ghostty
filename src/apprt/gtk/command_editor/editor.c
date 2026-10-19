/* An experimental GTK-owned draft, transferred explicitly to local ZLE.
 * No command text or ownership transitions are inferred from terminal output.
 */
#include "editor.h"
#include <glib/gstdio.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define MAX_DRAFT (1024 * 1024)
#define MAX_HEADER 96

struct GhosttyCommandEditor {
  GtkBox* slot;
  GtkWidget* terminal;
  GtkTextView* view;
  GtkLabel* status;
  GSocketService* service;
  GSocketConnection* connection;
  GByteArray *input, *output;
  char *directory, *path;
  guint timer;
  guint64 revision;
  gboolean active, pending, readonly;
  GtkEventController* keys;
};

static void hide(GhosttyCommandEditor* self) {
  self->active = FALSE;
  gtk_widget_set_visible(GTK_WIDGET(self->slot), FALSE);
  gtk_widget_grab_focus(self->terminal);
}

static void disconnect_client(GhosttyCommandEditor* self) {
  if (self->connection) {
    g_io_stream_close(G_IO_STREAM(self->connection), NULL, NULL);
    g_clear_object(&self->connection);
  }
  self->active = self->pending = FALSE;
  g_byte_array_set_size(self->input, 0);
  g_byte_array_set_size(self->output, 0);
}

static void failed(GhosttyCommandEditor* self) {
  disconnect_client(self);
  /* Keep the draft available for recovery/copy. The outcome of a failed
   * accept is unknown, so never retry it or automatically replay text. */
  gtk_text_view_set_editable(self->view, FALSE);
  gtk_widget_set_visible(GTK_WIDGET(self->slot), TRUE);
  gtk_label_set_text(self->status,
                     "Shell connection lost. Copy your draft before continuing "
                     "in the terminal.");
  gtk_widget_grab_focus(self->terminal);
}

static gboolean frame(GhosttyCommandEditor* self,
                      const char* op,
                      guint64 revision,
                      guint cursor,
                      const guint8* body,
                      guint length) {
  if (!strcmp(op, "state")) {
    if (revision != self->revision + 1 || (!self->pending && self->revision) ||
        memchr(body, 0, length) ||
        !g_utf8_validate((const char*)body, length, NULL) ||
        cursor > (guint)g_utf8_strlen((const char*)body, length))
      return FALSE;
    self->revision = revision;
    self->pending = FALSE;
    GtkTextBuffer* buffer = gtk_text_view_get_buffer(self->view);
    gtk_text_buffer_set_text(buffer, (const char*)body, length);
    GtkTextIter iter;
    gtk_text_buffer_get_iter_at_offset(buffer, &iter, cursor);
    gtk_text_buffer_place_cursor(buffer, &iter);
    gtk_text_view_set_editable(self->view, !self->readonly);
    gtk_label_set_text(
        self->status,
        "Enter to run · Shift+Enter for a new line · Esc for shell input");
    self->active = TRUE;
    gtk_widget_set_visible(GTK_WIDGET(self->slot), TRUE);
    gtk_widget_grab_focus(GTK_WIDGET(self->view));
    return TRUE;
  }
  if (!strcmp(op, "done") && revision == self->revision && self->pending &&
      !length && !cursor) {
    disconnect_client(self);
    hide(self);
    return TRUE;
  }
  return FALSE;
}

/* Both directions use: GCE1 operation revision cursor utf8_bytes LF body.
 * A bounded byte count preserves embedded newlines and trailing whitespace.
 */
static gboolean parse(GhosttyCommandEditor* self) {
  while (self->input->len) {
    guint8* newline = memchr(self->input->data, '\n', self->input->len);
    if (!newline)
      return self->input->len <= MAX_HEADER;
    guint header_length = newline - self->input->data + 1;
    if (header_length > MAX_HEADER)
      return FALSE;
    char header[MAX_HEADER + 1], operation[16];
    unsigned long long revision;
    unsigned cursor, length;
    int consumed = 0;
    memcpy(header, self->input->data, header_length);
    header[header_length] = 0;
    if (sscanf(header, "GCE1 %15s %llu %u %u%n", operation, &revision, &cursor,
               &length, &consumed) != 4 ||
        consumed != (int)header_length - 1 || length > MAX_DRAFT)
      return FALSE;
    if (self->input->len < header_length + length)
      return TRUE;
    if (!frame(self, operation, revision, cursor,
               self->input->data + header_length, length))
      return FALSE;
    /* A done frame clears the input and closes the connection. */
    if (!self->connection)
      return TRUE;
    g_byte_array_remove_range(self->input, 0, header_length + length);
  }
  return TRUE;
}

static gboolean poll_connection(gpointer data) {
  GhosttyCommandEditor* self = data;
  if (!self->connection)
    return G_SOURCE_CONTINUE;
  GSocket* socket = g_socket_connection_get_socket(self->connection);
  GError* error = NULL;
  if (self->output->len) {
    gssize n = g_socket_send(socket, (const char*)self->output->data,
                             self->output->len, NULL, &error);
    if (n > 0)
      g_byte_array_remove_range(self->output, 0, n);
    else if (!g_error_matches(error, G_IO_ERROR, G_IO_ERROR_WOULD_BLOCK)) {
      g_clear_error(&error);
      failed(self);
      return G_SOURCE_CONTINUE;
    }
    g_clear_error(&error);
  }
  guint8 bytes[8192];
  /* Limit work per main-loop iteration so a local peer cannot starve GTK. */
  for (int i = 0; i < 16 && self->connection; i++) {
    gssize n =
        g_socket_receive(socket, (char*)bytes, sizeof(bytes), NULL, &error);
    if (n < 0 && g_error_matches(error, G_IO_ERROR, G_IO_ERROR_WOULD_BLOCK)) {
      g_clear_error(&error);
      break;
    }
    if (n <= 0) {
      g_clear_error(&error);
      failed(self);
      break;
    }
    if (self->input->len + n > MAX_DRAFT + MAX_HEADER) {
      failed(self);
      break;
    }
    g_byte_array_append(self->input, bytes, n);
    if (!parse(self)) {
      failed(self);
      break;
    }
  }
  return G_SOURCE_CONTINUE;
}

static gboolean incoming(GSocketService* service,
                         GSocketConnection* connection,
                         GObject* source,
                         gpointer data) {
  (void)service;
  (void)source;
  GhosttyCommandEditor* self = data;
  GSocket* socket = g_socket_connection_get_socket(connection);
  GError* error = NULL;
  GCredentials* credentials = g_socket_get_credentials(socket, &error);
  gboolean allowed = credentials && g_credentials_get_unix_user(
                                        credentials, &error) == getuid();
  g_clear_object(&credentials);
  g_clear_error(&error);
  if (self->connection || !allowed) {
    g_io_stream_close(G_IO_STREAM(connection), NULL, NULL);
    return TRUE;
  }
  self->connection = g_object_ref(connection);
  g_socket_set_blocking(socket, FALSE);
  self->revision = 0;
  self->pending = TRUE;
  return TRUE;
}

static void operation(GhosttyCommandEditor* self, const char* op) {
  if (!self->active || self->pending || !self->connection)
    return;
  GtkTextBuffer* buffer = gtk_text_view_get_buffer(self->view);
  GtkTextIter start, end, cursor;
  gtk_text_buffer_get_bounds(buffer, &start, &end);
  char* text = gtk_text_buffer_get_text(buffer, &start, &end, TRUE);
  gsize length = strlen(text);
  if (length > MAX_DRAFT) {
    gtk_label_set_text(
        self->status,
        "Command is too large. Reduce it before sending to the shell.");
    g_free(text);
    return;
  }
  gtk_text_buffer_get_iter_at_mark(buffer, &cursor,
                                   gtk_text_buffer_get_insert(buffer));
  char* header = g_strdup_printf("GCE1 %s %" G_GUINT64_FORMAT " %d %zu\n", op,
                                 self->revision,
                                 gtk_text_iter_get_offset(&cursor), length);
  g_byte_array_append(self->output, (const guint8*)header, strlen(header));
  g_byte_array_append(self->output, (const guint8*)text, length);
  g_free(header);
  g_free(text);
  /* Relinquish ownership before ZLE can run an interactive completion widget.
   * Repeated Enter events cannot queue another accept while awaiting a reply.
   */
  self->pending = TRUE;
  gtk_text_view_set_editable(self->view, FALSE);
  hide(self);
}

static gboolean key_pressed(GtkEventControllerKey* controller,
                            guint key,
                            guint code,
                            GdkModifierType mods,
                            gpointer data) {
  (void)code;
  GhosttyCommandEditor* self = data;
  if (!self->active || self->pending || self->readonly)
    return FALSE;
  GtkTextBuffer* buffer = gtk_text_view_get_buffer(self->view);
  gboolean ctrl = (mods & GDK_CONTROL_MASK) != 0;
  gboolean shift = (mods & GDK_SHIFT_MASK) != 0;
  if (ctrl && shift &&
      (key == GDK_KEY_C || key == GDK_KEY_X || key == GDK_KEY_V ||
       key == GDK_KEY_c || key == GDK_KEY_x || key == GDK_KEY_v)) {
    GdkClipboard* clipboard = gtk_widget_get_clipboard(GTK_WIDGET(self->view));
    if (key == GDK_KEY_C || key == GDK_KEY_c)
      gtk_text_buffer_copy_clipboard(buffer, clipboard);
    else if (key == GDK_KEY_X || key == GDK_KEY_x)
      gtk_text_buffer_cut_clipboard(buffer, clipboard, TRUE);
    else
      gtk_text_buffer_paste_clipboard(buffer, clipboard, NULL, TRUE);
    return TRUE;
  }
  if (ctrl && (key == GDK_KEY_d || key == GDK_KEY_D) &&
      gtk_text_buffer_get_char_count(buffer)) {
    if (!gtk_text_buffer_delete_selection(buffer, TRUE, TRUE)) {
      GtkTextIter start, end;
      gtk_text_buffer_get_iter_at_mark(buffer, &start,
                                       gtk_text_buffer_get_insert(buffer));
      end = start;
      if (gtk_text_iter_forward_char(&end))
        gtk_text_buffer_delete(buffer, &start, &end);
    }
    return TRUE;
  }
  const char* op = NULL;
  if ((key == GDK_KEY_Return || key == GDK_KEY_KP_Enter) && !shift && !ctrl &&
      !(mods & GDK_ALT_MASK))
    op = "accept";
  else if (key == GDK_KEY_Escape)
    op = "terminal";
  else if (key == GDK_KEY_Tab && !shift && !ctrl)
    op = "complete";
  else if (ctrl && (key == GDK_KEY_g || key == GDK_KEY_G))
    op = "cancel";
  else if (ctrl && (key == GDK_KEY_c || key == GDK_KEY_C) &&
           !gtk_text_buffer_get_has_selection(buffer))
    op = "cancel";
  else if (ctrl && (key == GDK_KEY_d || key == GDK_KEY_D) &&
           !gtk_text_buffer_get_char_count(buffer))
    op = "eof";
  else if (!shift && !ctrl && (key == GDK_KEY_Up || key == GDK_KEY_Down)) {
    GtkTextIter cursor;
    gtk_text_buffer_get_iter_at_mark(buffer, &cursor,
                                     gtk_text_buffer_get_insert(buffer));
    int line = gtk_text_iter_get_line(&cursor);
    if (key == GDK_KEY_Up && line == 0)
      op = "previous";
    else if (key == GDK_KEY_Down &&
             line == gtk_text_buffer_get_line_count(buffer) - 1)
      op = "next";
  }
  if (!op)
    return FALSE; /* Native text editing, selection, paste and IME. */
  GdkEvent* event =
      gtk_event_controller_get_current_event(GTK_EVENT_CONTROLLER(controller));
  if (event && gtk_text_view_im_context_filter_keypress(self->view, event))
    return TRUE; /* Enter may commit an IME composition, not a command. */
  operation(self, op);
  return TRUE;
}

GhosttyCommandEditor* ghostty_command_editor_new(GtkBox* slot,
                                                 GtkWidget* terminal) {
  GhosttyCommandEditor* self = g_new0(GhosttyCommandEditor, 1);
  self->slot = slot;
  self->terminal = terminal;
  self->input = g_byte_array_new();
  self->output = g_byte_array_new();
  self->directory = g_dir_make_tmp("ghostty-command-editor-XXXXXX", NULL);
  if (!self->directory)
    goto error;
  self->path = g_build_filename(self->directory, "zle.sock", NULL);
  self->service = g_socket_service_new();
  GSocketAddress* address = g_unix_socket_address_new(self->path);
  gboolean ok = g_socket_listener_add_address(
      G_SOCKET_LISTENER(self->service), address, G_SOCKET_TYPE_STREAM,
      G_SOCKET_PROTOCOL_DEFAULT, NULL, NULL, NULL);
  g_object_unref(address);
  if (!ok)
    goto error;
  g_signal_connect(self->service, "incoming", G_CALLBACK(incoming), self);
  g_socket_service_start(self->service);

  GtkWidget* scroll = gtk_scrolled_window_new();
  gtk_scrolled_window_set_policy(GTK_SCROLLED_WINDOW(scroll), GTK_POLICY_NEVER,
                                 GTK_POLICY_AUTOMATIC);
  gtk_scrolled_window_set_min_content_height(GTK_SCROLLED_WINDOW(scroll), 64);
  gtk_scrolled_window_set_max_content_height(GTK_SCROLLED_WINDOW(scroll), 200);
  gtk_scrolled_window_set_propagate_natural_height(GTK_SCROLLED_WINDOW(scroll),
                                                   TRUE);
  self->view = GTK_TEXT_VIEW(gtk_text_view_new());
  gtk_text_view_set_monospace(self->view, TRUE);
  gtk_text_view_set_wrap_mode(self->view, GTK_WRAP_WORD_CHAR);
  gtk_text_view_set_left_margin(self->view, 12);
  gtk_text_view_set_right_margin(self->view, 12);
  gtk_text_view_set_top_margin(self->view, 8);
  gtk_text_view_set_bottom_margin(self->view, 8);
  gtk_widget_set_name(GTK_WIDGET(self->view), "ghostty-command-editor");
  gtk_scrolled_window_set_child(GTK_SCROLLED_WINDOW(scroll),
                                GTK_WIDGET(self->view));
  gtk_box_append(slot, scroll);
  self->status = GTK_LABEL(gtk_label_new(NULL));
  gtk_label_set_xalign(self->status, 0);
  gtk_widget_add_css_class(GTK_WIDGET(self->status), "dim-label");
  gtk_widget_set_margin_start(GTK_WIDGET(self->status), 12);
  gtk_widget_set_margin_bottom(GTK_WIDGET(self->status), 6);
  gtk_box_append(slot, GTK_WIDGET(self->status));
  GtkEventController* keys = gtk_event_controller_key_new();
  self->keys = keys;
  gtk_event_controller_set_propagation_phase(keys, GTK_PHASE_CAPTURE);
  g_signal_connect(keys, "key-pressed", G_CALLBACK(key_pressed), self);
  gtk_widget_add_controller(GTK_WIDGET(self->view), keys);
  self->timer = g_timeout_add(10, poll_connection, self);
  return self;
error:
  ghostty_command_editor_free(self);
  return NULL;
}

const char* ghostty_command_editor_socket(GhosttyCommandEditor* self) {
  return self->path;
}

gboolean ghostty_command_editor_focus(GhosttyCommandEditor* self) {
  return self->active && gtk_widget_grab_focus(GTK_WIDGET(self->view));
}

gboolean ghostty_command_editor_forward_key(GhosttyCommandEditor* self,
                                            GtkEventControllerKey* controller,
                                            guint key,
                                            GdkModifierType mods) {
  if (!self->active)
    return FALSE;
  /* Clicking output still allows scrollback selection/copy. Other terminal
   * keys must go to the editor, never accumulate in the blocked ZLE's PTY.
   */
  if ((mods & (GDK_CONTROL_MASK | GDK_SHIFT_MASK)) ==
          (GDK_CONTROL_MASK | GDK_SHIFT_MASK) &&
      (key == GDK_KEY_C || key == GDK_KEY_c))
    return FALSE;
  gtk_widget_grab_focus(GTK_WIDGET(self->view));
  gtk_event_controller_key_forward(controller, GTK_WIDGET(self->view));
  return TRUE;
}

void ghostty_command_editor_set_readonly(GhosttyCommandEditor* self,
                                         gboolean readonly) {
  self->readonly = readonly;
  gtk_text_view_set_editable(self->view,
                             self->active && !self->pending && !readonly);
}

void ghostty_command_editor_free(GhosttyCommandEditor* self) {
  if (!self)
    return;
  if (self->timer)
    g_source_remove(self->timer);
  if (self->service) {
    g_socket_service_stop(self->service);
    g_socket_listener_close(G_SOCKET_LISTENER(self->service));
    g_signal_handlers_disconnect_by_data(self->service, self);
    g_clear_object(&self->service);
  }
  disconnect_client(self);
  /* Children are owned by slot, but callbacks must not outlive self. */
  if (self->view) {
    g_signal_handlers_disconnect_by_data(self->keys, self);
    GtkWidget* child;
    while ((child = gtk_widget_get_first_child(GTK_WIDGET(self->slot))))
      gtk_box_remove(self->slot, child);
  }
  if (self->path)
    g_unlink(self->path);
  if (self->directory)
    g_rmdir(self->directory);
  g_free(self->path);
  g_free(self->directory);
  g_byte_array_unref(self->input);
  g_byte_array_unref(self->output);
  g_free(self);
}

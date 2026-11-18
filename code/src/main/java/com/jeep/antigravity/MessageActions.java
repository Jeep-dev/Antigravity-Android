package com.jeep.antigravity;

import android.app.Dialog;
import android.content.Intent;
import android.os.Build;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

/** What can be done with a message: its menu, selecting text, translating, putting a message of the user's back in the box. */
final class MessageActions extends Part {
    MessageActions(MainActivity app) { super(app); }

    /** A message pressed long: a small card where the finger is, in place of selecting its text. */
    void messageMenu(Chat.Msg m) {
        app.screen.list.performHapticFeedback(android.view.HapticFeedbackConstants.LONG_PRESS);
        Chat chat = app.current;
        boolean user = m.kind == Chat.Msg.USER;
        int shade = dp(24); // room for the card's whole shadow
        FrameLayout room = new FrameLayout(app);
        room.setPadding(shade, shade, shade, shade);
        LinearLayout card = sheets.menuCard();
        room.addView(card, new FrameLayout.LayoutParams(-2, -2));
        android.widget.PopupWindow pop = new android.widget.PopupWindow(room, -2, -2, true);
        pop.setBackgroundDrawable(new android.graphics.drawable.ColorDrawable(0));
        pop.setOutsideTouchable(true);
        pop.setElevation(0); // the window's own shadow is square: only the card's, round, is drawn
        // Selecting is what is done most; it shows the Markdown, so a table's cells can be selected too.
        sheets.menuItem(card, pop, R.drawable.ic_edit_lines, "选择文字", null, () -> selectSheet(user ? m.text : replyText(m, false)));
        if (user) {
            sheets.menuItem(card, pop, R.drawable.ic_copy, "复制", null, () -> app.copy(m.text));
        } else {
            sheets.menuItem(card, pop, R.drawable.ic_file, "复制 Markdown", null, () -> app.copy(replyText(m, false)));
            sheets.menuItem(card, pop, R.drawable.ic_copy, "复制文字", null, () -> app.copy(replyText(m, true)));
        }
        // agy cannot go back to a message nor take one out: a message of the user's is put back in
        // the box instead, to be changed and sent again as a new one.
        if (user) sheets.menuItem(card, pop, R.drawable.ic_pencil, "放进输入框", null, () -> {
            String typed = app.box.input.getText().toString();
            app.box.input.setText(typed.trim().isEmpty() ? m.text : typed + "\n" + m.text);
            app.box.input.setSelection(app.box.input.length());
            app.box.input.requestFocus();
            app.box.showKeyboard();
        });
        // Where the finger was, kept on the screen: above it when there is no room below.
        room.measure(View.MeasureSpec.UNSPECIFIED, View.MeasureSpec.UNSPECIFIED);
        int w = room.getMeasuredWidth(), h = room.getMeasuredHeight();
        int screenW = app.getResources().getDisplayMetrics().widthPixels, screenH = app.getResources().getDisplayMetrics().heightPixels;
        int x = Math.max(0, Math.min((int) app.screen.touchX - w / 2, screenW - w));
        int y = (int) app.screen.touchY + h > screenH - dp(24) ? (int) app.screen.touchY - h : (int) app.screen.touchY;
        pop.showAtLocation(app.screen.list, Gravity.NO_GRAVITY, x, Math.max(0, y));
        // It grows out of the finger.
        card.setPivotX(Math.max(0, Math.min(w - 2 * shade, app.screen.touchX - x - shade)));
        card.setPivotY(Math.max(0, Math.min(h - 2 * shade, app.screen.touchY - Math.max(0, y) - shade)));
        card.setAlpha(0f);
        card.setScaleX(0.8f);
        card.setScaleY(0.8f);
        card.animate().alpha(1f).scaleX(1f).scaleY(1f).setDuration(250)
                .setInterpolator(Motion.EMPHASIZED).start();
    }

    /** A reply's text blocks: as shown (plain) or as written (Markdown). */
    private String replyText(Chat.Msg m, boolean plain) {
        StringBuilder s = new StringBuilder();
        for (Chat.Block b : m.blocks) {
            if (b.kind != Chat.Block.TEXT) continue;
            if (s.length() > 0) s.append("\n\n");
            s.append(plain ? app.md.plain(b.text) : b.text);
        }
        return s.toString();
    }

    /** The text in a sheet, to select a part of it. */
    private void selectSheet(String text) {
        Dialog sheet = sheets.sheet("选择文字", null);
        TextView t = Ui.text(app, text, 15.5f, R.color.text);
        t.setLineSpacing(0, 1.1f);
        t.setTextIsSelectable(true);
        sheets.content(sheet).addView(t, Ui.fill());
        // The sheet reaches the screen's bottom edge (no navigation bar): the text, and the scroll it
        // is in, end above the system's gesture strip, or a swipe there is the system's (one-handed
        // mode), and with room under them.
        View box = ((ViewGroup) sheet.findViewById(android.R.id.content)).getChildAt(0);
        android.view.WindowInsets in = app.getWindow().getDecorView().getRootWindowInsets();
        int gestures = in != null && Build.VERSION.SDK_INT >= 29 ? in.getMandatorySystemGestureInsets().bottom : 0;
        box.setPadding(box.getPaddingLeft(), box.getPaddingTop(), box.getPaddingRight(), box.getPaddingBottom() + gestures);
        sheet.show();
    }

    /**
     * The text in Google Translate, as its 翻译 for selected text does (shown over the app, nothing
     * replaced); without it, the apps that take selected text, to pick one.
     */
    void translate(String text) {
        String s = text.trim();
        if (s.isEmpty()) return;
        // Far more than it translates at once, and within what an intent may carry.
        if (s.length() > 100_000) s = s.substring(0, 100_000);
        Intent i = new Intent(Intent.ACTION_PROCESS_TEXT).setType("text/plain")
                .putExtra(Intent.EXTRA_PROCESS_TEXT, s).putExtra(Intent.EXTRA_PROCESS_TEXT_READONLY, true);
        try {
            app.startActivity(new Intent(i).setPackage("com.google.android.apps.translate"));
        } catch (android.content.ActivityNotFoundException e) {
            try {
                app.startActivity(Intent.createChooser(i, "翻译"));
            } catch (RuntimeException e2) {
                toast("没有可以翻译的应用");
            }
        }
    }

}

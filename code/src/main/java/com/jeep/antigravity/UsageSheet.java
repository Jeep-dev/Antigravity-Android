package com.jeep.antigravity;

import android.app.Dialog;
import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.text.TextUtils;
import android.view.Gravity;
import android.view.View;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * 用量 (the top bar's menu): how full this conversation's context window is, and what it has used so
 * far (tokens, cost), as agy counts them (the bridge's /context: agy's session stats); and the account's
 * model quota, the five hours and the week of each group of models, as agy's /usage tells it (the
 * bridge's /usage), with the AI credits left.
 *
 * The context is one segmented bar of the window, a part per kind, each kind always in its own
 * colour, with a legend naming them; a limit is a meter of what is used, whose fill turns to warning,
 * then danger, as it runs out, said in words too.
 */
final class UsageSheet extends Part {
    /** The categorical palette (validated, light and dark), slots in a fixed order. */
    private static final int[] LIGHT = {0xFF2A78D6, 0xFFEB6834, 0xFF1BAF7A, 0xFFEDA100, 0xFFE87BA4, 0xFF008300, 0xFF6250D6, 0xFFE34948};
    private static final int[] DARK = {0xFF3987E5, 0xFFD95926, 0xFF199E70, 0xFFC98500, 0xFFD55181, 0xFF008300, 0xFF9085E9, 0xFFE66767};
    /** The kinds of what fills the context, each its own slot whatever else is there: a colour follows its kind. */
    private static final String[][] KINDS = {
            {"System prompt", "系统提示"}, {"System tools", "系统工具"}, {"MCP tools", "MCP 工具"},
            {"Custom agents", "自定义代理"}, {"Memory files", "记忆文件"}, {"Skills", "技能"},
            {"Messages", "消息"}, {"MCP server instructions", "MCP 服务说明"}};

    UsageSheet(MainActivity app) { super(app); }

    void show() {
        Dialog sheet = sheets.sheet("用量", null);
        LinearLayout box = sheets.content(sheet);

        label(box, "上下文");
        LinearLayout context = card(box);
        context.addView(waitingLine("正在读取…"));

        label(box, "本对话");
        LinearLayout stats = card(box);
        stats.addView(waitingLine("正在读取…"));

        label(box, "订阅额度");
        LinearLayout plan = card(box);
        plan.addView(waitingLine("正在读取…"));

        sheet.show();
        loadPlan(sheet, plan);

        Chat chat = app.current;
        bridge.post("/context", obj("chat", chat.key, "sessionId", chat.sessionId), (status, data) -> {
            if (!sheet.isShowing() || app.isFinishing() || app.isDestroyed()) return;
            context.removeAllViews();
            stats.removeAllViews();
            if (status != 200 || data == null) {
                context.addView(waitingLine("读取失败：还没连上 agy"));
                stats.addView(waitingLine("同上"));
                return;
            }
            JSONObject c = data.optJSONObject("context");
            if (c == null) {
                context.addView(waitingLine("这个对话在 agy 里还没打开：发一条消息后就能看到"));
                stats.addView(waitingLine("同上"));
                return;
            }
            fillContext(context, c);
            if (c.optJSONObject("stats") != null) fillStats(stats, c.optJSONObject("stats"));
            else stats.addView(waitingLine("暂无统计"));
        });
    }

    private void loadPlan(Dialog sheet, LinearLayout plan) {
        bridge.get("/usage", (status, data) -> {
            if (!sheet.isShowing() || app.isFinishing() || app.isDestroyed()) return;
            plan.removeAllViews();
            if (status != 200 || data == null) { plan.addView(waitingLine("读取失败：还没连上 agy")); return; }
            String error = data.optString("error");
            if (error.equals("login")) plan.addView(waitingLine("agy 还没登录：在 设置 → 账号 用 Google 登录后就能看到"));
            else if (error.equals("apiKey")) plan.addView(waitingLine("用 Gemini API Key 时没有这些额度"));
            else if (!error.isEmpty()) plan.addView(waitingLine("读取失败（" + error + "）"));
            else fillPlan(plan, data.optJSONArray("groups"), data.isNull("credits") ? -1 : data.optDouble("credits", -1));
        });
    }

    // ---- 上下文 ----

    private void fillContext(LinearLayout card, JSONObject c) {
        long total = Math.max(0, c.optLong("totalTokens")), rawMax = c.optLong("maxTokens");
        if (rawMax <= 0) {
            card.addView(waitingLine(total > 0 ? "已用 " + tokens(total) + "（窗口上限未知）" : "尚未上报上下文窗口"));
            return;
        }
        long max = rawMax;
        int[] palette = Themes.dark(app) ? DARK : LIGHT;

        // The headline: the share of the window in use, and how much of how much.
        LinearLayout head = Ui.row(app);
        head.setGravity(Gravity.BOTTOM);
        TextView pct = Ui.text(app, Math.round(100.0 * total / max) + "%", 30, R.color.text);
        pct.setTypeface(Ui.MEDIUM);
        head.addView(pct);
        head.addView(new View(app), new LinearLayout.LayoutParams(0, 1, 1));
        TextView of = Ui.text(app, tokens(total) + " / " + tokens(max), 14, R.color.text2);
        of.setPadding(0, 0, 0, dp(5));
        head.addView(of);
        card.addView(head, Ui.fill());

        // The parts in use, each in its kind's colour; the buffer kept for compacting in grey; the rest free.
        List<long[]> parts = new ArrayList<>();
        List<String> names = new ArrayList<>();
        long buffer = 0;
        JSONArray cats = c.optJSONArray("categories");
        for (int i = 0; cats != null && i < cats.length(); i++) {
            JSONObject k = cats.optJSONObject(i);
            if (k == null || k.optBoolean("deferred")) continue; // not loaded: in no part of the window
            String name = k.optString("name");
            long t = k.optLong("tokens");
            if (name.equals("Free space")) continue;
            if (name.equals("Autocompact buffer")) { buffer = t; continue; }
            if (t <= 0) continue;
            parts.add(new long[]{t, slot(name)});
            names.add(name);
        }
        int bufferColor = Ui.color(app, R.color.borderStrong);
        Segments bar = new Segments(app, max, Ui.color(app, R.color.surface2));
        for (long[] p : parts) bar.add(p[0], palette[(int) p[1]]);
        if (buffer > 0) bar.add(buffer, bufferColor);
        bar.setContentDescription("上下文已用 " + tokens(total) + "，共 " + tokens(max));
        card.addView(bar, new LinearLayout.LayoutParams(-1, dp(10)));
        Ui.margins(bar, app, 0, 10, 0, 6);

        // The legend, in two columns (all of the sheet on one screen): each part's name and amount, in the bar's order.
        List<Object[]> items = new ArrayList<>();
        for (int i = 0; i < parts.size(); i++) items.add(new Object[]{palette[(int) parts.get(i)[1]], chinese(names.get(i)), parts.get(i)[0]});
        if (buffer > 0) items.add(new Object[]{bufferColor, "自动压缩预留", buffer});
        items.add(new Object[]{Ui.color(app, R.color.surface2), "空闲", Math.max(0, max - total - buffer)});
        int rows = (items.size() + 1) / 2;
        for (int r = 0; r < rows; r++) {
            LinearLayout line = Ui.row(app);
            for (int col = 0; col < 2; col++) {
                int i = col * rows + r; // down the first column, then the second: the bar's order
                LinearLayout cell = i < items.size()
                        ? legend((int) items.get(i)[0], (String) items.get(i)[1], (long) items.get(i)[2]) : Ui.row(app);
                LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0, -2, 1);
                if (col == 0) lp.setMargins(0, 0, dp(14), 0);
                line.addView(cell, lp);
            }
            card.addView(line, Ui.fill());
        }
    }

    private static int slot(String name) {
        for (int i = 0; i < KINDS.length; i++) if (KINDS[i][0].equals(name)) return i;
        return 7; // anything else: the last slot, shared
    }

    private static String chinese(String name) {
        for (String[] k : KINDS) if (k[0].equals(name)) return k[1];
        return name;
    }

    private LinearLayout legend(int color, String name, long tokens) {
        LinearLayout row = Ui.row(app);
        row.setPadding(0, dp(4), 0, dp(4));
        View dot = new View(app);
        dot.setBackground(Ui.round(color, dp(3), dp(1), Ui.color(app, R.color.border)));
        row.addView(dot, new LinearLayout.LayoutParams(dp(10), dp(10)));
        TextView n = Ui.text(app, name, 13.5f, R.color.text);
        n.setPadding(dp(8), 0, dp(4), 0);
        n.setSingleLine(true);
        n.setEllipsize(TextUtils.TruncateAt.END);
        row.addView(n, Ui.weight());
        row.addView(Ui.text(app, tokens(tokens), 13.5f, R.color.text2));
        return row;
    }

    // ---- 本对话 ----

    /** What this conversation has used so far, as agy counts it (its session stats): tokens and cost. */
    private void fillStats(LinearLayout card, JSONObject stats) {
        JSONObject t = stats.optJSONObject("tokens");
        if (t == null) t = new JSONObject();
        statRow(card, "输入", tokens(t.optLong("input")));
        statRow(card, "输出", tokens(t.optLong("output")));
        if (t.optLong("thinking") > 0) statRow(card, "思考", tokens(t.optLong("thinking")));
        statRow(card, "缓存读取", tokens(t.optLong("cacheRead")));
        if (t.optLong("cacheWrite") > 0) statRow(card, "缓存写入", tokens(t.optLong("cacheWrite")));
        double cost = stats.optDouble("cost", 0);
        if (cost > 0) statRow(card, "花费", String.format(Locale.ROOT, cost < 0.01 ? "$%.4f" : "$%.2f", cost));
        statRow(card, "消息", stats.optInt("userMessages") + " 条提问 · " + stats.optInt("toolCalls") + " 次工具调用");
    }

    private void statRow(LinearLayout card, String name, String value) {
        LinearLayout row = Ui.row(app);
        row.setPadding(0, dp(5), 0, dp(5));
        row.addView(Ui.text(app, name, 14, R.color.text), Ui.weight());
        row.addView(Ui.text(app, value, 14, R.color.text2));
        card.addView(row, Ui.fill());
    }

    // ---- 订阅额度 ----

    /** Each group of models sharing a quota: its name and models, then a meter per limit, of what is used. */
    private void fillPlan(LinearLayout card, JSONArray groups, double credits) {
        boolean any = false;
        for (int i = 0; groups != null && i < groups.length(); i++) {
            JSONObject g = groups.optJSONObject(i);
            JSONArray limits = g == null ? null : g.optJSONArray("limits");
            if (limits == null || limits.length() == 0) continue;
            if (any) card.addView(new View(app), new LinearLayout.LayoutParams(1, dp(16)));
            any = true;
            TextView name = Ui.text(app, groupName(g.optString("name")), 15, R.color.text);
            name.setTypeface(Ui.MEDIUM);
            card.addView(name, Ui.fill());
            if (!g.optString("models").isEmpty()) {
                TextView models = Ui.text(app, g.optString("models"), 12.5f, R.color.text3);
                models.setPadding(0, dp(2), 0, 0);
                card.addView(models, Ui.fill());
            }
            for (int k = 0; k < limits.length(); k++) {
                JSONObject l = limits.optJSONObject(k);
                if (l != null) limit(card, l);
            }
        }
        if (!any) card.addView(waitingLine("agy 没有说这个账号的额度"));
        if (credits >= 0) {
            if (any) card.addView(new View(app), new LinearLayout.LayoutParams(1, dp(10)));
            statRow(card, "AI 积分（额度用完后使用）", String.format(Locale.ROOT, credits == Math.rint(credits) ? "%.0f" : "%.2f", credits));
        }
    }

    private void limit(LinearLayout card, JSONObject l) {
        boolean known = !l.isNull("remaining");
        double left = known ? l.optDouble("remaining", 1) : 1;
        int used = !known ? 0 : left <= 0 ? 100 : (int) Math.max(0, Math.min(99, Math.round(100 * (1 - left))));
        // The fill carries how near it is to running out; words say it too.
        int fill, state = 0;
        if (known && left <= 0) { fill = R.color.danger; state = 2; }
        else if (used >= 80) { fill = R.color.warn; state = 1; }
        else fill = R.color.accent;

        LinearLayout head = Ui.row(app);
        head.setPadding(0, dp(10), 0, 0);
        String window = l.optString("window");
        TextView name = Ui.text(app, "5h".equals(window) ? "5 小时额度" : "weekly".equals(window) ? "每周额度" : l.optString("name"), 14, R.color.text);
        head.addView(name, Ui.weight());
        if (state > 0) {
            TextView s = Ui.text(app, state == 2 ? "已用完" : "快用完了", 12.5f, fill);
            s.setPadding(0, 0, dp(8), 0);
            head.addView(s);
        }
        head.addView(Ui.text(app, known ? "已用 " + used + "%" : "未知", 14, R.color.text2));
        card.addView(head, Ui.fill());

        int fillColor = Ui.color(app, fill);
        Segments meter = new Segments(app, 100, Glass.alpha(fillColor, 0.18f));
        if (known) meter.add(used, fillColor);
        meter.setContentDescription(name.getText() + (known ? " 已用 " + used + "%" : " 未知"));
        card.addView(meter, new LinearLayout.LayoutParams(-1, dp(8)));
        Ui.margins(meter, app, 0, 7, 0, 5);
        String resets = resets(l.optString("resetsAt"));
        if (!resets.isEmpty()) card.addView(Ui.text(app, resets, 12.5f, R.color.text3));
    }

    private static String groupName(String name) {
        switch (name) {
            case "Gemini Models": return "Gemini 模型";
            case "Claude and GPT models": return "Claude 和 GPT 模型";
            default: return name;
        }
    }

    /** When a limit is full again: in minutes or hours when near, else the day and time, in the phone's zone. */
    private static String resets(String iso) {
        if (iso == null || iso.isEmpty() || iso.equals("null")) return "";
        try {
            java.time.OffsetDateTime at = java.time.OffsetDateTime.parse(iso);
            long mins = java.time.Duration.between(java.time.OffsetDateTime.now(), at).toMinutes();
            if (mins <= 0) return "即将重置";
            if (mins < 60) return mins + " 分钟后重置";
            if (mins < 24 * 60) return (mins / 60) + " 小时 " + (mins % 60) + " 分钟后重置";
            java.time.ZonedDateTime local = at.atZoneSameInstant(java.time.ZoneId.systemDefault());
            String[] week = {"周一", "周二", "周三", "周四", "周五", "周六", "周日"};
            return String.format(Locale.ROOT, "%d月%d日 %s %02d:%02d 重置", local.getMonthValue(), local.getDayOfMonth(),
                    week[local.getDayOfWeek().getValue() - 1], local.getHour(), local.getMinute());
        } catch (RuntimeException e) {
            return "";
        }
    }

    // ---- 共用 ----

    private void label(LinearLayout box, String text) {
        TextView t = Ui.text(app, text, 14, R.color.text2);
        t.setTypeface(Ui.MEDIUM);
        t.setPadding(dp(6), dp(box.getChildCount() > 0 ? 14 : 0), 0, dp(6));
        box.addView(t);
    }

    private LinearLayout card(LinearLayout box) {
        LinearLayout card = Ui.column(app);
        card.setPadding(dp(16), dp(12), dp(16), dp(13));
        card.setBackground(Ui.round(col(R.color.surface), dp(18), dp(1), col(R.color.border)));
        box.addView(card, Ui.fill());
        return card;
    }

    private TextView waitingLine(String text) {
        TextView t = Ui.text(app, text, 14, R.color.text2);
        t.setLineSpacing(0, 1.2f);
        return t;
    }

    /** 1,234 → 1.2k, 1,000,000 → 1m, 1,048,576 → 1.05m. */
    static String tokens(long n) {
        if (n < 1000) return String.valueOf(n);
        if (n < 999_950) return trim(n / 1000.0, 1) + "k";
        return trim(n / 1_000_000.0, 2) + "m";
    }

    private static String trim(double v, int places) {
        String s = String.format(Locale.ROOT, places == 2 ? "%.2f" : "%.1f", v);
        while (s.contains(".") && (s.endsWith("0") || s.endsWith("."))) s = s.substring(0, s.length() - 1);
        return s;
    }

    /**
     * One whole split into parts, left to right: each part as wide as its share, a 2 dp gap of the surface
     * between parts, the ends round; what is left is the track. A part at zero draws nothing.
     */
    private static final class Segments extends View {
        private final long whole;
        private final int track, surface;
        private final List<long[]> parts = new ArrayList<>();
        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Path clip = new Path();
        private final RectF r = new RectF();

        Segments(Context c, long whole, int track) {
            super(c);
            this.whole = Math.max(1, whole);
            this.track = track;
            this.surface = Ui.color(c, R.color.surface);
        }

        void add(long amount, int color) {
            if (amount > 0) parts.add(new long[]{amount, color});
        }

        @Override protected void onDraw(Canvas canvas) {
            float w = getWidth(), h = getHeight(), gap = 2 * getResources().getDisplayMetrics().density;
            clip.reset();
            r.set(0, 0, w, h);
            clip.addRoundRect(r, h / 2, h / 2, Path.Direction.CW);
            canvas.save();
            canvas.clipPath(clip);
            paint.setColor(track);
            canvas.drawRect(r, paint);
            float x = 0, least = Math.max(gap * 1.5f, h / 3);
            for (int i = 0; i < parts.size(); i++) {
                float width = Math.max(least, w * parts.get(i)[0] / whole);
                float end = Math.min(w, x + width);
                paint.setColor((int) parts.get(i)[1]);
                canvas.drawRect(x, 0, Math.max(x, end - (end < w ? gap : 0)), h, paint);
                // The gap after each part shows the card's surface.
                if (end < w) { paint.setColor(surface); canvas.drawRect(end - gap, 0, end, h, paint); }
                x = end;
                if (x >= w) break;
            }
            canvas.restore();
        }
    }
}

package com.jeep.antigravity;

import android.app.Dialog;
import android.view.WindowManager;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONObject;

/**
 * Settings → 账号: how agy signs in. With a Google account, as `agy acp --login` does it in a
 * terminal (the bridge runs it): a link opened in the browser, then the code the page shows pasted
 * here. Or with a Gemini API key (kept by the bridge, given to agy as GEMINI_API_KEY). Then the
 * models agy offers.
 */
final class AccountPage extends Part {
    private boolean loggingIn;

    AccountPage(MainActivity app) { super(app); }

    /** Who agy is signed in as, as the bridge last said. */
    JSONObject account() {
        try {
            return new JSONObject(prefs.getString("account", "{}"));
        } catch (org.json.JSONException e) {
            return new JSONObject();
        }
    }

    void fill(LinearLayout box) {
        JSONObject who = account();
        boolean google = "google".equals(who.optString("method"));
        boolean key = "apiKey".equals(who.optString("method"));
        app.settings.section(box, "Google 账号");
        LinearLayout g = app.settings.card(box);
        if (google) {
            String email = who.optString("email");
            sheets.option(g, R.drawable.ic_check, email.isEmpty() ? "已登录" : email, email.isEmpty() ? null : "已登录", false, null);
            sheets.option(g, R.drawable.ic_refresh, "换一个账号", null, false, this::login);
            sheets.option(g, R.drawable.ic_x, "退出登录", null, true, this::logoutAsk);
        } else {
            sheets.option(g, R.drawable.ic_globe, "用 Google 登录", key ? "现在用的是 API Key" : "个人账号可以免费用", false, this::login);
        }
        app.settings.section(box, "Gemini API Key");
        LinearLayout k = app.settings.card(box);
        sheets.option(k, R.drawable.ic_link, "API Key", key ? "已填（正在使用）" : "没有填", false, this::keySheet);

        app.settings.section(box, "模型");
        LinearLayout models = app.settings.card(box);
        int n = agent.claudeModels.length();
        sheets.option(models, R.drawable.ic_spark, "可用模型", n == 0 ? "登录后显示" : String.valueOf(n), false, this::modelsSheet);
        sheets.option(models, R.drawable.ic_terminal, "在终端里登录", "企业账号、Vertex 等其他方式", false, () -> {
            toast("在终端里运行 agy，按提示登录；登录后回来发消息即可");
            app.openTerminal();
        });
    }

    // ---- Google: the link, then the code ----

    private void login() {
        if (!app.link.linked) { toast("后台还没启动好"); return; }
        if (loggingIn) return;
        loggingIn = true;
        toast("正在打开登录页…");
        bridge.post("/login", new JSONObject(), (status, data) -> {
            loggingIn = false;
            if (app.isFinishing() || app.isDestroyed()) return;
            if (status != 200) {
                toast("没能开始登录：" + data.optString("error", "HTTP " + status));
                return;
            }
            String url = data.optString("url");
            app.openUrl(url);
            codeSheet(url);
        });
    }

    private void codeSheet(String url) {
        Dialog sheet = sheets.sheet("粘贴授权码", "在浏览器里用 Google 账号登录并同意授权，页面上会显示一串授权码：复制下来，贴在这里（贴整个网址也行）。");
        EditText code = sheets.field(sheet, "授权码", "", "4/0A…");
        LinearLayout again = sheets.card(sheet);
        Ui.margins(again, app, 0, 12, 0, 0);
        sheets.option(again, R.drawable.ic_globe, "重新打开登录页", null, false, () -> app.openUrl(url));
        sheets.option(again, R.drawable.ic_copy, "复制登录链接", "在别的浏览器里打开", false, () -> app.copy(url));
        TextView cancel = sheets.button("取消", false, v -> sheet.dismiss());
        TextView ok = sheets.button("登录", true, null);
        ok.setOnClickListener(v -> {
            String c = code.getText().toString().trim();
            if (c.isEmpty()) { toast("先粘贴授权码"); return; }
            ok.setEnabled(false);
            ok.setText("正在登录…");
            bridge.post("/login-code", obj("code", c), (status, data) -> {
                ok.setEnabled(true);
                ok.setText("登录");
                if (status == 200) {
                    sheet.dismiss();
                    toast("已登录：" + data.optString("email"));
                    refreshConfig(4);
                } else {
                    String why = data.optString("error", "HTTP " + status);
                    if (why.contains("重新开始")) sheet.dismiss();
                    toast("没能登录：" + why);
                }
            });
        });
        ok.setBackground(Ui.press(app, Ui.round(col(R.color.text), dp(10)), dp(10)));
        ok.setTextColor(col(R.color.bg));
        sheets.actions(sheet, cancel, ok);
        if (sheet.getWindow() != null) sheet.getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        sheet.show();
    }

    private void logoutAsk() {
        Dialog ask = sheets.sheet("退出 Google 账号？", "退出后要重新登录才能发消息；对话记录不会删。");
        TextView cancel = sheets.button("取消", false, v -> ask.dismiss());
        TextView ok = sheets.button("退出", true, v -> {
            ask.dismiss();
            bridge.post("/logout", new JSONObject(), (status, data) -> {
                if (status != 200) { toast("没能退出：" + data.optString("error", "HTTP " + status)); return; }
                toast("已退出");
                refreshConfig(1);
            });
        });
        sheets.actions(ask, cancel, ok);
        ask.show();
    }

    // ---- Gemini API key ----

    private void keySheet() {
        if (!app.link.linked) { toast("后台还没启动好"); return; }
        boolean has = "apiKey".equals(account().optString("method"));
        Dialog sheet = sheets.sheet("Gemini API Key", has ? "已经填过；填新的会替换它" : "在 Google AI Studio 里创建。填了以后，agy 用这个 Key 而不是 Google 账号");
        EditText key = sheets.field(sheet, "API Key", "", has ? "（不改就留空）" : "AIza…");
        TextView remove = sheets.button(has ? "删除" : "取消", false, null);
        TextView ok = sheets.button("保存", true, null);
        remove.setOnClickListener(v -> {
            if (!has) { sheet.dismiss(); return; }
            saveKey("", sheet, remove, ok);
        });
        ok.setOnClickListener(v -> {
            String k = key.getText().toString().trim();
            if (k.isEmpty()) { sheet.dismiss(); return; }
            saveKey(k, sheet, remove, ok);
        });
        ok.setBackground(Ui.press(app, Ui.round(col(R.color.text), dp(10)), dp(10)));
        ok.setTextColor(col(R.color.bg));
        sheets.actions(sheet, remove, ok);
        if (sheet.getWindow() != null) sheet.getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        sheet.show();
    }

    private void saveKey(String key, Dialog sheet, TextView remove, TextView ok) {
        remove.setEnabled(false);
        ok.setEnabled(false);
        bridge.post("/auth", obj("provider", "gemini", "key", key), (status, data) -> {
            remove.setEnabled(true);
            ok.setEnabled(true);
            if (app.isFinishing() || app.isDestroyed()) return;
            if (status != 200) { toast("保存失败：" + data.optString("error", "HTTP " + status)); return; }
            toast(key.isEmpty() ? "已删除" : "已保存");
            sheet.dismiss();
            refreshConfig(4);
        });
    }

    /** The bridge reads agy's models again a moment after a sign-in: asked a few times until it has. */
    void refreshConfig(int tries) {
        if (app.isFinishing() || app.isDestroyed()) return;
        bridge.get("/config", (status, data) -> {
            if (status != 200 || app.isFinishing() || app.isDestroyed()) return;
            int before = agent.claudeModels.length();
            agent.applyConfig(data);
            app.settings.refreshSettings();
            if (tries > 1 && agent.claudeModels.length() == before) ui.postDelayed(() -> refreshConfig(tries - 1), 2500);
        });
    }

    private void modelsSheet() {
        Dialog sheet = sheets.sheet("可用模型", "在输入框下方的模型按钮或 /model 里选");
        LinearLayout card = sheets.card(sheet);
        for (int i = 0; i < agent.claudeModels.length(); i++) {
            JSONObject m = agent.claudeModels.optJSONObject(i);
            if (m != null) sheets.option(card, 0, m.optString("displayName"), m.optString("provider"), false, null);
        }
        if (agent.claudeModels.length() == 0) sheets.empty(card, "还没有：先登录");
        sheet.show();
    }
}

package com.jeep.antigravity;

import android.widget.LinearLayout;

import org.json.JSONException;
import org.json.JSONObject;

/** Settings → 扩展: skills, MCP servers (mcp_config.json) and agy's own settings.json. */
final class ExtPage extends Part {
    /** The 扩展 page's count of skills, read off the main thread; null until read. */
    private Integer skillCount;

    ExtPage(MainActivity app) { super(app); }

    void fillExt(LinearLayout box, JSONObject launch) {
        Integer n = skillCount;
        skillCount = null; // read again next time
        if (n == null) {
            work.execute(() -> {
                int read = app.skills.skillList().size();
                ui.post(() -> {
                    if (!"ext".equals(app.settings.settingsAt) || app.settings.settingsPage == null) return;
                    skillCount = read;
                    app.settings.fillSettings(app.link.linked ? app.settings.launchCache : null);
                });
            });
        }
        LinearLayout ext = app.settings.card(box);
        sheets.option(ext, R.drawable.ic_spark, "技能", n == null || n == 0 ? null : String.valueOf(n), false, app.skills::skillsSheet);
        sheets.option(ext, R.drawable.ic_tool, "MCP 服务器", "~/.gemini/config/mcp_config.json", false,
                () -> rawSheet(userland.file(Userland.HOME + "/.gemini/config/mcp_config.json"), "{\n  \"mcpServers\": {\n  }\n}\n"));
        sheets.option(ext, R.drawable.ic_gear, "settings.json", "agy 的设置（权限规则、模型等）", false,
                () -> rawSheet(agent.settingsFile(), "{\n}\n"));
    }

    /** One of agy's JSON files, edited whole; one that does not read as JSON is not saved. agy
     *  reads it when it starts: it is started again once no reply runs. */
    void rawSheet(java.io.File f, String empty) {
        String text = f.exists() ? agent.readFile(f) : empty;
        sheets.textSheet(f.getName(), text, "{ }", true, (t, done) -> {
            String out = t.trim().isEmpty() ? "{}" : t;
            try {
                new JSONObject(out);
            } catch (JSONException e) {
                toast("不是有效的 JSON：" + e.getMessage());
                return;
            }
            try {
                Userland.replace(f, out.endsWith("\n") ? out : out + "\n");
            } catch (Exception e) {
                toast("保存失败：" + e.getMessage());
                return;
            }
            agent.relaunch();
            toast("已保存");
            done.run();
            app.account.refreshConfig(3);
        }, app.settings::refreshSettings);
    }
}

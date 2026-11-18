package com.jeep.antigravity;

import android.app.Dialog;
import android.net.Uri;
import android.view.Gravity;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/** The sheets where the folder, the model and thinking level and AGENTS.md are picked or written. */
final class Pickers extends Part {
    Pickers(MainActivity app) { super(app); }

    private static String shortModel(String m) {
        return m == null || m.isEmpty() ? "默认模型" : m.replaceAll("(?i)\\[1m\\]$", "");
    }

    /** The model in use and its thinking level, as the box's pill shows them ("Opus 5.5 中等"). */
    String modelLabel() {
        String name = null, value = agent.model;
        if (value.isEmpty()) {
            // agy's own choice: the model its "default" stands for, by that one's name.
            String resolved = "";
            for (int i = 0; i < agent.claudeModels.length(); i++) {
                JSONObject cm = agent.claudeModels.optJSONObject(i);
                if (cm != null && cm.optString("value").equals("default")) resolved = cm.optString("resolvedModel");
            }
            for (int i = 0; i < agent.claudeModels.length() && name == null; i++) {
                JSONObject cm = agent.claudeModels.optJSONObject(i);
                if (cm != null && !cm.optString("value").equals("default") && !resolved.isEmpty()
                        && resolved.equals(cm.optString("resolvedModel"))) name = listedName(cm);
            }
            // agy's own choice: its default model (settings.json), by its name when listed.
            for (int i = 0; i < agent.claudeModels.length() && name == null && !agent.defaultModel.isEmpty(); i++) {
                JSONObject cm = agent.claudeModels.optJSONObject(i);
                if (cm != null && cm.optString("value").equals(agent.defaultModel)) name = listedName(cm);
            }
            if (name == null) name = agent.defaultModel.isEmpty() ? "默认模型" : shortModel(agent.defaultModel);
        } else {
            for (int i = 0; i < agent.claudeModels.length() && name == null; i++) {
                JSONObject cm = agent.claudeModels.optJSONObject(i);
                if (cm != null && cm.optString("value").equals(value)) name = listedName(cm);
            }
            if (name == null) name = shortModel(value);
        }
        String level = agent.effortsOf(agent.model).contains(agent.effortInUse()) ? effortName(agent.effortInUse()) : "";
        String label = level.isEmpty() ? name : name + " " + level;
        // A mode that changes what happens without asking is shown with the model.
        if ("plan".equals(agent.mode) || "bypassPermissions".equals(agent.mode)) label += " · " + modeName(agent.mode);
        return label;
    }

    /** The permission modes: id, name, what it does. */
    private static final String[][] MODES = {
            {"default", "每次询问", "都先问"},
            {"acceptEdits", "自动改文件", "命令还会问"},
            {"auto", "自动", "只问不可逆的"},
            {"plan", "规划", "先写计划"},
            {"bypassPermissions", "全部允许", "什么都不问"},
    };

    String modeName(String mode) {
        for (String[] m : MODES) if (m[0].equals(mode)) return m[1];
        return "默认";
    }

    /** 权限: how much agy does without asking, for the next messages. */
    void modeSheet(Runnable after) {
        Dialog sheet = sheets.sheet("权限", "agy 运行命令、改文件之前要不要先问你，从下一条消息起生效。"
                + "自动改文件：改工作目录里的文件不问；自动：安全、可撤销的直接做；规划：先研究、写出计划再动手；"
                + "全部允许：只在信得过的目录里用。");
        LinearLayout card = sheets.card(sheet);
        List<String> offered = new ArrayList<>(agent.permissionModes);
        if (offered.isEmpty()) offered.add("default");
        for (String[] m : MODES) {
            if (!offered.contains(m[0])) continue;
            sheets.option(card, 0, m[1], m[2], "bypassPermissions".equals(m[0]), () -> {
                agent.mode = m[0];
                prefs.edit().putString("mode", agent.mode).apply();
                sheet.dismiss();
                app.screen.updateChrome();
                if (after != null) after.run();
            });
            if (m[0].equals(agent.mode) || (agent.mode.isEmpty() && "default".equals(m[0]))) sheets.check(card);
        }
        sheet.show();
    }

    /** A model of agy's list by its name with the version ("Opus 5.5"), as the model menu lists it. */
    private static String listedName(JSONObject cm) {
        String desc = cm.optString("description");
        return desc.contains(" · ") ? desc.substring(0, desc.indexOf(" · ")) : cm.optString("displayName", cm.optString("value"));
    }

    private static String effortName(String e) {
        switch (e == null ? "" : e.toLowerCase(Locale.ROOT)) {
            case "off": return "不思考";
            case "minimal": return "最低";
            case "low": return "低";
            case "medium": return "中等";
            case "high": return "高";
            case "xhigh": return "超高";
            case "max": return "最高";
            default: return e == null ? "" : e;
        }
    }

    void folderSheet(String path) {
        String at = path != null ? path : !agent.cwd.isEmpty() ? agent.cwd : agent.defaultCwd;
        bridge.get("/dirs?path=" + Uri.encode(at), (status, d) -> {
            if (status != 200) { toast("读取目录失败"); return; }
            String here = d.optString("path");
            Dialog sheet = sheets.sheet("工作目录", agent.tilde(here));
            LinearLayout card = sheets.card(sheet);
            String parent = d.isNull("parent") ? "" : d.optString("parent");
            if (!parent.isEmpty()) sheets.option(card, R.drawable.ic_up, "上一级", null, false, () -> { sheet.dismiss(); folderSheet(parent); });
            JSONArray dirs = d.optJSONArray("dirs");
            for (int i = 0; dirs != null && i < dirs.length(); i++) {
                String name = dirs.optString(i);
                sheets.option(card, R.drawable.ic_folder, name, null, false, () -> { sheet.dismiss(); folderSheet(here + "/" + name); });
            }
            if (dirs == null || dirs.length() == 0) {
                sheets.empty(card, "没有子目录");
            }
            LinearLayout field = Ui.row(app);
            EditText name = new EditText(app);
            name.setHint("新建目录名");
            name.setSingleLine(true);
            name.setTextSize(15);
            name.setTextColor(col(R.color.text));
            name.setHintTextColor(col(R.color.text3));
            name.setPadding(dp(12), 0, dp(12), 0);
            name.setBackground(Ui.round(col(R.color.surface), dp(10), dp(1), col(R.color.borderStrong)));
            field.addView(name, new LinearLayout.LayoutParams(0, dp(40), 1));
            TextView make = sheets.button("新建", false, v -> {
                String n = name.getText().toString().trim();
                if (n.isEmpty() || n.contains("/")) { toast("目录名不能为空，也不能带 /"); return; }
                bridge.post("/mkdir", obj("path", here + "/" + n), (s2, r2) -> {
                    if (s2 == 200) { sheet.dismiss(); folderSheet(here + "/" + n); } else toast("新建失败");
                });
            });
            field.addView(make);
            Ui.margins(make, app, 8, 0, 0, 0);
            sheets.content(sheet).addView(field, Ui.fill());
            Ui.margins(field, app, 0, 10, 0, 0);
            TextView cancel = sheets.button("取消", false, v -> sheet.dismiss());
            TextView use = sheets.button("使用这个目录", true, v -> {
                agent.cwd = here;
                prefs.edit().putString("cwd", agent.cwd).apply();
                if (app.current.sessionId == null) app.current.cwd = agent.cwd;
                sheet.dismiss();
                app.renderNow();
                app.drawer.renderSessions(); // the drawer may list only this folder's
                app.settings.refreshSettings();
            });
            use.setBackground(Ui.press(app, Ui.round(col(R.color.text), dp(10)), dp(10)));
            use.setTextColor(col(R.color.bg));
            sheets.actions(sheet, cancel, use);
            sheet.show();
        });
    }

    void modelSheet() {
        Dialog sheet = sheets.sheet("模型", null);
        // Thinking: one segmented bar.
        LinearLayout bar = Ui.row(app);
        bar.setPadding(dp(3), dp(3), dp(3), dp(3));
        bar.setBackground(Ui.round(col(R.color.surface2), dp(12)));
        String level = agent.effortInUse();
        List<String> levels = agent.effortsOf(agent.model);
        for (String e : levels) {
            boolean on = e.equals(level);
            // The model's levels in one row: each by its short Chinese name.
            TextView b = Ui.text(app, effortName(e), 12.5f, on ? R.color.text : R.color.text2);
            b.setGravity(Gravity.CENTER);
            b.setSingleLine(true);
            if (on) {
                b.setTypeface(Ui.MEDIUM);
                b.setBackground(Ui.round(col(R.color.bg), dp(9), dp(1), col(R.color.border)));
            }
            b.setOnClickListener(v -> {
                agent.effort = e;
                prefs.edit().putString("effort", agent.effort).apply();
                sheet.dismiss();
                app.screen.updateChrome();
            });
            bar.addView(b, new LinearLayout.LayoutParams(0, dp(34), 1));
        }
        if (levels.size() > 1) {
            sheets.content(sheet).addView(bar, Ui.fill());
            Ui.margins(bar, app, 0, 0, 0, 12);
        }
        // agy's own list first ("default": no --model, its own choice), then ones from the
        // settings and typed here; each once.
        LinearLayout own = null;
        List<String> listed = new ArrayList<>();
        for (int i = 0; i < agent.claudeModels.length(); i++) {
            JSONObject cm = agent.claudeModels.optJSONObject(i);
            if (cm == null) continue;
            String value = cm.optString("value");
            String resolved = cm.optString("resolvedModel");
            listed.add(value);
            listed.add(resolved);
            // "default" is one of the others under another name: not listed twice.
            if (value.equals("default")) continue;
            String pick = value;
            if (own == null) own = sheets.card(sheet);
            // Its full name with the version, as agy describes it ("Opus 5.5 · …").
            String desc = cm.optString("description");
            String name = desc.contains(" · ") ? desc.substring(0, desc.indexOf(" · ")) : cm.optString("displayName", value);
            sheets.option(own, 0, name, cm.optString("provider", null), false, () -> {
                agent.model = pick;
                if (!agent.effort.isEmpty() && !agent.effortsOf(pick).contains(agent.effort)) agent.effort = "";
                prefs.edit().putString("model", agent.model).putString("effort", agent.effort).apply();
                sheet.dismiss();
                app.screen.updateChrome();
            });
            if (agent.model.equals(pick) || (agent.model.isEmpty() && (agent.defaultResolved(resolved) || pick.equals(agent.defaultModel)))) sheets.check(own);
        }
        if (own != null) Ui.margins(own, app, 0, 0, 0, 10);
        List<String> all = new ArrayList<>();
        if (!agent.defaultModel.isEmpty()) all.add(agent.defaultModel);
        for (String m : agent.models) if (!all.contains(m)) all.add(m);
        for (String m : usedModels()) if (!all.contains(m)) all.add(m);
        all.removeAll(listed);
        String inUse = agent.model.isEmpty() ? agent.defaultModel : agent.model;
        LinearLayout card = sheets.card(sheet);
        // Typed under 其他模型 and nowhere else (not the settings' list nor the default): those a long press may remove.
        List<String> typed = usedModels();
        typed.removeAll(agent.models);
        typed.remove(agent.defaultModel);
        for (String m : all) {
            boolean big = m.toLowerCase(Locale.ROOT).endsWith("[1m]");
            sheets.option(card, 0, shortModel(m), big ? "1M" : null, false, () -> {
                agent.model = m;
                prefs.edit().putString("model", agent.model).apply();
                sheet.dismiss();
                app.screen.updateChrome();
            });
            // One typed under 其他模型 (not from the settings): a long press takes it off the list.
            if (typed.contains(m)) card.getChildAt(card.getChildCount() - 1).setOnLongClickListener(v -> {
                removeModelAsk(sheet, m);
                return true;
            });
            if (m.equals(inUse)) sheets.check(card);
        }
        LinearLayout modeCard = sheets.card(sheet);
        Ui.margins(modeCard, app, 0, 10, 0, 0);
        sheets.option(modeCard, R.drawable.ic_check, "权限", modeName(agent.mode), false, () -> { sheet.dismiss(); modeSheet(null); });
        sheets.option(card, R.drawable.ic_plus, "其他模型", null, false, () -> {
            sheet.dismiss();
            sheets.textSheet("其他模型", "", "模型名", false, (t, done) -> {
                String name = t.trim();
                if (name.isEmpty()) return;
                List<String> used = usedModels();
                used.remove(name);
                used.add(0, name);
                prefs.edit().putString("usedModels", new JSONArray(used.subList(0, Math.min(8, used.size()))).toString()).apply();
                agent.model = name;
                prefs.edit().putString("model", agent.model).apply();
                app.screen.updateChrome();
                done.run();
            }, null);
        });
        sheet.show();
    }

    /** Asks, then takes a model typed under 其他模型 off the list; if it was the one in use, agy's default is. */
    private void removeModelAsk(Dialog picker, String m) {
        Dialog ask = sheets.sheet("移除 " + m + "？", "只是从这个列表里去掉，需要时可以在「其他模型」里再加。");
        TextView cancel = sheets.button("取消", false, v -> ask.dismiss());
        TextView ok = sheets.button("移除", true, v -> {
            ask.dismiss();
            List<String> used = usedModels();
            used.remove(m);
            prefs.edit().putString("usedModels", new JSONArray(used).toString()).apply();
            if (agent.model.equals(m)) {
                agent.model = "";
                prefs.edit().putString("model", "").apply();
                app.screen.updateChrome();
            }
            picker.dismiss();
            modelSheet();
        });
        sheets.actions(ask, cancel, ok);
        ask.show();
    }

    private List<String> usedModels() {
        List<String> out = new ArrayList<>();
        try {
            JSONArray a = new JSONArray(prefs.getString("usedModels", "[]"));
            for (int i = 0; i < a.length(); i++) out.add(a.optString(i));
        } catch (org.json.JSONException ignored) { /* none */ }
        return out;
    }

    void memorySheet(String scope, String folder, String title) {
        String q = "/memory?scope=" + scope + (folder == null ? "" : "&cwd=" + Uri.encode(folder));
        bridge.get(q, (status, data) -> {
            if (status != 200) { toast("读取 AGENTS.md 失败"); return; }
            sheets.textSheet(title, data.optString("text"), "", true, (t, done) -> {
                JSONObject body = obj("scope", scope, "text", t);
                if (folder != null) body = obj("scope", scope, "cwd", folder, "text", t);
                bridge.post("/memory", body, (s2, r2) -> {
                    if (s2 == 200) { toast("已保存"); done.run(); } else toast("保存失败");
                });
            }, app.settings::refreshSettings);
        });
    }
}

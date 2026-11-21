# Antigravity for Android (v0.1.5)

Google's Antigravity CLI (`agy`) on the phone in one app, no Termux needed: its own application id
(`com.jeep.antigravity`), name and icon. No WebView: the interface is plain Android views (Java), so
scrolling and text selection are the system's own. The app is ClaudeCode-Android (and PiAgent-Android)
with agy in place of Claude Code: the interface is the same, the bridge speaks the Agent Client
Protocol (`agy acp`).

## How it works

- The app carries its own Linux userland: Alpine (bash, Python, git, ripgrep, fd, curl), packed as
  `assets/runtime/rootfs.tar.xz` by `runtime/build-rootfs.sh`, and Termux's proot
  (`runtime/fetch-proot.sh`). Both are built in CI (cached by the scripts' hash). agy is not in it: on
  first launch the app runs the userland's `agy-update`, which does what Google's installer does
  (the release manifest for linux arm64 musl, the build it names, checked against its SHA-512),
  streamed to disk, as `/usr/local/bin/agy`; agy updates itself after that (`agy update` too). On
  first launch, or after an update that brings a new userland, the app unpacks it into its files
  (over what is there: `/root` and packages added with `apk add` stay). targetSdk is 28 so the app
  may run programs from its own files, as Termux does.
- `BridgeService`, a foreground service holding a wake lock, runs `assets/bridge.py` under proot as
  root of the userland; it listens on a Unix socket, `~/.agyapp/bridge.sock` in the userland (in this
  app's own files, mode 0600), not on a port: no other app can reach it, so requests carry no token.
  The app speaks HTTP to it over a `LocalSocket`, checks it, has the service start it when it is gone
  or of another version, and gives it 30 s to answer. The event stream resumes where it left off with
  the same bridge (it keeps the last 4000 events). The bridge logs its starts, stops, requests and
  agy's starts, exits and stderr to `~/.agyapp/bridge.log`; Settings → 关于 → 日志 shows it.
- One `agy acp` process holds every conversation as one of its sessions (JSON-RPC over its stdin and
  stdout). It is started when the app opens a conversation (once signed in), and stopped after 3
  hours unused; when it ends, a session is loaded in the next one again. Reactive wakeups (timers,
  background tasks, subagents) stream and persist automatically even when no `session/prompt` is in
  flight. A new conversation is
  `session/new` (with the system prompt set in the app in `_meta.antigravity.systemPrompt.append`);
  a saved one is `session/load`, whose replay of the conversation (users' messages, replies, thinking,
  tool calls with their results) is kept as its transcript. Model and mode are `session/set_model`
  and `session/set_mode` before a turn; a turn is `session/prompt`, stopped with `session/cancel`
  (and ended by the bridge if agy has not ended it 10 s later).
- What agy says of a session (`session/update`) is translated by the bridge into the stream-json
  messages the app's `Chat` reads (as Claude Code's were): `agent_message_chunk` and
  `agent_thought_chunk` are text and thinking blocks, `tool_call` / `tool_call_update` a tool step and
  its result (commands as Bash, files read as Read, edits as Edit or Write with agy's diff, searches
  as Grep, fetches as WebFetch; others keep agy's own name), `plan` a TodoWrite step, the prompt's
  answer the turn's result. agy says a failed model request in the reply's text (after a ⚠): that is
  told as the turn's error, with what to do for the usual ones (not signed in, a region the model is
  not offered in, quota).
- agy asking whether a tool may run (`session/request_permission`) is Claude Code's
  `control_request` (`can_use_tool`): the tool's row asks, and 允许 / 拒绝 is agy's `allow_once` /
  `reject_once`. The app's permission modes are agy's (default, accept-edits, auto, plan) and
  全部允许, where the bridge answers yes itself. A question still open when the turn ends is
  cancelled.
- agy's first session after it starts is the slow one (1.2 to 2.7 s with no network, 3 to 5 s on the phone signed
  in with Google, which also makes its first connections; the next ones 0.15 s): when the app opens a conversation the bridge starts agy and makes one session ahead,
  deleted at once as for the models (`agy primed` in the log). `reply began after X s` in the log
  says how long a reply took to begin, in parts: session ready, prompt sent, the model's first output.
- A message written while a reply runs waits below it; with 插入 (the default) it is sent as soon as
  the reply runs a tool, as another `session/prompt`: agy takes it into the running turn and ends the
  prompt before it, at which point the app shows it as a message of the conversation. It cannot be
  taken back then.
- Models: agy lists each model at each of its thinking levels ("Gemini 4 Argon (High)" is
  `gemini-4-argon-high`). The bridge lists them to the app as one model with its levels, and gives
  agy the id for the model and level picked (the nearest level the model has). agy tells them only
  with a session (`agy models` wants the terminal's own sign-in, not `agy acp --login`'s): after a
  sign-in the bridge makes one, reads them, and deletes it at once; each session's answer brings
  them again.
- 用量 shows the account's model quota as agy's `/usage` tells it (the bridge runs `agy -p /usage
  --output-format json`, which answers with no turn, no quota spent and no conversation left behind, and
  takes the app's sign-in): per group of models, the five-hour and weekly limits with what is used and
  when each is full again, and the AI credits left (`/credits`).
- Signing in: Settings → 账号 → 用 Google 登录 / 添加账号 has the bridge run `agy acp --login` under a
  pseudo-terminal: the link it prints is opened in the browser, and the code the page shows after
  signing in is pasted in the app and written to it. Signed-in Google accounts' OAuth tokens are kept
  under `~/.agyapp/accounts/` (mode 0600) so tapping another saved account swaps the active token in
  one tap (`/account/switch`, `/account/remove`) without re-running browser OAuth. A Gemini API key is kept in
  `~/.agyapp/gemini-api-key` (mode 0600), given to agy as `GEMINI_API_KEY`, with agy's
  `modelProvider` set to `gemini`. agy is started again after a sign-in or account switch once no reply runs.
- The conversation list is agy's own (`~/.gemini/antigravity-cli/conversation_summaries.db`, read
  only; sub-agents' conversations and empty ones left out); names given in the app are kept in
  `~/.agyapp/titles.json`. A transcript is kept in `~/.agyapp/history/` with when agy last changed
  the session, and read again from agy when it changed since (continued in the terminal). Pictures
  sent from the app are kept in `~/.agyapp/images/` (agy replays none). Deleting a conversation
  closes it in agy and removes its files and its line in agy's list.
- `Bridge` talks to the bridge (HTTP, event stream); `Chat` turns the stream-json messages (or a
  session's history) into messages and blocks; `ThreadAdapter` shows them in a RecyclerView; `Md`
  renders Markdown with Markwon; `DrawerFrame` is the drawer that a horizontal swipe anywhere opens.
  `MainActivity` keeps the conversations open and is made of parts (`Part`): `Link`, `ChatScreen`,
  `Box` (with `SlashCommands` and `Attachments`), `Sender`, `Finder`, `MessageActions`, `Drawer`,
  `Settings` with its pages (`LookPage`, `AccountPage`, `ExtPage` with `Skills`, `AppPage`),
  `Pickers` (folder, model and level, permission mode), `Sheets`, `UsageSheet`, `Voice` (the phone's
  recognizer) and `Agent` (what is known of agy).

## Tests

- `ChatTest` applies scripts of stream-json messages (and a saved history) to a `Chat` and compares
  the conversation after each with `src/test/resources/chat/*.txt` (`-Drecord=true` records again).
- `src/test/python/test_bridge_http.py` starts the bridge's HTTP handler with a fake `agy` (its ACP
  server as the real one behaves: replies, thinking, tool calls asked about, an error said in the
  reply, a session replayed when loaded, a prompt taken into a running turn; its SQLite list; its
  sign-in) and compares every route's status and body, and the events of a few conversations, with
  `bridge_http.golden.json` (`--record` writes it again). Run it with `python3 -I`.
- `ScreensTest` and `ThemesTest` render the main screens with Robolectric's native graphics:
  `gradle :code:testDebugUnitTest --tests com.jeep.antigravity.ScreensTest -Dshots=<dir>`, or the
  Screens workflow on GitHub.

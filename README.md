# Polaris

One local window where your AI agents hand work back to you.

Agents that run unattended still need a person for some things: answering a question, or running a command their safety rules forbid. Those requests tend to sit in reports, easy to miss. Polaris turns them into cards you can act on, in a desktop window served by a server only your own Mac can reach.

By Cameron Mills. Designed and directed by me, with the code and its checks written by Claude Code agents to my specification. This is the standalone version of the dashboard I use every day with my own agent system.

## What it does

- **Questions.** One queue of everything waiting on your answer, oldest blocker first. Any agent can ask by writing a small JSON file. An answer can queue follow-up work, so a yes launches it and a no launches nothing. Skip sends a question to the back, and "Ask me later" hides it until 21:00 or 09:00 the next day.
- **Run these.** When an agent hits an action it may not take, it hands you the exact command instead. A command must fit on one line with no `#` comment. If it uses git or a relative path, it must also begin with `cd` into an absolute directory, so it works from whatever folder your terminal is in. Anything else is refused when it is added. A checker marks each command safe, unverified, done or stale every 10 minutes. It also checks sooner when a new commit on the watched branch of `POLARIS_REPO` touches a file a command names. Destructive steps, such as a forced push or deleting a tracked file, count as stale, and a stale command has no Copy button.
- **Pins.** Finished work pops up as a modal the next time Polaris opens or regains focus, one at a time, until you dismiss it.
- **Answers.** What came back from what you asked, newest first.
- **Terminals.** Real shells in tabs (xterm.js over a pty), under tmux when it is installed, so closing Polaris only detaches. A session tab runs Claude Code, or any command you choose.
- **The orb.** A WebGL2 sphere on the Home screen that shows the state: idle, working while agents run, red when a run broke. An attention signal changes it when something new needs you, and a click on the reason acknowledges it. Without WebGL2 it falls back to a 2D canvas.
- **A Mac app.** One script wraps it as `Polaris.app` with its own icon, installing nothing.

## Architecture

```
your agents ──► $POLARIS_HOME/                       polaris/server.py (127.0.0.1 only)
                  questions/*.json   ─ questions.py ─┐
                  commands/*.md      ─ run_these.py ─┼─► GET  /api/desk  one JSON, polled every 3 s
                  pins/*.json        ─ pins.py ──────┤   GET  /api/mod/<questions|answers|pins|run_these>
                  answers/*.md       ┐               │   POST /api/mod/questions, /api/mod/pins, /api/do
                  feeds/status.json  ┴ sources.py ───┤   POST /api/send   (the input line, to inbox.jsonl)
                  state/             ─ attention.py ─┘   /api/pty/*       (terminal.py, token on every call)
                                                         GET  /           web/index.html (no token in it)
```

- **Stdlib only.** The server, the stores and the checker use the Python standard library. pywebview is optional, for a native window.
- **A pluggable data layer.** Polaris owns its three stores (questions, commands, pins). Everything else comes from a Source. The default reads plain files, so a tool in any language can feed it. To plug in your own system, write a class with four methods (`status`, `answers`, `questions`, `answer`) and set `POLARIS_SOURCES`. `examples/my_source.py` is a working example.
- **Local only, with a token.** The server binds 127.0.0.1 only. It refuses any Host header that is not `127.0.0.1` or `localhost`, so a web page in your browser cannot reach it through DNS rebinding. It mints a random token at each start. Every `/api/` route except `/api/health` needs that token in a header, including every read and every terminal call. The launcher opens the page with the token in the URL fragment (`#t=...`), which browsers never send to a server. The page then moves it into session storage and clears it from the address bar. The token is also written to `$POLARIS_HOME/state/token` with mode 0600, so a second launch can open a window on the running desk. That file is removed when Polaris exits normally. After a crash it stays behind, which is harmless because the token dies with the server. The URL with its fragment may land in your browser history, readable only by your own account, and that token too is dead once Polaris restarts.
- **What the token does and does not protect.** It stops web pages, other machines on your network and other user accounts on the Mac from reading Polaris or opening a shell through it. It does not stop programs running as your own user. They can read the token file, as they can read the rest of your files, so treat anything you run as yourself as able to reach Polaris.
- **One desk at a time.** A second start finds the running server through `state/desk.json`, asks it for `/api/health` and brings its window forward instead of starting another.

## Requirements

- macOS 12 or later. The server and the page also run on Linux, but the Mac app, window focusing and the app test are macOS only.
- Python 3.9 or later.
- `curl` and `tar` for setup, which fetches xterm.js from the npm registry and checks its hashes.
- Optional: tmux (terminals that survive closing Polaris), pywebview (a native window), Claude Code (for the session tab).

## Setup

1. Clone the repository and go into it.

   ```bash
   git clone https://github.com/cmills-dev/polaris-desk.git
   cd polaris-desk
   ```

2. Run the setup script. It creates the data folder at `~/.polaris` with example data, fetches xterm.js into `web/vendor/`, and runs the tests.

   ```bash
   ./setup.sh
   ```

   Use `./setup.sh --no-examples` for an empty data folder, `--native` to add a `.venv` with pywebview for a native window, and `--app` to build `~/Applications/Polaris.app`.

3. Optionally copy the settings file and edit it. `bin/polaris` reads `.env` at start.

   ```bash
   cp .env.example .env
   ```

4. Start Polaris.

   ```bash
   bin/polaris                # a native window with --native, else your browser
   bin/polaris --no-window    # serve only and print the URL (the token is in ~/.polaris/state/token)
   ```

5. Put it on your PATH if you like.

   ```bash
   ln -s "$PWD/bin/polaris" ~/.local/bin/polaris
   ```

The example data is marked as such on every card, and a banner says so. Delete the files under `~/.polaris` when you are ready for your own.

## Configuration

Every setting is an environment variable, and `.env.example` lists them with placeholder values.

| Variable | Default | What it sets |
| --- | --- | --- |
| `POLARIS_HOME` | `~/.polaris` | The data folder |
| `POLARIS_REPO` | the current folder | Where terminals and command probes start |
| `POLARIS_BRANCH` | `main` | The branch of `POLARIS_REPO` the checker watches for new commits |
| `POLARIS_SHARED` | none | Checkouts other agents share. A branch switch or stash there is judged destructive |
| `POLARIS_SECRETS_GLOB` | `secrets.` | A path fragment marking secret files. Any command naming it is judged destructive |
| `POLARIS_PATH_PREFIXES` | none | Extra relative path prefixes the checker treats as files |
| `POLARIS_SESSION_CMD` | `claude` | What the session terminal tab runs |
| `POLARIS_QUEUE_CMD` | none | A command that receives a follow-up as JSON on stdin. Without it, follow-ups land in `queue/` |
| `POLARIS_SOURCES` | none | Your own data source, as `package.module:ClassName` |
| `POLARIS_CHECK_EVERY` | `60` | Seconds between checker ticks. Each tick acts only when a check is due. `0` turns it off |
| `POLARIS_TZ` | the Mac's own | Time zone for "Ask me later" |

## Usage

An agent asks you something:

```bash
cat > ~/.polaris/questions/deploy-staging.json <<'JSON'
{"id": "deploy-staging", "question": "Deploy the staging build now?",
 "options": ["yes", "not today"], "context": "All tests passed at 14:02.",
 "source": "deploy agent",
 "on_answer": {"if": ["yes"], "queue": {"title": "Deploy staging", "text": "Run the staging deploy."}}}
JSON
```

An agent hands you a command it may not run:

```bash
python3 -m polaris.run_these add --title "Install the new launch agent" \
  --why "installing launch agents is left to a person" --from run-42 \
  --line '! cd "/path/to/repo" && bash scripts/install-agent.sh' \
  --expect "installed: 1 agent" --done-if 'launchctl list | grep -q my.agent'
python3 -m polaris.run_these list
python3 -m polaris.run_these check --dry-run
```

A finished job leaves you a pin:

```bash
python3 -m polaris.pins add --title "The weekly report is ready" --body "Three charts, one open question." --link reports/weekly.md
```

A script or a scheduled job shows a screen:

```bash
python3 -m polaris.open questions
```

Your agents report what they are doing by writing `~/.polaris/feeds/status.json`. The shape is in `polaris/sources.py` and a full example is in `examples/home/feeds/status.json`.

Keys: `h` Home, `q` Questions, `A` Answers, `r` Run these, `` ` `` Terminal, `/` the input line. On Questions, `1` to `9` pick an option, Enter files a typed answer, `s` skips, `l` asks later and `d` shows the details. In a terminal every key goes to the shell, and Esc then a Polaris key, or a click outside, gives them back.

## Tests

```bash
python3 tests/run_all.py
```

Five suites, all over scratch folders, never your real data folder:

- `pins`, `questions` and `attention` test each store and the attention signal on its own.
- `run these` builds a scratch git repository and checks every checker rule: merged branches, patches that no longer apply, destructive commands, missing files, done probes, the one-line rule, secret refusal and when a check is due.
- `server` starts a real server and drives every route. It confirms that the page carries no token, that the desk, module, terminal and send routes refuse a request without the token, that a request from another Host is refused, and that the token file is readable by its owner only. It sends bytes through a pty and back, runs a real shell under tmux with a scratch session name, and builds the Mac app into a temporary folder.

The tmux check is skipped when tmux is not installed. The app check is skipped off macOS or without `sips` and `iconutil`.

## Limitations

- It is a single-user desk for one Mac. There are no accounts and no remote access, on purpose. Programs running as your own user can reach it, as explained under Architecture.
- The checker models common shell, git and file commands. Anything beyond it is marked "not verified", never plain safe, so read those before pasting.
- The input line only appends to `inbox.jsonl`. Your own session or agent has to read that file.
- Agents and jobs come from a feed you write. Polaris does not start or stop agents itself.
- The bigger dashboard this came from also had trackers, revision drills and voice. Those depend on the rest of that system and are not part of this repository.

## Licence

MIT. See `LICENSE`. xterm.js and its fit addon are MIT licensed too, fetched at setup with their own licence files.

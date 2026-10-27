# pr-review

Unattended code review for Bitbucket pull requests, run by Claude Code on your Mac.

A launchd job polls Bitbucket every minute for open PRs that list you as a reviewer. When a PR is new or gets a push, the watcher checks the head out into a git worktree, writes the diff and a `context.json`, and starts a hidden interactive `claude` session (in `screen`) that runs the `/pr-auto-review` skill. The skill reads the change, verifies every candidate finding with a separate subagent, and publishes the review as a private claude.ai Artifact. The watcher then sends you the link on Slack. Later pushes get incremental reviews on the same page, with earlier findings marked fixed when they are.

The watcher's Claude session is locked down because PR content is untrusted: `dontAsk` permissions, no MCP servers or plugins, no git or general shell, writes limited to the run's `review.json` and `result.json`, and a fail-closed `PreToolUse` guard (`pr-review guard`) that restricts which Artifact and Agent calls are allowed.

## Requirements

- macOS (launchd, Keychain, `/usr/bin/screen`) and the system `/usr/bin/python3`. No third-party Python packages.
- Claude Code CLI, logged in, at `~/.local/bin/claude` (or set `PR_REVIEW_CLAUDE_BIN`).
- A local clone of the repository to review. Worktrees and run directories are created in the clone's parent folder, so that folder must be one Claude Code already trusts. To use another trusted folder, set `PR_REVIEW_WORKTREES_DIR` and `PR_REVIEW_RUNS_DIR`.
- A Bitbucket API token with read access to the repository, and a Slack bot token with `chat:write`.

## Installation

Clone this repo wherever you like, `cd` into it, and run each step from the repo root. The commands use `$PWD` to fill in the location.

1. Create your settings:

   ```sh
   cp .env.example .env   # then fill in every value
   ```

2. Store both tokens in the login Keychain (each command prompts for the secret):

   ```sh
   security add-generic-password -s bitbucket-api-token -a you@example.com -w
   security add-generic-password -s slack-pr-review-bot-token -a slack -w
   ```

3. Link the skill so Claude Code finds it:

   ```sh
   ln -s "$PWD/skills/pr-auto-review" ~/.claude/skills/pr-auto-review
   ```

4. Check the setup and baseline the PRs that are already open, so the watcher only reviews them after their next push:

   ```sh
   /usr/bin/python3 -I bin/pr-review list
   /usr/bin/python3 -I bin/pr-review seed
   ```

5. Create the watcher wrapper. It finds this repo by following the skill link from step 3:

   ```sh
   mkdir -p ~/.claude/scripts ~/.claude/logs
   cat > ~/.claude/scripts/pr-review-watch.sh <<'EOF'
   #!/bin/zsh
   export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
   export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"
   LOG="$HOME/.claude/logs/pr-review.log"
   SKILL="$HOME/.claude/skills/pr-auto-review"
   REPO="${SKILL:A:h:h}"
   if [[ ! -f "$REPO/bin/pr-review" ]]; then
       print -r -- "[$(date '+%Y-%m-%d %H:%M:%S')] watcher not started: $SKILL does not link into a pr-review repo" >> "$LOG"
       exit 1
   fi
   exec /usr/bin/python3 -I "$REPO/bin/pr-review" watch >> "$LOG" 2>&1
   EOF
   ```

6. Create the launchd job, which runs the wrapper every 60 seconds and once at login:

   ```sh
   cat > ~/Library/LaunchAgents/local.pr-review.watcher.plist <<EOF
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
       <key>Label</key><string>local.pr-review.watcher</string>
       <key>ProgramArguments</key>
       <array>
           <string>/bin/zsh</string>
           <string>$HOME/.claude/scripts/pr-review-watch.sh</string>
       </array>
       <key>StartInterval</key><integer>60</integer>
       <key>RunAtLoad</key><true/>
       <key>StandardOutPath</key><string>$HOME/.claude/logs/pr-review-launchd.log</string>
       <key>StandardErrorPath</key><string>$HOME/.claude/logs/pr-review-launchd.log</string>
   </dict>
   </plist>
   EOF
   ```

7. Start it:

   ```sh
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.pr-review.watcher.plist
   ```

If you move the repo later, re-run step 3 to point the skill link at the new location; the wrapper follows it.

## Usage

Once loaded, the watcher needs nothing from you. Each tick logs a line such as `tick: 32 PRs, no action` to `~/.claude/logs/pr-review.log`, and each finished review arrives as a Slack DM with the Artifact link.

`pr-review` below is short for `/usr/bin/python3 -I <repo>/bin/pr-review`, where `<repo>` is wherever you cloned this project. An alias saves typing: `alias pr-review='/usr/bin/python3 -I <repo>/bin/pr-review'`.

| Task | Command |
| --- | --- |
| Review a PR by hand, inside Claude Code | `/pr-auto-review <PR number>` |
| Review a PR through the watcher pipeline | `pr-review run <PR number>` |
| List PRs that list you as a reviewer | `pr-review list` |
| Run one watcher tick now | `zsh ~/.claude/scripts/pr-review-watch.sh` |
| Watch a review in progress | `screen -r pr-review-<id>` (detach with Ctrl-A D) |
| Re-baseline open PRs, keeping review history | `pr-review seed --force` |
| Pause the watcher | `launchctl bootout gui/$(id -u)/local.pr-review.watcher` |
| Resume the watcher | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.pr-review.watcher.plist` |

The `prepare`, `render` and `finish` commands are steps the skill runs itself, and `guard` is the permission hook; you don't call them directly.

Reviews follow your own rules: the skill reads `~/.claude/CLAUDE.md` and the reviewed project's Claude Code memory (its `MEMORY.md` and every `feedback_*.md` note), so project-specific review rules belong there rather than in the skill.

If a run stalls on an expired Claude login, the watcher DMs you once. Run `/login`, then `pr-review run <id>` for any PR that used up its three attempts.

## Files

| Path | Contents |
| --- | --- |
| `~/.claude/pr-review/` | Watcher state (`state.json`), stored review pages, lock file |
| `<clone parent>/.pr-review-runs/<id>-<head>/` | Per-run `context.json`, `diff.patch`, `review.json`, `page.html`, `result.json` (kept 14 days) |
| `<clone parent>/.pr-review-worktrees/<id>/` | Worktree of the PR head during a run |
| `~/.claude/logs/pr-review.log` | Watcher log |

`<clone parent>` is the parent folder of the reviewed repository's clone, unless `PR_REVIEW_RUNS_DIR` or `PR_REVIEW_WORKTREES_DIR` say otherwise.

## Tests

From the repo root:

```sh
/usr/bin/python3 -I -m unittest discover -s tests -t .
```

The suite uses fake settings from `tests/test.env`, so it needs no `.env`, tokens or network.

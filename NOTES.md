# Claude session spike findings

Use the SCREEN section (S1-S5) below. The headless `claude -p` approach (H1-H4) is SUPERSEDED: it has no Artifact tool, so it is not usable for publishing.

# Headless `claude -p` (SUPERSEDED, labels H1-H4)

Claude CLI 2.1.310, run from the spike scratch dir with:
`claude -p "/pr-review-spike <abs>/page.html" --output-format json --json-schema '<schema>' --permission-mode dontAsk --strict-mcp-config --allowedTools "Read Grep Glob Skill Agent Artifact Bash(git log *) Edit(//<scratch>/allowed/**) Write(//<scratch>/allowed/**)"` (exit 0, ~19s)

## H1 Structured output key
Top-level key `structured_output` (object). `result` holds the same JSON as a string. Shape:
{"type":"result","subtype":"success","is_error":false,"result":"<json string>","structured_output":{"status":"...","artifact_url":"...","error":"..."},"session_id":"...","total_cost_usd":0.36,"num_turns":10,"permission_denials":[{"tool_name":"Write","tool_input":{...}}],...}
`permission_denials` lists denied tool calls (useful for auditing).

## H2 Absolute-path allow rule syntax
`Edit(//<abs path>/**)` and `Write(//<abs path>/**)` WORK as-is (double slash = absolute). Write to allowed/ok.txt succeeded; write to nope.txt outside was denied. `Bash(git log *)` allowed git log (Bash allow rules proven here and again in S2 check); `rm -f /tmp/x` and even `ls /tmp/x` denied under dontAsk.

## H3 Slash command under -p
`-p "/pr-review-spike <arg>"` DOES invoke the personal skill (~/.claude/skills/pr-review-spike); no rephrasing needed.

## H4 Artifact publish/update: FAILED (GATE)
The headless session has NO `Artifact` tool. ToolSearch finds only DesignSync. Same result with and without `--strict-mcp-config`, with `--allowedTools Artifact`, and with `--tools "default,Artifact"`. Tool list headless: Agent, Bash, Edit, ListAgents, Read, ReportFindings, ScheduleWakeup, Skill, ToolSearch, Workflow, Write, Cron*, DesignSync, EnterWorktree, ExitWorktree, LSP, Monitor, NotebookEdit, PushNotification, RemoteTrigger, SendMessage, TaskStop, WebSearch (plus claude.ai MCPs without --strict-mcp-config). No spike artifact was created; update-by-url untested.
Side note: the headless model was claude-opus-5-5 (settings.json "model": "opus").

# Screen approach (WORKS, labels S1-S5)

Claude 2.1.310. Supersedes H4 above: the interactive session HAS the Artifact tool.

## S1 Launch command (non-TTY, stdin /dev/null, TERM set)
Placeholders: `<scratch>` = the driver's working dir containing `allowed/` (result.json is written there); `<cwd>` = the directory claude starts in (see S2); `<html>` = absolute path of the page.
TERM=xterm-256color /usr/bin/screen -dmS pr-review-spike /bin/zsh -c 'cd <cwd> && exec ~/.local/bin/claude "/pr-review-spike <html> [<artifact url>]" --permission-mode dontAsk --strict-mcp-config --add-dir <scratch>/allowed --allowedTools "Read Grep Glob Skill Agent Artifact Edit(//<scratch>/allowed/**) Write(//<scratch>/allowed/**)"' < /dev/null
The slash-command skill ran; Write rule `//abs/**` worked. claude does NOT exit after finishing (stays at its prompt), so the driver must poll for result.json and then `screen -S pr-review-spike -X quit` (verify with `screen -ls`).

## S2 Dialogs
A brand-new cwd (not under a trusted dir) shows the trust dialog and blocks forever: "Accessing workspace: <cwd> / Quick safety check: Is this a project you created or one you trust? ... o No, exit / Yes, I trust this folder / Enter to confirm / Esc to cancel". Avoided with NO config edits: a NEW subdirectory of an already-trusted directory (~/Downloads/pr-review-spike-cwd, parent ~/Downloads is trusted) showed no dialog. Trusted dirs in ~/.claude.json included ~/Code, ~/Desktop, ~/Downloads, and a few project folders under ~/Code. No onboarding, model, or version notice appeared. Use cwd = a dir under ~/Code (trusted) plus `--add-dir` for the worktree.
VERIFIED: new dir ~/Code/.pr-review-worktrees/spike-check (parent also new), launched with `--permission-mode dontAsk --strict-mcp-config --allowedTools 'Bash(git log *)'`: `-p 0` hardcopy after 8s showed the normal prompt and a finished reply, NO trust dialog. `Bash(git log *)` ran ("Ran 1 shell command"; git itself failed as the dir is not a repo). Only notice seen: "Your login expires in 2 days, run /login to renew" (not blocking; login expiry could stall a future unattended run, so watch for it in failure hardcopies). Directories removed afterwards.

## S3 Artifact
Publish OK: https://claude.ai/artifact/<id> (private, kept). Update by URL from a fresh hidden session: same URL, content changed ("Version 2 updated"), confirmed with Artifact read.

## S4 Timing
Launch to result.json (poll interval and timeout are the driver's choice; 5s poll / 10 min used here): ~10s for a first publish, ~18s for an update (read + publish) including 5s poll granularity.

## S5 hardcopy
`screen -S NAME -X hardcopy FILE` produces an EMPTY file when run without `-p 0`. Use `screen -S NAME -p 0 -X hardcopy FILE` (works, ~1s to appear, plain text of the 80x24 visible screen, wrapped lines, some bytes non-ASCII). Shows dialogs, tool output, and final message. Must be taken before quitting the session.

## S6 Protected ~/.claude (Task 8 finding)
dontAsk refuses Write to any path under `~/.claude/`, even with exact-file or `/**` `Write(...)` + `Edit(...)` allow rules and `--add-dir` on that directory. The same rules under `~/Code/...` work (exact file and glob). `Write(...)` rules alone grant nothing; the `Edit(...)` rule is what matches. So run dirs live in `<repo parent>/.pr-review-runs/<id>-<head>` (`RUNS_DIR`, overridable with `PR_REVIEW_RUNS_DIR`), not `~/.claude/pr-review/runs`.
Also: dontAsk auto-allows read-only Bash (e.g. `ls <dir> | grep x`) inside the working dirs (cwd and `--add-dir`), with no allow rule. Writes, redirects (`> file`), and anything outside those dirs are refused.

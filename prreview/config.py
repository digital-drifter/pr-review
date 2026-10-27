import os
from pathlib import Path
from typing import Dict, Mapping

PROJECT_DIR = Path(__file__).resolve().parent.parent


class ConfigMissing(Exception):
    pass


def read_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_settings(environ: Mapping[str, str]) -> Dict[str, str]:
    env_file = Path(environ.get("PR_REVIEW_ENV_FILE") or PROJECT_DIR / ".env")
    return {**read_env_file(env_file), **environ}


def required(settings: Mapping[str, str], name: str) -> str:
    value = settings.get(name, "")
    if not value:
        raise ConfigMissing("%s is not set; copy .env.example to .env and fill it in" % name)
    return value


SETTINGS = load_settings(os.environ)
HOME = Path.home()

REPO = Path(required(SETTINGS, "PR_REVIEW_REPO")).expanduser()
WORKSPACE = required(SETTINGS, "PR_REVIEW_BITBUCKET_WORKSPACE")
REPO_SLUG = required(SETTINGS, "PR_REVIEW_BITBUCKET_REPO_SLUG")
REPO_FULL_NAME = "%s/%s" % (WORKSPACE, REPO_SLUG)
REVIEWER_UUID = required(SETTINGS, "PR_REVIEW_BITBUCKET_REVIEWER_UUID")
BB_EMAIL = required(SETTINGS, "PR_REVIEW_BITBUCKET_EMAIL")
BB_KEYCHAIN_SERVICE = "bitbucket-api-token"
SLACK_KEYCHAIN_SERVICE = "slack-pr-review-bot-token"
SLACK_CHANNEL = required(SETTINGS, "PR_REVIEW_SLACK_CHANNEL")
DATA_DIR = HOME / ".claude" / "pr-review"
WORKTREES_DIR = Path(SETTINGS.get("PR_REVIEW_WORKTREES_DIR") or REPO.parent / ".pr-review-worktrees").expanduser()
RUNS_DIR = Path(SETTINGS.get("PR_REVIEW_RUNS_DIR") or REPO.parent / ".pr-review-runs").expanduser()
LOG_PATH = HOME / ".claude" / "logs" / "pr-review.log"
DASHBOARD_LOG = HOME / ".claude" / "logs" / "pr-review-dashboard.log"
DASHBOARD_TRIGGER_ID = SETTINGS.get("PR_REVIEW_DASHBOARD_TRIGGER_ID", "")
CLAUDE_BIN = SETTINGS.get("PR_REVIEW_CLAUDE_BIN") or str(HOME / ".local" / "bin" / "claude")
USER_SETTINGS = HOME / ".claude" / "settings.json"
INSTALLED_PLUGINS = HOME / ".claude" / "plugins" / "installed_plugins.json"
USER_CLAUDE_MD = HOME / ".claude" / "CLAUDE.md"
READ_DENY = (
    "%s/.ssh/**" % HOME,
    "%s/.aws/**" % HOME,
    "%s/.claude/.credentials.json" % HOME,
    "%s/**" % REPO,
)

MAX_ATTEMPTS = 3
MAX_UNCOUNTED_BLOCKS = 2
TRANSIENT_NOTIFY_AFTER = 15
RUN_RETENTION_SECONDS = 14 * 24 * 3600
HARDCOPY_INTERVAL = 60.0
STALL_SECONDS = 300
LONG_RUN_SECONDS = 3600

SIZE_EXCLUDES = (
    "composer.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "phpstan-baseline.neon",
    "eslint-baseline-errors.json",
    "public/build/**",
    "*.min.js",
    "*.min.css",
    "*.map",
)

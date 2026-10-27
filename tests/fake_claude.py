#!/usr/bin/python3 -I
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    record = os.environ.get("FAKE_CLAUDE_RECORD")
    if record:
        sessions = subprocess.run(["/usr/bin/screen", "-ls"], capture_output=True, text=True).stdout
        Path(record).write_text(json.dumps({
            "argv": sys.argv[1:], "cwd": os.getcwd(), "screen_ls": sessions,
            "restrict": os.environ.get("CLAUDE_CODE_RESTRICT_PERSONAL_CONFIG"),
        }))
    mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
    context_path = Path(sys.argv[1].split(" ", 1)[1])
    result = Path(json.loads(context_path.read_text())["run_dir"]) / "result.json"
    if mode == "exit":
        return 0
    if mode == "ok":
        time.sleep(1)
        url = os.environ.get("FAKE_CLAUDE_URL", "https://claude.ai/artifact/fake")
        result.write_text(json.dumps({"status": "published", "artifact_url": url, "error": ""}))
    elif mode == "bad":
        result.write_text("{not json")
    elif mode == "blocked":
        print("API Error: 401 Invalid API key · Please run /login", flush=True)
    elif mode == "body_blocker":
        print("PR file says: Claude usage limit reached. Please run /login", flush=True)
        for line in range(10):
            print("reading file %d" % line, flush=True)
    elif mode == "stall":
        print("Your login expires in 1 day · run /login to renew", flush=True)
        print("Read 401 lines", flush=True)
    elif mode == "busy":
        print("The new error says Not logged in · Please run /login", flush=True)
        for tick in range(15):
            print("working %d" % tick, flush=True)
            time.sleep(0.2)
        result.write_text(json.dumps({"status": "published", "artifact_url": os.environ["FAKE_CLAUDE_URL"], "error": ""}))
    while True:
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())

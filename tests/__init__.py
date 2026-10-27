import os
from pathlib import Path

for name in [name for name in os.environ if name.startswith("PR_REVIEW_")]:
    del os.environ[name]
os.environ["PR_REVIEW_ENV_FILE"] = str(Path(__file__).resolve().parent / "test.env")

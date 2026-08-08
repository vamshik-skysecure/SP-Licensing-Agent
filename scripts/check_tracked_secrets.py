from __future__ import annotations

import re
import subprocess
from pathlib import Path


PATTERNS = {
    "OpenAI API key": re.compile(rb"sk-(?:proj|svcacct)-[A-Za-z0-9_-]{20,}"),
    "Azure account key": re.compile(rb"AccountKey=[A-Za-z0-9+/=]{20,}"),
    "Meta access token": re.compile(rb"EAA[A-Za-z0-9]{40,}"),
    "Private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}

MAX_SCAN_BYTES = 10 * 1024 * 1024


def repository_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    )
    return [
        Path(value.decode("utf-8"))
        for value in result.stdout.split(b"\0")
        if value
    ]


def main() -> int:
    findings: list[str] = []
    for path in repository_files():
        if not path.is_file() or path.stat().st_size > MAX_SCAN_BYTES:
            continue
        content = path.read_bytes()
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                findings.append(f"{path.as_posix()}: possible {label}")

    if findings:
        print("Tracked secret scan failed:")
        print("\n".join(f"- {finding}" for finding in findings))
        return 1
    print("Repository secret scan passed: no configured credential patterns found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

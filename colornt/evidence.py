"""Extract observations from Actions logs, with URL credentials removed."""
import re
from urllib.parse import urlsplit, urlunsplit


def sanitize(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)

    def redact(match):
        raw = match.group(0)
        parts = urlsplit(raw)
        host = parts.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parts.scheme, host, parts.path, "REDACTED" if parts.query else "", ""))

    return re.sub(r"https?://[^\s\"'<>]+", redact, text)


def parse_log(text: str) -> dict:
    lines = sanitize(text).splitlines()
    debloat = []
    methods = {"framework.jar": [], "services.jar": []}
    current = None
    events = []
    skipped = []
    source_sha = None
    for index, line in enumerate(lines):
        message = re.sub(r"^\ufeff?\d{4}-\d\d-\d\dT[^ ]+ ", "", line)
        if "git log -1 --format=%H" in message and index + 1 < len(lines):
            match = re.search(r"\b[a-f0-9]{40}\b", lines[index + 1])
            if match:
                source_sha = match.group(0)
        match = re.search(r"\[DEBLOAT\] - Removing directory: build/(?:portrom|baserom)/images/(.+)$", message)
        if match:
            debloat.append(match.group(1))
        match = re.search(r"\[\*\] Target\s+: (framework.jar|services.jar)", message)
        if match:
            current = match.group(1)
        match = re.search(r"\[\d+/\d+\] (.+\.smali)::(.+)$", message)
        if match and current:
            methods[current].append({"path": match.group(1), "method": match.group(2)})
        if any(marker in message for marker in ("[PORT]", "[MODS]", "[OTA]", "[REPACK]", "[SCRIPT]", "[OK] Adaptive", "[OK] - Installed", "[INFO] - Replaced", "NexAlloy", "signature verification", "SELinux")):
            events.append({"line": index + 1, "message": message})
        if re.search(r"skipping|\[SKIP\]", message, re.I):
            skipped.append({"line": index + 1, "message": message})
    return {"schema": 1, "private_toolbuild_sha": source_sha,
            "observed_debloat_paths": sorted(set(debloat)),
            "observed_framework_methods": methods, "events": events, "skipped": skipped,
            "limitations": ["Observed log output describes one run, not every available function.",
                            "Method names do not reveal method implementations.",
                            "No OTA package was built in the supplied fastboot run."]}

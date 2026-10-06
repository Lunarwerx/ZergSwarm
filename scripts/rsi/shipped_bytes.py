"""Bytes a user installs: committed size at HEAD of the files in the zswarm/ package (code plus package-data). Prints shipped_bytes=<n>."""
import subprocess

SHIPPED = (".py", ".toml", ".json", ".md", ".html", ".js", ".svg")
listing = subprocess.run(["git", "ls-tree", "-r", "-l", "-z", "HEAD"], capture_output=True, check=True).stdout
total = 0
for entry in listing.split(b"\0"):
    if not entry:
        continue
    meta, path = entry.decode("utf-8", "replace").split("\t", 1)
    size = meta.split()[3]
    parts = path.split("/")
    if size == "-" or parts[0] != "zswarm" or len(parts) < 2:
        continue
    if any(p.startswith(".") or p == "__pycache__" for p in parts):
        continue
    if path.lower().endswith(SHIPPED):
        total += int(size)
print(f"shipped_bytes={total}")

"""Rewrite a built wheel so its Requires-Dist is exactly the given pins.

An MCP server's deployable wheel installs the closure it was tested with on every
host that follows it, so its metadata carries the lock's exact pins rather than the
pyproject's ranges, plus the commit it was built from as Project-URL "Revision".
Usage: pin_wheel.py <wheel> <pins-file> <revision-url>
"""

import base64
import hashlib
import shutil
import sys
import tempfile
import zipfile


def digest(data: bytes) -> str:
    return (
        "sha256="
        + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    )


def main(wheel: str, pins_file: str, revision: str) -> None:
    with open(pins_file) as f:
        pins = [
            p.strip() for p in f if p.strip() and not p.lstrip().startswith(("#", "-"))
        ]
    loose = [p for p in pins if "==" not in p.split(";")[0]]
    if loose:
        raise SystemExit(f"pin_wheel: not exact: {loose}")
    with zipfile.ZipFile(wheel) as zin:
        items = {i.filename: zin.read(i.filename) for i in zin.infolist()}
    meta = next(n for n in items if n.endswith(".dist-info/METADATA"))
    record = next(n for n in items if n.endswith(".dist-info/RECORD"))
    head, _, body = items[meta].decode().partition("\n\n")
    kept = [
        ln
        for ln in head.splitlines()
        if not ln.startswith(("Requires-Dist:", "Provides-Extra:"))
    ]
    kept += [f"Requires-Dist: {p}" for p in pins] + [
        f"Project-URL: Revision, {revision}"
    ]
    items[meta] = ("\n".join(kept) + "\n\n" + body).encode()
    rows = [f"{n},{digest(d)},{len(d)}" for n, d in items.items() if n != record]
    items[record] = ("\n".join(rows + [f"{record},,"]) + "\n").encode()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".whl").name
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for n, d in items.items():
            zout.writestr(n, d)
    shutil.move(tmp, wheel)


if __name__ == "__main__":
    main(*sys.argv[1:])

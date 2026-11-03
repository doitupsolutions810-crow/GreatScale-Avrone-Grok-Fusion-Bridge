"""Verify every declared relay resource before using the repaired source tree."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify(root=ROOT):
    manifest = json.loads((root / "source-registry.json").read_text())
    entries = manifest.get("resources", [])
    seen, problems = set(), []
    if not isinstance(entries, list) or not entries:
        return {"ok": False, "resources_checked": 0, "problems": ["empty resource registry"]}
    for entry in entries:
        name = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name or name in seen:
            problems.append("invalid or duplicate resource")
            continue
        seen.add(name)
        path = root / name
        if (Path(name).is_absolute() or path.is_symlink() or not path.is_file()
                or not path.resolve().is_relative_to(root.resolve())):
            problems.append("missing or invalid resource: " + name)
        elif hashlib.sha256(path.read_bytes()).hexdigest() != entry.get("sha256"):
            problems.append("digest mismatch: " + name)
    return {"ok": not problems, "resources_checked": len(seen), "problems": problems}


if __name__ == "__main__":
    result = verify()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)

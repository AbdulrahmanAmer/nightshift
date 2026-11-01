import json
from pathlib import Path

from ns_common import models as m


def diff(a, b, path=""):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                print(f"  {path}/{k}: MISSING in file; model has {json.dumps(b[k])[:300]}")
            elif k not in b:
                print(f"  {path}/{k}: EXTRA in file: {json.dumps(a[k])[:300]}")
            else:
                diff(a[k], b[k], f"{path}/{k}")
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            diff(x, y, f"{path}[{i}]")
    elif a != b:
        print(f"  {path}: file={json.dumps(a)[:300]} model={json.dumps(b)[:300]}")


bad = 0
for name, model in m.SCHEMA_MODELS.items():
    f = Path("contracts/schemas") / f"{name}.schema.json"
    if not f.exists():
        print(name, "MISSING FILE")
        bad += 1
        continue
    a = json.loads(f.read_text(encoding="utf-8"))
    b = model.model_json_schema()
    if a != b:
        bad += 1
        print("MISMATCH", name)
        diff(a, b)
print("mismatches:", bad)

import re, os, sys

base = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(base, "ttech_api.py"), encoding="utf-8") as f:
    backend_src = f.read()

backend_routes = set()
for m in re.finditer(r'@app\.(get|post|put|delete|patch)\("(/[^"]+)"', backend_src):
    method = m.group(1).upper()
    path   = re.sub(r"\{[^}]+\}", "{id}", m.group(2))
    backend_routes.add((method, path))

frontend_calls = {}
static_dir = os.path.join(base, "static")
for fn in sorted(os.listdir(static_dir)):
    if not fn.endswith(".html"):
        continue
    with open(os.path.join(static_dir, fn), encoding="utf-8") as f:
        src = f.read()
    calls = re.findall(r"api\(['\"]([A-Z]+)['\"],\s*['\"`]([/][^'\"`\n\\$]+)['\"`]", src)
    if calls:
        frontend_calls[fn] = calls

print("=== FRONTEND -> BACKEND CROSS-REFERENCE ===\n")
missing = []
for fn, calls in frontend_calls.items():
    for method, path in calls:
        path_clean = path.split("?")[0].split("${")[0].rstrip("/")
        path_norm  = re.sub(r"\{[^}]+\}", "{id}", path_clean)
        exists = (method, path_norm) in backend_routes
        tag = "OK  " if exists else "MISS"
        if not exists:
            missing.append((fn, method, path))
            print(f"  [{tag}] {fn}: {method} {path}")

print("\n=== SUMMARY ===")
print(f"Missing endpoints: {len(missing)}")

print("\n=== ALL BACKEND ROUTES ===")
for method, path in sorted(backend_routes):
    print(f"  {method:6} {path}")

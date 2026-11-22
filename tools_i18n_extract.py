"""Collect every translatable message id (Chinese) from app/ and report ones missing from app/i18n_en.json.
Usage: python tools_i18n_extract.py [--write-missing out.json]   (also refreshes app/i18n_js_keys.json)"""
import ast, glob, json, os, re, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def py_ids():
    ids = set()
    for f in glob.glob(os.path.join(ROOT, "app", "*.py")):
        tree = ast.parse(open(f, encoding="utf-8").read())
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("T", "tr") and n.args \
                    and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                ids.add(n.args[0].value)
        for n in tree.body:   # module-level tables (TDict / LEVELS / SAMPLES)
            if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
                for c in ast.walk(n.value):
                    if isinstance(c, ast.Constant) and isinstance(c.value, str) and CJK.search(c.value):
                        ids.add(c.value)
    return ids


def unq(s, q):
    return s.encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8") if "\\" in s else s


def tpl_ids():
    ids = set()
    for f in glob.glob(os.path.join(ROOT, "app", "templates", "*.html")):
        s = open(f, encoding="utf-8").read()
        for m in re.finditer(r'\b_\(\s*"((?:[^"\\]|\\.)*)"', s):
            ids.add(json.loads('"' + m.group(1) + '"'))
        for m in re.finditer(r"\b_\(\s*'((?:[^'\\]|\\.)*)'", s):
            ids.add(m.group(1).replace("\\'", "'"))
    return ids


def js_ids():
    ids = set()
    files = glob.glob(os.path.join(ROOT, "app", "static", "*.js")) + glob.glob(os.path.join(ROOT, "app", "templates", "*.html"))
    for f in files:
        s = open(f, encoding="utf-8").read()
        for m in re.finditer(r"\bT\('((?:[^'\\]|\\.)*)'", s):
            raw = m.group(1)
            ids.add(re.sub(r"\\(.)", lambda x: {"n": "\n", "t": "\t"}.get(x.group(1), x.group(1)), raw))
    return ids


if __name__ == "__main__":
    cat = json.load(open(os.path.join(ROOT, "app", "i18n_en.json"), encoding="utf-8"))
    p, t, j = py_ids(), tpl_ids(), js_ids()
    allids = p | t | j
    json.dump(sorted(j), open(os.path.join(ROOT, "app", "i18n_js_keys.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    missing = sorted(i for i in allids if i not in cat)
    unused = sorted(k for k in cat if k not in allids)
    print(f"py={len(p)} tpl={len(t)} js={len(j)} total={len(allids)} missing={len(missing)} unused={len(unused)}")
    if "--write-missing" in sys.argv:
        json.dump(missing, open(sys.argv[sys.argv.index("--write-missing") + 1], "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    bad = [k for k, v in cat.items() if CJK.search(v) and k not in ("中文", "简体中文")]
    if bad:
        print("CJK left in English values:", bad[:10])
    sys.exit(1 if missing else 0)

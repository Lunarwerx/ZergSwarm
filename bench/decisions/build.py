"""Build the frozen DECISION suites: typed questions (pick one option / yes-no / rate on a scale) with gold answers.

These exist to benchmark TypeSafe's Jev (a "System One" model that only answers typed questions) against
the swarm's generative models on the SAME items, but they are ordinary data: any arm can be run on them.
Every suite is written once to `bench/decisions/data/<suite>.jsonl` and COMMITTED, so the items never drift
and a results-DB row keyed on the suite's hash stays comparable forever. Rebuild only to change a suite.

    python bench/decisions/build.py            # every suite (network needed for the public ones)
    python bench/decisions/build.py --only commit_match,bug_seeded

Suites and where their gold comes from (no LLM ever wrote a label):
    boolq         public, human-labelled yes/no over a passage (google/boolq validation), balanced 40/40
    anli          public, adversarial NLI round 3 (facebook/anli test_r3), 3-way, balanced 20/20/20
    banking77     public, 77-way customer-intent routing (mteb/banking77 test)
    hard_choice   bench/hard_reasoning.py's 17 trap problems as a 4-way choice (right, trap, 2 distractors)
    doc_to_code   zswarm's own functions: which of 4 name-masked bodies does this docstring describe?
    bug_seeded    zswarm's own functions, half with ONE mechanical mutation: does it contradict its docstring?
    commit_match  zswarm's own git history: does this diff implement this commit subject? (half shuffled)
    triage        hand-authored swarm-operations judgments (bench/decisions/triage.py), audited blind

zswarm's own code is used for the code suites on purpose: it is private, so no model has memorised it, and it
is the code the swarm actually works on. `.secrets/` is excluded from every diff, and a commit whose diff
carries a key-shaped string is dropped whole.
"""
from __future__ import annotations

import argparse
import ast
import json
import random
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DATA = HERE / "data"
SEED = 20260921
HF = "https://datasets-server.huggingface.co/rows?dataset={ds}&config={cfg}&split={split}&offset={off}&length={n}"
KEYISH = re.compile(r"(?<![A-Za-z0-9_/.-])(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}")

sys.path.insert(0, str(REPO))


def _get(url: str, tries: int = 5) -> dict:
    for k in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.loads(r.read())
        except Exception:  # noqa: BLE001 - the datasets server 5xx's under load; back off and retry
            if k == tries - 1:
                raise
            time.sleep(2 ** k)
    return {}


def _hf_rows(ds: str, cfg: str, split: str, total: int, rng: random.Random, pages: int) -> list[dict]:
    """`pages` random 100-row pages of a split: a spread sample without pulling the whole split."""
    offs = sorted(rng.sample(range(0, max(1, total - 99), 100), min(pages, max(1, total // 100))))
    out = []
    for off in offs:
        out += [r["row"] for r in _get(HF.format(ds=ds, cfg=cfg, split=split, off=off, n=100)).get("rows", [])]
    return out


# ---------------------------------------------------------------- public, human-labelled

def boolq(rng: random.Random) -> list[dict]:
    rows = _hf_rows("google/boolq", "default", "validation", 3270, rng, 8)
    yes = [r for r in rows if r["answer"]]
    no = [r for r in rows if not r["answer"]]
    pick = rng.sample(yes, 40) + rng.sample(no, 40)
    rng.shuffle(pick)
    return [{"id": f"boolq_{i:03d}", "type": "noul", "state": r["passage"], "instructions": r["question"].strip().capitalize().rstrip("?") + "?",
             "criteria": None, "gold": bool(r["answer"])} for i, r in enumerate(pick)]


ANLI = {"entailment": "The premise makes the hypothesis definitely true.",
        "neutral": "The premise neither proves nor disproves the hypothesis; it could be true or false.",
        "contradiction": "The premise makes the hypothesis definitely false."}


def anli(rng: random.Random) -> list[dict]:
    rows = _hf_rows("facebook/anli", "plain_text", "test_r3", 1200, rng, 12)
    names = ["entailment", "neutral", "contradiction"]
    pick = []
    for lab in range(3):
        pick += rng.sample([r for r in rows if r["label"] == lab], 20)
    rng.shuffle(pick)
    return [{"id": f"anli_{i:03d}", "type": "choice", "state": {"premise": r["premise"], "hypothesis": r["hypothesis"]},
             "instructions": "What is the relationship between `premise` and `hypothesis`?", "criteria": dict(ANLI), "gold": names[r["label"]]}
            for i, r in enumerate(pick)]


def banking77(rng: random.Random) -> list[dict]:
    rows = []
    for off in range(0, 3080, 100):
        rows += [r["row"] for r in _get(HF.format(ds="mteb/banking77", cfg="default", split="test", off=off, n=100)).get("rows", [])]
    labels = sorted({r["label_text"] for r in rows})
    assert len(labels) == 77, len(labels)
    by = {lab: [r for r in rows if r["label_text"] == lab] for lab in labels}
    pick = [rng.choice(by[lab]) for lab in labels] + rng.sample(rows, 3)  # every intent once, 80 items
    rng.shuffle(pick)
    crit = {lab: lab.replace("_", " ") for lab in labels}
    return [{"id": f"banking77_{i:03d}", "type": "choice", "state": r["text"], "instructions": "Which intent best describes this customer's banking message?",
             "criteria": crit, "gold": r["label_text"]} for i, r in enumerate(pick)]


# ---------------------------------------------------------------- the trap problems, as a choice

DISTRACTORS = {
    "bat_ball": ("5", "10", "1", "15"), "machines": ("5", "100", "20", "1"), "lilypad": ("47", "24", "46", "12"),
    "handshakes": ("12", "66", "11", "33"), "avg_speed": ("45", "50", "40", "60"), "two_children": ("1/3", "1/2", "1/4", "2/3"),
    "marbles": ("3/10", "9/25", "2/5", "3/5"), "day_of_week": ("friday", "wednesday", "thursday", "saturday"), "syllogism": ("no", "yes"),
    "strawberry_r": ("3", "2", "1", "4"), "clock_angle": ("7.5", "0", "15", "22.5"), "sequence": ("42", "40", "36", "44"),
    "painters": ("2", "8", "4", "3"), "discount": ("36", "40", "32", "38"), "ages": ("18", "12", "16", "20"),
    "months_28": ("12", "1", "0", "11"), "current_speed": ("2.5", "5", "12.5", "1.5"),
}


def hard_choice(rng: random.Random) -> list[dict]:
    from bench.hard_reasoning import PROBLEMS

    out = []
    for pid, prompt, _accepted, _trap in PROBLEMS:
        opts = list(DISTRACTORS[pid])
        gold = opts[0]
        rng.shuffle(opts)
        out.append({"id": f"hard_{pid}", "type": "choice", "state": prompt, "instructions": "Which option is the correct answer to this problem?",
                    "criteria": {o: None for o in opts}, "gold": gold})
    return out


# ---------------------------------------------------------------- zswarm's own code

def _functions() -> list[tuple[str, ast.FunctionDef]]:
    out = []
    for f in sorted(list((REPO / "zswarm").glob("*.py")) + list((REPO / "bench").glob("*.py"))):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node)
                src = ast.unparse(node)
                if doc and len(doc) >= 60 and 4 <= src.count("\n") <= 35 and len(src) < 2200:
                    out.append((f"{f.parent.name}/{f.name}", node))
    return out


def _strip_doc(node: ast.FunctionDef) -> ast.FunctionDef:
    n = ast.parse(ast.unparse(node)).body[0]
    if n.body and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant) and isinstance(n.body[0].value.value, str):
        n.body = n.body[1:] or [ast.Pass()]
    return n


def _masked_body(node: ast.FunctionDef) -> str:
    """The function without its docstring, renamed to `f` (and every self-reference with it)."""
    n = _strip_doc(node)
    name = n.name
    n.name = "f"
    src = ast.unparse(n)
    return re.sub(rf"\b{re.escape(name)}\b", "f", src)


def _masked_doc(node: ast.FunctionDef) -> str:
    return re.sub(rf"`?\b{re.escape(node.name)}\b`?", "this function", ast.get_docstring(node) or "")


def doc_to_code(rng: random.Random) -> list[dict]:
    fns = _functions()
    by_file: dict[str, list] = {}
    for f, n in fns:
        by_file.setdefault(f, []).append(n)
    targets = rng.sample(fns, 50)
    out = []
    for i, (f, node) in enumerate(targets):
        pool = [n for n in by_file[f] if n is not node]
        if len(pool) < 3:
            pool += [n for g, n in fns if g != f]
        others = rng.sample(pool, 3)
        cands = [node] + others
        rng.shuffle(cands)
        keys = "ABCD"
        out.append({"id": f"doc2code_{i:03d}", "type": "choice", "state": {"docstring": _masked_doc(node)},
                    "instructions": "Which candidate function does `docstring` describe?",
                    "criteria": {k: _masked_body(c) for k, c in zip(keys, cands)}, "gold": keys[cands.index(node)], "meta": {"file": f, "fn": node.name}})
    return out


SWAP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
        ast.In: ast.NotIn, ast.NotIn: ast.In}



class _Mutate(ast.NodeTransformer):
    def __init__(self, target_index: int, kind: str):
        self.i, self.kind, self.n, self.done = target_index, kind, -1, ""

    def _hit(self, node) -> bool:
        self.n += 1
        return self.n == self.i

    def visit(self, node):
        kinds = {"cmp": ast.Compare, "bool": ast.BoolOp, "not": ast.UnaryOp, "arith": ast.BinOp, "const": ast.Constant}
        if isinstance(node, kinds[self.kind]) and self._match(node):
            if self._hit(node):
                return self._apply(node)
        return self.generic_visit(node)

    def _match(self, node) -> bool:
        if self.kind == "cmp":
            return type(node.ops[0]) in SWAP
        if self.kind == "not":
            return isinstance(node.op, ast.Not)
        if self.kind == "arith":
            return isinstance(node.op, (ast.Add, ast.Sub)) and not any(isinstance(x, ast.Constant) and isinstance(x.value, str) for x in (node.left, node.right))
        if self.kind == "const":
            return type(node.value) is int and node.value in (0, 1, 2)
        return True

    def _apply(self, node):
        if self.kind == "cmp":
            node.ops[0] = SWAP[type(node.ops[0])]()
            self.done = "comparison operator flipped"
        elif self.kind == "bool":
            node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
            self.done = "and/or swapped"
        elif self.kind == "not":
            self.done = "`not` removed"
            return node.operand
        elif self.kind == "arith":
            node.op = ast.Sub() if isinstance(node.op, ast.Add) else ast.Add()
            self.done = "+/- swapped"
        elif self.kind == "const":
            node.value = node.value + 1
            self.done = "integer constant off by one"
        return node


def bug_seeded(rng: random.Random) -> list[dict]:
    fns = [(f, n) for f, n in _functions() if len(ast.get_docstring(n) or "") >= 80]
    pick = rng.sample(fns, 60)
    out = []
    for i, (f, node) in enumerate(pick):
        base = ast.parse(ast.unparse(node)).body[0]
        mutate = i % 2 == 0
        detail = ""
        if mutate:
            doc_expr = base.body[0]
            body_only = ast.Module(body=base.body[1:], type_ignores=[])
            # Count each kind's sites in the transformer's OWN traversal order, then pick one by index: a site
            # list from ast.walk (breadth-first) would index a different node than the depth-first transformer hits.
            counts = {}
            for kind in ("cmp", "bool", "not", "arith", "const"):
                probe = _Mutate(-1, kind)
                probe.visit(ast.parse(ast.unparse(body_only)))
                if probe.n >= 0:
                    counts[kind] = probe.n + 1
            if not counts:
                mutate = False
            else:
                kind = rng.choice(sorted(counts))
                m = _Mutate(rng.randrange(counts[kind]), kind)
                base.body = [doc_expr] + m.visit(body_only).body
                detail = m.done
        out.append({"id": f"bug_{i:03d}", "type": "noul", "state": {"code": ast.unparse(base)},
                    "instructions": "Does the function in `code` contain a bug, meaning its code does not do what its docstring says for some valid input?",
                    "criteria": {"true": "The code contradicts its own docstring for at least one valid input.", "false": "The code does what its docstring says."},
                    "gold": mutate, "meta": {"file": f, "fn": node.name, "mutation": detail}})
    rng.shuffle(out)
    return out


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60).stdout


def commit_match(rng: random.Random) -> list[dict]:
    shas = _git("log", "--no-merges", "--format=%H").split()
    commits = []
    for sha in shas:
        subject = _git("log", "-1", "--format=%s", sha).strip()
        diff = _git("show", "--format=", "--stat", "--patch", "--unified=2", sha, "--", ".", ":!.secrets", ":!*.jsonl", ":!bench/out")
        if not diff.strip() or KEYISH.search(diff):
            continue  # nothing reviewable, or a key-shaped string: never send it anywhere
        files = set(re.findall(r"^diff --git a/(\S+)", diff, re.M))
        commits.append({"sha": sha, "subject": subject, "diff": diff[:9000] + ("\n... (diff truncated)" if len(diff) > 9000 else ""), "files": files})
    pick = rng.sample(commits, min(60, len(commits)))
    out = []
    for i, c in enumerate(pick):
        positive = i % 2 == 0
        subject = c["subject"]
        if not positive:
            # A decoy subject from a commit that touched NONE of the same files, so it cannot also be true.
            decoys = [d for d in commits if d is not c and not (d["files"] & c["files"])]
            subject = rng.choice(decoys)["subject"]
        out.append({"id": f"commit_{i:03d}", "type": "noul", "state": {"message": subject, "diff": c["diff"]},
                    "instructions": "Does `diff` implement the change described by `message`?",
                    "criteria": {"true": "The diff makes the change the message describes.", "false": "The diff makes some other change; the message describes something this diff does not do."},
                    "gold": positive, "meta": {"sha": c["sha"][:10]}})
    rng.shuffle(out)
    return out


def triage(_rng: random.Random) -> list[dict]:
    from bench.decisions.triage import ITEMS

    return [dict(it) for it in ITEMS]


SUITES = {"boolq": boolq, "anli": anli, "banking77": banking77, "hard_choice": hard_choice, "doc_to_code": doc_to_code,
          "bug_seeded": bug_seeded, "commit_match": commit_match, "triage": triage}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma list of suites")
    a = ap.parse_args(argv)
    names = [s.strip() for s in a.only.split(",")] if a.only else list(SUITES)
    DATA.mkdir(parents=True, exist_ok=True)
    for name in names:
        items = SUITES[name](random.Random(f"{SEED}-{name}"))
        for it in items:
            it["suite"] = name
        (DATA / f"{name}.jsonl").write_text("".join(json.dumps(it, ensure_ascii=False) + "\n" for it in items), encoding="utf-8")
        golds: dict[str, int] = {}
        for it in items:
            golds[str(it["gold"])] = golds.get(str(it["gold"]), 0) + 1
        top = sorted(golds.items(), key=lambda kv: -kv[1])[:4]
        print(f"{name:14s} {len(items):4d} items   gold spread {top}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

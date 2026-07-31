import subprocess, tempfile, json, os
from pathlib import Path

def G(*a, cwd):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
                       cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

tmp = Path(tempfile.mkdtemp())
origin = tmp / "o.git"; seed = tmp / "seed"; work = tmp / "work"
subprocess.run(["git", "init", "-qb", "main", "--bare", str(origin)], check=True)
subprocess.run(["git", "init", "-qb", "main", str(seed)], check=True)
for i in range(100):
    (seed / "f.txt").write_text(f"{i}\n"); G("add", "-A", cwd=seed); G("commit", "-qm", f"b{i}", cwd=seed)
G("push", "-q", str(origin), "main", cwd=seed)
subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
for i in range(300):
    (seed / "f.txt").write_text(f"s{i}\n"); G("add", "-A", cwd=seed); G("commit", "-qm", f"s{i}", cwd=seed)
G("push", "-q", str(origin), "main", cwd=seed)
for i in range(300):
    (work / "l.txt").write_text(f"l{i}\n"); G("add", "-A", cwd=work); G("commit", "-qm", f"l{i}", cwd=work)

subprocess.run(["git", "update-ref", "-d", "refs/remotes/origin/main"], cwd=work)
trace = tmp / "t.jsonl"
env = dict(os.environ); env["GIT_TRACE2_EVENT"] = str(trace); env["GIT_TRACE2_EVENT_NESTING"] = "10"
subprocess.run(["git", "-c", "protocol.version=2", "-c", "fetch.negotiationAlgorithm=skipping",
                "fetch", "-q", "origin"], cwd=work, check=True, env=env, capture_output=True)
for line in trace.read_text().splitlines():
    d = json.loads(line)
    if d.get("event") == "data":
        print(d.get("category"), d.get("key"), d.get("value"))

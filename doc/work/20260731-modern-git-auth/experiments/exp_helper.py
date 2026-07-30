import subprocess, sys, tempfile, os
from pathlib import Path
sys.path.insert(0, "."); sys.path.insert(0, "src")
from tests.integration.auth_harness import AuthenticatedRemote, USERNAME, PASSWORD

tmp = Path(tempfile.mkdtemp())
srv = AuthenticatedRemote(tmp).start()
work = srv.clone_anonymously(tmp / "work")
srv.add_remote_commit()

HELPER = (
    '!f() { if [ "$1" = get ]; then'
    ' printf "username=%s\\npassword=%s\\n"'
    ' "$GITCLIENT_ASKPASS_USERNAME" "$GITCLIENT_ASKPASS_PASSWORD"; fi; }; f'
)
env = dict(os.environ)
env.update({
    "GITCLIENT_ASKPASS_USERNAME": USERNAME,
    "GITCLIENT_ASKPASS_PASSWORD": PASSWORD,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",  # askpass 경로는 계속 막는다
    "LC_ALL": "C",
})
for label, extra in [
    ("interactive=false + helper", ["-c", "credential.interactive=false", "-c", f"credential.helper={HELPER}"]),
    ("체인 비움 + helper (저장 안 함 경로)", ["-c", "credential.interactive=false", "-c", "credential.helper=", "-c", f"credential.helper={HELPER}"]),
]:
    r = subprocess.run(["git", *extra, "fetch"], cwd=work, capture_output=True, text=True, env=env)
    print(f"{label}: rc={r.returncode} {r.stderr.strip().splitlines()[:1]}")

# 적대적 비밀번호 통과 (실제 인증까지)
srv2 = AuthenticatedRemote(tmp / "h", password='a>b&echo pwn^!').start()
work2 = srv2.clone_anonymously(tmp / "h" / "work")
srv2.add_remote_commit()
env2 = dict(env); env2["GITCLIENT_ASKPASS_PASSWORD"] = 'a>b&echo pwn^!'
r = subprocess.run(["git", "-c", "credential.interactive=false", "-c", f"credential.helper={HELPER}", "fetch"],
                   cwd=work2, capture_output=True, text=True, env=env2)
print(f"적대적 비밀번호: rc={r.returncode}")
srv.stop(); srv2.stop()

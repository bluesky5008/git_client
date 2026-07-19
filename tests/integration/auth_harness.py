"""인증이 필요한 원격 테스트 하네스.

`file://` 하네스로는 인증 경로를 전혀 태울 수 없다. 여기서는 Basic 인증을
요구하는 **진짜 git HTTP 서버**를 세운다 — `git http-backend`를 CGI로 돌리므로
실제 원격과 같은 스마트 HTTP 프로토콜을 탄다.

이게 필요한 이유: 인증은 이 앱에서 유일하게 "로컬 하네스로 검증했다고
안심할 수 없는" 경로다. 401을 돌려주는 가짜 서버로는 "프롬프트가 뜬다"까지만
확인되고, 자격증명이 실제로 통과하는지·전송량 계측이 인증 뒤에도 살아 있는지는
알 수 없다.
"""

from __future__ import annotations

import base64
import http.server
import os
import subprocess
import threading
from pathlib import Path

from tests.integration.remote_harness import AUTHOR_ENV, git

USERNAME = "alice"
PASSWORD = "s3cret-token"


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    project_root: Path
    username: str
    password: str
    # 인증 시도를 기록한다 — "몇 번 물어봤는가"를 테스트가 확인할 수 있게.
    attempts: list[bool]

    def _challenge(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="git"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            self.attempts.append(False)
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            self.attempts.append(False)
            return False
        ok = decoded == f"{self.username}:{self.password}"
        self.attempts.append(ok)
        return ok

    def _serve(self) -> None:
        if not self._authorized():
            self._challenge()
            return

        path, _, query = self.path.partition("?")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        env = dict(os.environ)
        env.update(
            {
                "GIT_PROJECT_ROOT": str(self.project_root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "REQUEST_METHOD": self.command,
                "PATH_INFO": path,
                "QUERY_STRING": query,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(length),
                "REMOTE_USER": self.username,
                "SERVER_PROTOCOL": "HTTP/1.1",
                "GIT_HTTP_MAX_REQUEST_BUFFER": "100M",
            }
        )
        # protocol v2 협상은 이 헤더로 이뤄진다. CGI 규약상 요청 헤더는
        # HTTP_* 로 전달해야 하는데, 빠뜨리면 서버가 조용히 v0으로 떨어진다 —
        # 그러면 하네스가 "v2를 지원하지 않는 서버"가 되어, 우리 최적화 2순위
        # (protocol v2 명시)가 테스트에서 아예 검증되지 않는다.
        git_protocol = self.headers.get("Git-Protocol")
        if git_protocol:
            env["HTTP_GIT_PROTOCOL"] = git_protocol
        proc = subprocess.run(
            ["git", "http-backend"], input=body, capture_output=True, env=env
        )

        head, separator, payload = proc.stdout.partition(b"\r\n\r\n")
        if not separator:
            head, _, payload = proc.stdout.partition(b"\n\n")

        status = 200
        headers: list[tuple[str, str]] = []
        for line in head.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            name, _, value = line.partition(":")
            value = value.strip()
            if name.lower() == "status":
                status = int(value.split()[0])
            else:
                headers.append((name, value))

        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _serve
    do_POST = _serve

    def log_message(self, *args) -> None:  # noqa: ANN002 - 서버 로그를 죽인다
        pass


class AuthenticatedRemote:
    """Basic 인증을 요구하는 git HTTP 원격."""

    def __init__(
        self,
        root: Path,
        *,
        username: str = USERNAME,
        password: str = PASSWORD,
    ) -> None:
        self.root = root
        self.username = username
        self.password = password
        self.served = root / "served"
        self.origin = self.served / "repo.git"
        self.seed = root / "seed"
        self.attempts: list[bool] = []
        self._server: http.server.ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        assert self._server is not None, "start()를 먼저 불러야 한다"
        return f"http://127.0.0.1:{self._server.server_address[1]}/repo.git"

    def start(self, *, commits: int = 3) -> AuthenticatedRemote:
        self.served.mkdir(parents=True, exist_ok=True)
        git("init", "--bare", "-b", "main", str(self.origin))
        git("config", "http.receivepack", "true", cwd=self.origin)

        git("init", "--quiet", "-b", "main", str(self.seed))
        for index in range(commits):
            (self.seed / f"f{index}.txt").write_text(
                f"내용 {index}\n" * 40, encoding="utf-8"
            )
            git("add", "-A", cwd=self.seed)
            git(
                *AUTHOR_ENV, "commit", "--quiet", "-m", f"커밋 {index}",
                cwd=self.seed,
            )
        git("push", "--quiet", str(self.origin), "main", cwd=self.seed)

        handler = type(
            "BoundHandler",
            (_Handler,),
            {
                "project_root": self.served,
                "username": self.username,
                "password": self.password,
                "attempts": self.attempts,
            },
        )
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def clone_anonymously(self, destination: Path) -> Path:
        """자격증명 없이 clone한 작업 저장소.

        clone 자체는 인증이 필요하므로, 여기서는 서버를 거치지 않고 bare
        저장소를 직접 복제한 뒤 원격 주소만 HTTP로 바꾼다. 이렇게 하면
        "저장소는 이미 있고 fetch/push에서 인증이 필요한" 상태를 만들 수 있다.
        """
        git("clone", "--quiet", str(self.origin), str(destination))
        git("remote", "set-url", "origin", self.url, cwd=destination)
        return destination

    def add_remote_commit(self) -> None:
        """원격에 커밋을 하나 올린다 — 인증된 fetch가 가져올 거리."""
        existing = len(list(self.seed.glob("f*.txt")))
        (self.seed / f"f{existing}.txt").write_text(
            f"내용 {existing}\n" * 40, encoding="utf-8"
        )
        git("add", "-A", cwd=self.seed)
        git(*AUTHOR_ENV, "commit", "--quiet", "-m", f"커밋 {existing}", cwd=self.seed)
        git("push", "--quiet", str(self.origin), "main", cwd=self.seed)

    def origin_head(self) -> str:
        return git("rev-parse", "main", cwd=self.origin).stdout.strip()

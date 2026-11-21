"""A write role (builder, improver) writes its output file to /work/.factory/out, outside the repo.

OpenCode auto-rejects a tool call outside the project ("permission requested: external_directory
... auto-rejecting") unless the path is allowed, so before opencode_permission allowed that one
directory a builder committed its code and then could not write build.md: four different models
failed the build step the same way. This runs the real OpenCode binary from the runner image against
a scripted OpenAI-compatible server (no real model) to prove the file is written now, and was not
without the permission."""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from swfactory.domain.types import ToolPolicy
from swfactory.engines.opencode import opencode_permission

IMAGE = "swfactory/runner:0.1.0"
OLD_WRITE_PERMISSION = {"edit": "allow", "bash": "allow", "webfetch": "deny"}

pytestmark = pytest.mark.docker


def _chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> bytes:
    body = {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "scripted",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage:
        body["usage"] = usage
    return b"data: " + json.dumps(body).encode() + b"\n\n"


class Scripted(BaseHTTPRequestHandler):
    """First request: call the `write` tool. After the tool result: say done."""

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        have_result = any(m.get("role") == "tool" for m in req.get("messages", []))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if have_result:
            self.wfile.write(_chunk({"role": "assistant", "content": "done"}))
            self.wfile.write(_chunk({}, "stop", usage))
        else:
            args = json.dumps({"filePath": "/work/.factory/out/build.md", "content": "hello"})
            call = {"index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "write", "arguments": args}}  # fmt: skip
            self.wfile.write(_chunk({"role": "assistant", "tool_calls": [call]}))
            self.wfile.write(_chunk({}, "tool_calls", usage))
        self.wfile.write(b"data: [DONE]\n\n")


SCRIPT = r"""
set -u
mkdir -p /work/repo /work/.factory/out /home/factory/opencode/runs/t /home/factory/.cache/opencode
cd /work/repo && git init -q .
cp -an "$OPENCODE_CACHE"/. /home/factory/.cache/opencode/ 2>/dev/null
echo "You are a test agent. Do what the user asks with your tools." \
  > /home/factory/opencode/runs/t/system.md
cat > /home/factory/opencode/runs/t/opencode.json <<JSON
{"autoupdate":false,"share":"disabled",
 "provider":{"factory":{"npm":"@ai-sdk/openai-compatible","name":"t",
  "options":{"baseURL":"http://host.docker.internal:$PORT/v1","apiKey":"none"},
  "models":{"scripted":{"name":"scripted"}}}},
 "agent":{"factory":{"prompt":"{file:/home/factory/opencode/runs/t/system.md}","mode":"primary"}}}
JSON
export OPENCODE_CONFIG=/home/factory/opencode/runs/t/opencode.json OPENCODE_DISABLE_CLAUDE_CODE=1 \
  OPENCODE_DISABLE_PROJECT_CONFIG=1 HOME=/home/factory \
  XDG_DATA_HOME=/home/factory/opencode/data XDG_CONFIG_HOME=/home/factory/opencode/config \
  XDG_STATE_HOME=/home/factory/opencode/state XDG_CACHE_HOME=/home/factory/.cache
echo "write the report" | timeout 120 opencode run --format json -m factory/scripted \
  --agent factory > /tmp/events.json 2> /tmp/err.txt
f=/work/.factory/out/build.md
test -f $f && echo "WROTE: $(cat $f)" || echo "NOT WRITTEN"
grep -o 'permission requested[^"]*' /tmp/err.txt /tmp/events.json | head -2
"""


def _run_opencode(permission: dict) -> str:
    server = ThreadingHTTPServer(("0.0.0.0", 0), Scripted)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        out = subprocess.run(
            ["docker", "run", "--rm", "--add-host", "host.docker.internal:host-gateway",
             "-e", f"PORT={server.server_port}",
             "-e", f"OPENCODE_PERMISSION={json.dumps(permission)}",
             "--entrypoint", "sh", IMAGE, "-c", SCRIPT],
            capture_output=True, text=True, timeout=200,
        )  # fmt: skip
    finally:
        server.shutdown()
    return out.stdout + out.stderr


def _image_present() -> bool:
    return (
        subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0
    )


@pytest.mark.skipif(not _image_present(), reason="opt-in: build swfactory/runner:0.1.0 first")
def test_write_role_can_write_its_output_file_outside_the_repo():
    out = _run_opencode(opencode_permission(ToolPolicy.WRITE))
    assert "WROTE: hello" in out, out


@pytest.mark.skipif(not _image_present(), reason="opt-in: build swfactory/runner:0.1.0 first")
def test_without_the_permission_opencode_rejects_that_write():
    out = _run_opencode(OLD_WRITE_PERMISSION)
    assert "NOT WRITTEN" in out and "external_directory" in out, out

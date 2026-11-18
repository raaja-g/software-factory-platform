"""Browser smoke screenshots: start the app in the gate box, load each path in Chromium."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from swfactory.sandbox.base import Sandbox
from swfactory.sandbox.paths import IN, REPO

if TYPE_CHECKING:
    from swfactory.definition.models import SmokeConfig

SCRIPT_PATH = "/tmp/smoke.cjs"
SHOT_DIR = "/tmp/smoke"

SMOKE_SCRIPT = r"""
const { chromium } = require('playwright');
const fs = require('fs');
const cfg = JSON.parse(process.argv[2]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const out = { ready: false, statuses: {}, console_errors: [] };
  const base = `http://127.0.0.1:${cfg.port}`;
  const deadline = Date.now() + cfg.ready_timeout_s * 1000;
  while (Date.now() < deadline) {
    try {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 2000);
      await fetch(base + '/', { signal: ctl.signal });
      clearTimeout(t);
      out.ready = true;
      break;
    } catch (e) {
      await sleep(500);
    }
  }
  if (!out.ready) {
    console.log(JSON.stringify(out));
    return;
  }
  fs.mkdirSync(cfg.shot_dir, { recursive: true });
  const browser = await chromium.launch({ args: ['--disable-dev-shm-usage'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    page.on('console', (m) => {
      if (m.type() === 'error') out.console_errors.push(m.text().slice(0, 300));
    });
    page.on('pageerror', (e) => out.console_errors.push(String(e).slice(0, 300)));
    for (let i = 0; i < cfg.paths.length; i++) {
      const p = cfg.paths[i];
      try {
        const resp = await page.goto(base + p, { waitUntil: 'load', timeout: 30000 });
        out.statuses[p] = resp ? resp.status() : 0;
        await page.screenshot({ path: `${cfg.shot_dir}/${cfg.names[i]}.png`, fullPage: true });
      } catch (e) {
        out.statuses[p] = 0;
        out.console_errors.push(`${p}: ${String(e).slice(0, 300)}`);
      }
    }
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(out));
})().catch((e) => {
  console.log(JSON.stringify({ ready: false, statuses: {}, console_errors: [String(e)] }));
});
"""


def screenshot_path(name: str) -> str:
    return f"{IN}/screenshots/{name}.png"


def parse_smoke_output(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    return {}


@dataclass(frozen=True, slots=True)
class SmokeResult:
    passed: bool
    shots: tuple[tuple[str, bytes], ...]
    statuses: Mapping[str, int]
    console_errors: tuple[str, ...]
    detail: str


class SmokeRunner:
    async def run(self, box: Sandbox, cfg: SmokeConfig, *, timeout_s: int = 300) -> SmokeResult:
        start = await box.exec(
            [
                "bash",
                "-lc",
                f"cd {REPO} && setsid nohup {cfg.start_command} > /tmp/app.log 2>&1 & echo $!",
            ],
            cwd=REPO,
            timeout_s=60,
        )
        pid = start.stdout.strip().splitlines()[-1].strip() if start.stdout.strip() else ""
        try:
            await box.write_text(SCRIPT_PATH, SMOKE_SCRIPT)
            args = json.dumps(
                {
                    "port": cfg.port,
                    "paths": cfg.paths,
                    "names": cfg.screenshots,
                    "ready_timeout_s": cfg.ready_timeout_s,
                    "shot_dir": SHOT_DIR,
                }
            )
            res = await box.exec(["node", SCRIPT_PATH, args], cwd=REPO, timeout_s=timeout_s)
            if res.timed_out:
                return SmokeResult(False, (), {}, (), f"smoke timed out after {timeout_s} s")
            out = parse_smoke_output(res.stdout)
            statuses = {str(k): int(v) for k, v in dict(out.get("statuses") or {}).items()}
            errors = tuple(str(e) for e in out.get("console_errors") or ())
            if not out:
                return SmokeResult(False, (), {}, (), "smoke script printed no result")
            if not out.get("ready"):
                detail = f"app not ready on port {cfg.port} after {cfg.ready_timeout_s} s"
                return SmokeResult(False, (), statuses, errors, detail)
            bad = [
                f"{p} {statuses.get(p, 0)}"
                for p in cfg.paths
                if not 200 <= statuses.get(p, 0) < 400
            ]
            shots: list[tuple[str, bytes]] = []
            for name in cfg.screenshots:
                data = await box.read_bytes(f"{SHOT_DIR}/{name}.png")
                if data is not None:
                    shots.append((name, data))
            if bad:
                return SmokeResult(
                    False, tuple(shots), statuses, errors, "bad status: " + ", ".join(bad)
                )
            if len(shots) != len(cfg.screenshots):
                missing = sorted(set(cfg.screenshots) - {n for n, _ in shots})
                return SmokeResult(
                    False,
                    tuple(shots),
                    statuses,
                    errors,
                    "missing screenshots: " + ", ".join(missing),
                )
            return SmokeResult(True, tuple(shots), statuses, errors, f"{len(cfg.paths)} paths ok")
        finally:
            if pid.isdigit():
                await box.exec(["bash", "-lc", f"kill -- -{pid} 2>/dev/null; true"], timeout_s=30)

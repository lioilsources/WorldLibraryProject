#!/usr/bin/env python3
"""L40S filler: enrich the corpus whenever the L40S queue has nothing else.

The L40S queue (qctl job `wlp-filler`, profile K) signals through markers in
the queue directory on the VM (L40S_QUEUE in rag/.env, reached over ssh as
L40S_SSH):

  WLP_F_READY  exists only while the filler slot is up and vLLM serves :8000
  WLP_F_STOP   the slot is being taken away — stop clients within 1–2 min
  WLP_F_DONE   we touch it once the corpus is finished; the filler ends for good

Clients run on this Mac (PG on JODA, vLLM through an ssh tunnel on
localhost:8000). Every 30 s the markers are polled; connection errors count as
a pause, never as a failure. Killing a client is safe — resume is in the DB
(input_sha), so a restart repeats nothing.

    nohup .venv/bin/python3 l40s_filler.py > logs/l40s_filler.log 2>&1 & disown
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from enrich_chunks import load_dotenv  # noqa: E402

HOST = Q = ""  # L40S_SSH, L40S_QUEUE from rag/.env, set in main()
URL = "http://localhost:8000/v1"
MODEL = os.getenv("MODEL", "qwen3-30b-a3b")
PRIORITY = os.getenv("PRIORITY", "3")
CHUNK_WORKERS = os.getenv("CHUNK_WORKERS", "128")
# Night of 2026-11-17: 2.4–3 % of chunks hit the default 900-token cap and stay
# unenriched; the left-overs are retried with a higher cap.
CHUNK_MAX_TOKENS = os.getenv("CHUNK_MAX_TOKENS", "1400")
# Chapter prompts are ~32k tokens: while chunks run, few chapter workers keep
# the KV cache for chunks; once chunks are done, chapters get the GPU.
CHAPTER_WORKERS_SHARED = os.getenv("CHAPTER_WORKERS_SHARED", "8")
CHAPTER_WORKERS_ALONE = os.getenv("CHAPTER_WORKERS_ALONE", "24")
# Work summaries read the chapter summaries, so they run once chunks are done.
WORK_WORKERS = os.getenv("WORK_WORKERS", "16")
# Left-over share that counts as finished: a few chunks/chapters fail on
# every attempt (truncated output, over context) and must not block DONE.
DONE_SHARE = float(os.getenv("DONE_SHARE", "0.01"))
POLL = 30
PY = str(HERE / ".venv/bin/python3")
LOGS = HERE / "logs"


def ts() -> str:
    return time.strftime("%F %T")


def log(msg: str) -> None:
    print(f"{ts()} {msg}", flush=True)


def ssh(cmd: str) -> int | None:
    """Exit code of a command on the VM, None when the VM is unreachable."""
    try:
        r = subprocess.run(["ssh", "-n", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", HOST, cmd],
                           capture_output=True, timeout=40)
    except subprocess.TimeoutExpired:
        return None
    return None if r.returncode == 255 else r.returncode


def marker(name: str) -> bool | None:
    rc = ssh(f"test -e {Q}/{name}")
    return None if rc is None else rc == 0


def vllm_up() -> bool:
    try:
        with urllib.request.urlopen(f"{URL}/models", timeout=5) as r:
            return f'"{MODEL}"' in r.read().decode()
    except Exception:
        return False


def remaining() -> tuple[int, int, int, int, int, int] | None:
    """(chunks left/total, chapters left/total, works left/total) for priority ≤ PRIORITY."""
    try:
        import psycopg
        with psycopg.connect(os.environ["PG_DSN"], connect_timeout=10) as c, c.cursor() as cur:
            cur.execute("""SELECT count(*) FILTER (WHERE e.chunk_id IS NULL), count(*)
                           FROM chunks ch JOIN works w ON w.id = ch.work_id
                           LEFT JOIN chunk_enrichment e ON e.chunk_id = ch.id
                           WHERE w.priority <= %s""", (int(PRIORITY),))
            a, b = cur.fetchone()
            cur.execute("""SELECT count(*) FILTER (WHERE ch.summary_medium IS NULL), count(*)
                           FROM chapters ch JOIN works w ON w.id = ch.work_id
                           WHERE ch.chunk_count > 0 AND w.priority <= %s""", (int(PRIORITY),))
            c_, d = cur.fetchone()
            cur.execute("""SELECT count(*) FILTER (WHERE summary_input_sha IS NULL OR summary_input_sha NOT LIKE 'work-v1:%%'), count(*)
                           FROM works WHERE priority <= %s""", (int(PRIORITY),))
            e, f = cur.fetchone()
            return a, b, c_, d, e, f
    except Exception as e:  # DB hiccup = unknown, not finished
        log(f"remaining: {e}")
        return None


class Client:
    def __init__(self, name: str, args: list[str]):
        self.name, self.args, self.proc = name, args, None
        self.fast_exits = 0

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, extra: list[str] = ()) -> None:
        f = open(LOGS / f"l40s_filler_{self.name}.log", "a")
        self.proc = subprocess.Popen([PY, *self.args, *extra], cwd=HERE, stdout=f, stderr=subprocess.STDOUT)
        self.started = time.time()
        log(f"start {self.name} {' '.join(extra)} pid={self.proc.pid}")

    def stop(self) -> None:
        if not self.running():
            return
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        log(f"stopped {self.name}")

    def reap(self) -> int | None:
        """Exit code if the client finished on its own since the last check."""
        if self.proc is None or self.proc.poll() is None:
            return None
        rc = self.proc.returncode
        self.fast_exits = self.fast_exits + 1 if time.time() - self.started < 300 else 0
        log(f"{self.name} exited rc={rc} after {int(time.time() - self.started)} s")
        self.proc = None
        return rc


def main() -> int:
    global HOST, Q
    load_dotenv(HERE / ".env")
    HOST, Q = os.environ["L40S_SSH"], os.environ["L40S_QUEUE"]
    LOGS.mkdir(exist_ok=True)
    base = ["--llm-url", URL, "--model", MODEL, "--priority", PRIORITY]
    chunks = Client("chunks", ["enrich_chunks.py", *base, "--order", "depth"])
    chapters = Client("chapters", ["enrich_chapters.py", *base, "--max-model-len", "32768"])
    works = Client("works", ["enrich_works.py", *base, "--workers", WORK_WORKERS])
    chunks_idle = False  # chunks queue drained (only stubborn failures left)
    active = False

    def shutdown(*_):
        chunks.stop(); chapters.stop(); works.stop()
        log("filler stopped by signal")
        sys.exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log(f"filler up: model={MODEL} priority<={PRIORITY}")
    while True:
        ready, stop = marker("WLP_F_READY"), marker("WLP_F_STOP")
        go = ready is True and stop is not True and vllm_up()
        if not go:
            if active:
                chunks.stop(); chapters.stop(); works.stop()
                why = "STOP" if stop else ("VM unreachable" if ready is None else "READY gone or vLLM down")
                log(f"pause ({why})")
                active = False
            time.sleep(POLL)
            continue
        if not active:
            log("READY — resuming")
            active = True

        for cl in (chunks, chapters, works):
            cl.reap()

        if not chunks.running() and not chunks_idle:
            if chunks.fast_exits >= 2:
                chunks_idle = True
                log("chunks queue drained; chapters get the GPU")
                chapters.stop()
            else:
                chunks.start(["--workers", CHUNK_WORKERS, "--max-tokens", CHUNK_MAX_TOKENS])
        # Start work summaries before the DONE check below, which would otherwise
        # reset chunks_idle on the first pass and never let them run.
        if chunks_idle and not works.running() and works.fast_exits < 2:
            works.start()
        if not chapters.running():
            if chapters.fast_exits >= 2 and chunks_idle and works.running():
                pass  # chapters drained; wait for the work summaries before checking DONE
            elif chapters.fast_exits >= 2 and chunks_idle:
                rem = remaining()
                log(f"remaining: {rem}")
                if (rem and rem[0] <= DONE_SHARE * rem[1] and rem[2] <= DONE_SHARE * rem[3]
                        and rem[4] <= DONE_SHARE * rem[5]):
                    rc = ssh(f"touch {Q}/WLP_F_DONE")
                    log(f"corpus done — WLP_F_DONE {'sent' if rc == 0 else f'FAILED rc={rc}'}")
                    return 0
                # not done yet: something else wrote back or failures piled up
                chunks_idle = False; chunks.fast_exits = chapters.fast_exits = works.fast_exits = 0
            elif chapters.fast_exits >= 2:
                pass  # chapters drained before chunks; re-check once chunks are done
            else:
                w = CHAPTER_WORKERS_ALONE if chunks_idle else CHAPTER_WORKERS_SHARED
                chapters.start(["--workers", w])
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())

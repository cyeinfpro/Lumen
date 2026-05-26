"""Executable entrypoint for the desktop worker sidecar."""

from __future__ import annotations

from arq.worker import run_worker

from app.main import WorkerSettings


def main() -> None:
    run_worker(WorkerSettings)


if __name__ == "__main__":
    main()

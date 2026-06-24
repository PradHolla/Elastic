from __future__ import annotations

import argparse
import signal

from .service import build_worker_context, poll_once, run_worker_loop


def main() -> None:
    parser = argparse.ArgumentParser(description="Elastic worker queue processor")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Poll once and process a single message.")
    mode.add_argument("--loop", action="store_true", help="Keep polling and processing messages until shutdown.")
    parser.add_argument(
        "--delete",
        dest="delete",
        action="store_true",
        default=True,
        help="Delete messages after processing. This is the default.",
    )
    parser.add_argument(
        "--no-delete",
        dest="delete",
        action="store_false",
        help="Leave messages on the queue after processing.",
    )
    parser.add_argument(
        "--wait-time-seconds",
        type=int,
        default=5,
        help="SQS long-poll wait time used by the worker.",
    )
    args = parser.parse_args()

    context = build_worker_context()
    _install_signal_handlers(context.shutdown_event)

    if args.once:
        poll_once(delete=args.delete, context=context, wait_time_seconds=args.wait_time_seconds)
        return

    run_worker_loop(context=context, delete=args.delete, wait_time_seconds=args.wait_time_seconds)


def _install_signal_handlers(shutdown_event) -> None:
    def _handle_signal(signum, _frame) -> None:
        print(f"[worker] received signal {signum}; requesting shutdown")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)


if __name__ == "__main__":
    main()

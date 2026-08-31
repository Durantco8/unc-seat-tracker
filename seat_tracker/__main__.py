"""Entry point: python -m seat_tracker"""

import argparse
import logging

from seat_tracker.poller import run_poll_loop


def main():
    parser = argparse.ArgumentParser(description="UNC Seat Tracker polling loop")
    parser.add_argument("--db", default="seat_tracker.db", help="SQLite database path")
    parser.add_argument(
        "--interval", type=int, default=180,
        help="Seconds between polling passes (default: 180)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    run_poll_loop(db_path=args.db, interval_seconds=args.interval)


if __name__ == "__main__":
    main()

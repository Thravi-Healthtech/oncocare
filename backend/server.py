from __future__ import annotations

import argparse
import os
from http.server import ThreadingHTTPServer
from pathlib import Path


def load_env(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(Path(os.environ.get("ONCOCARE_ENV_FILE", Path(__file__).resolve().parents[1] / ".env")))

from .api import make_handler  # noqa: E402
from .database import migrate, seed_demo  # noqa: E402
from .extraction import ai_status  # noqa: E402


def start_scheduler(db_path: str) -> None:
    """Every minute: send scheduled WhatsApp reminders that are due and close idle WhatsApp check-ins."""
    import threading
    import time
    from . import whatsapp

    def loop() -> None:
        while True:
            try:
                whatsapp.run_due(db_path)
            except Exception as error:  # keep the scheduler alive
                print(f"WhatsApp scheduler: {type(error).__name__}: {error}")
            time.sleep(60)
    threading.Thread(target=loop, name="whatsapp-scheduler", daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="Thravi OncoCare local API server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--db", default="data/oncocare.db")
    parser.add_argument("--static-dir", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--seed", action="store_true", help="Add the synthetic demo patients whose MRNs are missing")
    args = parser.parse_args()

    if os.environ.get("ONCOCARE_PUBLIC", "").lower() in {"1", "true", "yes"}:
        password = os.environ.get("ONCOCARE_DEV_PASSWORD", "")
        if len(password) < 8 or password == "1234":
            raise SystemExit("ONCOCARE_PUBLIC is set: ONCOCARE_DEV_PASSWORD must be set to a value of 8+ characters.")
    migrate(args.db)
    if args.seed:
        seed_demo(args.db)
    handler = make_handler(args.db, args.static_dir)
    start_scheduler(args.db)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Thravi OncoCare running at http://{args.host}:{args.port}")
    print(f"Clinician app: http://{args.host}:{args.port}/clinician · Patient portal: http://{args.host}:{args.port}/portal")
    print(f"API health: http://{args.host}:{args.port}/health")
    status = ai_status()
    print(f"AI lab extraction: {'ON (' + status['model'] + ')' if status['enabled'] else 'OFF - ' + status['hint']}")
    from .whatsapp import status as wa_status
    wa = wa_status()
    print(f"WhatsApp: {wa['mode']}" + (f" ({wa['note']})" if wa["note"] else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()


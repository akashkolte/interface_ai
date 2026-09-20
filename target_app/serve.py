"""Run both tenant instances.

Two origins, one codebase -- the stand-in for two institutions running the same
vendor product:

    base           http://localhost:5000   CoreServicing 7.2
    creditunion_b  http://localhost:5001   CoreServicing 7.4  (relabelled, extra
                                                               disclosure step)

    python -m target_app.serve
"""

from __future__ import annotations

import logging
import threading

from werkzeug.serving import make_server

from target_app.app import create_app
from target_app.tenants import TENANTS


def _serve(tenant_id: str) -> None:
    cfg = TENANTS[tenant_id]
    server = make_server("127.0.0.1", cfg.port, create_app(tenant_id), threaded=True)
    server.serve_forever()


def main() -> None:
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    threads = [
        threading.Thread(target=_serve, args=(tid,), daemon=True)
        for tid in TENANTS
    ]
    for t in threads:
        t.start()
    for tid, cfg in TENANTS.items():
        print(f"  {tid:15} http://localhost:{cfg.port}   {cfg.display_name} ({cfg.app_version})")
    print("\nCtrl-C to stop.")
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()

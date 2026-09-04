from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .service import BrainMemoryService
from .runtime import build_vector_index_from_env
from .api import BrainAPIServer


def default_db() -> Path:
    return Path(os.environ.get("BRAIN_DB_PATH", "./brain-data/brain.db"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain-foundation")
    parser.add_argument("--db", type=Path, default=default_db())
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    sub.add_parser("rebuild-index")
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--host", default=os.environ.get("BRAIN_HOST", "127.0.0.1"))
    p_serve.add_argument("--port", type=int, default=int(os.environ.get("BRAIN_PORT", "8765")))
    p_status = sub.add_parser("status")
    p_status.add_argument("--owner")
    p_search = sub.add_parser("search")
    p_search.add_argument("query")
    p_search.add_argument("--owner", required=True)
    p_search.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)

    service = BrainMemoryService(args.db, vector_index=build_vector_index_from_env())
    if args.cmd == "health":
        print(json.dumps(service.health(), indent=2))
    elif args.cmd == "rebuild-index":
        print(json.dumps(service.rebuild_vector_index(), indent=2, sort_keys=True))
    elif args.cmd == "serve":
        api_key = os.environ.get("JARVIS_BRAIN_API_KEY", "")
        server = BrainAPIServer((args.host, args.port), service, api_key)
        print(f"Jarvis Brain API listening on {args.host}:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    elif args.cmd == "status":
        print(json.dumps(service.store.counts(args.owner), indent=2, sort_keys=True))
    elif args.cmd == "search":
        print(json.dumps([hit.__dict__ for hit in service.search(owner_id=args.owner, query=args.query, limit=args.limit)], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

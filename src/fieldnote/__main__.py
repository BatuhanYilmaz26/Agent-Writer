"""User-facing Fieldnote commands. Run from the repository root."""

import argparse
import json
from pathlib import Path

from fieldnote.logging_config import configure_logging
from fieldnote.runtime import run_async
from fieldnote.settings import Settings


def main():
    parser = argparse.ArgumentParser(prog="python -m fieldnote", description="Fieldnote local development tools")
    parser.add_argument("--env-file", type=Path, help="Explicit dotenv configuration file")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("settings", help="Show a sanitized configuration summary")
    commands.add_parser("serve", help="Start the loopback API and browser workspace")
    worker = commands.add_parser("worker", help="Process durable queued runs")
    worker.add_argument("--once", action="store_true", help="Process at most one currently due job")
    retention = commands.add_parser("retention", help="Purge expired events, sources, sessions and old terminal runs")
    retention.add_argument("--batch-size", type=int, default=100)
    demo = commands.add_parser("demo", help="Write a synthetic transient fixture draft without a database")
    demo.add_argument("--topic", default="A practical guide to building a writing habit")
    demo.add_argument("--output", type=Path, default=Path(".local/demo.md"))
    args = parser.parse_args()
    try:
        settings = (
            Settings(_env_file=args.env_file)  # type: ignore[call-arg]
            if args.env_file
            else Settings()
        )
        configure_logging(settings)
        if args.command == "settings":
            print(json.dumps(settings.sanitized_summary(), indent=2))
        elif args.command == "serve":
            import uvicorn

            from fieldnote.main import create_app

            if settings.database_url is None:
                parser.error("Set DATABASE_URL to your PostgreSQL runtime connection string first.")
            uvicorn.run(create_app(settings), host=settings.bind_host, port=settings.port,
                        loop="fieldnote.runtime:loop_factory", proxy_headers=False, access_log=False)
        elif args.command == "worker":
            from fieldnote.worker.runner import serve_worker

            run_async(serve_worker(settings, once=args.once))
        elif args.command == "retention":
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

            from fieldnote.storage.pool import database_pool
            from fieldnote.storage.repository import Repository

            if settings.database_url is None:
                parser.error("Set DATABASE_URL before running retention.")
            dsn = settings.database_url.get_secret_value()

            async def purge():
                async with database_pool(
                    dsn,
                    min_size=settings.database_pool_min_size,
                    max_size=settings.database_pool_max_size,
                    timeout=settings.database_pool_timeout,
                ) as pool:
                    repository = Repository(
                        pool, dsn, graph_version=settings.graph_version
                    )
                    async with pool.connection() as connection:
                        saver = AsyncPostgresSaver(
                            connection,
                            serde=JsonPlusSerializer(
                                pickle_fallback=False,
                                allowed_msgpack_modules=None,
                                allowed_json_modules=None,
                            ),
                        )
                        return await repository.purge_expired(
                            event_retention_days=settings.event_retention_days,
                            source_retention_days=settings.source_retention_days,
                            run_retention_days=settings.run_retention_days,
                            deleted_grace_hours=settings.deleted_run_grace_hours,
                            batch_size=args.batch_size,
                            delete_checkpoint=saver.adelete_thread,
                        )

            print(json.dumps(run_async(purge()), sort_keys=True))
        else:
            from langgraph.checkpoint.memory import InMemorySaver

            from fieldnote.domain import CreateRunRequest
            from fieldnote.graph import build_graph, initial_state, make_fixture_dependencies

            async def generate_demo():
                deps = make_fixture_dependencies(CreateRunRequest(topic=args.topic))
                graph = build_graph(deps).compile(checkpointer=InMemorySaver())
                seed = initial_state()
                return await graph.ainvoke(seed, {"configurable": {"thread_id": seed["run_id"]}})

            result = run_async(generate_demo())
            args.output.parent.mkdir(parents=True, exist_ok=True)
            if args.output.exists():
                parser.error("Output already exists. Choose a new --output path.")
            args.output.write_text(result["final_markdown"], encoding="utf-8")
            print(f"Synthetic draft saved: {args.output}. Review required; no live AI/research or durable database was used.")
    except (ValueError, RuntimeError) as exc:
        # Configuration validation hides inputs, while raw connection exceptions
        # must not become a convenient way to print secrets.
        parser.error(str(exc) if isinstance(exc, RuntimeError) else "Invalid configuration or brief. Check the supplied settings and input fields.")


if __name__ == "__main__":
    main()

"""User-facing AgentWriter commands. Run from the repository root."""

import argparse
import json
from pathlib import Path

from agentwriter.runtime import run_async
from agentwriter.settings import Settings


def main():
    parser = argparse.ArgumentParser(prog="python -m agentwriter", description="AgentWriter local development tools")
    parser.add_argument("--env-file", type=Path, help="Explicit dotenv configuration file")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("settings", help="Show a sanitized configuration summary")
    commands.add_parser("serve", help="Start the loopback API and browser workspace")
    worker = commands.add_parser("worker", help="Process durable queued runs")
    worker.add_argument("--once", action="store_true", help="Process at most one currently due job")
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
        if args.command == "settings":
            print(json.dumps(settings.sanitized_summary(), indent=2))
        elif args.command == "serve":
            import uvicorn

            from agentwriter.main import create_app

            if settings.database_url is None:
                parser.error("Set DATABASE_URL to your PostgreSQL runtime connection string first.")
            uvicorn.run(create_app(settings), host=settings.bind_host, port=settings.port,
                        loop="agentwriter.runtime:loop_factory", proxy_headers=False, access_log=False)
        elif args.command == "worker":
            from agentwriter.worker.runner import serve_worker

            run_async(serve_worker(settings, once=args.once))
        else:
            from langgraph.checkpoint.memory import InMemorySaver

            from agentwriter.domain import CreateRunRequest
            from agentwriter.graph import build_graph, initial_state, make_fixture_dependencies

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

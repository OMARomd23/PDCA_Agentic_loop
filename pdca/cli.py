"""pdca CLI entry point."""
import sys

import typer

from pdca import config

app = typer.Typer(add_completion=False)


@app.command()
def main(
    task: str = typer.Argument(..., help="The task to accomplish"),
    loop: bool = typer.Option(False, "--loop", help="Cycle until ADOPT or a budget stop"),
    max_cycles: int = typer.Option(0, "--max-cycles",
                                   help="Optional cycle cap (0 = unlimited; budgets govern)"),
    max_seconds: int = typer.Option(config.MAX_SECONDS, "--max-seconds"),
    max_tokens: int = typer.Option(config.MAX_TOKENS_TOTAL, "--max-tokens",
                                   help="Total token budget — the primary stop"),
    workdir: str = typer.Option("./work", "--workdir"),
):
    """Run a PDCA loop on TASK. Without --loop: one full cycle, then the check report."""
    # Phase lines must appear live even when stdout is piped to a file/log.
    sys.stdout.reconfigure(line_buffering=True)
    from pdca import runner  # deferred: importing it builds the LLM client

    raise typer.Exit(runner.run(task, workdir, loop, max_cycles, max_seconds, max_tokens))


if __name__ == "__main__":
    app()

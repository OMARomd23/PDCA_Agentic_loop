"""pdca CLI entry point.

Two usage shapes share one binary:
  pdca "<task>" [--loop ...]      run a PDCA loop now (the historical default)
  pdca schedule add|list|remove|run ...   manage recurring scheduled runs

To keep the bare ``pdca "<task>"`` form working now that subcommands exist, the
``entry`` wrapper injects an implicit ``run`` subcommand when the first argument
is not a known subcommand (or a help flag). Explicit ``pdca run "<task>"`` works
too — handy if a task text happens to collide with a subcommand name.
"""
import sys

import typer

from pdca import config

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="PDCA-loop autonomous coding agent (DeepSeek).")
schedule_app = typer.Typer(no_args_is_help=True,
                           help="Manage recurring, self-directed pdca runs.")
app.add_typer(schedule_app, name="schedule")

# First-token values that must NOT be rewritten into the implicit `run` command.
_KNOWN = {"run", "schedule", "_run-scheduled"}


@app.command("run")
def run_cmd(
    task: str = typer.Argument(..., help="The task to accomplish"),
    loop: bool = typer.Option(False, "--loop", help="Cycle until ADOPT or a budget stop"),
    max_cycles: int = typer.Option(0, "--max-cycles",
                                   help="Optional cycle cap (0 = unlimited; budgets govern)"),
    max_seconds: int = typer.Option(config.MAX_SECONDS, "--max-seconds"),
    max_tokens: int = typer.Option(config.MAX_TOKENS_TOTAL, "--max-tokens",
                                   help="Total token budget — the primary stop"),
    workdir: str = typer.Option("./work", "--workdir"),
    notify: bool = typer.Option(False, "--notify",
                                help="Email a status report when the run finishes "
                                     "(requires --notify-to)"),
    notify_to: str = typer.Option("", "--notify-to",
                                  help="Recipient email for --notify (required when --notify is set)"),
):
    """Run a PDCA loop on TASK. Without --loop: one full cycle, then the check report."""
    if notify and not notify_to:
        typer.echo("error: --notify requires --notify-to <email>")
        raise typer.Exit(2)
    # Phase lines must appear live even when stdout is piped to a file/log.
    sys.stdout.reconfigure(line_buffering=True)
    from pdca.core import runner  # deferred: importing it builds the LLM client

    raise typer.Exit(runner.run(task, workdir, loop, max_cycles, max_seconds,
                                max_tokens, trigger="manual",
                                notify=notify, notify_to=notify_to))


@app.command("_run-scheduled", hidden=True)
def run_scheduled(name: str = typer.Argument(..., help="Stored job name")):
    """Internal: the entrypoint a cron line calls. Runs a stored job once as a
    fresh, complete pdca run (tagged trigger=scheduled:<name>)."""
    sys.stdout.reconfigure(line_buffering=True)
    from pdca.scheduling import scheduler

    raise typer.Exit(scheduler.run_once(name))


@schedule_app.command("add")
def schedule_add(
    prompt: str = typer.Argument(..., help="The task prompt to run on each wake-up"),
    every: str = typer.Option(..., "--every",
                              help="5m / 30m / 2h / hourly / daily / weekly, or a 5-field cron expr"),
    name: str = typer.Option(..., "--name", help="Unique job name ([A-Za-z0-9_-])"),
    workdir: str = typer.Option("", "--workdir",
                                help="Workdir for the run (default ~/.pdca_agent/work/<name>)"),
    notify: bool = typer.Option(False, "--notify",
                                help="Email a status report after each scheduled run "
                                     "(requires --notify-to)"),
    notify_to: str = typer.Option("", "--notify-to",
                                  help="Recipient email for --notify (required when --notify is set)"),
):
    """Store a job and install an idempotent crontab line that runs it."""
    if notify and not notify_to:
        typer.echo("error: --notify requires --notify-to <email>")
        raise typer.Exit(2)
    from pdca.scheduling import scheduler

    try:
        job = scheduler.add(name, prompt, every, workdir or None,
                            notify_to=notify_to if notify else None)
    except ValueError as e:
        typer.echo(f"error: {e}")
        raise typer.Exit(1) from e
    typer.echo(f"added job '{name}': schedule '{job['every']}' -> cron '{job['cron']}' (installed)")
    typer.echo(f"  prompt:  {job['prompt']}")
    typer.echo(f"  workdir: {job['workdir']}")
    typer.echo(f"  notify:  {job['notify_to'] or 'off'}")


@schedule_app.command("list")
def schedule_list():
    """Show installed jobs (name, schedule, prompt, workdir, last run)."""
    from pdca.scheduling import scheduler

    jobs = scheduler.list_jobs()
    if not jobs:
        typer.echo("(no scheduled jobs)")
        return
    for j in jobs:
        flag = "cron✓" if j["installed"] else "cron✗ (line missing!)"
        typer.echo(f"- {j['name']}  [{j['every']} -> {j['cron']}]  {flag}  "
                   f"last_run={j['last_run'] or 'never'}")
        typer.echo(f"    prompt:  {j['prompt']}")
        typer.echo(f"    workdir: {j['workdir']}")
        typer.echo(f"    notify:  {j.get('notify_to') or 'off'}")


@schedule_app.command("remove")
def schedule_remove(name: str = typer.Argument(..., help="Job name to remove")):
    """Remove a job's crontab line and its stored entry."""
    from pdca.scheduling import scheduler

    removed = scheduler.remove(name)
    typer.echo(f"removed '{name}' (crontab line + stored job)" if removed
               else f"no stored job '{name}' (any matching crontab line was still cleared)")


@schedule_app.command("run")
def schedule_run(name: str = typer.Argument(..., help="Job name to run now")):
    """Run a stored job once, immediately — for testing without waiting for cron."""
    sys.stdout.reconfigure(line_buffering=True)
    from pdca.scheduling import scheduler

    raise typer.Exit(scheduler.run_once(name))


def entry():
    """Console-script entry. Injects the implicit `run` subcommand so that
    `pdca "<free text task>" [opts]` keeps working alongside `pdca schedule ...`."""
    argv = sys.argv[1:]
    help_flags = {"--help", "-h", "--install-completion", "--show-completion"}
    if argv and argv[0] not in _KNOWN and argv[0] not in help_flags:
        sys.argv.insert(1, "run")
    app()


if __name__ == "__main__":
    entry()

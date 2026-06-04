"""`homelabsage env-diff <container>` — diff running env vs incoming image env.

Reads the container's effective env via the docker SDK, fetches the
image's declared `Config.Env` from a `docker pull` or the local cache,
then prints the diff with severity colouring. Exits non-zero when any
finding has severity ≥ medium so the CLI can drive cron alerts.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ..config import load_config
from ..env_diff import diff as compute_diff
from ._common import CONFIG_OPT, VERBOSE_OPT, app, console, setup_logging


@app.command(name="env-diff")
def env_diff_cmd(
    container: str = typer.Argument(..., help="Container name or id."),
    config: Path = CONFIG_OPT,
    incoming_image: str = typer.Option(
        "", "--image",
        help=(
            "Image ref to compare against. Defaults to the same "
            "image:tag the container is currently running (which "
            "is useful for an Unraid-style 'pull latest before "
            "applying' workflow)."
        ),
    ),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Diff a container's effective env against an incoming image's env."""
    setup_logging(verbose)
    _ = load_config(config)   # ensure config is valid even if unused
    try:
        import docker
    except ImportError:
        console.print(
            "[red]The `docker` package isn't installed.[/red] "
            "Install it with `pip install docker`."
        )
        raise SystemExit(2)  # noqa: B904

    client = docker.from_env()
    try:
        c = client.containers.get(container)
    except Exception as e:
        console.print(f"[red]Couldn't fetch container {container!r}: {e}[/red]")
        raise SystemExit(2)  # noqa: B904

    container_env = (c.attrs.get("Config") or {}).get("Env") or []
    image_ref = incoming_image or (
        (c.attrs.get("Config") or {}).get("Image") or ""
    )
    if not image_ref:
        console.print("[red]Container has no image ref; can't diff.[/red]")
        raise SystemExit(2)

    try:
        img = client.images.get(image_ref)
    except Exception:
        console.print(f"[yellow]Pulling {image_ref}…[/yellow]")
        try:
            img = client.images.pull(image_ref)
        except Exception as e:
            console.print(f"[red]docker pull failed: {e}[/red]")
            raise SystemExit(2)  # noqa: B904

    image_env = (img.attrs.get("Config") or {}).get("Env") or []
    findings = compute_diff(container_env, image_env)
    if not findings:
        console.print("[green]No env differences.[/green]")
        raise SystemExit(0)

    bad = 0
    for f in findings:
        colour = {
            "high": "red", "medium": "yellow", "info": "cyan",
        }.get(f.severity, "white")
        if f.severity in ("medium", "high"):
            bad += 1
        if f.kind == "new":
            console.print(
                f"[{colour}]NEW[/{colour}] {f.name} → "
                f"image default = {f.image_value!r}"
            )
        elif f.kind == "removed":
            console.print(
                f"[{colour}]REMOVED[/{colour}] {f.name} = "
                f"{f.container_value!r} (no longer in image)"
            )
        else:
            console.print(
                f"[{colour}]CHANGED[/{colour}] {f.name}: "
                f"container={f.container_value!r} image={f.image_value!r}"
            )
    raise SystemExit(0 if bad == 0 else 1)

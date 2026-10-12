"""``gtm-bridge`` — pair once, then run.

Two commands because the user only ever does two things: prove to the control
plane that this machine is theirs, and leave it running.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import httpx
from rich.console import Console

from gtmos_bridge import __version__
from gtmos_bridge.budget import DailyBudget
from gtmos_bridge.config import STATE_DIR, load_credentials, new_bridge_id, save_credentials
from gtmos_bridge.ledger import ActionLedger
from gtmos_bridge.executor import Executor, ExecutorError
from gtmos_bridge.linkedin import LinkedInExecutor
from gtmos_bridge.routing import RoutingExecutor
from gtmos_bridge.tunnel import Tunnel
from gtmos_bridge.web import WebExecutor

console = Console()


async def _pair(url: str) -> int:
    """Device-code pairing: the bridge asks, the user approves in a browser."""
    base = url.rstrip("/")
    async with httpx.AsyncClient(timeout=30.0) as client:
        start = await client.post(f"{base}/bridge/pair/start")
        start.raise_for_status()
        data = start.json()
        code, verify_url = data["code"], data["verification_url"]

        console.print()
        console.print("  Approve this bridge at:", style="bold")
        console.print(f"    {verify_url}", style="cyan")
        console.print(f"  Code: [bold yellow]{code}[/bold yellow]")
        console.print()
        console.print("  Waiting for approval…", style="dim")

        deadline = data.get("expires_in", 600)
        interval = data.get("interval", 3)
        waited = 0
        while waited < deadline:
            await asyncio.sleep(interval)
            waited += interval
            poll = await client.post(f"{base}/bridge/pair/poll", json={"code": code})
            if poll.status_code == 200:
                token = poll.json()["token"]
                bridge_id = new_bridge_id()
                save_credentials(base, token, bridge_id, poll.json().get("daily_limit", 100))
                console.print("  [green]Paired.[/green] Now run: [bold]gtm-bridge run[/bold]")
                return 0
            if poll.status_code not in (202, 428):
                console.print(f"  [red]Pairing failed:[/red] {poll.text}")
                return 1
    console.print("  [red]Pairing code expired.[/red] Run `gtm-bridge pair` again.")
    return 1


async def _run(*, web: bool) -> int:
    config = load_credentials()
    if config is None:
        console.print("  [red]Not paired.[/red] Run: [bold]gtm-bridge pair --url <control-plane>[/bold]")
        return 1

    console.print(f"  Starting LinkedIn session (profile stays on this machine)…", style="dim")
    executor = LinkedInExecutor()
    await executor.start()

    authenticated, _ = await executor.health()
    if not authenticated:
        console.print("  [yellow]LinkedIn session is not valid.[/yellow] Log in via the "
                      "browser window the server opens, then restart.")

    routed: Executor = executor
    web_agent: WebExecutor | None = None
    if web:
        console.print("  Starting web agent (read-only, own browser profile)…", style="dim")
        try:
            web_agent = WebExecutor()
            await web_agent.start()
            routed = RoutingExecutor(executor, web_agent)
        except ExecutorError as exc:
            # The LinkedIn half still works; say so rather than refusing to run.
            console.print(f"  [yellow]Web agent unavailable:[/yellow] {exc}")
            web_agent = None

    tunnel = Tunnel(
        config.ws_url,
        config.token,
        bridge_id=config.bridge_id,
        version=__version__,
        executor=routed,
        ledger=ActionLedger(STATE_DIR / "ledger.db"),
        budget=DailyBudget(STATE_DIR / "budget.db", limit=config.daily_limit),
        linkedin_username=executor.linkedin_username,
    )
    console.print(f"  Connecting to {config.control_plane_url}…", style="dim")
    try:
        await tunnel.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if web_agent is not None:
            await web_agent.aclose()
        await executor.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gtm-bridge", description=__doc__)
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    pair = sub.add_parser("pair", help="Attach this machine to your GTM-OS account")
    pair.add_argument("--url", required=True, help="Control plane URL, e.g. https://api.gtm-os.dev")

    run = sub.add_parser("run", help="Run the bridge (leave this open)")
    run.add_argument(
        "--web",
        action="store_true",
        help="Also serve read-only web tools via the Stagehand agent in ~/stagehand-agent",
    )
    sub.add_parser("status", help="Show pairing and budget state")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.command == "pair":
        return asyncio.run(_pair(args.url))
    if args.command == "run":
        return asyncio.run(_run(web=args.web))
    if args.command == "status":
        config = load_credentials()
        if config is None:
            console.print("  Not paired.")
            return 1
        budget = DailyBudget(STATE_DIR / "budget.db", limit=config.daily_limit)
        console.print(f"  Control plane : {config.control_plane_url}")
        console.print(f"  Bridge id     : {config.bridge_id}")
        console.print(f"  Budget today  : {budget.used_today()}/{budget.limit}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())

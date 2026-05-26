#!/usr/bin/env python3
"""Register C3PO Oracle slash commands with Discord.

Run once (or after command changes). Guild registration is instant; global
registration can take up to 1 hour to propagate.

Usage:
  python3 scripts/register_discord_commands.py --guild-id GUILD_ID   # instant (dev/testing)
  python3 scripts/register_discord_commands.py --global              # global (production)
"""

import os, sys, json, argparse
import requests
from dotenv import load_dotenv

load_dotenv()

APPLICATION_ID = os.environ.get("ORACLE_APPLICATION_ID", "").strip()
BOT_TOKEN      = os.environ.get("ORACLE_BOT_TOKEN", "").strip()

if not APPLICATION_ID or not BOT_TOKEN:
    print("Error: ORACLE_APPLICATION_ID and ORACLE_BOT_TOKEN must be set in .env", file=sys.stderr)
    sys.exit(1)

COMMANDS = [
    {
        "name":        "ask",
        "description": "Ask C3PO a question — synthesized answer from the Protocol Institute corpus",
        "options": [
            {
                "type":        3,       # STRING
                "name":        "question",
                "description": "Your question",
                "required":    True,
            }
        ],
    },
    {
        "name":        "search",
        "description": "Semantic search across the PI corpus — returns sources without synthesis",
        "options": [
            {
                "type":        3,       # STRING
                "name":        "query",
                "description": "Search query",
                "required":    True,
            }
        ],
    },
    {
        "name":        "help",
        "description": "Show C3PO help and links",
    },
]


def register(guild_id=None):
    headers = {
        "Authorization": f"Bot {BOT_TOKEN}",
        "Content-Type":  "application/json",
    }
    base = f"https://discord.com/api/v10/applications/{APPLICATION_ID}"
    if guild_id:
        url   = f"{base}/guilds/{guild_id}/commands"
        scope = f"guild {guild_id} (instant)"
    else:
        url   = f"{base}/commands"
        scope = "global (up to 1h to propagate)"

    res = requests.put(url, headers=headers, json=COMMANDS)
    if res.ok:
        cmds = res.json()
        print(f"Registered {len(cmds)} commands ({scope}):")
        for c in cmds:
            print(f"  /{c['name']} — {c['description']}")
    else:
        print(f"Error {res.status_code}: {res.text}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--guild-id", metavar="ID", help="Register for a specific guild (instant)")
    g.add_argument("--global",   dest="global_", action="store_true", help="Register globally")
    args = p.parse_args()
    register(guild_id=args.guild_id)

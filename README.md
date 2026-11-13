# Bvm Vps V1 Bot — LXC Edition

Made by **MineFlareX**
Repo: https://github.com/mineflarex-collab/Bvm-v1bot

Fully automated Discord bot for managing LXC/LXD-based VPS containers — creation, resizing, suspension, port forwarding, multi-node support, and admin controls, all from Discord.

## Requirements

- A fresh Ubuntu or Debian server (root access)
- A Discord Bot Token ([Discord Developer Portal](https://discord.com/developers/applications))
- Your Discord User ID (main admin)
- `git` installed (`sudo apt install git -y` if missing)

## Installation (from GitHub)

Clone the repo and run the installer as root:

```bash
git clone https://github.com/mineflarex-collab/Bvm-v1bot.git
cd Bvm-v1bot
chmod +x install.sh
sudo ./install.sh
```

Then follow the prompts:
1. Choose your OS (Ubuntu / Debian) when prompted.
2. Enter your **Discord Bot Token** and **Main Admin Discord ID** when asked.

> If you're not using GitHub, just place `bot.py` and `install.sh` in the same folder on your server and run `sudo ./install.sh` directly.

The script will:
- Install and initialize LXD/LXC
- Install Python 3, pip, and required packages (`discord.py`, `requests`)
- Copy `bot.py` to `/root/bot.py`
- Create and enable a `bot.service` systemd unit so the bot runs 24/7 and restarts on failure or reboot

## Managing the Bot

| Action | Command |
|---|---|
| Check status | `systemctl status bot` |
| View live logs | `journalctl -u bot -f` |
| Restart | `sudo systemctl restart bot` |
| Stop | `sudo systemctl stop bot` |
| Disable autostart | `sudo systemctl disable bot` |

## Configuration

Environment variables are set directly in `/etc/systemd/system/bot.service`. After editing that file, apply changes with:

```bash
sudo systemctl daemon-reload
sudo systemctl restart bot
```

Key variables:

| Variable | Description | Default |
|---|---|---|
| `DISCORD_TOKEN` | Your bot's token | — (required) |
| `MAIN_ADMIN_ID` | Your Discord user ID. Supports **multiple IDs**, comma-separated (e.g. `id1,id2,id3`) | — (required) |
| `BOT_NAME` | Display name used in embeds | `Bvm-v1` |
| `PREFIX` | Command prefix | `!` |
| `VPS_USER_ROLE_ID` | Role ID granted to VPS owners | — |
| `DEFAULT_STORAGE_POOL` | LXD storage pool name | `default` |

## Multi-Node Setup (node-agent.py)

To add a remote node, copy `node-agent.py` to the other server (it needs LXD installed and Python 3, no extra packages) and run:

```bash
python3 node-agent.py --api_key=YOUR_KEY --port=5000
```

Then in Discord run `!node create`. The node's **API key is sent to you in DM** (it is never posted in a channel). Use `!node edit <id>` to regenerate it.

## Admin Tools

| Command | What it does |
|---|---|
| `!audit [count] [@admin]` | Activity log - which admin ran which command / used which admin button |
| `!health` | Host CPU, RAM, disk, network speed, internet check |
| `!node-health` | Live status of all nodes (online, latency, CPU/RAM/disk, VPS count) |
| `!alerts [on\|off\|channel #ch]` | Auto alerts (DM to main admins or an admin channel) when CPU/RAM/disk is high or a node goes offline |
| `!maintenance on [reason]` / `off` | Temporarily disables `!manage`, `!manage-shared` and `!ports` for normal users |
| `!backup-status` | Last database backup, age, size and count |
| `!diagnostics [container]` | Detects VPS problems (status mismatch, missing port forwards, no internet/DNS, full disk...) |
| `!dashboard` | Overview of all VPS, nodes, ports, backups and bot status |

## VPS Expiry System

Every VPS can have an expiry date. Set it when creating: `!create 1 1 5 @user 30` (30 days; `0` = no expiry; leave it out to use `!expiry-default`).

- The owner (DM) and the admins (alert channel / DM to main admins) get **automatic reminders** at **7 days, 3 days, 1 day and 3 hours** before expiry. The DM says "your VPS expires in 7 days - contact the administrator to extend your expiry".
- When the VPS **expires it is suspended automatically** (the owner and the admins are told). Renewing it with `!expiry-add` starts it again. A VPS that an admin suspended manually is never started automatically.
- Each reminder is sent only once. The bot checks every 10 minutes.

| Command | Who | What it does |
|---|---|---|
| `!myexpiry` | users | Shows when your VPS expire |
| `!create <ram> <cpu> <disk> @user [days]` | admin | Create a VPS with an optional expiry in days |
| `!expiry-add <container> <days>` (alias `!extend-vps`) | admin | Adds days to the expiry (from today if it already expired) and DMs the owner |
| `!expiry-remove <container> <days>` | admin | Removes days from the expiry (refused if the date would be in the past) |
| `!set-expiry <container> <days\|YYYY-MM-DD\|none>` | admin | Sets an exact expiry, or removes it with `none` |
| `!expiry-list [days]` | admin | Expired VPS and VPS expiring in the next N days (default 7) |
| `!expiry-default [days\|off]` | admin | Expiry for new VPS when `!create` has no days |
| `!expiry-reminders [on\|off]` | admin | Expiry reminders + expiry auto-suspend on/off |

Existing VPS start with **no expiry** (use `!set-expiry`). Dates use the server's time zone. Expiry is shown in `!manage`, `!vpsinfo` and `!dashboard`.

## Auto-Suspend for High Usage (mining / abuse guard)

A running VPS that stays above the **CPU or RAM limit (default 90%) for 5 minutes in a row** is stopped and suspended automatically; the owner gets a DM and the admins an alert. Whitelisted VPS (`!whitelist-vps <container> add`) are skipped.

| Command | What it does |
|---|---|
| `!auto-suspend` | Shows the status |
| `!auto-suspend on [minutes]` / `off` | Turns the guard on/off, optionally sets how many minutes (1-120) |
| `!set-threshold <cpu> <ram>` | Changes the CPU/RAM limits |

Safety net: if more than 3 VPS trigger in the same check, nobody is suspended and the admins get a warning (that is almost always a measuring problem, not mining). Unsuspend with `!unsuspend-vps <container>`.

## Custom Prefix

`!prefix` shows the current prefix. `!prefix 1` changes it (then use `1help`, `1myvps`...). `!prefix reset` goes back to the default from `bot.service`. Saved in the database, so it survives restarts. If you forget it, mention the bot: `@Bot prefix reset`. Main Admin only (also works as `!set-prefix`), 1-5 characters.

## Automatic setup of every VPS (LXC, not Docker)

Every new VPS is a full LXC container and gets all of this **automatically while it is created** (also after an OS reinstall, migrate and clone): nesting/privileged mode, `/dev/net/tun` (Tailscale/VPN), `/dev/fuse`, **`/dev/kvm` + `/dev/vhost-net` when the host supports KVM** (nested virtualization, QEMU, emulators; on hosts without KVM the VPS simply starts without it), a higher open-files limit, and working **root password SSH login** (sshd installed, drop-ins fixed, root unlocked).

**Existing VPS are upgraded automatically too.** After the bot starts (and every 20 minutes) it goes through every VPS that is not upgraded yet and applies: `/dev/net/tun`, `/dev/kvm`, limits, SSH password-login fix (sshd config, root unlocked) and a `tailscaled` restart if Tailscale runs in userspace mode. Running VPS are not restarted, **root passwords and data are never changed**. Stopped VPS are retried automatically once they run. `!fix-vps all` does the same right now for every VPS; `!fix-vps <container>` repairs one VPS (and also re-applies its saved root password). To use KVM the host itself needs it (`ls /dev/kvm` on the host; on a cloud VPS the provider must allow nested virtualization).

## Docker / Pterodactyl / Tailscale inside a VPS

New VPS now get a `/dev/net/tun` device (Tailscale/VPN), a higher open-files limit and every LXC setting is applied separately (one unsupported option no longer skips the rest). The installer/updater also loads `tun`, `overlay` and `br_netfilter` on the host.

**Tailscale + SSH password login not working?** Run `!fix-vps <container>` (VPS must be running). It forces `PermitRootLogin yes` / `PasswordAuthentication yes` (also in `/etc/ssh/sshd_config.d/*.conf`, which cloud images use to disable passwords), installs openssh-server if missing, unlocks root, re-applies the saved root password, restarts sshd, and restarts `tailscaled` so it uses the `/dev/net/tun` device (without it Tailscale runs in userspace mode and incoming SSH over the Tailscale IP does not work). The result shows whether sshd listens on port 22 and whether Tailscale is in kernel mode.

For VPS created before this update run `!fix-vps <container>` (or `!fix-vps all`). It adds the missing settings without touching data or restarting the VPS, and shows a check of Tailscale and Docker (storage driver). Tailscale: `curl -fsSL https://tailscale.com/install.sh | sh && tailscale up`.

## VPS Transfer

Moves a VPS from one user to another **without touching the VPS data**: the container is not stopped, copied, renamed or deleted - only ownership records change (in a single all-or-nothing database transaction).

`!transfer-vps <container> @new_owner [keep-shares] [keep-password]` (admin)

- Shows a confirmation (✅ / ❌, only the admin who ran it can press) before anything changes.
- Moves the VPS record, its **port forwards and the matching port quota**, the VPS role, and keeps the **expiry / suspension / whitelist** state.
- By default it **revokes shared access** (`keep-shares` keeps it) and **resets the root password**, sending the new SSH login to the new owner by DM (`keep-password` skips this). The old owner is notified by DM.
- The container name (hostname) stays the same. The new owner finds the VPS under `!manage` (it is added at the end of their list).
- Written to the audit log as `vps-transfer`. Panels the previous owner still had open stop working ("VPS Changed").

## Admin Management

- `!admin-add @user` / `!admin-remove @user` — manage regular admins by mention (main admin only). Requires the user to be in the server.
- `!add-admin <user_id>` / `!rm-admin <user_id>` — manage **main admins** by raw Discord ID (main admin only). Works even if the user isn't in the server yet. At least one main admin is always kept.
- `!admin-list` — shows all main admins and regular admins.

Multiple main admins are stored in the database (seeded from `MAIN_ADMIN_ID`), so changes made with `!add-admin`/`!rm-admin` persist across restarts.

## Notes

- The bot runs as **root** because it directly manages LXC containers via `lxc`/`lxd` — keep the bot token and server access secured.
- `vps.db` (SQLite) is created automatically in `/root` on first run and stores nodes, VPS records, admins, and settings.
- Run `!help` in Discord after the bot is online to see the full command list.

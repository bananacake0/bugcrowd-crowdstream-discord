# Bugcrowd CrowdStream Discord bot

Posts new Bugcrowd CrowdStream acceptances and disclosures to Discord. Each run
refreshes every paid bug-bounty program from Bugcrowd's catalog, probes its
engagement-specific CrowdStream, and fetches every available page. Results from
all programs are merged and sorted together by acceptance or disclosure date
before delivery, and IDs already recorded in `processed_ids.json` are skipped.
Discord deliveries are grouped into batches of up to ten reports with a
three-second pause between batches. Embeds retain the program logo and use a
compact priority badge matching the reference layout.

`programs.json` records the current paid-program catalog and whether each
program exposes a CrowdStream. If `DISCORD_PROGRAMS_WEBHOOK_URL` is configured,
programs entering or leaving the paid catalog are also posted to that channel.

## Local run

1. Install [uv](https://docs.astral.sh/uv/).
2. Copy `.env.example` to `.env` and add the report webhook URL. Add a separate
   program-changes webhook if those alerts should be enabled.
3. Run `uv run --locked main.py`.

## Manual VPS run

The included service runs the complete all-program, all-page scan only when it
is started manually. It uses a dedicated unprivileged account and keeps mutable
state in `/var/lib/bugcrowd-crowdstream-discord`, separate from the Git checkout.

The example below assumes a Debian or Ubuntu VPS, the repository is available
as `$REPOSITORY_URL`, and `uv` is installed at `/usr/local/bin/uv`.

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin crowdstream
sudo install -d -o crowdstream -g crowdstream /opt/bugcrowd-crowdstream-discord
sudo -u crowdstream git clone "$REPOSITORY_URL" /opt/bugcrowd-crowdstream-discord
cd /opt/bugcrowd-crowdstream-discord
sudo -u crowdstream /usr/local/bin/uv sync --locked --no-dev

sudo install -d -m 700 -o crowdstream -g crowdstream \
  /var/lib/bugcrowd-crowdstream-discord
sudo install -m 600 -o crowdstream -g crowdstream programs.json \
  /var/lib/bugcrowd-crowdstream-discord/programs.json
sudo install -m 640 -o root -g crowdstream /dev/null \
  /etc/bugcrowd-crowdstream-discord.env
sudoedit /etc/bugcrowd-crowdstream-discord.env
```

Add these values to `/etc/bugcrowd-crowdstream-discord.env`:

```dotenv
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/replace-with-your-webhook
DISCORD_PROGRAMS_WEBHOOK_URL=https://discord.com/api/webhooks/replace-with-your-program-changes-webhook
CROWDSTREAM_HISTORY_FILE=/var/lib/bugcrowd-crowdstream-discord/processed_ids.json
CROWDSTREAM_PROGRAMS_FILE=/var/lib/bugcrowd-crowdstream-discord/programs.json
```

Install the service and start a scan manually:

```bash
sudo install -m 644 deploy/systemd/crowdstream.service \
  /etc/systemd/system/crowdstream.service
sudo systemctl daemon-reload
sudo systemctl start crowdstream.service
```

Use `sudo systemctl start crowdstream.service` for each later scan and follow its
output with `journalctl -u crowdstream.service -f`. Because the repository
intentionally has no `processed_ids.json`, the first run treats all currently
visible reports as new and sends the full backfill. Later manual runs persist IDs
in `/var/lib` and send only reports not already recorded.

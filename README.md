# Bugcrowd CrowdStream Discord bot

Fetches public CrowdStream reports from every paid Bugcrowd bug-bounty program and posts them to Discord. The project currently runs manually: there is no GitHub Actions workflow, cron job, or enabled `systemd` timer.

## Current behavior

Each run performs one complete scan:

1. Fetches every page of Bugcrowd's paid `bug_bounty` program catalog. VDPs are excluded.
2. Probes page 1 of every program's engagement-specific CrowdStream.
3. Treats a genuine HTTP 404 as CrowdStream disabled for that program.
4. Reads `pagination_meta.total_pages` and fetches every advertised page, from the oldest page down to page 1.
5. Merges reports from every program, removes duplicate or previously delivered submission IDs, and globally sorts the remaining reports oldest-first by acceptance or disclosure date.
6. Delivers the reports to Discord and records each successfully delivered batch.

"All reports" means every report exposed by the programs' public CrowdStream JSON endpoints. It does not include private submissions or reports Bugcrowd does not publish to CrowdStream.

Bugcrowd requests are sequential. HTTP 429 and server errors are retried, including Bugcrowd's `Retry-After` delay, so rate limiting is never mistaken for a disabled CrowdStream.

## Discord messages

Report embeds can include:

- Report or acceptance title
- Researcher and profile link when Bugcrowd makes the name visible
- Engagement name and link
- Reward when `amount` is not null
- Priority and matching sidebar color
- Acceptance or disclosure date
- Original submission date from `created_at`
- Program logo thumbnail

Discord messages contain at most ten embeds and respect the 6,000-character embed limit. The bot pauses for at least three seconds between messages and retries rate limits and temporary server errors. If a batch fails permanently, delivery stops to preserve oldest-to-newest order. IDs from earlier successful batches remain saved, so the next manual run resumes safely.

When `DISCORD_PROGRAMS_WEBHOOK_URL` is configured, programs entering or leaving the paid catalog are posted to a separate Discord channel.

New-program alerts also include metadata from the program's latest public changelog: industry, status, participation, reward model, publish date, in-scope target count, logo, and a short tagline.

## State files

The repository intentionally starts with two valid empty JSON arrays:

| File                 | Purpose                                                                  |
| -------------------- | ------------------------------------------------------------------------ |
| `programs.json`      | Current paid-program catalog, names, slugs, and CrowdStream availability |
| `processed_ids.json` | Submission IDs successfully delivered to Discord                         |

On the first run, the bot repopulates `programs.json` and treats every currently visible CrowdStream report as new. During the latest development scan on 30 August 2026, the complete backfill contained 3,832 unique reports, or about 384 Discord messages. The live total will change.

If the program-changes webhook is enabled while `programs.json` is empty, the first run also announces every paid program as newly added.

Both files are written atomically. Preserve them between runs unless a deliberate full backfill is required.

## Requirements

- Linux or macOS
- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- A Discord webhook for report alerts
- An optional second Discord webhook for paid-program changes

## Configuration

Copy the example environment file:

```bash
cp .env.example .env
```

Configure these values:

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `DISCORD_WEBHOOK_URL` | Yes | None | Webhook receiving report embeds |
| `DISCORD_PROGRAMS_WEBHOOK_URL` | No | None | Webhook receiving paid-program additions and removals |
| `CROWDSTREAM_HISTORY_FILE` | No | `processed_ids.json` | Persistent delivered-submission state |
| `CROWDSTREAM_PROGRAMS_FILE` | No | `programs.json` | Persistent paid-program catalog state |

Never commit real webhook URLs. Rotate any webhook that has been exposed in chat, logs, or version control.

If program-change alerts are not needed, remove `DISCORD_PROGRAMS_WEBHOOK_URL` instead of leaving the example placeholder configured.

## Run locally

Install the locked dependencies and run one complete scan:

```bash
uv sync --locked
uv run --locked main.py
```

The command may take several minutes because it fetches every CrowdStream page sequentially and honors Bugcrowd rate limits.

## Manual VPS deployment

The included `systemd` service is a hardened one-shot service. It runs only when manually started and cannot be enabled as a scheduled service in its current form.

The following example assumes Debian or Ubuntu, `uv` installed at `/usr/local/bin/uv`, and the repository URL stored in `REPOSITORY_URL`:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin crowdstream
sudo install -d -o crowdstream -g crowdstream /opt/bugcrowd-crowdstream-discord
sudo -u crowdstream git clone "$REPOSITORY_URL" /opt/bugcrowd-crowdstream-discord
cd /opt/bugcrowd-crowdstream-discord
sudo -u crowdstream /usr/local/bin/uv sync --locked --no-dev

sudo install -d -m 700 -o crowdstream -g crowdstream \
  /var/lib/bugcrowd-crowdstream-discord
sudo install -m 600 -o crowdstream -g crowdstream \
  programs.json processed_ids.json /var/lib/bugcrowd-crowdstream-discord/
sudo install -m 640 -o root -g crowdstream /dev/null \
  /etc/bugcrowd-crowdstream-discord.env
sudoedit /etc/bugcrowd-crowdstream-discord.env
```

Add the following configuration to `/etc/bugcrowd-crowdstream-discord.env`:

```dotenv
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/replace-with-your-webhook
DISCORD_PROGRAMS_WEBHOOK_URL=https://discord.com/api/webhooks/replace-with-your-program-changes-webhook
CROWDSTREAM_HISTORY_FILE=/var/lib/bugcrowd-crowdstream-discord/processed_ids.json
CROWDSTREAM_PROGRAMS_FILE=/var/lib/bugcrowd-crowdstream-discord/programs.json
```

Install the manual service:

```bash
sudo install -m 644 deploy/systemd/crowdstream.service \
  /etc/systemd/system/crowdstream.service
sudo systemctl daemon-reload
```

Start a scan and follow its logs:

```bash
sudo systemctl start --no-block crowdstream.service
sudo journalctl -fu crowdstream.service
```

Run `sudo systemctl start crowdstream.service` whenever another scan is needed. The state under `/var/lib/bugcrowd-crowdstream-discord` survives Git updates and prevents duplicate report delivery.

## Development checks

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked -m unittest discover -s tests -v
```

The current suite contains 30 tests covering catalog pagination, complete CrowdStream pagination, global ordering, date semantics, embed construction, retries, Discord batching, atomic state persistence, empty-state bootstrap, and VPS state-path configuration.

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

## GitHub Actions

The workflow in `.github/workflows/crowdstream.yml` runs at the beginning of
every hour (`12:00`, `1:00`, `2:00`, and so on) and can also be started manually
from the Actions tab.

1. Create a GitHub repository and push this project to its default branch.
2. Open **Settings → Secrets and variables → Actions**.
3. Add a repository secret named `DISCORD_WEBHOOK_URL` containing the complete
   Discord webhook URL.
4. Optionally add `DISCORD_PROGRAMS_WEBHOOK_URL` for paid-program catalog
   changes.
5. Open **Actions → CrowdStream to Discord → Run workflow** for the first run.

The workflow commits `processed_ids.json` and `programs.json` to the default
branch. This state is required because GitHub-hosted runners are temporary. The
workflow grants only `contents: write`, prevents overlapping runs, and persists
successful batches even if a later batch fails.

If the state commit is rejected, allow GitHub Actions to write repository
contents under **Settings → Actions → General → Workflow permissions** and make
sure branch protection permits the `github-actions[bot]` update.

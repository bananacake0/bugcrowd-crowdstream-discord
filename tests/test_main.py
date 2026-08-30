import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main


class FakeResponse:
    def __init__(self, status, *, payload=None, headers=None):
        self.status = status
        self.payload = payload
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self, **kwargs):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)


class DateAndEmbedTests(unittest.TestCase):
    def test_format_iso_date(self):
        self.assertEqual(
            main.format_iso_date("2026-07-12T16:38:11.110Z"), "12 Jul 2026"
        )
        self.assertEqual(main.format_iso_date("not-a-date"), "not-a-date")
        self.assertEqual(main.format_iso_date(None), "")

    def test_disclosure_embed_uses_disclosure_date_and_link(self):
        embed = main.build_discord_embed(
            {
                "engagement_name": "NASA VDP",
                "engagement_path": "/engagements/nasa-vdp",
                "created_at": "2026-07-12T16:38:11.110Z",
                "accepted_at": "13 Jul 2026",
                "disclosed_at": "27 Aug 2026",
                "disclosed": True,
                "title": "Disclosure example",
                "disclosure_report_url": "/disclosures/example",
                "priority": 5,
                "logo_url": "https://example.com/nasa-logo.png",
                "researcher_username": "hidden-user",
                "researcher_profile_path": "https://bugcrowd.com/h/hidden-user",
                "crowdstream_name_visible": False,
            }
        )

        self.assertEqual(embed["title"], "Disclosed: Disclosure example")
        self.assertEqual(embed["url"], "https://bugcrowd.com/disclosures/example")
        self.assertEqual(embed["footer"]["text"], "Submitted on 12 Jul 2026")
        self.assertEqual(
            embed["thumbnail"]["url"],
            "https://example.com/nasa-logo.png",
        )
        self.assertEqual(
            embed["fields"][-1],
            {
                "name": "\u200b",
                "value": "Disclosed on 27 Aug 2026",
                "inline": False,
            },
        )
        self.assertNotIn("Researcher", {field["name"] for field in embed["fields"]})

    def test_acceptance_embed_uses_acceptance_date(self):
        embed = main.build_discord_embed(
            {
                "engagement_name": "Example",
                "engagement_path": "/engagements/example",
                "submission_state_text": "Submission accepted on target: example.com",
                "accepted_at": "28 Aug 2026",
                "submission_state_date_text": "Accepted on 28 Aug 2026",
                "created_at": "2026-08-20T12:00:00Z",
                "amount": "$500",
                "priority": 3,
                "researcher_username": "researcher",
                "researcher_profile_path": "/researchers/researcher",
                "crowdstream_name_visible": True,
                "logo_url": "https://example.com/program-logo.png",
            }
        )

        self.assertEqual(embed["title"], "Submission accepted on target: example.com")
        self.assertEqual(embed["footer"]["text"], "Submitted on 20 Aug 2026")
        self.assertEqual(
            embed["thumbnail"], {"url": "https://example.com/program-logo.png"}
        )
        self.assertEqual(
            [field["name"] for field in embed["fields"]],
            ["Researcher", "Engagement", "Reward", "Priority", "\u200b"],
        )
        self.assertEqual(embed["fields"][2]["value"], "$500")
        self.assertEqual(embed["fields"][3]["value"], "`P3`")
        self.assertEqual(
            embed["fields"][-1],
            {
                "name": "\u200b",
                "value": "Accepted on 28 Aug 2026",
                "inline": False,
            },
        )

    def test_each_priority_has_readable_text_badge_and_sidebar_color(self):
        for priority in main.COLOR_BY_PRIORITY:
            with self.subTest(priority=priority):
                embed = main.build_discord_embed({"priority": priority})
                priority_field = next(
                    field for field in embed["fields"] if field["name"] == "Priority"
                )

                self.assertEqual(priority_field["value"], f"`P{priority}`")
                self.assertEqual(embed["color"], main.COLOR_BY_PRIORITY[priority])
                self.assertNotIn("thumbnail", embed)

    def test_program_logo_remains_thumbnail_for_high_priority(self):
        embed = main.build_discord_embed(
            {
                "priority": 1,
                "logo_url": "https://example.com/program-logo.png",
            }
        )

        self.assertEqual(
            embed["thumbnail"], {"url": "https://example.com/program-logo.png"}
        )
        self.assertNotIn("Reward", {field["name"] for field in embed["fields"]})

    def test_external_bugcrowd_path_is_rejected(self):
        self.assertEqual(
            main.bugcrowd_url("https://example.com/path"), main.BUGCROWD_ORIGIN
        )

    def test_malformed_optional_fields_use_safe_defaults(self):
        embed = main.build_discord_embed(
            {
                "engagement_name": 123,
                "engagement_path": 456,
                "submission_state_text": [],
                "submission_state_date_text": {},
                "created_at": False,
                "disclosed": True,
                "disclosed_at": None,
                "title": None,
                "disclosure_report_url": 789,
            }
        )

        self.assertEqual(embed["title"], "Disclosed: Report")
        self.assertEqual(embed["footer"]["text"], "Submitted on Unknown")
        self.assertEqual(embed["fields"][-1]["value"], "Disclosed on Recently")

    def test_crowdstream_sort_key_uses_event_date(self):
        reports = [
            {"id": "newest", "accepted_at": "30 Aug 2026"},
            {
                "id": "middle",
                "disclosed": True,
                "disclosed_at": "20 Aug 2026",
            },
            {"id": "oldest", "accepted_at": "5 Aug 2026"},
        ]

        reports.sort(key=main.crowdstream_sort_key)

        self.assertEqual(
            [report["id"] for report in reports], ["oldest", "middle", "newest"]
        )


class HistoryTests(unittest.TestCase):
    def test_missing_history_is_empty_and_save_is_sorted(self):
        with tempfile.TemporaryDirectory() as directory:
            history_file = Path(directory) / "processed_ids.json"
            self.assertEqual(main.load_processed_ids(history_file), set())

            main.save_processed_ids({"second", "first"}, history_file)

            self.assertEqual(main.load_processed_ids(history_file), {"first", "second"})
            self.assertEqual(json.loads(history_file.read_text()), ["first", "second"])
            self.assertEqual(list(history_file.parent.glob("*.tmp")), [])

    def test_corrupt_history_stops_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            history_file = Path(directory) / "processed_ids.json"
            history_file.write_text("not-json", encoding="utf-8")

            with self.assertRaises(main.HistoryError):
                main.load_processed_ids(history_file)

    def test_invalid_history_shape_stops_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            history_file = Path(directory) / "processed_ids.json"
            history_file.write_text('{"id": "unexpected"}', encoding="utf-8")

            with self.assertRaises(main.HistoryError):
                main.load_processed_ids(history_file)


class ProgramsTests(unittest.TestCase):
    def test_empty_program_list_is_a_valid_bootstrap_state(self):
        with tempfile.TemporaryDirectory() as directory:
            programs_file = Path(directory) / "programs.json"
            programs_file.write_text("[]\n", encoding="utf-8")

            self.assertEqual(main.load_programs(programs_file), [])

    def test_loads_valid_programs(self):
        with tempfile.TemporaryDirectory() as directory:
            programs_file = Path(directory) / "programs.json"
            programs_file.write_text(
                json.dumps(
                    [
                        {
                            "slug": "atlassian",
                            "name": "Atlassian",
                            "crowdstream_enabled": True,
                        },
                        {
                            "slug": "launchdarkly-mbb-og",
                            "name": "LaunchDarkly",
                            "crowdstream_enabled": False,
                        },
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                main.load_programs(programs_file),
                [
                    {
                        "slug": "atlassian",
                        "name": "Atlassian",
                        "crowdstream_enabled": True,
                    },
                    {
                        "slug": "launchdarkly-mbb-og",
                        "name": "LaunchDarkly",
                        "crowdstream_enabled": False,
                    },
                ],
            )

    def test_rejects_duplicate_or_unsafe_program_slugs(self):
        invalid_program_lists = [
            [
                {
                    "slug": "duplicate",
                    "name": "First",
                    "crowdstream_enabled": True,
                },
                {
                    "slug": "duplicate",
                    "name": "Second",
                    "crowdstream_enabled": True,
                },
            ],
            [
                {
                    "slug": "../unsafe",
                    "name": "Unsafe",
                    "crowdstream_enabled": True,
                }
            ],
        ]

        for programs in invalid_program_lists:
            with self.subTest(programs=programs):
                with tempfile.TemporaryDirectory() as directory:
                    programs_file = Path(directory) / "programs.json"
                    programs_file.write_text(json.dumps(programs), encoding="utf-8")

                    with self.assertRaises(main.ProgramsError):
                        main.load_programs(programs_file)

    def test_save_programs_is_sorted_and_atomic(self):
        programs = [
            {"slug": "zebra", "name": "Zebra", "crowdstream_enabled": False},
            {"slug": "alpha", "name": "Alpha", "crowdstream_enabled": True},
        ]
        with tempfile.TemporaryDirectory() as directory:
            programs_file = Path(directory) / "programs.json"

            main.save_programs(programs, programs_file)

            self.assertEqual(
                [program["slug"] for program in main.load_programs(programs_file)],
                ["alpha", "zebra"],
            )
            self.assertEqual(list(programs_file.parent.glob("*.tmp")), [])

    def test_catalog_slug_preserves_case_required_by_bugcrowd(self):
        self.assertEqual(
            main.paid_program_from_catalog(
                {
                    "name": "CoinDesk Data",
                    "briefUrl": "/engagements/CCData-mbb-og",
                }
            ),
            {"slug": "CCData-mbb-og", "name": "CoinDesk Data"},
        )


class SelectionTests(unittest.TestCase):
    def test_selection_skips_processed_invalid_and_duplicate_ids(self):
        results = [
            {"id": "new"},
            {"id": "old"},
            {"id": "new"},
            {"missing": "id"},
            "invalid",
        ]

        self.assertEqual(main.select_new_items(results, {"old"}), [{"id": "new"}])


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    @patch("main.asyncio.sleep", new_callable=AsyncMock)
    async def test_fetch_validates_http_status(self, sleep):
        session = FakeSession([FakeResponse(503) for _ in range(5)])

        with self.assertRaises(main.CrowdstreamError):
            await main.fetch_crowdstream_page(session, "atlassian", page=1)
        self.assertEqual(sleep.await_count, 4)

    async def test_fetch_accepts_expected_response(self):
        payload = {"results": [{"id": "one"}]}
        session = FakeSession([FakeResponse(200, payload=payload)])

        self.assertEqual(
            await main.fetch_crowdstream_page(session, "atlassian", page=1),
            payload,
        )
        self.assertEqual(
            session.calls,
            [
                (
                    "GET",
                    "https://bugcrowd.com/engagements/atlassian/crowdstream.json",
                    {"headers": {"User-Agent": main.USER_AGENT}, "params": {"page": 1}},
                )
            ],
        )

    @patch("main.fetch_crowdstream_page", new_callable=AsyncMock)
    async def test_fetches_all_program_pages_then_globally_sorts_oldest_first(
        self, fetch_page
    ):
        fetch_page.side_effect = [
            {
                "results": [{"id": "atlassian-new", "accepted_at": "30 Aug 2026"}],
                "pagination_meta": {"total_pages": 2},
            },
            {"results": [{"id": "atlassian-older", "accepted_at": "10 Aug 2026"}]},
            None,
            {
                "results": [{"id": "rapyd-newest", "accepted_at": "31 Aug 2026"}],
                "pagination_meta": {"total_pages": 3},
            },
            {"results": [{"id": "rapyd-oldest", "accepted_at": "5 Aug 2026"}]},
            {"results": [{"id": "rapyd-middle", "accepted_at": "20 Aug 2026"}]},
        ]
        session = object()
        programs = [
            {
                "slug": "atlassian",
                "name": "Atlassian",
                "crowdstream_enabled": True,
            },
            {
                "slug": "launchdarkly",
                "name": "LaunchDarkly",
                "crowdstream_enabled": False,
            },
            {"slug": "rapyd", "name": "Rapyd", "crowdstream_enabled": True},
        ]

        new_items = await main.fetch_new_items(session, set(), programs)

        self.assertEqual(
            [call.args[1] for call in fetch_page.await_args_list],
            [
                "atlassian",
                "atlassian",
                "launchdarkly",
                "rapyd",
                "rapyd",
                "rapyd",
            ],
        )
        self.assertEqual(
            [call.kwargs["page"] for call in fetch_page.await_args_list],
            [1, 2, 1, 1, 3, 2],
        )
        self.assertTrue(fetch_page.await_args_list[0].kwargs["allow_not_found"])
        self.assertTrue(fetch_page.await_args_list[2].kwargs["allow_not_found"])
        self.assertTrue(fetch_page.await_args_list[3].kwargs["allow_not_found"])
        self.assertTrue(programs[0]["crowdstream_enabled"])
        self.assertFalse(programs[1]["crowdstream_enabled"])
        self.assertTrue(programs[2]["crowdstream_enabled"])
        self.assertEqual(
            [item["id"] for item in new_items],
            [
                "rapyd-oldest",
                "atlassian-older",
                "rapyd-middle",
                "atlassian-new",
                "rapyd-newest",
            ],
        )

    @patch("main.asyncio.sleep", new_callable=AsyncMock)
    async def test_bugcrowd_retries_rate_limit_then_succeeds(self, sleep):
        payload = {"results": [], "pagination_meta": {"total_pages": 0}}
        session = FakeSession(
            [
                FakeResponse(429, headers={"Retry-After": "0.25"}),
                FakeResponse(200, payload=payload),
            ]
        )

        result = await main.fetch_crowdstream_page(session, "atlassian")

        self.assertEqual(result, payload)
        sleep.assert_awaited_once_with(0.25)

    async def test_fetches_every_paid_catalog_page_and_deduplicates_slug(self):
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    payload={
                        "engagements": [
                            {"name": "Alpha", "briefUrl": "/engagements/alpha"},
                            {"name": "Beta", "briefUrl": "/engagements/beta"},
                        ],
                        "paginationMeta": {"limit": 2, "totalCount": 4},
                    },
                ),
                FakeResponse(
                    200,
                    payload={
                        "engagements": [
                            {"name": "Gamma", "briefUrl": "/engagements/gamma"},
                            {"name": "Beta", "briefUrl": "/engagements/beta"},
                        ]
                    },
                ),
            ]
        )

        programs = await main.fetch_paid_programs(session)

        self.assertEqual(
            programs,
            [
                {"slug": "alpha", "name": "Alpha"},
                {"slug": "beta", "name": "Beta"},
                {"slug": "gamma", "name": "Gamma"},
            ],
        )
        self.assertEqual([call[2]["params"]["page"] for call in session.calls], [1, 2])

    async def test_fetches_latest_program_metadata_from_changelog(self):
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    payload={
                        "changelogs": [
                            {
                                "id": "older",
                                "publishedAt": "2026-01-01T00:00:00Z",
                                "changelogState": "Archived",
                            },
                            {
                                "id": "latest",
                                "publishedAt": "2026-08-30T00:00:00Z",
                                "changelogState": "Latest",
                            },
                        ]
                    },
                ),
                FakeResponse(
                    200,
                    payload={
                        "industryName": "Finance",
                        "statusLabel": "In progress",
                        "participation": "open",
                        "rewardAllocation": "pay_for_success",
                        "publishedAt": "2026-08-30T00:00:00Z",
                        "logoUrl": "https://example.com/logo.png",
                        "data": {
                            "brief": {"tagline": "Find bugs safely."},
                            "scope": [
                                {
                                    "inScope": True,
                                    "targets": [{"id": "one"}, {"id": "two"}],
                                },
                                {"inScope": False, "targets": [{"id": "three"}]},
                            ],
                        },
                    },
                ),
            ]
        )

        metadata = await main.fetch_program_metadata(session, "nubank")

        self.assertEqual(
            metadata,
            {
                "industry": "Finance",
                "status": "In progress",
                "participation": "open",
                "reward_model": "pay_for_success",
                "published_at": "2026-08-30T00:00:00Z",
                "target_count": 2,
                "logo_url": "https://example.com/logo.png",
                "tagline": "Find bugs safely.",
            },
        )
        self.assertEqual(
            [call[1] for call in session.calls],
            [
                "https://bugcrowd.com/engagements/nubank/changelog.json",
                "https://bugcrowd.com/engagements/nubank/changelog/latest.json",
            ],
        )

    def test_program_change_embed_includes_metadata(self):
        embed = main.build_program_change_embed(
            {
                "slug": "nubank",
                "name": "Nubank",
                "crowdstream_enabled": True,
            },
            "added",
            {
                "industry": "Finance",
                "status": "In progress",
                "participation": "open",
                "reward_model": "pay_for_success",
                "published_at": "2026-08-30T00:00:00Z",
                "target_count": 2,
                "logo_url": "https://example.com/logo.png",
                "tagline": "Find bugs safely.",
            },
        )

        fields = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertNotIn("Slug", fields)
        self.assertEqual(fields["Industry"], "Finance")
        self.assertEqual(fields["Reward model"], "Pay For Success")
        self.assertEqual(fields["In-scope targets"], "2")
        self.assertEqual(fields["Published"], "30 Aug 2026")
        self.assertEqual(embed["thumbnail"], {"url": "https://example.com/logo.png"})
        self.assertIn("Find bugs safely.", embed["description"])

    @patch("main.fetch_paid_programs", new_callable=AsyncMock)
    async def test_refresh_programs_preserves_known_state_and_marks_new_unknown(
        self, fetch_paid_programs
    ):
        fetch_paid_programs.return_value = [
            {"slug": "existing", "name": "Renamed Existing"},
            {"slug": "new", "name": "New"},
        ]
        previous = [
            {"slug": "existing", "name": "Existing", "crowdstream_enabled": True},
            {"slug": "removed", "name": "Removed", "crowdstream_enabled": False},
        ]

        refreshed, added, removed = await main.refresh_programs(object(), previous)

        self.assertEqual(
            refreshed,
            [
                {
                    "slug": "existing",
                    "name": "Renamed Existing",
                    "crowdstream_enabled": True,
                },
                {"slug": "new", "name": "New", "crowdstream_enabled": False},
            ],
        )
        self.assertEqual(added, [refreshed[1]])
        self.assertEqual(removed, [previous[1]])

    @patch("main.asyncio.sleep", new_callable=AsyncMock)
    async def test_discord_prefers_json_retry_after_then_succeeds(self, sleep):
        session = FakeSession(
            [
                FakeResponse(
                    429,
                    payload={"retry_after": 0.4, "global": False},
                    headers={
                        "Retry-After": "400",
                        "X-RateLimit-Scope": "shared",
                        "X-RateLimit-Limit": "5",
                        "X-RateLimit-Remaining": "0",
                    },
                ),
                FakeResponse(204),
            ]
        )

        cooldown = await main.send_to_discord(
            session, "https://discord.com/webhook", [{"id": "one"}]
        )

        self.assertEqual(cooldown, 0.0)
        self.assertEqual(len(session.calls), 2)
        sleep.assert_awaited_once_with(0.4)

    async def test_discord_returns_cooldown_when_bucket_is_exhausted(self):
        session = FakeSession(
            [
                FakeResponse(
                    204,
                    headers={
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset-After": "1.25",
                    },
                )
            ]
        )

        cooldown = await main.send_to_discord(
            session, "https://discord.com/webhook", [{"id": "one"}]
        )

        self.assertEqual(cooldown, 1.25)

    async def test_discord_does_not_retry_permanent_client_error(self):
        session = FakeSession([FakeResponse(400)])

        with self.assertRaises(main.DiscordDeliveryError):
            await main.send_to_discord(
                session, "https://discord.com/webhook", [{"id": "one"}]
            )
        self.assertEqual(len(session.calls), 1)

    @patch("main.asyncio.sleep", new_callable=AsyncMock)
    @patch("main.send_discord_payload", new_callable=AsyncMock)
    async def test_program_changes_are_batched_for_their_webhook(self, send, sleep):
        send.return_value = 0.0
        added = [
            {
                "slug": f"added-{index}",
                "name": f"Added {index}",
                "crowdstream_enabled": True,
            }
            for index in range(11)
        ]
        removed = [
            {
                "slug": "removed",
                "name": "Removed",
                "crowdstream_enabled": False,
            }
        ]

        await main.deliver_program_changes(
            object(), "https://discord.com/program-webhook", added, removed
        )

        self.assertEqual(
            [len(call.args[2]["embeds"]) for call in send.await_args_list], [10, 2]
        )
        self.assertEqual(
            send.await_args_list[-1].args[2]["embeds"][-1]["title"],
            "Paid program removed",
        )
        sleep.assert_awaited_once_with(3.0)

    def test_delivery_batches_use_at_most_ten_embeds(self):
        items = [{"id": str(index)} for index in range(23)]

        batches = main.batch_items_for_discord(items)

        self.assertEqual([len(batch) for batch in batches], [10, 10, 3])
        self.assertEqual(
            [item["id"] for batch in batches for item in batch],
            [item["id"] for item in items],
        )

    @patch("main.asyncio.sleep", new_callable=AsyncMock)
    @patch("main.send_to_discord", new_callable=AsyncMock)
    async def test_only_successful_delivery_batches_are_recorded(self, send, sleep):
        send.side_effect = [0.0, main.DiscordDeliveryError("failed")]
        new_items = [{"id": str(index)} for index in range(21)]
        processed_ids = {"existing"}

        with tempfile.TemporaryDirectory() as directory:
            history_file = Path(directory) / "processed_ids.json"
            failures = await main.deliver_new_items(
                object(),
                "https://discord.com/webhook",
                new_items,
                processed_ids,
                history_file,
            )

            self.assertEqual(failures, 11)
            self.assertEqual(
                main.load_processed_ids(history_file),
                {"existing", *(str(index) for index in range(10))},
            )
            self.assertEqual(
                [
                    [item["id"] for item in call.args[2]]
                    for call in send.await_args_list
                ],
                [
                    [str(index) for index in range(10)],
                    [str(index) for index in range(10, 20)],
                ],
            )
            sleep.assert_awaited_once_with(3.0)


class MainConfigurationTests(unittest.IsolatedAsyncioTestCase):
    @patch("main.run_bot", new_callable=AsyncMock)
    @patch("main.load_dotenv")
    async def test_main_passes_vps_state_paths_from_environment(
        self, load_dotenv, run_bot
    ):
        run_bot.return_value = 0
        environment = {
            "DISCORD_WEBHOOK_URL": "https://discord.com/report-webhook",
            "DISCORD_PROGRAMS_WEBHOOK_URL": "https://discord.com/program-webhook",
            "CROWDSTREAM_HISTORY_FILE": "/var/lib/crowdstream/processed_ids.json",
            "CROWDSTREAM_PROGRAMS_FILE": "/var/lib/crowdstream/programs.json",
        }

        with patch.dict(main.os.environ, environment, clear=True):
            result = await main.main()

        self.assertEqual(result, 0)
        load_dotenv.assert_called_once_with()
        run_bot.assert_awaited_once_with(
            environment["DISCORD_WEBHOOK_URL"],
            history_file=Path(environment["CROWDSTREAM_HISTORY_FILE"]),
            programs_file=Path(environment["CROWDSTREAM_PROGRAMS_FILE"]),
            programs_webhook_url=environment["DISCORD_PROGRAMS_WEBHOOK_URL"],
        )


if __name__ == "__main__":
    unittest.main()

"""Card gpu-kit: the GPU kit's tests, part 2 of 4.

The cases, their world and what each one proves are in tests/gpukit_cases.py; this file only names the
ones collected here. CI splits the test job by FILE (dev/ci_shards.py): as one file these cases ran for more
than 20 minutes and their shard reached the job's 30-minute limit (CI run 43038567089), so they are collected
from 4 files of about equal size. A new case goes into gpukit_cases.py and into ONE of these lists
(tests/test_gpu_kit_parts.py fails when a case is in none or in two).
"""
# ruff: noqa: F401  (the imported names are the fixtures and the tests pytest collects from this file)

from tests.gpukit_cases import (
    nightshift,
    test_a_dry_run_on_the_simulated_card_is_marked_and_runs_both_nights_at_once,
    test_a_nested_zip_is_scrubbed_entry_by_entry,
    test_a_night_that_makes_nothing_for_the_kit_is_a_fail_with_its_reason,
    test_a_refused_key_says_why_and_the_video_does_not_start,
    test_each_stored_stock_key_is_tried_through_the_products_route_before_the_video,
    test_the_one_reader_of_the_nights_record,
    test_the_owners_own_routes_to_automatic_runs_are_theirs,
    test_the_real_windows_readers_are_refused_under_pytest,
    test_the_scrubber_masks_every_form_of_keys_tokens_the_user_the_home_and_the_pc,
    world,
)

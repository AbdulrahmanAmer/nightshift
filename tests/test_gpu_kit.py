"""Card gpu-kit: the GPU kit's tests, part 1 of 4.

The cases, their world and what each one proves are in tests/gpukit_cases.py; this file only names the
ones collected here. CI splits the test job by FILE (dev/ci_shards.py): as one file these cases ran for more
than 20 minutes and their shard reached the job's 30-minute limit (CI run 43038567089), so they are collected
from 4 files of about equal size. A new case goes into gpukit_cases.py and into ONE of these lists
(tests/test_gpu_kit_parts.py fails when a case is in none or in two).
"""
# ruff: noqa: F401  (the imported names are the fixtures and the tests pytest collects from this file)

from tests.gpukit_cases import (
    nightshift,
    test_a_failed_story_job_is_recorded_with_its_code_and_the_kit_goes_on,
    test_a_failing_step_still_yields_the_bundle,
    test_a_first_run_goes_k0_to_k4_and_stops_with_the_morning_date,
    test_a_home_without_its_models_fails_k1_on_the_products_own_check,
    test_a_job_that_finished_while_the_kit_was_closed_is_not_resumed,
    test_a_newer_kit_collects_the_nights_an_older_one_armed,
    test_a_second_kit_at_the_same_time_is_refused,
    test_an_error_the_kit_did_not_expect_still_puts_back_and_writes_the_zip,
    test_automatic_runs_are_put_back_as_they_were,
    test_closing_the_window_is_an_interrupt,
    test_each_step_interrupted_resumes_on_the_next_run,
    test_no_secret_leaves_in_the_zip,
    test_the_collected_folder_and_zip_hold_every_part,
    test_the_dry_run_switches_work_only_on_a_test_install,
    test_the_hand_checks_are_steps_with_questions_observations_and_files,
    test_the_kit_channel_is_never_planned_and_is_put_back_on_every_exit,
    test_the_kit_makes_no_network_request,
    test_the_kit_reads_what_the_product_writes,
    test_the_night_after_the_two_puts_back_first_and_makes_nothing_unless_the_runs_are_the_owners,
    test_the_nights_are_read_from_the_runs_rows_and_the_ledger,
    test_the_paste_answers_are_recorded_and_later_keeps_it_open,
    test_the_story_job_is_resumed_not_made_again,
    test_too_close_to_the_run_time_the_video_waits,
    world,
)

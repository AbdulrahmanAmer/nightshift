"""Card gpu-kit: the GPU kit's tests, part 3 of 4.

The cases, their world and what each one proves are in tests/gpukit_cases.py; this file only names the
ones collected here. CI splits the test job by FILE (dev/ci_shards.py): as one file these cases ran for more
than 20 minutes and their shard reached the job's 30-minute limit (CI run 43038567089), so they are collected
from 4 files of about equal size. A new case goes into gpukit_cases.py and into ONE of these lists
(tests/test_gpu_kit_parts.py fails when a case is in none or in two).
"""
# ruff: noqa: F401  (the imported names are the fixtures and the tests pytest collects from this file)

from tests.gpukit_cases import (
    nightshift,
    test_a_clip_job_past_its_bound_is_cancelled,
    test_a_clip_job_that_fails_is_a_fail_and_the_kit_goes_on,
    test_a_code_window_left_by_a_closed_kit_is_closed_on_the_next_run,
    test_a_coded_start_failure_shows_that_codes_hint,
    test_a_job_route_that_always_times_out_still_ends_by_time,
    test_a_kill_after_the_clip_post_makes_no_second_clip_job,
    test_a_kill_mid_download_leaves_no_stock_clip_in_the_cache,
    test_a_night_that_knew_no_time_limit_is_a_fail,
    test_a_night_whose_time_rule_stops_the_kit_video_passes_with_the_summarys_own_reason,
    test_a_run_that_starts_cold_at_each_step_brings_the_engine_up_first,
    test_a_saved_clip_job_is_followed_after_a_cold_start_not_polled_dead,
    test_a_skip_of_a_selected_proof_fails_k2,
    test_an_old_decision_of_another_night_never_passes_a_night_that_made_nothing,
    test_an_unanswered_cancel_never_says_cancelled,
    test_an_unfinished_update_is_the_products_own_refusal_and_nothing_is_started,
    test_nightshift_code_is_opened_prompted_closed_and_frees_the_card,
    test_no_key_stored_is_not_run_and_the_video_goes_on,
    test_once_a_night_two_runs_in_one_armed_night_make_one_kit_video,
    test_prove_all_never_selects_the_footage_proofs_on_a_real_install,
    test_the_day_one_features_each_have_their_own_result,
    test_the_kits_own_engine_is_stopped_when_nothing_else_uses_it,
    test_the_owners_code_survives_a_kit_run_that_ends_normally,
    test_the_owners_code_survives_a_kit_run_that_ends_on_not_run,
    test_the_owners_open_code_window_is_never_touched,
    test_the_stored_values_are_masked_whatever_their_shape,
    world,
)

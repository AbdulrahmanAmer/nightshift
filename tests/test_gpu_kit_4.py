"""Card gpu-kit: the GPU kit's tests, part 4 of 4.

The cases, their world and what each one proves are in tests/gpukit_cases.py; this file only names the
ones collected here. CI splits the test job by FILE (dev/ci_shards.py): as one file these cases ran for more
than 20 minutes and their shard reached the job's 30-minute limit (CI run 43038567089), so they are collected
from 4 files of about equal size. A new case goes into gpukit_cases.py and into ONE of these lists
(tests/test_gpu_kit_parts.py fails when a case is in none or in two).
"""
# ruff: noqa: F401  (the imported names are the fixtures and the tests pytest collects from this file)

from tests.gpukit_cases import (
    nightshift,
    test_a_clip_that_came_for_the_same_words_later_is_not_the_kits_to_remove,
    test_a_job_the_owner_started_while_the_kit_ran_is_not_cancelled_by_the_kits_exit,
    test_a_line_the_kit_says_reaches_a_file_or_a_pipe_at_once,
    test_a_line_with_a_character_the_output_cannot_take_is_written_escaped_not_raised,
    test_a_night_still_at_work_is_waited_for_and_nothing_is_switched_off_under_it,
    test_a_nightly_run_at_work_keeps_nightshift_running,
    test_a_test_voice_a_channel_was_given_is_left,
    test_an_engine_the_owner_restarted_after_a_killed_kit_run_is_not_stopped_by_the_next_run,
    test_an_unattended_run_asks_nothing_keeps_the_owners_questions_open_and_ends_with_one_json_line,
    test_an_unattended_runs_exit_code_says_what_happened,
    test_an_unattended_wait_always_names_the_time_to_run_it_again,
    test_an_update_in_progress_keeps_the_kit_from_stopping_nightshift,
    test_at_a_console_of_its_own_gpu_kit_cmd_waits_for_a_person_and_not_for_an_agent,
    test_at_a_keyboard_a_prompt_ends_at_its_bound_and_takes_what_is_typed,
    test_automatic_runs_the_night_after_settled_as_the_owners_are_not_switched_off,
    test_close_to_a_nightly_run_the_model_test_waits_and_nightshift_is_left_for_the_night,
    test_every_prompt_ends_by_time_whatever_the_input_is,
    test_gpu_kit_cmd_never_waits_for_a_key_nobody_can_press,
    test_gpukit_ps1_asks_for_the_folder_only_at_a_keyboard_and_by_time,
    test_the_dashboard_open_in_a_browser_keeps_nightshift_running,
    test_the_help_says_what_unattended_does_and_what_its_exit_codes_mean,
    test_the_kit_as_a_program_writes_utf8_to_a_file_whatever_the_code_page,
    test_the_kit_itself_unattended_reads_nothing_and_ends_with_the_json_line,
    test_the_kits_channel_is_not_paused_under_a_job_the_owner_queued_on_it,
    test_the_model_test_does_not_start_while_a_job_of_the_owner_runs,
    test_the_owners_code_survives_a_kit_run_that_is_killed_and_run_again,
    test_the_unattended_last_line_is_ascii_whatever_the_state_holds,
    test_what_the_kit_starts_never_gets_the_kits_own_input,
    world,
)

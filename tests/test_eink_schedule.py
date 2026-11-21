"""Pure scheduling / selection logic of the e-ink cycle."""
import random
from datetime import datetime

from frame.eink_cycle import (date_line, is_scheduled_wake, next_wake,
                              pick_next, wake_slots)


def test_wake_slots_single_and_spread():
    assert wake_slots('06:00', 1) == [360]
    assert wake_slots('06:00', 2) == [360, 1080]
    assert wake_slots('06:00', 4) == [0, 360, 720, 1080]
    assert len(wake_slots('06:30', 24)) == 24


def test_wake_slots_tolerates_garbage():
    assert wake_slots('nonsense', 1) == [360]
    assert wake_slots('06:00', 0) == [360]
    assert len(wake_slots('06:00', 99)) == 24


def test_next_wake_same_day_and_rollover():
    assert next_wake(datetime(2026, 11, 21, 5, 0), '06:00', 1) == datetime(2026, 11, 21, 6, 0)
    assert next_wake(datetime(2026, 11, 21, 7, 0), '06:00', 1) == datetime(2026, 11, 22, 6, 0)
    assert next_wake(datetime(2026, 11, 21, 7, 0), '06:00', 2) == datetime(2026, 11, 21, 18, 0)


def test_next_wake_skips_a_slot_that_is_too_close():
    # 05:59 with a 3-minute guard: 06:00 could be missed while shutting down.
    assert next_wake(datetime(2026, 11, 21, 5, 59), '06:00', 1) == datetime(2026, 11, 22, 6, 0)
    # the slot we just woke for must not be chosen again
    assert next_wake(datetime(2026, 11, 21, 6, 1), '06:00', 1) == datetime(2026, 11, 22, 6, 0)


def test_next_wake_over_month_end():
    assert next_wake(datetime(2026, 11, 30, 23, 0), '06:00', 1) == datetime(2026, 12, 1, 6, 0)


def test_scheduled_vs_manual_wake():
    assert is_scheduled_wake(1000.0, 1000.0 + 300)
    assert is_scheduled_wake(1000.0, 1000.0 - 600)
    assert not is_scheduled_wake(1000.0, 1000.0 + 4000)
    assert not is_scheduled_wake(1000.0, None)      # first boot ever


def test_pick_next_goes_through_all_before_repeating():
    random.seed(1)
    files = ['a.jpg', 'b.jpg', 'c.jpg', 'd.jpg']
    shown, seen = [], []
    for _ in range(4):
        choice, shown = pick_next(files, shown)
        seen.append(choice)
    assert sorted(seen) == files
    # new round: does not immediately repeat the last one
    nxt, shown2 = pick_next(files, shown)
    assert nxt != seen[-1]
    assert shown2 == [nxt]


def test_pick_next_handles_changes_and_empty():
    assert pick_next([], ['x']) == (None, [])
    # a photo deleted from the album drops out of the history
    choice, shown = pick_next(['a.jpg', 'b.jpg'], ['gone.jpg', 'a.jpg'])
    assert choice == 'b.jpg' and shown == ['a.jpg', 'b.jpg']
    # a single photo keeps showing
    assert pick_next(['a.jpg'], ['a.jpg'])[0] == 'a.jpg'


def test_date_line():
    assert date_line(datetime(2026, 11, 21), 'de') == 'Sa, 21. Nov'
    assert date_line(datetime(2026, 11, 21), 'en') == 'Sat 21 Nov'
    assert date_line(datetime(2026, 3, 1), 'xx') == 'So, 1. März'

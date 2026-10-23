import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from schedule import FRIDAY, burst_notice, clock_due, groups_to_post, job_ready, parse_text_command

MADRID = ZoneInfo("Europe/Madrid")


def at(hour, minute, day=24, weekday_date=None):
    if weekday_date is not None:
        return datetime(2026, 10, weekday_date, hour, minute, tzinfo=MADRID)
    return datetime(2026, 10, day, hour, minute, tzinfo=MADRID)


class ScheduleTests(unittest.TestCase):
    def test_daily_runs_once_after_the_clock_including_a_missed_start(self):
        self.assertFalse(clock_due(at(7, 59), "08:00", ""))
        self.assertTrue(clock_due(at(8, 0), "08:00", ""))
        self.assertTrue(clock_due(at(10, 15), "08:00", ""))
        self.assertFalse(clock_due(at(10, 15), "08:00", "2026-10-24"))
        self.assertTrue(clock_due(at(10, 15), "08:00", "2026-10-23"))
        self.assertFalse(clock_due(at(10, 15), "", ""))

    def test_weekly_runs_on_friday_and_can_share_the_day_with_daily(self):
        friday = at(18, 0, weekday_date=23)
        thursday = at(18, 0, weekday_date=22)
        self.assertEqual(friday.weekday(), FRIDAY)
        self.assertTrue(clock_due(friday, "18:00", "", FRIDAY))
        self.assertFalse(clock_due(thursday, "18:00", "", FRIDAY))
        self.assertTrue(clock_due(friday, "08:00", ""))
        self.assertFalse(job_ready(friday, "18:00", "", "2026-10-23T18:30:00+02:00", FRIDAY))
        later = at(18, 30, weekday_date=23)
        self.assertTrue(job_ready(later, "18:00", "", "2026-10-23T18:15:00+02:00", FRIDAY))

    def test_burst_latch_posts_once_until_the_count_drops(self):
        alerting, notify = burst_notice(False, 51, 50)
        self.assertTrue(alerting)
        self.assertTrue(notify)
        alerting, notify = burst_notice(True, 80, 50)
        self.assertTrue(alerting)
        self.assertFalse(notify)
        alerting, notify = burst_notice(True, 50, 50)
        self.assertFalse(alerting)
        self.assertFalse(notify)
        alerting, notify = burst_notice(False, 80, 0)
        self.assertFalse(alerting)
        self.assertFalse(notify)

    def test_manual_posts_keep_empty_groups_and_scheduled_posts_skip_them(self):
        groups = [
            {"name": "Alpha", "text": "resumen", "empty": False},
            {"name": "Gamma", "text": "Sin mensajes.", "empty": True},
        ]
        self.assertEqual(len(groups_to_post(groups, skip_empty=False)), 2)
        self.assertEqual([group["name"] for group in groups_to_post(groups, skip_empty=True)], ["Alpha"])

    def test_group_name_keeps_spaces(self):
        self.assertEqual(parse_text_command("resumen-diario Family Chat"), ("daily", "Family Chat"))
        self.assertEqual(parse_text_command("/resumen_semanal"), ("weekly", None))
        self.assertEqual(parse_text_command("grupos"), ("grupos", None))


if __name__ == "__main__":
    unittest.main()

"""Priority-weighted TRT scoring from saved task data, without external queries."""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

_MICROSECOND = timedelta(microseconds=1)
_MILLION = Decimal(1000000)


def seconds(value):
    """Exact decimal seconds, including subsecond DurationField values."""
    if not isinstance(value, timedelta):
        return None
    return Decimal(value // _MICROSECOND) / _MILLION


@dataclass
class ScoreSummary:
    task_count: int = 0
    completed_count: int = 0
    invalid_count: int = 0
    invalid_time_count: int = 0
    assigned_seconds: Decimal = Decimal(0)
    assigned_points: Decimal = Decimal(0)
    achieved_points: Decimal = Decimal(0)

    @property
    def available(self):
        # Do not present a partial calculation as a whole-person/team score.
        return self.task_count > 0 and not self.invalid_count and self.assigned_points > 0

    @property
    def percentage(self):
        return self.achieved_points * 100 / self.assigned_points if self.available else None


def summarize_items(items):
    """Summarize assigned items, preserving fractions until display.

    Automatic tasks use saved line count × configured seconds per line. Manual
    tasks use assigned seconds × the saved validated percentage.  Achievements
    above target are intentionally not capped. Invalid/missing data makes the
    aggregate unavailable rather than silently substituting zero.
    """
    result = ScoreSummary()
    for item in items:
        if not item.employee_id:
            continue
        result.task_count += 1
        assigned = seconds(item.assigned_time)
        if assigned is not None and assigned > 0:
            result.assigned_seconds += assigned
        else:
            result.invalid_time_count += 1

        task = item.task
        priority = task.priority
        if assigned is None or assigned <= 0 or not isinstance(priority, int) or priority <= 0:
            result.invalid_count += 1
            continue

        automatic = task.progress_query_id and task.progress_query.query != 'N/A'
        if automatic:
            duration = seconds(task.average_duration)
            lines = item.progress_lines
            if duration is None or duration <= 0 or not isinstance(lines, int) or lines < 0:
                result.invalid_count += 1
                continue
            completed = duration * lines
        else:
            rate = item.progression_rate
            if not isinstance(rate, int) or not 0 <= rate <= 100:
                result.invalid_count += 1
                continue
            completed = assigned * rate / 100

        result.assigned_points += assigned / priority
        result.achieved_points += completed / priority
        if completed >= assigned:
            result.completed_count += 1
    return result

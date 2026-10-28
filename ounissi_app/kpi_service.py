"""Read-only KPI snapshots, scoped to the requester and the team they lead.

No function in this module imports from a terminal or executes Xpert queries.
The displayed read time is not a source-synchronization timestamp.
"""
import calendar
import re
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Q
from django.utils import timezone

from accounts.models import Employee, Team
from .kpi_registry import settings_for
from .kpi_scoring import seconds, summarize_items
from .models import Schedule, ScheduleItem, Timesheet
from .navigation import ADMIN, LEADER, role_for

MONTH_RE = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')
POLL_SECONDS = 30


def parse_month(value, today=None):
    """Return the first day of a valid YYYY-MM, or raise ValueError."""
    today = today or timezone.localdate()
    if value is None or value == '':
        return today.replace(day=1)
    if not MONTH_RE.fullmatch(value):
        raise ValueError('Mois invalide. Utilisez le format AAAA-MM.')
    year, month = (int(part) for part in value.split('-'))
    try:
        return date(year, month, 1)
    except ValueError:
        raise ValueError('Mois invalide. Utilisez le format AAAA-MM.') from None


def format_number(value):
    value = Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    text = f'{value:,.2f}'.rstrip('0').rstrip('.')
    return text.replace(',', '\u00a0').replace('.', ',')


def format_duration(value):
    """Human-readable duration from exact seconds, rounded only for display."""
    total = int(Decimal(value).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    hours, remainder = divmod(max(0, total), 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f'{hours} h')
    if minutes or (hours and secs):
        parts.append(f'{minutes} min')
    if secs or not parts:
        parts.append(f'{secs} s' if secs else '0 min')
    return ' '.join(parts)


def current_schedule():
    """Match the active TRT selection without the status writes in the old helper."""
    now = timezone.localtime()
    return Schedule.objects.filter(is_template=False, date=now.date(),
                                   start_time__lte=now.time(), end_time__gte=now.time()).first()


def _time_pairs(record):
    return [(record.start_time, record.end_time),
            (record.start_time_2, record.end_time_2),
            (record.start_time_3, record.end_time_3)]


def _record_info(record):
    """Describe only saved punches; incomplete/invalid pairs are not work time."""
    if record.leave_type not in (None, '', Timesheet.LeaveType.NONE):
        return {'kind': 'leave', 'label': 'Congé enregistré', 'seconds': Decimal(0),
                'has_start': False, 'incomplete': False, 'last': None}
    worked = Decimal(0)
    incomplete = False
    ambiguous = False
    trustworthy = True
    previous_end = None
    open_indexes = []
    nonempty_indexes = []
    last = None
    for index, (start, end) in enumerate(_time_pairs(record)):
        if start or end:
            nonempty_indexes.append(index)
        if start:
            last = start
        if end:
            last = end
        if start and end:
            # Count the reliable prefix only. A pair after an incomplete pair,
            # an overlapping/reversed interval, or an overnight-looking pair is
            # ambiguous; summing it could count the same time twice.
            if not trustworthy or end <= start or (previous_end and start < previous_end):
                incomplete = ambiguous = True
                trustworthy = False
            else:
                worked += seconds(datetime.combine(record.date, end) - datetime.combine(record.date, start))
                previous_end = end
        elif start:
            open_indexes.append(index)
            incomplete = True
            if not trustworthy or (previous_end and start < previous_end):
                ambiguous = True
            trustworthy = False
        elif end:
            incomplete = ambiguous = True
            trustworthy = False
    has_start = any(start for start, _ in _time_pairs(record))
    if not nonempty_indexes:
        kind, label = 'unknown', 'Aucun horaire enregistré'
    elif open_indexes and len(open_indexes) == 1 and open_indexes[0] == nonempty_indexes[-1] and not ambiguous:
        kind, label = 'open', 'Pointage ouvert'
    elif incomplete:
        kind, label = 'unknown', 'Pointage à vérifier'
    else:
        kind, label = 'closed', 'Pointage clôturé'
    return {'kind': kind, 'label': label, 'seconds': worked, 'has_start': has_start,
            'incomplete': incomplete, 'last': last}


def _attendance(records, today):
    days = 0
    work_seconds = Decimal(0)
    pending = 0
    incomplete = 0
    future = 0
    rows = []
    for record in records:
        info = _record_info(record)
        if not record.is_confirmed:
            pending += 1
        elif record.date > today:
            future += 1
        elif info['kind'] != 'leave':
            if info['has_start']:
                days += 1
            work_seconds += info['seconds']
            incomplete += int(info['incomplete'] or info['kind'] == 'unknown')
        state = 'Confirmé' if record.is_confirmed else 'En attente'
        if record.date > today:
            state += ' · date future non comptabilisée'
        if info['kind'] not in ('leave', 'unknown'):
            state += f" · {format_duration(info['seconds'])} en plages complètes"
        rows.append({'primary': record.date.strftime('%d/%m/%Y'), 'secondary': state,
                     'status': info['label'], 'kind': info['kind']})
    return {'days': days, 'seconds': work_seconds, 'pending': pending,
            'incomplete': incomplete, 'future': future, 'rows': rows, 'count': len(records)}


def _team_records(members, records):
    by_employee = {record.employee_id: record for record in records}
    rows, counts = [], {'open': 0, 'closed': 0, 'unknown': 0, 'leave': 0}
    for member in members:
        record = by_employee.get(member.pk)
        info = _record_info(record) if record else None
        kind = info['kind'] if info else 'unknown'
        counts[kind] += 1
        name = ' '.join(part for part in (member.first_name, member.last_name) if part) or f'Employé {member.pk}'
        secondary = ('Confirmé' if record.is_confirmed else 'En attente') if record else 'Aucun pointage enregistré'
        if info and info['last']:
            secondary += f" · dernier horaire {info['last'].strftime('%H:%M')}"
        rows.append({'primary': name, 'secondary': secondary,
                     'status': info['label'] if info else 'État inconnu', 'kind': kind})
    return rows, counts


def _missing_score(summary, no_scope):
    if no_scope:
        return no_scope
    if not summary.task_count:
        return 'Aucune tâche affectée aujourd’hui.'
    if summary.invalid_count:
        return f'{summary.invalid_count} tâche(s) ont des données de progression, durée ou priorité invalides ou manquantes.'
    return 'Aucun objectif en points disponible.'


def _score_card(card, summary, no_scope, score=False):
    if no_scope or not summary.available:
        card.update(value='—', detail=_missing_score(summary, no_scope))
        return
    achieved = format_number(summary.achieved_points)
    assigned = format_number(summary.assigned_points)
    percentage = format_number(summary.percentage)
    if score:
        card.update(value=achieved, unit='points', detail=f'{assigned} points affectés · {percentage} % · TRT du jour')
    else:
        card.update(value=percentage, unit='%', detail=f'{achieved} / {assigned} points · TRT du jour')
        if card['presentation'] == 'bar':
            # The accessible bar is bounded; the actual percentage remains uncapped.
            card['bar_value'] = float(min(Decimal(100), max(Decimal(0), summary.percentage)))
            if summary.percentage > 100:
                card['warning'] = 'La barre est limitée à 100 % ; le résultat affiché ne l’est pas.'


def _tasks_card(card, summary, no_scope):
    if no_scope:
        card.update(value='—', detail=no_scope)
    elif not summary.task_count:
        card.update(value='—', detail='Aucune tâche affectée aujourd’hui.')
    else:
        card.update(value=f'{summary.completed_count} / {summary.task_count}', unit='tâches',
                    detail='Terminées / affectées dans les TRT du jour.')
        if summary.invalid_count:
            card['warning'] = f'{summary.invalid_count} tâche(s) sans données suffisantes pour déterminer leur état.'


def _personal_task(item):
    duration, assigned = seconds(item.task.average_duration), seconds(item.assigned_time)
    goal = str(int(assigned // duration)) if duration and duration > 0 and assigned is not None and assigned >= 0 else '—'
    # Automatic task percentages use the same saved line count and configured
    # seconds-per-line as the KPI score, even if a cached integer rate is stale.
    automatic = item.task.progress_query_id and item.task.progress_query.query != 'N/A'
    if automatic:
        lines = item.progress_lines
        rate = (Decimal(lines) * duration * 100 / assigned
                if isinstance(lines, int) and lines >= 0 and duration and duration > 0 and assigned and assigned > 0
                else None)
    else:
        rate = Decimal(item.progression_rate) if isinstance(item.progression_rate, int) and 0 <= item.progression_rate <= 100 else None
    name = item.task.task_name
    if item.task_identifier:
        name += f' : {item.task_identifier}'
    return {'id': item.pk, 'name': name, 'priority': str(item.task.priority or '—'), 'goal': goal,
            'rate_label': f'{format_number(rate)} %' if rate is not None else '—',
            'bar_value': float(min(Decimal(100), rate)) if rate is not None else 0}


def snapshot(user, surface, month=None, schedule_override=...):
    """Return one scoped, presentation-filtered snapshot for a signed-in user."""
    if surface not in ('live', 'dashboard'):
        raise ValueError('Emplacement inconnu.')
    now = timezone.localtime()
    today = now.date()
    month = month or today.replace(day=1)
    role = role_for(user)
    settings = [row for row in settings_for(role) if row['show_live' if surface == 'live' else 'show_dashboard']]
    keys = {row['definition'].key for row in settings}
    employee = Employee.objects.filter(user=user).first()
    schedule = current_schedule() if schedule_override is ... else schedule_override

    team = None
    members = []
    if employee and role in (LEADER, ADMIN) and any(key.startswith('team_') for key in keys):
        team = Team.objects.filter(leader=employee).prefetch_related('employees').first()
        if team:
            members = list(team.employees.all())
            if all(member.pk != employee.pk for member in members):
                members.append(employee)
            members.sort(key=lambda member: ((member.last_name or '').casefold(),
                                             (member.first_name or '').casefold(), member.pk))
    member_ids = {member.pk for member in members}
    personal_tasks_needed = surface == 'live' or bool(keys & {'my_progress', 'my_score', 'my_tasks', 'my_assigned'})
    team_tasks_needed = bool(keys & {'team_progress', 'team_score', 'team_tasks'})
    task_scope = Q(pk__in=[])
    if employee and personal_tasks_needed:
        task_scope |= Q(employee_id=employee.pk)
    if team and team_tasks_needed:
        task_scope |= Q(employee_id=employee.pk) | Q(employee_id__in=team.employees.values('pk'))
    # KPI totals cover every non-template TRT today, including completed or
    # future ones. The personal task table still shows only the active TRT.
    items = list(ScheduleItem.objects.filter(task_scope, schedule__date=today, schedule__is_template=False)
                 .select_related('task__progress_query').order_by('pk'))
    own_items = [item for item in items if employee and item.employee_id == employee.pk]
    active_items = [item for item in own_items if schedule and item.schedule_id == schedule.pk]
    team_items = [item for item in items if item.employee_id in member_ids]
    own_score, team_score = summarize_items(own_items), summarize_items(team_items)
    own_missing = 'Aucun profil d’employé associé à ce compte.' if not employee else None
    team_missing = 'Aucune équipe dirigée associée à ce compte.' if not team else None

    personal_records = []
    if employee and any(key.startswith('attendance_') for key in keys):
        last_day = calendar.monthrange(month.year, month.month)[1]
        personal_records = list(Timesheet.objects.filter(employee=employee, date__gte=month,
                                                        date__lte=month.replace(day=last_day)).order_by('-date'))
    attendance = _attendance(personal_records, today)
    team_record_rows, team_record_counts = [], {'open': 0, 'closed': 0, 'unknown': 0, 'leave': 0}
    if team and ('team_presence' in keys or 'team_records' in keys):
        records = list(Timesheet.objects.filter(Q(employee_id=employee.pk) | Q(employee_id__in=team.employees.values('pk')), date=today))
        team_record_rows, team_record_counts = _team_records(members, records)

    cards = []
    for setting in settings:
        definition = setting['definition']
        key = definition.key
        card = {'key': key, 'label': definition.label, 'icon': definition.icon,
                'presentation': setting['presentation'], 'value': '—', 'unit': '',
                'detail': '', 'warning': '', 'rows': []}
        if key == 'my_progress':
            _score_card(card, own_score, own_missing)
        elif key == 'my_score':
            _score_card(card, own_score, own_missing, score=True)
        elif key == 'my_tasks':
            _tasks_card(card, own_score, own_missing)
        elif key == 'my_assigned':
            if own_missing:
                card['detail'] = own_missing
            elif not own_score.task_count:
                card['detail'] = 'Aucune tâche affectée aujourd’hui.'
            elif own_score.invalid_time_count:
                card['detail'] = 'Au moins une durée affectée est invalide ou manquante.'
            else:
                card.update(value=format_duration(own_score.assigned_seconds), detail='Durées affectées dans les TRT du jour.')
        elif key.startswith('attendance_'):
            if not employee:
                card['detail'] = 'Aucun profil d’employé associé à ce compte.'
            elif not attendance['count']:
                card['detail'] = 'Aucun enregistrement pour ce mois ; une absence ne peut pas être déduite.'
            elif key == 'attendance_days':
                card.update(value=str(attendance['days']), unit='jours', detail='Jours avec entrée et pointage confirmé, hors congés.')
                if attendance['incomplete']:
                    card['warning'] = f"{attendance['incomplete']} pointage(s) confirmé(s) sont incomplets ou à vérifier."
            elif key == 'attendance_hours':
                card.update(value=format_duration(attendance['seconds']), detail='Plages complètes non ambiguës des pointages confirmés, hors congés.')
                if attendance['incomplete']:
                    card['warning'] = f"{attendance['incomplete']} pointage(s) confirmé(s) ont des plages incomplètes ou invalides."
            elif key == 'attendance_pending':
                card.update(value=str(attendance['pending']), unit='enregistrement(s)', detail='Pointages et congés en attente, exclus des totaux confirmés.')
            elif key == 'attendance_records':
                card.update(value=str(attendance['count']), unit='enregistrement(s)',
                            detail='Données enregistrées pour le mois sélectionné.', rows=attendance['rows'])
            if attendance['future'] and key in ('attendance_days', 'attendance_hours'):
                extra = f"{attendance['future']} enregistrement(s) futur(s) exclus."
                card['warning'] = ' '.join(filter(None, (card['warning'], extra)))
        elif key == 'team_progress':
            _score_card(card, team_score, team_missing)
        elif key == 'team_score':
            _score_card(card, team_score, team_missing, score=True)
        elif key == 'team_tasks':
            _tasks_card(card, team_score, team_missing)
        elif key in ('team_presence', 'team_records'):
            if not team:
                card['detail'] = 'Aucune équipe dirigée associée à ce compte.'
            elif key == 'team_presence':
                card.update(value=str(team_record_counts['open']), unit='pointage(s) ouvert(s)',
                            detail=f"Sur {len(members)} membre(s) · {team_record_counts['unknown']} état(s) inconnu(s) ou à vérifier.")
                card['warning'] = 'Pointages enregistrés, pas une présence en direct.'
            else:
                card.update(value=str(len(members)), unit='membre(s)',
                            detail='État enregistré aujourd’hui, pas une présence en direct.', rows=team_record_rows)
        cards.append(card)

    return {'cards': cards, 'surface': surface, 'month': f'{month.year:04d}-{month.month:02d}',
            'schedule_id': schedule.pk if schedule else None,
            'tasks': [_personal_task(item) for item in active_items] if surface == 'live' else [],
            'read_at': now.strftime('%H:%M:%S'), 'poll_seconds': POLL_SECONDS}


def annotate_schedule_employee_scores(schedule, schedule_employees, user):
    """Replace the legacy score display without exposing new data outside scope.

    The legacy management pages have broader pre-existing access. Do not add a
    precise score there for other teams (or to ordinary employee accounts).
    Administrators can view individual scores on the management pages.
    """
    employees = list(schedule_employees.select_related('employee'))
    role = role_for(user)
    allowed = set()
    task_scope = Q(pk__in=[])
    if role == ADMIN:
        allowed = {entry.employee_id for entry in employees}
        task_scope = Q(employee__isnull=False)
    elif role == LEADER:
        team = Team.objects.filter(leader__user=user).first()
        if team:
            allowed = set(team.employees.values_list('pk', flat=True)) | {team.leader_id}
            task_scope = Q(employee_id=team.leader_id) | Q(employee_id__in=team.employees.values('pk'))
    by_employee = {employee_id: [] for employee_id in allowed}
    if allowed:
        items = ScheduleItem.objects.filter(task_scope, schedule=schedule).select_related('task__progress_query')
        for item in items:
            if item.employee_id in by_employee:
                by_employee[item.employee_id].append(item)
    for entry in employees:
        if entry.employee_id not in allowed:
            entry.calculated_score = '—'
            entry.calculated_score_note = 'Score non disponible pour votre périmètre.'
            continue
        summary = summarize_items(by_employee[entry.employee_id])
        entry.calculated_score = format_number(summary.achieved_points) if summary.available else '—'
        entry.calculated_score_note = _missing_score(summary, None) if not summary.available else ''
    return employees

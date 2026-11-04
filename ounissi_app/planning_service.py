"""Safe, precise persistence for TRT templates and their dated copies.

The browser's planner works in integer microseconds (whole seconds for new
plans). The server treats it as an untrusted proposal and derives all stored
aggregate totals from the individual assignment rows.
"""
import hashlib
import json
from collections import defaultdict
from datetime import timedelta

from django.utils.dateparse import parse_time

from accounts.models import Employee, Rank, Team
from .models import Schedule, ScheduleEmployee, ScheduleItem, ScheduleTask, Task

MAX_SAFE_INTEGER = 9007199254740991  # exact in both JavaScript and Python
MAX_ROWS = 50000
TRANSFER_NAME = 'Transfer_Schedule'


class PlanningError(Exception):
    def __init__(self, message, *, conflict=False):
        super().__init__(message)
        self.conflict = conflict


def micros(value):
    if value is None:
        return 0
    return (value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds


def duration(value):
    return timedelta(microseconds=value)


def _valid_int(value, label, *, allow_zero=True):
    if type(value) is not int or value < (0 if allow_zero else 1) or value > MAX_SAFE_INTEGER:
        raise PlanningError(f'{label} est invalide. Actualisez la page et vérifiez les valeurs.')
    return value


def _id(value, label):
    return _valid_int(value, label, allow_zero=False)


def _duration(value, label):
    return _valid_int(value, label)


def _time(value, label):
    if not isinstance(value, str):
        raise PlanningError(f'{label} est invalide.')
    try:
        result = parse_time(value)
    except (TypeError, ValueError):
        result = None
    if result is None or result.tzinfo is not None:
        raise PlanningError(f'{label} est invalide.')
    return result


def catalog():
    tasks = [
        {'id': t.pk, 'name': t.task_name, 'type': t.task_type.name if t.task_type else 'Sans type',
         'priority': t.priority, 'rank': t.responsibility_level.value}
        for t in Task.objects.select_related('task_type', 'responsibility_level').order_by('task_name', 'pk')
    ]
    employees = [
        {'id': e.pk, 'name': str(e), 'rank': e.rank.value if e.rank else None}
        for e in Employee.objects.select_related('rank').order_by('first_name', 'last_name', 'pk')
    ]
    teams = []
    for team in Team.objects.prefetch_related('employees').order_by('name', 'pk'):
        members = {employee.pk for employee in team.employees.all()}
        if team.leader_id:
            members.add(team.leader_id)
        teams.append({'id': team.pk, 'name': team.name, 'members': sorted(members)})
    return {'tasks': tasks, 'employees': employees, 'teams': teams}


def load_snapshot(schedule, *, lock=False):
    """Read all definition data, including identifiers and legacy sub-seconds."""
    def rows(model):
        qs = model.objects.filter(schedule=schedule).order_by('pk')
        return list(qs.select_for_update() if lock else qs)

    task_rows = rows(ScheduleTask)
    employee_rows = rows(ScheduleEmployee)
    item_rows = rows(ScheduleItem)
    return {
        'schedule': schedule,
        'tasks': task_rows,
        'employees': employee_rows,
        'items': item_rows,
    }


def version(snapshot):
    schedule = snapshot['schedule']
    data = {
        'id': schedule.pk, 'name': schedule.schedule_name,
        'start': schedule.start_time.isoformat(), 'end': schedule.end_time.isoformat(),
        'tasks': [[r.pk, r.task_id, micros(r.total_time), micros(r.total_assigned_time), r.status, r.completion]
                  for r in snapshot['tasks']],
        'employees': [[r.pk, r.employee_id, micros(r.working_hours), micros(r.total_assigned_time), r.score]
                      for r in snapshot['employees']],
        'items': [[r.pk, r.task_id, r.employee_id, micros(r.assigned_time), r.task_identifier,
                   r.progression_rate, r.progress_lines] for r in snapshot['items']],
        # Eligibility and priority are part of what the planner displayed.
        'task_rules': sorted((t.pk, t.priority, t.responsibility_level.value) for t in
                             Task.objects.select_related('responsibility_level').in_bulk({r.task_id for r in snapshot['tasks']}).values()),
        'employee_rules': sorted((e.pk, e.rank.value if e.rank else None) for e in
                                 Employee.objects.select_related('rank').in_bulk({r.employee_id for r in snapshot['employees']}).values()),
    }
    canonical = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _lock_rule_ids(task_ids, employee_ids):
    # in_bulk batches for backends with parameter limits. Lock rank rows too:
    # the numeric value, not only the foreign key, determines eligibility.
    tasks = Task.objects.select_for_update().order_by('pk').in_bulk(sorted(task_ids))
    employees = Employee.objects.select_for_update().order_by('pk').in_bulk(sorted(employee_ids))
    rank_ids = {row.responsibility_level_id for row in tasks.values()}
    rank_ids.update(row.rank_id for row in employees.values() if row.rank_id)
    Rank.objects.select_for_update().order_by('pk').in_bulk(sorted(rank_ids))


def lock_rules(snapshot):
    """Serialize editor saves against concurrent task/employee rule edits."""
    _lock_rule_ids({row.task_id for row in snapshot['tasks']},
                   {row.employee_id for row in snapshot['employees']})


def totals_mismatch(snapshot):
    """Detect old aggregate drift before it is normalized from item rows."""
    by_task, by_employee = defaultdict(int), defaultdict(int)
    for item in snapshot['items']:
        if item.employee_id:
            by_task[item.task_id] += micros(item.assigned_time)
            by_employee[item.employee_id] += micros(item.assigned_time)
    return (any(micros(row.total_assigned_time) != by_task[row.task_id] for row in snapshot['tasks']) or
            any(micros(row.total_assigned_time) != by_employee[row.employee_id] for row in snapshot['employees']))


def draft_from_snapshot(snapshot, *, duplicate=False):
    """Refuse ambiguous legacy aggregates rather than silently dropping them."""
    schedule = snapshot['schedule']
    tasks = {}
    employees = {}
    for row in snapshot['tasks']:
        if row.task_id in tasks:
            raise PlanningError('Ce modèle contient plusieurs totaux pour une même tâche. Faites corriger ces données avant de le modifier ou de créer un TRT.')
        tasks[row.task_id] = {'id': row.task_id, 'total_us': micros(row.total_time)}
    for row in snapshot['employees']:
        if row.employee_id in employees:
            raise PlanningError('Ce modèle contient plusieurs totaux pour un même employé. Faites corriger ces données avant de le modifier ou de créer un TRT.')
        employees[row.employee_id] = {'id': row.employee_id, 'capacity_us': micros(row.working_hours)}
    items = []
    for row in snapshot['items']:
        if row.task_id not in tasks or (row.employee_id and row.employee_id not in employees):
            raise PlanningError('Une affectation de ce modèle ne possède pas de total de tâche ou d’employé. Faites corriger ces données avant de continuer.')
        items.append({'id': None if duplicate else row.pk, 'task_id': row.task_id,
                      'employee_id': row.employee_id, 'assigned_us': micros(row.assigned_time),
                      'identifier': row.task_identifier})
    for value in [r['total_us'] for r in tasks.values()] + [r['capacity_us'] for r in employees.values()] + [r['assigned_us'] for r in items]:
        if value < 0 or value > MAX_SAFE_INTEGER:
            raise PlanningError('Ce modèle contient une durée négative ou trop grande. Faites corriger ces données avant de continuer.')
    name = schedule.schedule_name or ''
    if duplicate:
        name = ('Copie — ' + name)[:255]
    return {'name': name, 'start': schedule.start_time.isoformat(), 'end': schedule.end_time.isoformat(),
            'tasks': list(tasks.values()), 'employees': list(employees.values()), 'items': items,
            'version': None if duplicate else version(snapshot)}


def _list(value, label):
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        raise PlanningError(f'La liste {label} est invalide ou trop longue.')
    return value


def validate_plan(payload, *, existing=None, check_confirmation=True, lock=False):
    """Validate against current DB ranks and derive every aggregate independently."""
    if not isinstance(payload, dict):
        raise PlanningError('Les données du modèle sont invalides.')
    name = payload.get('name')
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 255:
        raise PlanningError('Saisissez un nom de modèle (255 caractères maximum).')
    name = name.strip()
    if name == TRANSFER_NAME:
        raise PlanningError('Ce nom est réservé au transfert des tâches.')
    start = _time(payload.get('start'), 'L’heure de début')
    end = _time(payload.get('end'), 'L’heure de fin')
    task_input = _list(payload.get('tasks'), 'des tâches')
    employee_input = _list(payload.get('employees'), 'des employés')
    item_input = _list(payload.get('items'), 'des affectations')
    if not task_input:
        raise PlanningError('Sélectionnez au moins une tâche.')

    tasks = {}
    employees = {}
    for row in task_input:
        if not isinstance(row, dict):
            raise PlanningError('Une tâche est invalide.')
        pk = _id(row.get('id'), 'La tâche')
        if pk in tasks:
            raise PlanningError('Une tâche est sélectionnée plusieurs fois.')
        tasks[pk] = {'total_us': _duration(row.get('total_us'), 'La durée de la tâche'),
                     'priority': row.get('priority'), 'rank': row.get('rank')}
    for row in employee_input:
        if not isinstance(row, dict):
            raise PlanningError('Un employé est invalide.')
        pk = _id(row.get('id'), 'L’employé')
        if pk in employees:
            raise PlanningError('Un employé est sélectionné plusieurs fois.')
        employees[pk] = {'capacity_us': _duration(row.get('capacity_us'), 'Le temps de travail'),
                         'rank': row.get('rank')}

    # Hold current eligibility rules stable through the write transaction.
    # Read-only previews call this without locks.
    if lock:
        _lock_rule_ids(tasks, employees)
    # in_bulk() may exceed SQLite parameter limits for large lists; the ORM
    # performs the necessary backend-specific batching.
    task_objects = Task.objects.select_related('responsibility_level').in_bulk(tasks)
    employee_objects = Employee.objects.select_related('rank').in_bulk(employees)
    if len(task_objects) != len(tasks) or len(employee_objects) != len(employees):
        raise PlanningError('Une tâche ou un employé a été supprimé. Actualisez la page.', conflict=True)
    for pk, row in tasks.items():
        obj = task_objects[pk]
        if obj.priority not in (1, 2, 3, 4):
            raise PlanningError(f'La priorité de « {obj.task_name} » doit être comprise entre 1 et 4.')
        if type(row['priority']) is not int or row['priority'] != obj.priority or type(row['rank']) is not int or row['rank'] != obj.responsibility_level.value:
            raise PlanningError('La priorité ou le niveau d’une tâche a changé. Actualisez le modèle.', conflict=True)
    for pk, row in employees.items():
        obj = employee_objects[pk]
        rank = obj.rank.value if obj.rank else None
        if row['rank'] != rank or (row['rank'] is not None and type(row['rank']) is not int):
            raise PlanningError('Le niveau d’un employé a changé. Actualisez le modèle.', conflict=True)

    old_items = {item.pk: item for item in (existing['items'] if existing else [])}
    seen_ids = set()
    task_assigned, employee_assigned, unassigned_items = defaultdict(int), defaultdict(int), defaultdict(int)
    items = []
    zero_assigned_items = 0
    for row in item_input:
        if not isinstance(row, dict):
            raise PlanningError('Une affectation est invalide.')
        task_id = _id(row.get('task_id'), 'La tâche affectée')
        employee_id = row.get('employee_id')
        if task_id not in tasks:
            raise PlanningError('Une affectation utilise une tâche non sélectionnée.')
        if employee_id is not None:
            employee_id = _id(employee_id, 'L’employé affecté')
            if employee_id not in employees:
                raise PlanningError('Une affectation utilise un employé non sélectionné.')
            emp = employee_objects[employee_id]
            task = task_objects[task_id]
            if not emp.rank or emp.rank.value > task.responsibility_level.value:
                raise PlanningError(f'« {emp} » n’est pas éligible à « {task.task_name} ».')
        amount = _duration(row.get('assigned_us'), 'La durée affectée')
        identifier = row.get('identifier')
        if identifier is not None and (not isinstance(identifier, str) or len(identifier) > 100):
            raise PlanningError('La référence d’une affectation est invalide (100 caractères maximum).')
        item_id = row.get('id')
        if item_id is not None:
            item_id = _id(item_id, 'L’affectation')
            if item_id not in old_items or item_id in seen_ids:
                raise PlanningError('Une affectation ne correspond plus au modèle. Actualisez la page.', conflict=True)
            seen_ids.add(item_id)
        if employee_id is None:
            unassigned_items[task_id] += amount
        else:
            task_assigned[task_id] += amount
            employee_assigned[employee_id] += amount
            if amount == 0:
                zero_assigned_items += 1
        items.append({'id': item_id, 'task_id': task_id, 'employee_id': employee_id,
                      'assigned_us': amount, 'identifier': identifier})

    # The editor asks for confirmation before deleting or replacing rows.
    # Enforce the acknowledgement server-side too, so an incomplete payload
    # cannot silently discard existing manual work.
    if existing:
        removed_ids = set(old_items) - seen_ids
        acknowledged = _list(payload.get('acknowledged_removed_item_ids', []), 'des suppressions confirmées')
        acknowledged = {_id(value, 'Une suppression confirmée') for value in acknowledged}
        if not removed_ids.issubset(acknowledged):
            raise PlanningError('Confirmez la suppression des affectations existantes avant d’enregistrer.')

    for values in (task_assigned.values(), employee_assigned.values(), unassigned_items.values(),
                   (row['total_us'] for row in tasks.values()), (row['capacity_us'] for row in employees.values())):
        if any(value > MAX_SAFE_INTEGER for value in values):
            raise PlanningError('Un total dépasse la limite de durée prise en charge.')
    if (sum(row['total_us'] for row in tasks.values()) > MAX_SAFE_INTEGER or
        sum(row['capacity_us'] for row in employees.values()) > MAX_SAFE_INTEGER or
        sum(row['assigned_us'] for row in items) > MAX_SAFE_INTEGER):
        raise PlanningError('Le total des durées dépasse la limite prise en charge.')

    warnings = []
    remaining = {pk: max(0, row['total_us'] - task_assigned[pk]) for pk, row in tasks.items()}
    missing_tasks = [pk for pk, value in remaining.items() if value > 0]
    pending_rows = sum(1 for item in items if item['employee_id'] is None)
    over_tasks = [pk for pk, row in tasks.items() if task_assigned[pk] > row['total_us']]
    over_employees = [pk for pk, row in employees.items() if employee_assigned[pk] > row['capacity_us']]
    if missing_tasks or pending_rows:
        warnings.append(f'{len(missing_tasks)} tâche(s) ont du temps non affecté ; {pending_rows} ligne(s) sont sans employé.')
    if zero_assigned_items:
        warnings.append(f'{zero_assigned_items} affectation(s) ont une durée nulle ; vérifiez-les ou retirez-les.')
    if over_tasks or over_employees:
        warnings.append(f'{len(over_tasks)} tâche(s) dépassent leur durée et {len(over_employees)} employé(s) dépassent leur capacité.')
    if check_confirmation and warnings and payload.get('confirm_warnings') is not True:
        raise PlanningError('Confirmez les avertissements avant l’enregistrement : ' + ' '.join(warnings))
    return {'name': name, 'start': start, 'end': end, 'tasks': tasks, 'employees': employees,
            'items': items, 'task_assigned': task_assigned, 'employee_assigned': employee_assigned,
            'remaining': remaining, 'warnings': warnings, 'unassigned_items': unassigned_items}


def plan_for_snapshot(snapshot, catalog_data):
    """Build/validate a draft using current task and employee eligibility."""
    draft = draft_from_snapshot(snapshot)
    tasks = {r['id']: r for r in catalog_data['tasks']}
    employees = {r['id']: r for r in catalog_data['employees']}
    for row in draft['tasks']:
        if row['id'] not in tasks:
            raise PlanningError('Une tâche du modèle a été supprimée.')
        row.update(priority=tasks[row['id']]['priority'], rank=tasks[row['id']]['rank'])
    for row in draft['employees']:
        if row['id'] not in employees:
            raise PlanningError('Un employé du modèle a été supprimé.')
        row.update(rank=employees[row['id']]['rank'])
    plan = validate_plan(draft, existing=snapshot, check_confirmation=False)
    if totals_mismatch(snapshot):
        plan['warnings'].append('Les anciens totaux enregistrés ne correspondent pas aux lignes : ils seront recalculés dans le TRT daté.')
    return draft, plan


def _has_dependencies(item_ids, schedule_id):
    if not item_ids:
        return False
    # Protect CASCADE and SET_NULL references without issuing one query per
    # small item-ID chunk for very large templates.
    changed = set(item_ids)
    for relation in ScheduleItem._meta.related_objects:
        name = relation.field.name
        linked_ids = relation.related_model._default_manager.filter(
            **{f'{name}__schedule_id': schedule_id}
        ).values_list(f'{name}_id', flat=True)
        if any(pk in changed for pk in linked_ids.iterator(chunk_size=1000)):
            return True
    return False


def _delete_ids(model, ids):
    for offset in range(0, len(ids), 400):
        model.objects.filter(pk__in=ids[offset:offset + 400]).delete()


def persist_template(schedule, plan, *, existing=None):
    """Update only this template; preserve IDs/progress on unchanged rows."""
    old_tasks = {row.task_id: row for row in (existing['tasks'] if existing else [])}
    old_employees = {row.employee_id: row for row in (existing['employees'] if existing else [])}
    old_items = {row.pk: row for row in (existing['items'] if existing else [])}
    incoming = {row['id']: row for row in plan['items'] if row['id'] is not None}
    changed = []
    for pk, item in old_items.items():
        row = incoming.get(pk)
        if row is None or (row['task_id'], row['employee_id'], row['assigned_us'], row['identifier']) != (
            item.task_id, item.employee_id, micros(item.assigned_time), item.task_identifier
        ):
            changed.append(pk)
    if _has_dependencies(changed, schedule.pk):
        raise PlanningError('Une affectation modifiée ou retirée est liée à une réception. Aucune donnée n’a été effacée ; corrigez cette liaison avant de modifier le modèle.')

    schedule.schedule_name, schedule.start_time, schedule.end_time = plan['name'], plan['start'], plan['end']
    schedule.save(update_fields=['schedule_name', 'start_time', 'end_time'])

    task_new, task_update = [], []
    for pk, row in plan['tasks'].items():
        obj = old_tasks.get(pk) or ScheduleTask(schedule=schedule, task_id=pk)
        obj.total_time = duration(row['total_us'])
        obj.total_assigned_time = duration(plan['task_assigned'][pk])
        (task_update if obj.pk else task_new).append(obj)
    employee_new, employee_update = [], []
    for pk, row in plan['employees'].items():
        obj = old_employees.get(pk) or ScheduleEmployee(schedule=schedule, employee_id=pk)
        obj.working_hours = duration(row['capacity_us'])
        obj.total_assigned_time = duration(plan['employee_assigned'][pk])
        (employee_update if obj.pk else employee_new).append(obj)
    item_new, item_update = [], []
    for row in plan['items']:
        obj = old_items.get(row['id']) or ScheduleItem(schedule=schedule)
        obj.task_id, obj.employee_id = row['task_id'], row['employee_id']
        obj.assigned_time, obj.task_identifier = duration(row['assigned_us']), row['identifier']
        (item_update if obj.pk else item_new).append(obj)

    _delete_ids(ScheduleItem, [pk for pk in old_items if pk not in incoming])
    _delete_ids(ScheduleTask, [row.pk for pk, row in old_tasks.items() if pk not in plan['tasks']])
    _delete_ids(ScheduleEmployee, [row.pk for pk, row in old_employees.items() if pk not in plan['employees']])
    ScheduleTask.objects.bulk_create(task_new, batch_size=400)
    ScheduleEmployee.objects.bulk_create(employee_new, batch_size=400)
    ScheduleItem.objects.bulk_create(item_new, batch_size=400)
    if task_update:
        ScheduleTask.objects.bulk_update(task_update, ['total_time', 'total_assigned_time'], batch_size=200)
    if employee_update:
        ScheduleEmployee.objects.bulk_update(employee_update, ['working_hours', 'total_assigned_time'], batch_size=200)
    if item_update:
        ScheduleItem.objects.bulk_update(item_update, ['task', 'employee', 'assigned_time', 'task_identifier'], batch_size=200)


def copy_to_dated(snapshot, plan, *, date, start, end):
    """Clone from the database, never from HTML-rendered/truncated values."""
    target = Schedule.objects.create(date=date, start_time=start, end_time=end, is_template=False)
    ScheduleTask.objects.bulk_create([
        ScheduleTask(schedule=target, task_id=pk, total_time=duration(row['total_us']),
                     total_assigned_time=duration(plan['task_assigned'][pk]))
        for pk, row in plan['tasks'].items()
    ], batch_size=400)
    ScheduleEmployee.objects.bulk_create([
        ScheduleEmployee(schedule=target, employee_id=pk, working_hours=duration(row['capacity_us']),
                         total_assigned_time=duration(plan['employee_assigned'][pk]))
        for pk, row in plan['employees'].items()
    ], batch_size=400)
    ScheduleItem.objects.bulk_create([
        ScheduleItem(schedule=target, task_id=row['task_id'], employee_id=row['employee_id'],
                     assigned_time=duration(row['assigned_us']), task_identifier=row['identifier'])
        for row in plan['items']
    ], batch_size=400)
    return target

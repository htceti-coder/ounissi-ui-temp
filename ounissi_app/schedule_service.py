from datetime import timedelta
import re
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.apps import apps
from django.db import connection, transaction
from django.core.exceptions import ValidationError
from django.db.models import Sum, F, FloatField, Q
from django.db.models.functions import Cast
from accounts.models import Employee
from .models import Reception, Schedule, ScheduleItem, ScheduleTask, ScheduleEmployee, TaskGeneration, Task, User
from .xpertcon import XpertConnect

import logging

logger = logging.getLogger(__name__)


def update_all_schedule_statuses():
    """
    Updates the status of all schedules based on the current date and time.
    0 = Planifié, 1 = En Cours, 2 = Non Clôturé, 3 = Clôturé
    """
    now = timezone.localtime()
    today = now.date()
    current_time = now.time()
    limit_date = today - timedelta(days=30)

    # 1. Set older than 30 days to Clôturé (3)
    Schedule.objects.filter(is_template=False, date__lt=limit_date).exclude(schedule_status=3).update(schedule_status=3)

    # 2. Set past schedules (but newer than 30 days) to Non Clôturé (2)
    Schedule.objects.filter(is_template=False, date__lt=today, date__gte=limit_date).exclude(schedule_status=3).update(
        schedule_status=2)

    # 3. Handle Today's schedules
    today_schedules = Schedule.objects.filter(is_template=False, date=today).exclude(schedule_status=3)
    for sched in today_schedules:
        if current_time < sched.start_time:
            new_status = 0  # Planifié
        elif sched.start_time <= current_time <= sched.end_time:
            new_status = 1  # En Cours
        else:
            new_status = 2  # Non Clôturé (Ended today)

        if sched.schedule_status != new_status:
            sched.schedule_status = new_status
            sched.save(update_fields=['schedule_status'])


def get_current_active_schedule():
    """Returns the currently active schedule for today, or None."""
    update_all_schedule_statuses()
    now = timezone.localtime()
    return Schedule.objects.filter(
        is_template=False,
        date=now.date(),
        start_time__lte=now.time(),
        end_time__gte=now.time()
    ).first()


def get_schedule_context_data(schedule, employee=None):
    """
    Fetches all related items, tasks, and employees for a given schedule.
    If employee is provided, filters items for that specific employee.
    """
    if not schedule:
        return None

    context = {
        'schedule': schedule,
        'schedule_employees': ScheduleEmployee.objects.filter(schedule=schedule),
        'schedule_tasks': ScheduleTask.objects.filter(schedule=schedule),
    }

    if employee:
        context['schedule_items'] = ScheduleItem.objects.filter(schedule=schedule, employee=employee)
    else:
        context['schedule_items'] = ScheduleItem.objects.filter(schedule=schedule, employee__isnull=False)

    # Managers may plan later reception steps before their predecessors are
    # finished.  Keep the predecessor status visible, but never use it as an
    # assignment gate.
    no_employee_items = ScheduleItem.objects.filter(schedule=schedule, employee__isnull=True).select_related('task')
    non_assigned_items = []
    for item in no_employee_items:
        original_item = get_original_item(item)
        if not original_item:
            # Keep orphaned generated tasks visible so a manager can diagnose
            # their configuration instead of silently losing the assignment.
            if reception_identifier(item.task_identifier):
                item.previous_step_missing = True
                non_assigned_items.append(item)
            continue
        previous_items = process_items(
            ScheduleItem.objects.filter(schedule=original_item.schedule, task=original_item.task),
            item.task_identifier,
        )
        assigned_previous = [previous for previous in previous_items if previous.employee_id]
        item.previous_step_complete = bool(assigned_previous) and all(
            (previous.progression_rate or 0) >= 100 for previous in assigned_previous
        )
        non_assigned_items.append(item)

    context['non_assigned_items'] = non_assigned_items
    return context


# --- PROGRESS CALCULATION LOGIC ---

def run_raw_query(query_obj, parameters):
    """Helper to run queries and return dict (replaces old Pandas logic)"""
    try:
        if query_obj.type == 0:  # Internal SQLite
            with connection.cursor() as cursor:
                cursor.execute(query_obj.query, parameters)
                results = cursor.fetchall()
                columns = [col[0] for col in cursor.description]
                data_rows = [tuple(row) for row in results]
                return {'columns': columns, 'data_rows': data_rows}
        else:  # Xpertpharm
            conn = XpertConnect()
            conn.open_session()
            res = conn.run_query(query_obj.query, parameters)
            conn.close_session()
            return res
    except Exception as e:
        logger.error(f"Query Error: {e}")
        return None


def get_item_progress(item_id):
    """Refresh one auto task; a failed query must not erase saved progress."""
    item = ScheduleItem.objects.select_related('employee', 'schedule', 'task__progress_query').get(pk=item_id)
    query = item.task.progress_query
    if not query or not item.employee_id or not item.schedule.date:
        return False

    parameters = dict(query.parameters or {})
    model_name = parameters.pop('model_name', None)
    if (query.type == 1 or 'xp_id' in parameters) and not item.employee.xp_id:
        logger.warning('Employee has no Xpert identifier for schedule item %s', item.pk)
        return False
    parameters.update({
        'start_date': item.schedule.date,
        'end_date': item.schedule.date + timedelta(days=1),
        'xp_id': item.employee.xp_id,
    })

    needs_invoice = any(name in parameters for name in ('supplier', 'ref_tiers'))
    if needs_invoice and (not model_name or not reception_identifier(item.task_identifier)):
        logger.warning('Invoice progress query for schedule item %s has no model_name or reference', item.pk)
        return False
    if item.task_identifier and model_name:
        try:
            model = apps.get_model('ounissi_app', model_name)
            field_names = {field.name for field in model._meta.get_fields()}
            if {'supplier', 'ref_tiers'}.issubset(field_names):
                details = get_related_item_details(item, model)
                if not details:
                    logger.warning('No related reception details for schedule item %s', item.pk)
                    return False
                parameters['supplier'] = details[0].supplier
                parameters['ref_tiers'] = details[0].ref_tiers
                if not parameters['supplier'] or not parameters['ref_tiers']:
                    logger.warning('Incomplete invoice reference for schedule item %s', item.pk)
                    return False
            elif needs_invoice:
                logger.warning('Invoice model for schedule item %s does not contain supplier and reference', item.pk)
                return False
        except Exception:
            logger.exception('Unable to prepare progress parameters for schedule item %s', item.pk)
            return False

    results = run_raw_query(query, parameters)
    if not isinstance(results, dict) or 'error' in results:
        return False
    try:
        columns = results['columns']
        rows = results['data_rows']
        col_idx = columns.index('NBR_LINES')
        if len(rows) > 1:
            # Without the live query's grouping contract, summing or choosing
            # an arbitrary row could attribute another invoice/day to a task.
            raise ValueError('Progress query must return at most one row per assigned task')
        nbr_lines = int(rows[0][col_idx] or 0) if rows else 0
        if nbr_lines < 0 or item.task.average_duration is None:
            raise ValueError('Invalid line count or task duration')
        assigned_sec = item.assigned_time.total_seconds()
        task_sec = item.task.average_duration.total_seconds()
        if task_sec < 0:
            raise ValueError('Invalid task duration')
        # TOTAL_TIME in Xpert is NBR_LINES × the configured per-line time.
        # The application uses its configured task duration for this calculation.
        item.progress_lines = nbr_lines
        item.progression_rate = int((nbr_lines * task_sec / assigned_sec) * 100) if assigned_sec > 0 else 0
        item.save(update_fields=['progress_lines', 'progression_rate'])
        return True
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        logger.warning('Invalid progress result for schedule item %s', item.pk, exc_info=True)
        return False


#
# def update_schedule_progress(schedule, employee=None):
#     """Updates progress for all items in a schedule using a single DB connection."""
#
#     if employee:
#         schedule_items = ScheduleItem.objects.filter(schedule=schedule, employee=employee).select_related(
#             'task__progress_query', 'employee')
#     else:
#         schedule_items = ScheduleItem.objects.filter(schedule=schedule, employee__isnull=False).select_related(
#             'task__progress_query', 'employee')
#
#     # 1. OPEN A SINGLE XPERTPHARM CONNECTION FOR THE ENTIRE LOOP
#     conn = XpertConnect()
#     try:
#         conn.open_session()
#
#         # 2. Process all items using the active connection
#         for item in schedule_items:
#             if item.task.progress_query and item.task.progress_query.query != 'N/A':
#                 # We pass the active connection directly to a modified progress function
#                 _calculate_item_progress_with_conn(item, conn)
#
#     finally:
#         # 3. ENSURE THE CONNECTION CLOSES EVEN IF AN ERROR OCCURS
#         conn.close_session()
#
#     # 4. OPTIMIZE EMPLOYEE SCORES (No more Python looping, let the Database do the math)
#     # This replaces your python "sum(... for i in ...)" loop with a single lightning-fast database query
#     sched_emps = ScheduleEmployee.objects.filter(schedule=schedule)
#     for sched_emp in sched_emps:
#         # Calculate score: SUM(progress_lines / task.priority)
#         total_score = ScheduleItem.objects.filter(
#             employee=sched_emp.employee,
#             schedule=schedule,
#             task__priority__gt=0
#         ).aggregate(
#             score=Sum(Cast('progress_lines', FloatField()) / Cast('task__priority', FloatField()))
#         )['score'] or 0
#
#         sched_emp.score = total_score
#         sched_emp.save(update_fields=['score'])
#
#     # 5. OPTIMIZE TASK COMPLETION
#     for sched_task in ScheduleTask.objects.filter(schedule=schedule):
#         total_sec = sched_task.total_assigned_time.total_seconds()
#         if total_sec > 0:
#             # Aggregate the completed seconds directly in the database
#             completion_sec = ScheduleItem.objects.filter(
#                 task=sched_task.task,
#                 schedule=schedule
#             ).aggregate(
#                 completed=Sum((F('progression_rate') / 100.0) * F('assigned_time'))
#             )['completed']
#
#             # Extract total seconds from the timedelta if it exists
#             completion_sec_val = completion_sec.total_seconds() if completion_sec else 0
#
#             sched_task.completion = int((completion_sec_val / total_sec) * 100)
#             if sched_task.completion >= 100:
#                 sched_task.status = 2
#             sched_task.save(update_fields=['completion', 'status'])
#         else:
#             sched_task.delete()


def update_schedule_progress(schedule, employee=None):
    """Updates progress for all items in a schedule."""
    if employee:
        schedule_items = ScheduleItem.objects.filter(schedule=schedule, employee=employee)
    else:
        schedule_items = ScheduleItem.objects.filter(schedule=schedule, employee__isnull=False)

    attempted = 0
    failed = 0
    for item in schedule_items:
        if item.task.progress_query and item.task.progress_query.query != 'N/A':
            attempted += 1
            if not get_item_progress(item.id):
                failed += 1

    # Updating employees scores
    for sched_emp in ScheduleEmployee.objects.filter(schedule=schedule):
        score = sum((i.progress_lines or 0) / i.task.priority for i in
                    ScheduleItem.objects.filter(employee=sched_emp.employee, schedule=schedule) if i.task.priority)
        sched_emp.score = score
        sched_emp.save(update_fields=['score'])

    # Updating Tasks Completion
    for sched_task in ScheduleTask.objects.filter(schedule=schedule):
        completion_sec = sum(((i.progression_rate or 0) / 100) * i.assigned_time.total_seconds() for i in
                             ScheduleItem.objects.filter(task=sched_task.task, schedule=schedule))
        total_sec = (sched_task.total_assigned_time or timedelta()).total_seconds()

        if total_sec > 0:
            sched_task.completion = int((completion_sec / total_sec) * 100)
            if sched_task.completion >= 100:
                sched_task.status = 2
            sched_task.save(update_fields=['completion', 'status'])
        else:
            sched_task.delete()

    return {'attempted': attempted, 'failed': failed}


def update_manual_progress(schedule, updates):
    """Atomically update only assigned manual items from one TRT.

    ``updates`` contains validated (item_id, integer percentage) pairs. Automatic
    items are intentionally excluded; their progress belongs to their query.
    """
    with transaction.atomic():
        # Lock the TRT and selected rows before checking their current type and
        # ownership. This avoids accepting a stale or cross-TRT selection.
        Schedule.objects.select_for_update().get(pk=schedule.pk)
        item_ids = [item_id for item_id, _ in updates]
        items = list(ScheduleItem.objects.select_for_update().filter(pk__in=item_ids)
                     .select_related('task').prefetch_related('task__progress_query').order_by('pk'))
        if len(items) != len(item_ids) or any(
            item.schedule_id != schedule.pk or not item.employee_id or
            (item.task.progress_query_id and item.task.progress_query.query != 'N/A')
            for item in items
        ):
            raise ValidationError('La sélection doit contenir uniquement des tâches manuelles affectées de ce TRT. Actualisez la page et réessayez.')

        rates = dict(updates)
        for item in items:
            item.progression_rate = rates[item.pk]
        ScheduleItem.objects.bulk_update(items, ['progression_rate'])

        # Refresh only aggregates for affected task types. This does not run any
        # Xpert query or change assigned time / progress-line counts.
        task_ids = {item.task_id for item in items}
        items_by_task = {}
        microsecond = timedelta(microseconds=1)
        for task_id, rate, duration in ScheduleItem.objects.filter(
            schedule=schedule, task_id__in=task_ids,
        ).values_list('task_id', 'progression_rate', 'assigned_time'):
            # Integer microseconds avoid floating-point rounding a true 29% to 28%.
            items_by_task[task_id] = items_by_task.get(task_id, 0) + (rate or 0) * (duration // microsecond)
        for sched_task in ScheduleTask.objects.select_for_update().filter(schedule=schedule, task_id__in=task_ids):
            total = (sched_task.total_assigned_time or timedelta()) // microsecond
            if total <= 0:
                continue  # Preserve legacy empty aggregates; never delete them here.
            weighted = items_by_task.get(sched_task.task_id, 0)
            # Truncate toward zero, including for malformed legacy negative rates.
            sched_task.completion = weighted // total if weighted >= 0 else -((-weighted) // total)
            if sched_task.completion >= 100:
                sched_task.status = 2
            elif sched_task.status in (0, 1, 2):
                sched_task.status = 1 if sched_task.completion > 0 else 0
            sched_task.save(update_fields=['completion', 'status'])
    return len(items)


# --- UTILITY HELPERS ---

_LINE_SUFFIX = re.compile(r'(?:Lines:\d+<>\d+)+$')


def reception_identifier(identifier):
    """Remove only generated line-range suffixes, not text within a reference."""
    return _LINE_SUFFIX.sub('', identifier or '')


def process_items(queryset, identifier):
    """Find the exact supplier/reference, including its generated line ranges."""
    base = reception_identifier(identifier)
    if not base:
        return []
    candidates = queryset.filter(Q(task_identifier=base) | Q(task_identifier__startswith=base + 'Lines:'))
    return [candidate for candidate in candidates if reception_identifier(candidate.task_identifier) == base]


def get_original_item(item):
    generation = TaskGeneration.objects.filter(generated_task=item.task).order_by('order', 'pk').first()
    if not generation or not reception_identifier(item.task_identifier):
        return None

    # Prefer a parent in this TRT.  Transferred reception steps may only have a
    # matching parent in an earlier TRT; use the most recent exact match then.
    parents = ScheduleItem.objects.filter(task=generation.parent_task).select_related('schedule')
    local = process_items(parents.filter(schedule=item.schedule).order_by('pk'), item.task_identifier)
    if local:
        return next((parent for parent in local if parent.task_identifier == item.task_identifier), local[0])
    earlier = parents.filter(schedule__is_template=False)
    if item.schedule.date:
        earlier = earlier.filter(
            Q(schedule__date__lt=item.schedule.date) |
            Q(schedule__date=item.schedule.date, schedule__start_time__lte=item.schedule.start_time)
        )
    matches = process_items(earlier.order_by('-schedule__date', '-schedule__start_time', '-pk'), item.task_identifier)
    return matches[0] if matches else None


def missing_reception_followups(schedule):
    """Identify completed legacy receptions whose next step is absent in this TRT."""
    if not schedule:
        return []
    receptions = list(Reception.objects.filter(
        related_item__schedule=schedule, related_item__progression_rate__gte=100,
    ).select_related('related_item__task'))
    parent_ids = {reception.related_item.task_id for reception in receptions}
    generations = {}
    for generation in TaskGeneration.objects.filter(parent_task_id__in=parent_ids).select_related('generated_task').order_by('order', 'pk'):
        generations.setdefault(generation.parent_task_id, generation)
    existing = {
        (task_id, reception_identifier(identifier))
        for task_id, identifier in ScheduleItem.objects.filter(schedule=schedule).values_list('task_id', 'task_identifier')
        if identifier
    }
    missing = []
    for reception in receptions:
        identifier = reception_identifier(reception.related_item.task_identifier)
        generation = generations.get(reception.related_item.task_id)
        if generation and identifier and (generation.generated_task_id, identifier) in existing:
            continue
        missing.append({
            'reception': reception,
            'next_task': generation.generated_task if generation else None,
            'can_restore': bool(generation and identifier),
        })
    return missing


def get_related_item_details(schedule_item, model):
    related_objects = []
    for field in model._meta.get_fields():
        if field.is_relation and field.related_model == ScheduleItem:
            filter_kwargs = {field.name: schedule_item}
            related_objects.extend(model.objects.filter(**filter_kwargs))
    return related_objects


def update_schedule_from_item(task_id, assigned_time, schedule_id, employee_id=None, task_identifier=None,
                              progression_rate=None, item_id=None):
    # (Your existing logic, just moved here to keep views clean)
    existing_item = None
    if item_id:
        existing_item = get_object_or_404(ScheduleItem, pk=item_id)
    elif employee_id and task_id and schedule_id:
        query = {'employee_id': employee_id, 'task_id': task_id, 'schedule_id': schedule_id}
        if task_identifier:
            query['task_identifier'] = task_identifier
        existing_item = ScheduleItem.objects.filter(**query).first()

    if existing_item:
        schedule_item = existing_item
        assigned_delta = assigned_time - schedule_item.assigned_time
    else:
        schedule_item = ScheduleItem(task_id=task_id, schedule_id=schedule_id, progression_rate=0)
        assigned_delta = assigned_time

    if assigned_time: schedule_item.assigned_time = assigned_time
    if employee_id: schedule_item.employee_id = employee_id
    if task_identifier: schedule_item.task_identifier = task_identifier
    if progression_rate is not None: schedule_item.progression_rate = progression_rate

    schedule_item.save()
    return schedule_item, assigned_delta


def update_employee_task_from_item(schedule_item, assigned_delta):
    schedule = schedule_item.schedule
    employee = schedule_item.employee

    sched_emp, created = ScheduleEmployee.objects.get_or_create(
        employee=employee, schedule=schedule,
        defaults={'total_assigned_time': schedule_item.assigned_time}
    )
    if not created:
        sched_emp.total_assigned_time = (sched_emp.total_assigned_time or timedelta()) + assigned_delta
        sched_emp.save()

    sched_task, created = ScheduleTask.objects.get_or_create(
        task=schedule_item.task, schedule=schedule,
        defaults={'total_time': schedule_item.assigned_time, 'total_assigned_time': schedule_item.assigned_time}
    )
    if not created:
        sched_task.total_assigned_time = (sched_task.total_assigned_time or timedelta()) + assigned_delta
        sched_task.save()


def generate_group_task(schedule_item, schedule):
    """Create the configured next step once, unassigned, for this reception."""
    generation = TaskGeneration.objects.filter(parent_task=schedule_item.task).order_by('order', 'pk').first()
    if not generation:
        return False
    identifier = reception_identifier(schedule_item.task_identifier)
    with transaction.atomic():
        # Serialize requests for the same TRT so double submissions do not
        # generate duplicate next steps on databases with row-level locks.
        Schedule.objects.select_for_update().get(pk=schedule.pk)
        existing = process_items(
            ScheduleItem.objects.filter(schedule=schedule, task=generation.generated_task), identifier,
        )
        if existing:
            return True
        ScheduleItem.objects.create(
            schedule=schedule,
            task=generation.generated_task,
            task_identifier=identifier or schedule_item.task_identifier,
        )
    return True

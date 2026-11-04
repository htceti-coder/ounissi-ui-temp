"""Role-gated template authoring and dated TRT creation."""
import json
import logging
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import Http404, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from . import planning_service as planning, schedule_service
from .models import Schedule, ScheduleCreationRequest, ScheduleItem
from .navigation import ADMIN, MANAGERS, role_for
from .utils import getLoggedData

logger = logging.getLogger(__name__)


def _require_role(request, roles):
    if role_for(request.user) not in roles:
        raise PermissionDenied


def _templates():
    return Schedule.objects.filter(is_template=True).exclude(schedule_name=planning.TRANSFER_NAME).order_by('schedule_name', 'pk')


def _submission_key(raw):
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        raise planning.PlanningError('La référence de la demande est invalide. Actualisez la page.')


def _claim(key, user, *, is_template):
    """Claim inside the same transaction as the new schedule and stock step."""
    submission, created = ScheduleCreationRequest.objects.get_or_create(key=key, defaults={'user': user})
    if not created:
        if submission.user_id != user.pk or not submission.schedule_id or submission.schedule.is_template != is_template:
            raise planning.PlanningError('Cette demande a déjà été utilisée. Actualisez la page.', conflict=True)
        return submission, submission.schedule
    return submission, None


def _stock(schedule):
    # Intentionally retain the existing stock-generation behaviour for both
    # newly created templates and dated TRTs. Editing never regenerates stock.
    from .views import generate_schedule_items_for_stock_operations
    generate_schedule_items_for_stock_operations(schedule)


def _json_error(exc):
    return JsonResponse({'success': False, 'message': str(exc)}, status=409 if exc.conflict else 422)


def _editor_context(request, mode, snapshot=None):
    data = planning.catalog()
    initial = None
    if snapshot:
        initial = planning.draft_from_snapshot(snapshot, duplicate=mode == 'duplicate')
    data.update({'initial': initial, 'mode': mode, 'submission_key': str(uuid.uuid4())})
    return {
        'logged_data': getLoggedData(request), 'planner_data': data, 'mode': mode,
        'template_schedule': snapshot['schedule'] if snapshot else None,
        'aggregate_mismatch': planning.totals_mismatch(snapshot) if snapshot else False,
    }


@login_required
def template_editor(request, template_id=None, mode='create'):
    _require_role(request, (ADMIN,))
    if request.method not in ('GET', 'POST'):
        return HttpResponseNotAllowed(['GET', 'POST'])
    if mode not in ('create', 'edit', 'duplicate'):
        raise PermissionDenied
    schedule = get_object_or_404(_templates(), pk=template_id) if template_id is not None else None
    if mode != 'create' and schedule is None:
        raise PermissionDenied

    if request.method == 'GET':
        try:
            snapshot = planning.load_snapshot(schedule) if schedule else None
            return render(request, 'schedule/create_schedule.html', _editor_context(request, mode, snapshot))
        except planning.PlanningError as exc:
            return render(request, 'schedule/planning_error.html', {
                'logged_data': getLoggedData(request), 'planning_error': str(exc),
            }, status=422)

    try:
        payload = json.loads(request.POST.get('plan', ''))
    except (TypeError, ValueError):
        return _json_error(planning.PlanningError('Les données du modèle sont invalides.'))
    try:
        with transaction.atomic():
            if mode == 'edit':
                schedule = get_object_or_404(_templates().select_for_update(), pk=template_id)
                snapshot = planning.load_snapshot(schedule, lock=True)
                planning.lock_rules(snapshot)
                if not isinstance(payload, dict) or payload.get('version') != planning.version(snapshot):
                    raise planning.PlanningError('Ce modèle a changé pendant votre saisie. Actualisez la page avant de réessayer.', conflict=True)
                # Check for duplicate/missing legacy aggregates before any updates.
                planning.draft_from_snapshot(snapshot)
                plan = planning.validate_plan(payload, existing=snapshot, lock=True)
                planning.persist_template(schedule, plan, existing=snapshot)
                message = 'TRT type modifié. Les TRT datés existants restent inchangés.'
            else:
                if not isinstance(payload, dict):
                    raise planning.PlanningError('Les données du modèle sont invalides.')
                key = _submission_key(payload.get('submission_key'))
                submission, previous = _claim(key, request.user, is_template=True)
                if previous:
                    return JsonResponse({'success': True, 'url': reverse('schedule_details_with_id', args=[previous.pk]), 'replayed': True})
                plan = planning.validate_plan(payload, lock=True)
                schedule = Schedule.objects.create(schedule_name=plan['name'], start_time=plan['start'],
                                                   end_time=plan['end'], is_template=True)
                planning.persist_template(schedule, plan)
                _stock(schedule)
                submission.schedule = schedule
                submission.save(update_fields=['schedule'])
                message = 'TRT type créé.'
        messages.success(request, message)
        return JsonResponse({'success': True, 'url': reverse('schedule_details_with_id', args=[schedule.pk])})
    except planning.PlanningError as exc:
        return _json_error(exc)
    except IntegrityError:
        logger.exception('Unable to save TRT template because of a database constraint')
        return JsonResponse({'success': False, 'message': 'Enregistrement impossible : une donnée a changé ou une contrainte existe déjà. Actualisez et réessayez.'}, status=409)
    except Exception:
        logger.exception('Unable to save TRT template')
        return JsonResponse({'success': False, 'message': 'Enregistrement impossible. Aucune modification n’a été conservée ; vérifiez la configuration et réessayez.'}, status=500)


def _format_duration(value):
    sign = '-' if value < 0 else ''
    value = abs(value)
    seconds, fraction = divmod(value, 1_000_000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    suffix = ('.' + str(fraction).zfill(6).rstrip('0')) if fraction else ''
    return f'{sign}{hours:02d}:{minutes:02d}:{seconds:02d}{suffix}'


def _plan_page(request, schedule=None, *, error=None, status=200):
    context = {
        'logged_data': getLoggedData(request), 'templates_list': _templates(),
        'can_edit_templates': role_for(request.user) == ADMIN, 'selected_schedule': schedule,
        'planning_error': error, 'today': timezone.localdate().isoformat(),
        'submission_key': request.POST.get('submission_key') or str(uuid.uuid4()),
        'date_value': request.POST.get('schedule_date', '') if request.method == 'POST' else timezone.localdate().isoformat(),
        'start_value': request.POST.get('start_time', '') if request.method == 'POST' else (schedule.start_time.isoformat() if schedule else ''),
        'end_value': request.POST.get('end_time', '') if request.method == 'POST' else (schedule.end_time.isoformat() if schedule else ''),
        # A changed model must be reviewed and acknowledged again.
        'warnings_confirmed': request.POST.get('confirm_warnings') == 'yes' and status != 409,
    }
    if schedule:
        snapshot = planning.load_snapshot(schedule)
        context['version'] = planning.version(snapshot)
        context['item_count'] = len(snapshot['items'])
        try:
            catalog_data = planning.catalog()
            _, plan = planning.plan_for_snapshot(snapshot, catalog_data)
            context['warnings'] = plan['warnings']
            context['task_count'] = len(plan['tasks'])
            context['employee_count'] = len(plan['employees'])
            context['item_count'] = len(plan['items'])
            context['planned_time'] = _format_duration(sum(row['total_us'] for row in plan['tasks'].values()))
            context['assigned_time'] = _format_duration(sum(plan['task_assigned'].values()))
            # Use current catalog labels, not values supplied by the browser.
            task_names = {row['id']: row['name'] for row in catalog_data['tasks']}
            context['unassigned_tasks'] = [
                {'name': task_names.get(pk, str(pk)), 'duration': _format_duration(value)}
                for pk, value in plan['remaining'].items() if value > 0
            ]
            context['can_create_dated'] = True
        except planning.PlanningError as exc:
            context['data_error'] = str(exc)
        query = request.GET.get('q', '').strip()[:200]
        items = ScheduleItem.objects.filter(schedule=schedule).select_related('employee', 'task').order_by('task__priority', 'task__task_name', 'pk')
        if query:
            items = items.filter(Q(task__task_name__icontains=query) | Q(employee__first_name__icontains=query) |
                                 Q(employee__last_name__icontains=query) | Q(task_identifier__icontains=query))
        context['item_page'] = Paginator(items, 50).get_page(request.GET.get('page'))
        context['item_query'] = query
    return render(request, 'schedule/schedule_details2.html', context, status=status)


@login_required
def schedule_plan(request, schedule_id=None):
    _require_role(request, MANAGERS)
    if request.method == 'POST':
        # Keep old bookmark/form selection working without allowing an update.
        selected = request.POST.get('schedule', '')
        if not selected.isascii() or not selected.isdecimal():
            raise Http404
        schedule = get_object_or_404(_templates(), pk=selected)
        return redirect('schedule_details_with_id', schedule_id=schedule.pk)
    if request.method != 'GET':
        return HttpResponseNotAllowed(['GET', 'POST'])
    selected = schedule_id or request.GET.get('schedule')
    if selected and (not str(selected).isascii() or not str(selected).isdecimal()):
        raise Http404
    schedule = get_object_or_404(_templates(), pk=selected) if selected else None
    if schedule and schedule_id is None:
        # Canonical detail URLs keep the chosen model when a scoped search or
        # page link later submits its own query parameters.
        params = request.GET.copy()
        params.pop('schedule', None)
        target = reverse('schedule_details_with_id', args=[schedule.pk])
        return redirect(target + ('?' + params.urlencode() if params else ''))
    return _plan_page(request, schedule)


@login_required
def create_dated_schedule(request, template_id):
    _require_role(request, MANAGERS)
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    template = get_object_or_404(_templates(), pk=template_id)
    try:
        key = _submission_key(request.POST.get('submission_key'))
        try:
            date = parse_date(request.POST.get('schedule_date', ''))
        except (TypeError, ValueError):
            date = None
        if not date:
            raise planning.PlanningError('Choisissez une date valide pour le TRT.')
        start = planning._time(request.POST.get('start_time'), 'L’heure de début')
        end = planning._time(request.POST.get('end_time'), 'L’heure de fin')
        with transaction.atomic():
            submission, previous = _claim(key, request.user, is_template=False)
            if previous:
                return redirect('schedule_details_selected', schedule_id=previous.pk)
            template = get_object_or_404(_templates().select_for_update(), pk=template_id)
            snapshot = planning.load_snapshot(template, lock=True)
            planning.lock_rules(snapshot)
            if request.POST.get('version') != planning.version(snapshot):
                raise planning.PlanningError('Ce modèle a changé depuis son affichage. Vérifiez les nouveaux détails avant de créer le TRT.', conflict=True)
            _, plan = planning.plan_for_snapshot(snapshot, planning.catalog())
            if plan['warnings'] and request.POST.get('confirm_warnings') != 'yes':
                raise planning.PlanningError('Confirmez les avertissements avant de créer le TRT.')
            schedule = planning.copy_to_dated(snapshot, plan, date=date, start=start, end=end)
            _stock(schedule)
            schedule_service.update_all_schedule_statuses()
            submission.schedule = schedule
            submission.save(update_fields=['schedule'])
        messages.success(request, 'TRT daté créé à partir du modèle.')
        return redirect('schedule_details_selected', schedule_id=schedule.pk)
    except planning.PlanningError as exc:
        return _plan_page(request, template, error=str(exc), status=409 if exc.conflict else 422)
    except IntegrityError:
        return _plan_page(request, template, error='Un TRT existe déjà pour cette date et ces horaires, ou les données ont changé. Vérifiez avant de réessayer.', status=409)
    except Exception:
        logger.exception('Unable to create dated TRT')
        return _plan_page(request, template, error='Création impossible. Aucun TRT partiel n’a été conservé ; vérifiez la configuration et réessayez.', status=500)

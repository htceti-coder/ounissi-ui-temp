from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from . import kpi_service
from .kpi_registry import INDICATORS, settings_groups
from .models import KPIDisplaySetting
from .navigation import ADMIN, ROLES, role_for
from .utils import getLoggedData


@login_required
@require_http_methods(['GET', 'POST'])
def configure_kpis(request):
    if role_for(request.user) != ADMIN:
        raise PermissionDenied
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'reset':
            KPIDisplaySetting.objects.filter(metric_key__in=[item.key for item in INDICATORS],
                                             role__in=[role for role, _ in ROLES]).delete()
            messages.success(request, 'Les réglages par défaut des indicateurs ont été restaurés.')
        elif action == 'save':
            updates = []
            for role, _ in ROLES:
                for item in INDICATORS:
                    if role not in item.roles:
                        continue
                    prefix = f'{role}_{item.key}'
                    marker = request.POST.getlist(f'row_{prefix}')
                    if not marker:
                        continue  # A stale form cannot disable a newly added indicator.
                    if marker != ['1']:
                        return HttpResponseBadRequest('Formulaire de configuration invalide.')
                    style = request.POST.getlist(f'presentation_{prefix}')
                    position = request.POST.getlist(f'position_{prefix}')
                    if len(style) != 1 or style[0] not in item.presentations or len(position) != 1:
                        return HttpResponseBadRequest('Présentation ou ordre invalide.')
                    raw_position = position[0]
                    if not raw_position.isascii() or not raw_position.isdecimal() or len(raw_position) > 3:
                        return HttpResponseBadRequest('L’ordre doit être un entier entre 0 et 999.')
                    updates.append((role, item.key, {
                        'show_live': request.POST.get(f'live_{prefix}') == '1',
                        'show_dashboard': request.POST.get(f'dashboard_{prefix}') == '1',
                        'presentation': style[0], 'position': int(raw_position),
                    }))
            if not updates:
                return HttpResponseBadRequest('Formulaire de configuration invalide.')
            with transaction.atomic():
                for role, metric_key, values in updates:
                    KPIDisplaySetting.objects.update_or_create(role=role, metric_key=metric_key, defaults=values)
            messages.success(request, 'Les indicateurs ont été enregistrés.')
        else:
            return HttpResponseBadRequest('Action inconnue.')
        return redirect('configure_kpis')
    return render(request, 'settings/kpis.html', {
        'kpi_settings_groups': settings_groups(),
        'logged_data': getLoggedData(request),
    })


@login_required
@require_GET
@never_cache
def kpi_dashboard(request):
    try:
        period = kpi_service.parse_period(request.GET)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    return render(request, 'kpis/dashboard.html', {
        'kpi_snapshot': kpi_service.snapshot(request.user, 'dashboard', period),
        'logged_data': getLoggedData(request),
    })


@login_required
@require_GET
@never_cache
def kpi_snapshot(request):
    surface = request.GET.get('surface', 'live')
    try:
        period = kpi_service.parse_period(request.GET)
        data = kpi_service.snapshot(request.user, surface, period)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    # Cards are rendered/escaped server-side, so the polling client need not
    # construct HTML from employee names or task data.
    html = render_to_string('kpis/_cards.html', {'kpi_snapshot': data}, request=request)
    return JsonResponse({'html': html, 'card_count': len(data['cards']), 'read_at': data['read_at'],
                         'period': {'mode': period.mode, 'day': period.day_value, 'month': period.month_value,
                                    'label': period.label},
                         'schedule_id': data['schedule_id'], 'tasks': data['tasks']})

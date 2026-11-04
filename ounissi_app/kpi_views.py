from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from . import kpi_service
from .kpi_forms import CustomKPIForm
from .kpi_registry import INDICATORS, custom_metric_key, indicator_definitions, settings_groups
from .models import CustomKPI, KPIDisplaySetting
from .navigation import ADMIN, ROLES, role_for
from .utils import getLoggedData


def _render_kpi_settings(request, status=200):
    return render(request, 'settings/kpis.html', {
        'kpi_settings_groups': settings_groups(),
        'custom_kpis': CustomKPI.objects.select_related('updated_by'),
        'logged_data': getLoggedData(request),
    }, status=status)


@login_required
@require_http_methods(['GET', 'POST'])
def configure_kpis(request):
    if role_for(request.user) != ADMIN:
        raise PermissionDenied
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'reset':
            keys = [item.key for item in INDICATORS]
            for kpi_id in CustomKPI.objects.values_list('pk', flat=True):
                keys.extend((custom_metric_key(kpi_id, 'personal'), custom_metric_key(kpi_id, 'team')))
            KPIDisplaySetting.objects.filter(metric_key__in=keys,
                                             role__in=[role for role, _ in ROLES]).delete()
            messages.success(request, 'Les réglages par défaut des indicateurs ont été restaurés.')
        elif action == 'save':
            # Lock definitions while checking revisions and saving visibility.
            # Otherwise a stale matrix could republish a variant after another
            # administrator changed its scope and intentionally hid it.
            with transaction.atomic():
                updates = []
                definitions = indicator_definitions(for_update=True)
                for role, _ in ROLES:
                    for item in definitions:
                        if role not in item.roles:
                            continue
                        prefix = f'{role}_{item.key}'
                        marker = request.POST.getlist(f'row_{prefix}')
                        if not marker:
                            continue  # A stale form cannot disable a newly added indicator.
                        if marker != ['1']:
                            return HttpResponseBadRequest('Formulaire de configuration invalide.')
                        if item.custom and request.POST.getlist(f'revision_{prefix}') != [str(item.custom.revision)]:
                            messages.error(request, 'Un indicateur a été modifié depuis l’ouverture de cette page. Aucun réglage n’a été enregistré : vérifiez la matrice actualisée puis réessayez.')
                            return _render_kpi_settings(request, status=409)
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
                for role, metric_key, values in updates:
                    KPIDisplaySetting.objects.update_or_create(role=role, metric_key=metric_key, defaults=values)
            messages.success(request, 'Les indicateurs ont été enregistrés.')
        else:
            return HttpResponseBadRequest('Action inconnue.')
        return redirect('configure_kpis')
    return _render_kpi_settings(request)


@login_required
@require_http_methods(['GET', 'POST'])
def edit_custom_kpi(request, kpi_id=None):
    if role_for(request.user) != ADMIN:
        raise PermissionDenied
    current = get_object_or_404(CustomKPI, pk=kpi_id) if kpi_id is not None else None
    form = CustomKPIForm(request.POST if request.method == 'POST' else None, instance=current)
    status = 200
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            locked = CustomKPI.objects.select_for_update().filter(pk=kpi_id).first() if current else None
            if current and (not locked or locked.revision != form.cleaned_data['revision']):
                form.add_error(None, 'Cet indicateur a été modifié ailleurs. Rechargez la page avant de réessayer.')
                status = 409
            elif not current and form.cleaned_data['revision'] != 0:
                form.add_error(None, 'Version de formulaire invalide. Rechargez la page.')
                status = 409
            else:
                custom = form.save(commit=False)
                scope_changed = bool(locked and locked.scope != custom.scope)
                if locked:
                    custom.revision = locked.revision + 1
                else:
                    custom.created_by = request.user
                custom.updated_by = request.user
                custom.save()
                form.save_m2m()
                if scope_changed:
                    # A previously visible variant must not silently start
                    # displaying a different personal/team scope.
                    KPIDisplaySetting.objects.filter(metric_key__in=[custom_metric_key(custom.pk, 'personal'),
                                                                    custom_metric_key(custom.pk, 'team')]).delete()
                messages.success(request, 'Indicateur personnalisé enregistré. Choisissez sa visibilité par rôle et emplacement ci-dessous.')
                if scope_changed:
                    messages.info(request, 'Le périmètre a changé : ses anciens réglages de visibilité ont été désactivés.')
                return redirect('configure_kpis')
    return render(request, 'settings/kpi_form.html', {
        'form': form, 'custom_kpi': current, 'logged_data': getLoggedData(request),
    }, status=status)


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

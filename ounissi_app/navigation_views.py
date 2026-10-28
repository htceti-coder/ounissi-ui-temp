from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from .models import NavigationVisibility
from .navigation import ADMIN, ITEMS, ROLES, available_items, role_for, settings_sections
from .utils import getLoggedData


@login_required
@require_http_methods(['GET', 'POST'])
def configure_navigation(request):
    if role_for(request.user) != ADMIN:
        raise PermissionDenied

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'reset':
            NavigationVisibility.objects.filter(item_key__in=[item.key for item in ITEMS]).delete()
            messages.success(request, 'Les réglages par défaut du menu ont été restaurés.')
        elif action == 'save':
            # Update only rows rendered by this form. A newly added menu item
            # therefore keeps its default even if an old form is submitted.
            items = [item for item, _ in available_items() if request.POST.get(f'item_{item.key}') == '1']
            if not items:
                return HttpResponseBadRequest('Le formulaire de configuration est invalide.')
            with transaction.atomic():
                for item in items:
                    flags = {f'show_{role}': request.POST.get(f'show_{role}_{item.key}') == '1'
                             for role, _ in ROLES}
                    NavigationVisibility.objects.update_or_create(item_key=item.key, defaults=flags)
            messages.success(request, 'La visibilité du menu a été enregistrée.')
        else:
            return HttpResponseBadRequest('Action inconnue.')
        return redirect('configure_navigation')

    return render(request, 'settings/navigation.html', {
        'navigation_settings': settings_sections(),
        'logged_data': getLoggedData(request),
    })

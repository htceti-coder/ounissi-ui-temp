"""The curated sidebar and its per-role visibility defaults.

Visibility here only controls navigation, not authorization to the target views.
Keys are stable database identifiers; new links automatically use their defaults.
"""
from dataclasses import dataclass

from django.urls import NoReverseMatch, reverse

from accounts.models import Team
from .models import NavigationVisibility

USER = 'user'
LEADER = 'leader'
ADMIN = 'admin'
ROLES = ((USER, 'Utilisateurs'), (LEADER, 'Responsables d’équipe'), (ADMIN, 'Administrateurs'))
ALL = (USER, LEADER, ADMIN)
MANAGERS = (LEADER, ADMIN)
ADMINS = (ADMIN,)


@dataclass(frozen=True)
class NavigationItem:
    key: str
    section: str
    label: str
    url_name: str
    icon: str
    defaults: tuple
    external: bool = False
    staff_only: bool = False


SECTIONS = (
    ('personal', 'Mon espace'),
    ('operations', 'Exploitation et TRT'),
    ('reception', 'Réception'),
    ('stock', 'Stocks et emplacements'),
    ('people', 'RH et paie'),
    ('configuration', 'Configuration'),
    ('api', 'API et développeurs'),
)

ITEMS = (
    NavigationItem('home', 'personal', 'TRT actif', 'home', 'fa-home', ALL),
    NavigationItem('history', 'personal', 'Mon historique TRT', 'user_schedule_history', 'fa-history', ALL),
    NavigationItem('my_timesheet', 'personal', 'Mon pointage', 'my_timesheet', 'fa-clock-o', ALL),
    NavigationItem('request_leave', 'personal', 'Demander un congé', 'request_leave', 'fa-calendar-o', ALL),
    NavigationItem('active_trt', 'operations', 'Suivi TRT actif', 'actual_schedule_details', 'fa-sliders', MANAGERS),
    NavigationItem('manage_trt', 'operations', 'Gestion des TRT', 'schedule_details', 'fa-calendar-check-o', MANAGERS),
    NavigationItem('plan_trt', 'operations', 'Planification TRT', 'schedule_details2', 'fa-calendar', MANAGERS),
    NavigationItem('tasks', 'operations', 'Tableau des tâches', 'task_list', 'fa-tasks', MANAGERS),
    NavigationItem('create_task', 'operations', 'Créer une tâche', 'create_task', 'fa-plus-square-o', MANAGERS),
    NavigationItem('employees', 'operations', 'Liste du personnel', 'employee_list', 'fa-id-card-o', MANAGERS),
    NavigationItem('reception_v3', 'reception', 'Suivi réception (expérimental)', 'reception_dashboard_v3', 'fa-truck', MANAGERS),
    NavigationItem('stock', 'stock', 'État des stocks', 'etat_stocks', 'fa-cubes', MANAGERS),
    NavigationItem('locations', 'stock', 'Emplacements', 'emplacements', 'fa-map-o', MANAGERS),
    NavigationItem('assign_stock', 'stock', 'Assigner le stock', 'assign_stock', 'fa-exchange', MANAGERS),
    NavigationItem('attendance_import', 'people', 'Import pointage ZKTeco', 'attendance_logs', 'fa-download', ADMINS),
    NavigationItem('timesheets', 'people', 'Feuilles de pointage', 'timesheet_list', 'fa-clock-o', MANAGERS),
    NavigationItem('manage_leaves', 'people', 'Gestion des congés', 'manage_leaves', 'fa-calendar-check-o', MANAGERS),
    NavigationItem('salaries', 'people', 'Calcul salaires', 'salary_list', 'fa-money', ADMINS),
    NavigationItem('salary_items', 'people', 'Rubriques de paie', 'salary_item_list', 'fa-list-alt', ADMINS),
    NavigationItem('create_trt', 'configuration', 'Créer un TRT type', 'create_schedule', 'fa-object-group', ADMINS),
    NavigationItem('queries', 'configuration', 'Tester les requêtes', 'test_queries', 'fa-terminal', ADMINS),
    NavigationItem('create_query', 'configuration', 'Créer une requête', 'create_query', 'fa-database', ADMINS),
    NavigationItem('django_admin', 'configuration', 'Administration Django', 'admin:index', 'fa-cog', ADMINS, staff_only=True),
    NavigationItem('api_docs', 'api', 'Documentation API', 'api-1.0.0:openapi-view', 'fa-book', ADMINS, external=True),
    NavigationItem('api_schema', 'api', 'Schéma OpenAPI', 'api-1.0.0:openapi-json', 'fa-code', ADMINS, external=True),
)


def role_for(user):
    """Highest level wins; Admin membership is checked across all groups."""
    if not user.is_authenticated:
        return None
    if user.is_superuser or user.groups.filter(name='Admin').exists():
        return ADMIN
    if Team.objects.filter(leader__user=user).exists():
        return LEADER
    return USER


def visibility_settings():
    return {setting.item_key: setting for setting in NavigationVisibility.objects.all()}


def visible_for(item, role, settings):
    setting = settings.get(item.key)
    return getattr(setting, f'show_{role}') if setting is not None else role in item.defaults


def available_items():
    """Skip optional URLs which are not installed rather than creating dead links."""
    available = []
    for item in ITEMS:
        try:
            url = reverse(item.url_name)
        except NoReverseMatch:
            continue
        available.append((item, url))
    return available


def menu_sections(user):
    role = role_for(user)
    if role is None:
        return [], False
    settings = visibility_settings()
    grouped = {key: [] for key, _ in SECTIONS}
    for item, url in available_items():
        if item.staff_only and not user.is_staff:
            continue
        if visible_for(item, role, settings):
            grouped[item.section].append({'key': item.key, 'label': item.label, 'url': url,
                                          'icon': item.icon, 'external': item.external})
    sections = [{'key': key, 'title': title, 'items': grouped[key]}
                for key, title in SECTIONS if grouped[key]]
    return sections, role == ADMIN


def settings_sections():
    """Rows for the admin's visibility matrix, including defaults for new links."""
    settings = visibility_settings()
    grouped = {key: [] for key, _ in SECTIONS}
    for item, _ in available_items():
        cells = [{'name': f'show_{role}_{item.key}', 'role_label': label,
                  'checked': visible_for(item, role, settings)} for role, label in ROLES]
        grouped[item.section].append({'key': item.key, 'label': item.label,
                                      'cells': cells, 'staff_only': item.staff_only})
    return [{'key': key, 'title': title, 'items': grouped[key]}
            for key, title in SECTIONS if grouped[key]]

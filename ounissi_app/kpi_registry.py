"""Indicator definitions and persisted presentation settings.

Settings select presentation only.  They never broaden the server-side personal
or team data scope enforced by kpi_service.
"""
from dataclasses import dataclass

from .models import CustomKPI, KPIDisplaySetting
from .navigation import ADMIN, LEADER, ROLES, USER

ALL = (USER, LEADER, ADMIN)
TEAM_ROLES = (LEADER, ADMIN)
PRESENTATIONS = {'value': 'Carte', 'bar': 'Carte avec barre de progression', 'details': 'Carte avec détails'}


@dataclass(frozen=True)
class Indicator:
    key: str
    label: str
    description: str
    icon: str
    roles: tuple
    live_defaults: tuple
    dashboard_defaults: tuple
    presentations: tuple
    default_presentation: str
    position: int
    group: str
    custom: object = None


INDICATORS = (
    Indicator('my_progress', 'Progression de mes tâches', 'Points réalisés / points affectés pour tous les TRT de la période.', 'fa-line-chart', ALL, ALL, ALL, ('value', 'bar'), 'bar', 10, 'trt'),
    Indicator('my_score', 'Mon score réalisé', 'Secondes réalisées divisées par la priorité de chaque tâche de la période.', 'fa-star-o', ALL, (), ALL, ('value',), 'value', 20, 'trt'),
    Indicator('my_tasks', 'Mes tâches terminées', 'Nombre de tâches affectées terminées pour tous les TRT de la période.', 'fa-check-square-o', ALL, ALL, ALL, ('value',), 'value', 30, 'trt'),
    Indicator('my_assigned', 'Mon temps affecté', 'Somme des durées affectées pour tous les TRT de la période.', 'fa-hourglass-half', ALL, (), ALL, ('value',), 'value', 40, 'trt'),
    Indicator('attendance_days', 'Jours travaillés confirmés', 'Jours de la période avec au moins une entrée et un pointage confirmé, hors congés.', 'fa-calendar-check-o', ALL, (), ALL, ('value',), 'value', 50, 'attendance'),
    Indicator('attendance_hours', 'Heures confirmées', 'Somme des plages complètes et non ambiguës des pointages confirmés.', 'fa-clock-o', ALL, (), ALL, ('value',), 'value', 60, 'attendance'),
    Indicator('attendance_pending', 'Enregistrements en attente', 'Pointages et congés non confirmés, séparés des totaux confirmés.', 'fa-hourglass-o', ALL, (), ALL, ('value',), 'value', 70, 'attendance'),
    Indicator('attendance_records', 'Détail de mon pointage', 'Enregistrements importés ou saisis pour la période sélectionnée.', 'fa-list-alt', ALL, (), ALL, ('value', 'details'), 'details', 80, 'attendance'),
    Indicator('team_progress', 'Progression de mon équipe', 'Points réalisés / points affectés par les membres de mon équipe sur la période.', 'fa-line-chart', TEAM_ROLES, TEAM_ROLES, TEAM_ROLES, ('value', 'bar'), 'bar', 90, 'team'),
    Indicator('team_score', 'Score de mon équipe', 'Somme des secondes réalisées / priorité des membres sur la période.', 'fa-star-o', TEAM_ROLES, (), TEAM_ROLES, ('value',), 'value', 100, 'team'),
    Indicator('team_tasks', 'Tâches terminées par mon équipe', 'Nombre de tâches affectées terminées pour tous les TRT de la période.', 'fa-check-square-o', TEAM_ROLES, TEAM_ROLES, TEAM_ROLES, ('value',), 'value', 110, 'team'),
    Indicator('team_presence', 'Pointages ouverts de mon équipe', 'Entrées enregistrées sans sortie correspondante sur la période ; pas une présence en direct.', 'fa-users', TEAM_ROLES, TEAM_ROLES, TEAM_ROLES, ('value',), 'value', 120, 'team'),
    Indicator('team_records', 'Pointages de mon équipe', 'État du jour sélectionné ou synthèse mensuelle des enregistrements par membre.', 'fa-address-book-o', TEAM_ROLES, (), TEAM_ROLES, ('value', 'details'), 'details', 130, 'team'),
)


PERCENT_CALCULATIONS = frozenset((CustomKPI.Calculation.WEIGHTED_PROGRESS,
                                  CustomKPI.Calculation.COMPLETION_RATE,
                                  CustomKPI.Calculation.AVERAGE_PROGRESS))


def custom_metric_key(kpi_id, scope):
    return f'custom_{kpi_id}_{scope}'


def indicator_definitions(*, for_update=False):
    """Predefined and active custom KPIs; every custom variant is hidden by default.

    Writers may lock definitions inside their transaction to validate a
    visibility form against the same revisions they are about to update.
    """
    result = list(INDICATORS)
    custom_kpis = CustomKPI.objects.filter(is_active=True).prefetch_related('tasks', 'queries')
    if for_update:
        custom_kpis = custom_kpis.select_for_update()
    for custom in custom_kpis:
        scopes = ('personal', 'team') if custom.scope == CustomKPI.Scope.BOTH else (custom.scope,)
        for scope in scopes:
            # A corrupt definition cannot manufacture an unrecognised scope or
            # expose a team KPI to an ordinary user.
            if scope not in ('personal', 'team'):
                continue
            team = scope == 'team'
            presentation = ('value', 'bar') if custom.target or custom.calculation in PERCENT_CALCULATIONS else ('value',)
            prefix = 'Équipe' if team else 'Personnel'
            description = f"{prefix} · {custom.get_calculation_display()}"
            if custom.description:
                description += f' · {custom.description}'
            result.append(Indicator(
                custom_metric_key(custom.pk, scope), f'{custom.name} · {prefix.lower()}', description,
                'fa-bar-chart', TEAM_ROLES if team else ALL, (), (), presentation, 'value', 500,
                'team' if team else 'trt', custom,
            ))
    return result


def settings_for(role, definitions=None):
    """Return valid effective settings in stable display order for one role."""
    stored = {s.metric_key: s for s in KPIDisplaySetting.objects.filter(role=role)}
    result = []
    for item in definitions if definitions is not None else indicator_definitions():
        if role not in item.roles:
            continue
        setting = stored.get(item.key)
        result.append({
            'definition': item,
            'show_live': setting.show_live if setting else role in item.live_defaults,
            'show_dashboard': setting.show_dashboard if setting else role in item.dashboard_defaults,
            'presentation': (setting.presentation if setting and setting.presentation in item.presentations
                             else item.default_presentation),
            'position': setting.position if setting else item.position,
        })
    return sorted(result, key=lambda row: (row['position'], row['definition'].position))


def settings_groups():
    groups = []
    definitions = indicator_definitions()
    for role, label in ROLES:
        rows = []
        for setting in settings_for(role, definitions):
            item = setting['definition']
            prefix = f'{role}_{item.key}'
            rows.append({**setting, 'prefix': prefix,
                         'choices': [(value, PRESENTATIONS[value]) for value in item.presentations]})
        groups.append({'role': role, 'label': label, 'rows': rows})
    return groups

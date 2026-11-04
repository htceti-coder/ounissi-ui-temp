from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import Employee, Rank, Team
from .kpi_registry import custom_metric_key
from .kpi_service import parse_period, snapshot
from .models import CustomKPI, KPIDisplaySetting, Query, Schedule, ScheduleItem, Task


class CustomKPITests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clock = patch('django.utils.timezone.now', return_value=timezone.make_aware(datetime(2026, 10, 28, 12)))
        cls.clock.start()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        try:
            super().tearDownClass()
        finally:
            cls.clock.stop()

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user('builder-admin', password='test')
        cls.admin.groups.add(Group.objects.get_or_create(name='Admin')[0])
        cls.superuser = User.objects.create_superuser('builder-superuser', password='test')
        cls.worker_user = User.objects.create_user('builder-worker', password='test')
        cls.leader_user = User.objects.create_user('builder-leader', password='test')
        cls.outside_user = User.objects.create_user('builder-outside', password='test')
        cls.rank = Rank.objects.create(value=6, label='Custom KPI')
        cls.worker = Employee.objects.create(user=cls.worker_user, first_name='Membre', rank=cls.rank)
        cls.leader = Employee.objects.create(user=cls.leader_user, first_name='Leader', rank=cls.rank)
        cls.outside = Employee.objects.create(user=cls.outside_user, first_name='Autre', rank=cls.rank)
        cls.team = Team.objects.create(name='Équipe KPI', leader=cls.leader)
        cls.team.employees.add(cls.worker)
        cls.today = Schedule.objects.create(date=date(2026, 10, 28), start_time=time(0), end_time=time(23, 59))
        cls.yesterday = Schedule.objects.create(date=date(2026, 10, 27), start_time=time(0), end_time=time(23, 59))
        cls.query = Query.objects.create(name='Saisie enregistrée', type=1, query='SELECT 1', parameters={})
        cls.other_query = Query.objects.create(name='Autre requête', type=1, query='SELECT 1', parameters={})
        cls.manual = Task.objects.create(task_name='Réception', responsibility_level=cls.rank,
                                        priority=2, average_duration=timedelta(seconds=1))
        cls.auto = Task.objects.create(task_name='Saisie', responsibility_level=cls.rank,
                                      priority=1, average_duration=timedelta(seconds=45), progress_query=cls.query)
        cls.other = Task.objects.create(task_name='Autre tâche', responsibility_level=cls.rank,
                                       priority=4, average_duration=timedelta(seconds=1), progress_query=cls.other_query)
        cls.worker_manual = ScheduleItem.objects.create(schedule=cls.today, employee=cls.worker, task=cls.manual,
                                                        assigned_time=timedelta(seconds=100), progression_rate=100)
        cls.worker_auto = ScheduleItem.objects.create(schedule=cls.today, employee=cls.worker, task=cls.auto,
                                                      assigned_time=timedelta(seconds=495), progress_lines=11)
        cls.leader_manual = ScheduleItem.objects.create(schedule=cls.today, employee=cls.leader, task=cls.manual,
                                                       assigned_time=timedelta(seconds=100), progression_rate=50)
        cls.outside_manual = ScheduleItem.objects.create(schedule=cls.today, employee=cls.outside, task=cls.manual,
                                                        assigned_time=timedelta(seconds=10000), progression_rate=100)
        cls.past_item = ScheduleItem.objects.create(schedule=cls.yesterday, employee=cls.worker, task=cls.manual,
                                                    assigned_time=timedelta(seconds=100), progression_rate=0)
        ScheduleItem.objects.create(schedule=cls.today, task=cls.manual, assigned_time=timedelta(seconds=100), progression_rate=100)
        template = Schedule.objects.create(date=date(2026, 10, 28), start_time=time(2), end_time=time(3), is_template=True)
        ScheduleItem.objects.create(schedule=template, employee=cls.worker, task=cls.manual,
                                    assigned_time=timedelta(seconds=10000), progression_rate=0)
        cls.create_url = reverse('create_custom_kpi')
        cls.settings_url = reverse('configure_kpis')

    def setUp(self):
        self.client.force_login(self.admin)

    def form_data(self, **updates):
        data = {'name': 'Performance réception', 'description': 'Données de réception', 'scope': 'both',
                'calculation': 'weighted_progress', 'selection': 'selected', 'tasks': [str(self.manual.pk), str(self.auto.pk)],
                'queries': [str(self.query.pk)], 'priority': '', 'kind': 'all', 'state': 'all',
                'target': '', 'is_active': 'on', 'revision': '0'}
        data.update(updates)
        return data

    def create(self, **updates):
        response = self.client.post(self.create_url, self.form_data(**updates))
        self.assertRedirects(response, self.settings_url, fetch_redirect_response=False)
        return CustomKPI.objects.latest('pk')

    @staticmethod
    def show(kpi, role, scope='personal', live=False, dashboard=True, style='value', position=1):
        return KPIDisplaySetting.objects.create(role=role, metric_key=custom_metric_key(kpi.pk, scope),
            show_live=live, show_dashboard=dashboard, presentation=style, position=position)

    @staticmethod
    def cards(user, surface='dashboard', period=None):
        return {card['key']: card for card in snapshot(user, surface, period)['cards']}

    def test_only_admin_and_superuser_can_create_edit_and_choose_visibility(self):
        kpi = self.create()
        edit_url = reverse('edit_custom_kpi', args=[kpi.pk])
        for user in (self.worker_user, self.leader_user):
            self.client.force_login(user)
            for url in (self.create_url, edit_url, self.settings_url):
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(self.client.post(url, self.form_data()).status_code, 403)
            self.assertNotContains(self.client.get(reverse('kpi_dashboard')), 'Créer un indicateur')
            self.assertNotContains(self.client.get(reverse('home')), 'Configurer les indicateurs')
        self.client.logout()
        self.assertEqual(self.client.get(self.create_url).status_code, 302)
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(self.create_url).status_code, 200)
        self.assertEqual(self.client.get(edit_url).status_code, 200)
        self.assertEqual(self.client.put(edit_url).status_code, 405)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.admin)
        self.assertEqual(csrf.post(self.create_url, self.form_data()).status_code, 403)
        csrf.get(self.create_url)
        self.assertEqual(csrf.post(self.create_url, self.form_data(name='CSRF valid'),
                                   HTTP_X_CSRFTOKEN=csrf.cookies['csrftoken'].value).status_code, 302)

    def test_create_defaults_hidden_then_admin_selects_role_surface_and_style(self):
        kpi = self.create()
        self.assertEqual(kpi.created_by, self.admin)
        self.assertEqual(kpi.updated_by, self.admin)
        self.assertEqual(kpi.revision, 1)
        self.assertEqual(set(kpi.tasks.values_list('pk', flat=True)), {self.manual.pk, self.auto.pk})
        self.assertEqual(set(kpi.queries.values_list('pk', flat=True)), {self.query.pk})
        personal, team = custom_metric_key(kpi.pk, 'personal'), custom_metric_key(kpi.pk, 'team')
        page = self.client.get(self.settings_url)
        self.assertContains(page, 'Performance réception')
        self.assertContains(page, f'row_user_{personal}')
        self.assertContains(page, f'row_leader_{team}')
        self.assertNotContains(page, f'row_user_{team}')
        self.client.force_login(self.worker_user)
        self.assertNotIn(personal, self.cards(self.worker_user))
        self.client.force_login(self.admin)
        prefix = f'user_{personal}'
        data = {'action': 'save', f'row_{prefix}': '1', f'revision_{prefix}': str(kpi.revision),
                f'live_{prefix}': '1', f'dashboard_{prefix}': '1',
                f'presentation_{prefix}': 'bar', f'position_{prefix}': '4'}
        self.assertRedirects(self.client.post(self.settings_url, data), self.settings_url, fetch_redirect_response=False)
        self.client.force_login(self.worker_user)
        for url in (reverse('home'), reverse('kpi_dashboard')):
            response = self.client.get(url)
            self.assertContains(response, f'data-kpi-key="{personal}"')
            self.assertContains(response, '545 / 545 points')
        card = self.cards(self.worker_user)[personal]
        self.assertEqual(card['value'], '100')
        self.assertEqual(card['bar_value'], 100)

    def test_combines_tasks_and_queries_once_with_personal_and_team_authorisation(self):
        kpi = self.create()
        personal, team = custom_metric_key(kpi.pk, 'personal'), custom_metric_key(kpi.pk, 'team')
        self.show(kpi, 'user', style='bar')
        self.show(kpi, 'leader', style='bar')
        self.show(kpi, 'leader', 'team', style='bar')
        self.show(kpi, 'admin', 'team')
        # Forged user/team setting cannot create team data access.
        self.show(kpi, 'user', 'team')
        worker = self.cards(self.worker_user)
        self.assertEqual(worker[personal]['detail'].split(' · ')[0], '545 / 545 points')
        self.assertNotIn(team, worker)
        leader = self.cards(self.leader_user)
        self.assertEqual(leader[personal]['value'], '50')
        self.assertEqual(leader[team]['detail'].split(' · ')[0], '570 / 595 points')
        self.assertEqual(leader[team]['value'], '95,8')
        self.assertEqual(self.cards(self.admin)[team]['value'], '—')  # Admin has no led team.
        self.assertIn('Aucune équipe dirigée', self.cards(self.admin)[team]['detail'])
        data = snapshot(self.leader_user, 'dashboard')
        self.assertEqual([group['key'] for group in data['groups']], ['personal', 'team'])

    def test_query_only_means_saved_automatic_task_lines_and_never_runs_sql(self):
        kpi = self.create(tasks=[], queries=[str(self.query.pk)], calculation='saved_lines', kind='automatic')
        key = custom_metric_key(kpi.pk, 'personal')
        self.show(kpi, 'user', live=True)
        self.client.force_login(self.worker_user)
        with patch('ounissi_app.schedule_service.run_raw_query') as xpert, \
             patch('ounissi_app.views.import_attendance_and_users') as terminal, \
             CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse('kpi_snapshot'), {'surface': 'live', 'period': 'day', 'day': '2026-10-28'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('>11<', response.json()['html'])
        self.assertEqual(self.cards(self.worker_user)[key]['value'], '11')
        xpert.assert_not_called(); terminal.assert_not_called()
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries.captured_queries))
        self.worker_auto.progress_lines = 7
        self.worker_auto.save()
        self.assertEqual(self.cards(self.worker_user)[key]['value'], '7')

    def test_rules_and_calculations_use_saved_data_and_selected_period(self):
        kpi = self.create(selection='all', tasks=[], queries=[], calculation='assigned_tasks', priority='2', state='complete')
        key = custom_metric_key(kpi.pk, 'personal')
        self.show(kpi, 'user', live=True)
        self.assertEqual(self.cards(self.worker_user)[key]['value'], '1')
        kpi.state = CustomKPI.State.INCOMPLETE
        kpi.save()
        self.assertEqual(self.cards(self.worker_user)[key]['value'], '—')
        month = parse_period({'period': 'month', 'month': '2026-10'})
        self.assertEqual(self.cards(self.worker_user, period=month)[key]['value'], '1')
        past = parse_period({'period': 'day', 'day': '2026-10-27'})
        data = snapshot(self.worker_user, 'live', past)
        self.assertEqual({t['id'] for t in data['tasks']}, {self.worker_manual.pk, self.worker_auto.pk})
        self.assertEqual(next(c for c in data['cards'] if c['key'] == key)['value'], '1')

    def test_average_progress_completion_ratio_and_target_bar(self):
        self.worker_manual.progression_rate = 0
        self.worker_manual.save()
        kpi = self.create(calculation='average_progress')
        key = custom_metric_key(kpi.pk, 'personal')
        setting = self.show(kpi, 'user', style='bar')
        self.assertEqual(self.cards(self.worker_user)[key]['value'], '50')
        self.assertIn('non pondérée', self.cards(self.worker_user)[key]['detail'])
        kpi.calculation = CustomKPI.Calculation.COMPLETION_RATE
        kpi.save()
        self.assertEqual(self.cards(self.worker_user)[key]['value'], '50')
        kpi.calculation = CustomKPI.Calculation.WEIGHTED_PROGRESS
        kpi.save()
        self.assertEqual(self.cards(self.worker_user)[key]['value'], '90,83')
        kpi.calculation = CustomKPI.Calculation.WEIGHTED_SCORE
        kpi.target = Decimal('400')
        kpi.save()
        card = self.cards(self.worker_user)[key]
        self.assertEqual(card['value'], '495')
        self.assertEqual(card['bar_value'], 100)
        self.assertIn('495 points sur un objectif de 400 points', card['bar_text'])
        self.assertIn('limitée', card['warning'])
        self.client.force_login(self.worker_user)
        html = self.client.get(reverse('kpi_snapshot'), {'surface': 'dashboard'}).json()['html']
        self.assertIn('aria-valuetext="495 points sur un objectif de 400 points"', html)
        self.assertIn('>495 points sur un objectif de 400 points</progress>', html)
        # Without a target, a forged/stale bar setting falls back safely to a card.
        kpi.target = None
        kpi.save()
        self.assertEqual(self.cards(self.worker_user)[key]['presentation'], 'value')
        setting.refresh_from_db()
        self.assertEqual(setting.presentation, 'bar')

    def test_custom_names_are_escaped_in_settings_and_visible_cards(self):
        name = 'KPI <script>alert(1)</script> & résultat'
        kpi = self.create(name=name)
        self.show(kpi, 'user', live=True)
        settings_page = self.client.get(self.settings_url)
        self.assertContains(settings_page, '&lt;script&gt;alert(1)&lt;/script&gt; &amp; résultat')
        self.assertNotContains(settings_page, '<script>alert(1)</script>')
        self.client.force_login(self.worker_user)
        html = self.client.get(reverse('kpi_snapshot'), {'surface': 'live'}).json()['html']
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt; &amp; résultat', html)
        self.assertNotIn('<script>alert(1)</script>', html)

    def test_assigned_time_completed_tasks_and_combined_filters(self):
        kpi = self.create(selection='all', tasks=[], queries=[], calculation='assigned_time',
                          priority='2', kind='manual', target='200')
        key = custom_metric_key(kpi.pk, 'personal')
        self.show(kpi, 'user', style='bar')
        card = self.cards(self.worker_user)[key]
        self.assertEqual(card['value'], '1 min 40 s')
        self.assertEqual(card['bar_value'], 50)
        self.assertIn('objectif de 3 min 20 s', card['bar_text'])
        self.assertIn('1 tâche(s)', card['detail'])

        kpi.calculation = CustomKPI.Calculation.COMPLETED_TASKS
        kpi.target = None
        kpi.priority = None
        kpi.kind = CustomKPI.Kind.AUTOMATIC
        kpi.save()
        self.assertEqual(self.cards(self.worker_user)[key]['value'], '1')
        self.worker_auto.progress_lines = 10
        self.worker_auto.save()
        self.assertEqual(self.cards(self.worker_user)[key]['value'], '0')
        kpi.state = CustomKPI.State.INCOMPLETE
        kpi.save()
        self.assertEqual(self.cards(self.worker_user)[key]['value'], '0')
        kpi.priority = 2  # No automatic priority-2 task: filters are ANDed.
        kpi.save()
        self.assertEqual(self.cards(self.worker_user)[key]['value'], '—')
        kpi.priority = None
        kpi.calculation = CustomKPI.Calculation.ASSIGNED_TASKS
        kpi.save()
        self.assertEqual(self.cards(self.worker_user)[key]['value'], '1')

    def test_query_sources_cannot_select_manual_placeholder_queries(self):
        placeholder = Query.objects.create(name='Pas de requête', query='N/A', parameters={})
        manual = Task.objects.create(task_name='Manuelle historique', responsibility_level=self.rank,
                                     progress_query=placeholder, priority=2)
        ScheduleItem.objects.create(schedule=self.today, employee=self.worker, task=manual,
                                    assigned_time=timedelta(seconds=100), progression_rate=100)
        form = self.client.get(self.create_url).context['form']
        self.assertNotIn(placeholder, list(form.fields['queries'].queryset))
        response = self.client.post(self.create_url, self.form_data(tasks=[], queries=[str(placeholder.pk)]))
        self.assertEqual(response.status_code, 200)
        self.assertIn('queries', response.context['form'].errors)
        self.assertFalse(CustomKPI.objects.exists())

        kpi = self.create(tasks=[], queries=[str(self.query.pk)], calculation='assigned_tasks')
        key = custom_metric_key(kpi.pk, 'personal')
        self.show(kpi, 'user')
        self.assertEqual(self.cards(self.worker_user)[key]['value'], '1')
        # A formerly valid source can later be changed to N/A. Its manual
        # tasks must not be matched through the saved query selector.
        self.query.query = 'N/A'
        self.query.save()
        self.assertEqual(self.cards(self.worker_user)[key]['value'], '—')
        # Explicitly selected manual tasks remain supported.
        kpi.tasks.set([manual])
        self.assertEqual(self.cards(self.worker_user)[key]['value'], '1')

    def test_invalid_progress_and_disappearing_source_do_not_give_false_or_broader_results(self):
        kpi = self.create(selection='all', tasks=[], queries=[], calculation='completed_tasks')
        key = custom_metric_key(kpi.pk, 'personal')
        self.show(kpi, 'user')
        self.worker_auto.progress_lines = None
        self.worker_auto.save()
        result = self.cards(self.worker_user)[key]
        self.assertEqual(result['value'], '—')
        self.assertIn('invalides', result['detail'])
        kpi.calculation = CustomKPI.Calculation.ASSIGNED_TASKS
        kpi.state = CustomKPI.State.COMPLETE
        kpi.save()
        result = self.cards(self.worker_user)[key]
        self.assertEqual(result['value'], '—')
        self.assertIn('état ne peut pas être déterminé', result['detail'])
        # A selected task removed from the database must not become ALL tasks.
        unused = Task.objects.create(task_name='Deleted source', responsibility_level=self.rank)
        kpi.selection = CustomKPI.Selection.SELECTED; kpi.state = CustomKPI.State.ALL; kpi.save()
        kpi.tasks.set([unused]); kpi.queries.clear(); unused.delete()
        result = self.cards(self.worker_user)[key]
        self.assertEqual(result['value'], '—')
        self.assertIn('Aucune tâche ou requête liée', result['detail'])

    def test_invalid_forms_are_rejected_without_partial_writes(self):
        invalid = (
            {'selection': 'selected', 'tasks': [], 'queries': []},
            {'calculation': 'unknown'}, {'scope': 'organisation'}, {'priority': '9'},
            {'tasks': ['999999']}, {'queries': ['999999']}, {'target': '0'},
            {'target': '-1'}, {'calculation': 'saved_lines', 'kind': 'manual'},
        )
        for changes in invalid:
            with self.subTest(changes=changes):
                response = self.client.post(self.create_url, self.form_data(**changes))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['form'].errors)
                self.assertFalse(CustomKPI.objects.exists())
                if changes.get('selection') == 'selected':
                    self.assertContains(response, 'aria-describedby="id_tasks_helptext id_tasks_error"')
                    self.assertContains(response, 'id="id_tasks_error"')

    def test_visibility_matrix_can_save_more_than_default_django_field_limit(self):
        CustomKPI.objects.bulk_create([
            CustomKPI(name=f'Indicateur {index:02}', scope=CustomKPI.Scope.BOTH,
                      selection=CustomKPI.Selection.ALL)
            for index in range(60)
        ])
        page = self.client.get(self.settings_url)
        data = {'action': 'save'}
        for group in page.context['kpi_settings_groups']:
            for row in group['rows']:
                prefix = row['prefix']
                data[f'row_{prefix}'] = '1'
                data[f'presentation_{prefix}'] = row['presentation']
                data[f'position_{prefix}'] = str(row['position'])
                if row['definition'].custom:
                    data[f'revision_{prefix}'] = str(row['definition'].custom.revision)
                if row['show_live']:
                    data[f'live_{prefix}'] = '1'
                if row['show_dashboard']:
                    data[f'dashboard_{prefix}'] = '1'
        self.assertGreater(len(data), 1000)  # Django's normal field limit.
        self.assertRedirects(self.client.post(self.settings_url, data), self.settings_url,
                             fetch_redirect_response=False)
        self.assertEqual(KPIDisplaySetting.objects.filter(metric_key__startswith='custom_').count(), 300)

    def test_stale_visibility_matrix_does_not_republish_after_scope_change(self):
        kpi = self.create()
        team = custom_metric_key(kpi.pk, 'team')
        prefix = f'leader_{team}'
        page = self.client.get(self.settings_url)
        self.assertContains(page, f'name="revision_{prefix}" value="1"')
        self.show(kpi, 'leader', 'team', live=True)
        old_matrix = {'action': 'save', f'row_{prefix}': '1', f'revision_{prefix}': '1',
                      f'live_{prefix}': '1', f'dashboard_{prefix}': '1',
                      f'presentation_{prefix}': 'bar', f'position_{prefix}': '5',
                      # Even earlier valid rows must not be partially saved.
                      'row_user_my_score': '1', 'live_user_my_score': '1',
                      'presentation_user_my_score': 'value', 'position_user_my_score': '6'}
        edit = reverse('edit_custom_kpi', args=[kpi.pk])
        self.assertEqual(self.client.post(edit, self.form_data(scope='team', revision='1')).status_code, 302)
        self.assertFalse(KPIDisplaySetting.objects.filter(metric_key=team).exists())
        response = self.client.post(self.settings_url, old_matrix)
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, 'Aucun réglage n’a été enregistré', status_code=409)
        self.assertContains(response, f'name="revision_{prefix}" value="2"', status_code=409)
        self.assertFalse(KPIDisplaySetting.objects.filter(metric_key__in=[team, 'my_score']).exists())
        old_matrix[f'revision_{prefix}'] = '2'
        self.assertEqual(self.client.post(self.settings_url, old_matrix).status_code, 302)
        self.assertTrue(KPIDisplaySetting.objects.get(role='leader', metric_key=team).show_live)

    def test_edit_version_scope_change_activation_and_reset_are_safe(self):
        kpi = self.create()
        personal, team = custom_metric_key(kpi.pk, 'personal'), custom_metric_key(kpi.pk, 'team')
        self.show(kpi, 'user', live=True)
        self.show(kpi, 'leader', 'team', live=True)
        url = reverse('edit_custom_kpi', args=[kpi.pk])
        response = self.client.post(url, self.form_data(name='Renommé', revision='1', scope='team'))
        self.assertRedirects(response, self.settings_url, fetch_redirect_response=False)
        kpi.refresh_from_db()
        self.assertEqual((kpi.name, kpi.scope, kpi.revision), ('Renommé', 'team', 2))
        self.assertFalse(KPIDisplaySetting.objects.filter(metric_key__in=[personal, team]).exists())
        self.assertNotIn(team, self.cards(self.leader_user))  # hidden after scope change
        stale = self.client.post(url, self.form_data(name='Écrasement', revision='1'))
        self.assertEqual(stale.status_code, 409)
        kpi.refresh_from_db(); self.assertEqual(kpi.name, 'Renommé')
        self.show(kpi, 'leader', 'team')
        self.assertIn(team, self.cards(self.leader_user))
        data = self.form_data(name='Renommé', revision='2', scope='team')
        data.pop('is_active')
        self.assertEqual(self.client.post(url, data).status_code, 302)
        kpi.refresh_from_db(); self.assertFalse(kpi.is_active)
        self.assertNotIn(team, self.cards(self.leader_user))
        self.assertTrue(KPIDisplaySetting.objects.filter(metric_key=team).exists())
        data['is_active'] = 'on'; data['revision'] = '3'
        self.client.post(url, data)
        self.assertIn(team, self.cards(self.leader_user))
        self.assertEqual(self.client.post(self.settings_url, {'action': 'reset'}).status_code, 302)
        self.assertTrue(CustomKPI.objects.filter(pk=kpi.pk).exists())
        self.assertNotIn(team, self.cards(self.leader_user))

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
from .kpi_scoring import summarize_items
from .kpi_service import parse_month, snapshot
from .models import KPIDisplaySetting, Query, Schedule, ScheduleEmployee, ScheduleItem, Task, Timesheet


class KPITests(TestCase):
    @classmethod
    def setUpClass(cls):
        fixed = timezone.make_aware(datetime(2026, 10, 28, 12, 0))
        cls.clock = patch('django.utils.timezone.now', return_value=fixed)
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
        cls.worker_user = User.objects.create_user('worker-kpi', password='test')
        cls.leader_user = User.objects.create_user('leader-kpi', password='test')
        cls.outside_user = User.objects.create_user('other-kpi', password='test')
        cls.other_leader_user = User.objects.create_user('other-leader-kpi', password='test')
        cls.admin = User.objects.create_user('admin-kpi', password='test')
        cls.admin.groups.add(Group.objects.get_or_create(name='Admin')[0])
        cls.root = User.objects.create_superuser('root-kpi', password='test')
        cls.no_profile = User.objects.create_user('no-profile-kpi', password='test')
        cls.rank = Rank.objects.create(value=7, label='Employé KPI')
        cls.worker = Employee.objects.create(user=cls.worker_user, first_name='Alice', last_name='Membre', rank=cls.rank)
        cls.leader = Employee.objects.create(user=cls.leader_user, first_name='Lina', last_name='Responsable', rank=cls.rank)
        cls.outside = Employee.objects.create(user=cls.outside_user, first_name='Confidentiel', last_name='Autre équipe', rank=cls.rank)
        cls.other_leader = Employee.objects.create(user=cls.other_leader_user, first_name='Autre', last_name='Responsable', rank=cls.rank)
        cls.team = Team.objects.create(name='Équipe Alpha', leader=cls.leader)
        cls.team.employees.add(cls.worker)  # Leader is not in the M2M and must still be included.
        other_team = Team.objects.create(name='Équipe Beta', leader=cls.other_leader)
        other_team.employees.add(cls.outside)
        cls.schedule = Schedule.objects.create(date=date(2026, 10, 28), start_time=time(0), end_time=time(23, 59))
        cls.old_schedule = Schedule.objects.create(date=date(2026, 10, 27), start_time=time(0), end_time=time(23, 59))
        cls.auto_query = Query.objects.create(name='KPI auto', type=1, query='SELECT 1', parameters={})
        cls.manual = Task.objects.create(task_name='Manuel prioritaire', responsibility_level=cls.rank,
                                         priority=2, average_duration=timedelta(seconds=1))
        cls.auto = Task.objects.create(task_name='Saisie 11 lignes', responsibility_level=cls.rank, priority=1,
                                       average_duration=timedelta(seconds=45), progress_query=cls.auto_query)
        cls.leader_task = Task.objects.create(task_name='Tâche responsable', responsibility_level=cls.rank,
                                             priority=4, average_duration=timedelta(seconds=1))
        cls.manual_item = ScheduleItem.objects.create(schedule=cls.schedule, employee=cls.worker, task=cls.manual,
                                                       assigned_time=timedelta(seconds=100), progression_rate=100)
        cls.auto_item = ScheduleItem.objects.create(schedule=cls.schedule, employee=cls.worker, task=cls.auto,
                                                     assigned_time=timedelta(seconds=495), progress_lines=11, progression_rate=0)
        cls.leader_item = ScheduleItem.objects.create(schedule=cls.schedule, employee=cls.leader, task=cls.leader_task,
                                                       assigned_time=timedelta(seconds=20), progression_rate=50)
        cls.outside_item = ScheduleItem.objects.create(schedule=cls.schedule, employee=cls.outside, task=cls.manual,
                                                        assigned_time=timedelta(seconds=10000), progression_rate=0)
        ScheduleItem.objects.create(schedule=cls.old_schedule, employee=cls.worker, task=cls.manual,
                                    assigned_time=timedelta(seconds=10000), progression_rate=0)
        ScheduleItem.objects.create(schedule=cls.schedule, task=cls.manual,
                                    assigned_time=timedelta(seconds=10000), progression_rate=0)
        ScheduleEmployee.objects.create(schedule=cls.schedule, employee=cls.worker, score=999)
        ScheduleEmployee.objects.create(schedule=cls.schedule, employee=cls.leader, score=999)
        Timesheet.objects.create(employee=cls.worker, date=date(2026, 10, 5), start_time=time(8), end_time=time(12),
                                 start_time_2=time(13), end_time_2=time(17), is_confirmed=True)
        Timesheet.objects.create(employee=cls.worker, date=date(2026, 10, 6), start_time=time(8), end_time=time(11),
                                 start_time_2=time(13), is_confirmed=True)
        Timesheet.objects.create(employee=cls.worker, date=date(2026, 10, 7), start_time=time(8), end_time=time(12))
        Timesheet.objects.create(employee=cls.worker, date=date(2026, 10, 8), leave_type=Timesheet.LeaveType.PAID, is_confirmed=True)
        Timesheet.objects.create(employee=cls.worker, date=date(2026, 10, 28), start_time=time(8))
        Timesheet.objects.create(employee=cls.worker, date=date(2026, 10, 29), start_time=time(8), end_time=time(16), is_confirmed=True)
        Timesheet.objects.create(employee=cls.worker, date=date(2026, 9, 5), start_time=time(8), end_time=time(18), is_confirmed=True)
        Timesheet.objects.create(employee=cls.outside, date=date(2026, 10, 28), start_time=time(8))

    def setUp(self):
        self.client.force_login(self.worker_user)

    def cards(self, user=None, surface='dashboard', month=None):
        data = snapshot(user or self.worker_user, surface, month=month)
        return {card['key']: card for card in data['cards']}

    def test_priority_weighted_score_and_saved_line_duration(self):
        items = ScheduleItem.objects.filter(pk__in=[self.manual_item.pk, self.auto_item.pk]).select_related('task__progress_query')
        result = summarize_items(items)
        # 100 / 2 = 50; 11 × 45 / 1 = 495. Assigned points use the same priorities.
        self.assertEqual(result.achieved_points, Decimal('545'))
        self.assertEqual(result.assigned_points, Decimal('545'))
        self.assertEqual(result.percentage, Decimal('100'))
        self.assertEqual(result.completed_count, 2)
        cards = self.cards()
        self.assertEqual(cards['my_progress']['value'], '100')
        self.assertEqual(cards['my_progress']['detail'], '545 / 545 points · TRT du jour')
        self.assertEqual(cards['my_score']['value'], '545')
        self.assertEqual(cards['my_tasks']['value'], '2 / 2')
        self.assertEqual(cards['my_assigned']['value'], '9 min 55 s')

    def test_fractional_points_and_manual_percentage_keep_precision(self):
        self.manual_item.assigned_time = timedelta(seconds=1)
        self.manual_item.progression_rate = 100
        self.manual_item.save()
        self.auto_item.delete()
        result = summarize_items(ScheduleItem.objects.filter(pk=self.manual_item.pk).select_related('task__progress_query'))
        self.assertEqual(result.achieved_points, Decimal('0.5'))
        self.assertEqual(result.assigned_points, Decimal('0.5'))
        self.assertEqual(result.percentage, Decimal('100'))
        self.assertEqual(self.cards()['my_score']['value'], '0,5')
        self.manual_item.progression_rate = 50
        self.manual_item.save()
        self.assertEqual(self.cards()['my_progress']['value'], '50')
        self.assertEqual(self.cards()['my_score']['value'], '0,25')

    def test_uncapped_auto_score_and_bounded_accessible_bar(self):
        self.auto_item.progress_lines = 22
        self.auto_item.save()
        cards = self.cards()
        self.assertGreater(Decimal(cards['my_progress']['value'].replace(',', '.')), 100)
        self.assertEqual(cards['my_progress']['bar_value'], 100)
        self.assertIn('limitée', cards['my_progress']['warning'])
        self.assertEqual(cards['my_score']['value'], '1\u00a0040')

    def test_invalid_data_is_not_silently_included_as_zero(self):
        self.auto_item.progress_lines = None
        self.auto_item.save()
        cards = self.cards()
        self.assertEqual(cards['my_score']['value'], '—')
        self.assertEqual(cards['my_progress']['value'], '—')
        self.assertIn('1 tâche(s)', cards['my_progress']['detail'])
        self.assertEqual(cards['my_tasks']['value'], '1 / 2')
        self.assertIn('sans données', cards['my_tasks']['warning'])
        self.auto_item.progress_lines = 1
        self.auto_item.save()
        self.auto.average_duration = None
        self.auto.save()
        self.assertEqual(self.cards()['my_progress']['value'], '—')
        self.auto.average_duration = timedelta(seconds=45)
        self.auto.save()
        self.manual.priority = 0
        self.manual.save()
        self.assertEqual(self.cards()['my_progress']['value'], '—')
        self.manual.priority = 2
        self.manual.save()
        self.manual_item.assigned_time = timedelta(0)
        self.manual_item.save()
        self.assertEqual(self.cards()['my_assigned']['value'], '—')
        self.assertEqual(self.cards()['my_progress']['value'], '—')

    def test_empty_trt_no_profile_and_inactive_trt_are_not_fabricated(self):
        empty = self.cards(self.no_profile)
        self.assertEqual(empty['my_progress']['value'], '—')
        self.assertIn('Aucun profil', empty['my_progress']['detail'])
        self.schedule.end_time = time(11)
        self.schedule.save()
        # Daily totals survive after the active TRT has ended, but the personal
        # live task table must not show a no-longer-active schedule.
        self.assertEqual(self.cards()['my_progress']['value'], '100')
        live = snapshot(self.worker_user, 'live')
        self.assertIsNone(live['schedule_id'])
        self.assertEqual(live['tasks'], [])
        self.schedule.end_time = time(23, 59)
        self.schedule.save()
        ScheduleItem.objects.filter(employee=self.worker, schedule=self.schedule).delete()
        self.assertIn('Aucune tâche', self.cards()['my_progress']['detail'])

    def test_daily_kpis_cover_all_todays_trts_but_table_shows_only_active_trt(self):
        morning = Schedule.objects.create(date=date(2026, 10, 28), start_time=time(6), end_time=time(8), schedule_status=3)
        future = Schedule.objects.create(date=date(2026, 10, 28), start_time=time(15), end_time=time(17))
        template = Schedule.objects.create(date=date(2026, 10, 28), start_time=time(1), end_time=time(2), is_template=True)
        earlier = ScheduleItem.objects.create(schedule=morning, employee=self.worker, task=self.manual,
                                                assigned_time=timedelta(seconds=100), progression_rate=50)
        later = ScheduleItem.objects.create(schedule=future, employee=self.worker, task=self.manual,
                                              assigned_time=timedelta(seconds=100), progression_rate=0)
        ScheduleItem.objects.create(schedule=template, employee=self.worker, task=self.manual,
                                    assigned_time=timedelta(seconds=10000), progression_rate=0)
        cards = self.cards()
        self.assertEqual(cards['my_progress']['value'], '88,37')
        self.assertEqual(cards['my_progress']['detail'], '570 / 645 points · TRT du jour')
        self.assertEqual(cards['my_tasks']['value'], '2 / 4')
        self.assertEqual(self.cards(self.leader_user)['team_progress']['detail'], '572,5 / 650 points · TRT du jour')
        live = snapshot(self.worker_user, 'live')
        self.assertEqual({task['id'] for task in live['tasks']}, {self.manual_item.pk, self.auto_item.pk})
        self.assertNotIn(earlier.pk, [task['id'] for task in live['tasks']])
        self.assertNotIn(later.pk, [task['id'] for task in live['tasks']])

    def test_team_calculation_includes_leader_but_excludes_other_teams(self):
        cards = self.cards(self.leader_user)
        self.assertEqual(cards['team_score']['value'], '547,5')  # 545 + (20 × 50% / 4)
        self.assertEqual(cards['team_progress']['value'], '99,55')
        self.assertEqual(cards['team_progress']['detail'], '547,5 / 550 points · TRT du jour')
        self.assertEqual(cards['team_tasks']['value'], '2 / 3')
        self.assertEqual(cards['team_presence']['value'], '1')
        rows = cards['team_records']['rows']
        self.assertEqual({row['primary'] for row in rows}, {'Alice Membre', 'Lina Responsable'})
        self.assertEqual({row['status'] for row in rows}, {'Pointage ouvert', 'État inconnu'})
        self.assertIn('pas une présence en direct', cards['team_presence']['warning'])
        html = self.client.get(reverse('kpi_dashboard')).content.decode()
        self.assertNotIn('Confidentiel', html)
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse('kpi_snapshot'), {'surface': 'dashboard', 'team': 'Beta', 'employee': self.outside.pk})
        self.assertNotIn('Confidentiel', response.json()['html'])
        self.assertIn('Alice Membre', response.json()['html'])

    def test_admin_without_led_team_and_worker_cannot_access_team_indicators(self):
        admin = self.cards(self.admin)
        self.assertEqual(admin['team_score']['value'], '—')
        self.assertIn('Aucune équipe dirigée', admin['team_score']['detail'])
        # Even a corrupt/forged database setting cannot create an employee team KPI.
        KPIDisplaySetting.objects.create(role='user', metric_key='team_records', show_live=True,
                                         show_dashboard=True, presentation='details', position=0)
        worker = self.cards()
        self.assertNotIn('team_records', worker)
        self.assertNotIn('team_progress', worker)

    def test_monthly_confirmed_days_hours_pending_and_future(self):
        cards = self.cards()
        self.assertEqual(cards['attendance_days']['value'], '2')
        self.assertEqual(cards['attendance_hours']['value'], '11 h')
        self.assertIn('1 pointage(s)', cards['attendance_hours']['warning'])
        self.assertIn('1 enregistrement(s) futur(s)', cards['attendance_hours']['warning'])
        self.assertEqual(cards['attendance_pending']['value'], '2')
        rows = cards['attendance_records']['rows']
        self.assertEqual(len(rows), 6)
        self.assertTrue(any('date future non comptabilisée' in row['secondary'] for row in rows))
        self.assertTrue(any('Congé enregistré' in row['status'] for row in rows))
        self.assertFalse(any(row['primary'] == '05/09/2026' for row in rows))
        september = self.cards(month=date(2026, 9, 1))
        self.assertEqual(september['attendance_days']['value'], '1')
        self.assertEqual(september['attendance_hours']['value'], '10 h')
        self.assertEqual(september['attendance_pending']['value'], '0')

    def test_overlapping_and_out_of_order_punches_are_not_double_counted(self):
        Timesheet.objects.create(employee=self.worker, date=date(2026, 10, 9), is_confirmed=True,
                                 start_time=time(8), end_time=time(12), start_time_2=time(11), end_time_2=time(15))
        cards = self.cards()
        self.assertEqual(cards['attendance_days']['value'], '3')
        self.assertEqual(cards['attendance_hours']['value'], '15 h')  # 11 + the safe 08:00–12:00 prefix
        self.assertIn('2 pointage(s)', cards['attendance_hours']['warning'])
        record = next(row for row in cards['attendance_records']['rows'] if row['primary'] == '09/10/2026')
        self.assertEqual(record['status'], 'Pointage à vérifier')
        self.assertIn('non ambiguës', cards['attendance_hours']['detail'])

    def test_confirmed_record_without_punches_is_flagged_as_unknown(self):
        Timesheet.objects.create(employee=self.worker, date=date(2026, 10, 9), is_confirmed=True)
        cards = self.cards()
        self.assertEqual(cards['attendance_days']['value'], '2')
        self.assertIn('2 pointage(s)', cards['attendance_days']['warning'])
        self.assertIn('2 pointage(s)', cards['attendance_hours']['warning'])
        row = next(row for row in cards['attendance_records']['rows'] if row['primary'] == '09/10/2026')
        self.assertEqual(row['status'], 'Aucun horaire enregistré')

    def test_task_table_and_score_use_the_same_saved_automatic_progress(self):
        data = snapshot(self.worker_user, 'live')
        task = next(item for item in data['tasks'] if item['id'] == self.auto_item.pk)
        self.assertEqual(task['goal'], '11')
        self.assertEqual(task['rate_label'], '100 %')  # cached progression_rate is deliberately zero
        self.assertEqual(task['bar_value'], 100)
        self.auto_item.progress_lines = 22
        self.auto_item.save()
        task = next(item for item in snapshot(self.worker_user, 'live')['tasks'] if item['id'] == self.auto_item.pk)
        self.assertEqual(task['rate_label'], '200 %')
        self.assertEqual(task['bar_value'], 100)

    def test_early_month_keeps_four_digit_year_for_subsequent_poll(self):
        response = self.client.get(reverse('kpi_dashboard'), {'month': '0001-01'})
        self.assertContains(response, 'data-kpi-month="0001-01"')
        self.assertEqual(self.client.get(reverse('kpi_snapshot'), {'surface': 'dashboard', 'month': '0001-01'}).status_code, 200)

    def test_empty_attendance_does_not_infer_absence(self):
        cards = self.cards(month=date(2025, 1, 1))
        for key in ('attendance_days', 'attendance_hours', 'attendance_pending', 'attendance_records'):
            self.assertEqual(cards[key]['value'], '—')
            self.assertIn('absence ne peut pas être déduite', cards[key]['detail'])

    def test_ambiguous_or_incomplete_punches_do_not_claim_presence(self):
        record = Timesheet.objects.create(employee=self.leader, date=date(2026, 10, 28),
                                          end_time=time(9), start_time_2=time(10))
        cards = self.cards(self.leader_user)
        leader = next(row for row in cards['team_records']['rows'] if row['primary'] == 'Lina Responsable')
        self.assertEqual(leader['status'], 'Pointage à vérifier')
        self.assertEqual(cards['team_presence']['value'], '1')
        record.end_time = None
        record.end_time_2 = time(12)
        record.save()
        leader = next(row for row in self.cards(self.leader_user)['team_records']['rows'] if row['primary'] == 'Lina Responsable')
        self.assertEqual(leader['status'], 'Pointage clôturé')
        record.start_time_2 = time(14)
        record.save()
        leader = next(row for row in self.cards(self.leader_user)['team_records']['rows'] if row['primary'] == 'Lina Responsable')
        self.assertEqual(leader['status'], 'Pointage à vérifier')  # no inferred overnight work

    def test_manual_bulk_change_is_visible_in_next_snapshot_without_querying_xpert(self):
        self.client.force_login(self.leader_user)
        with patch('ounissi_app.schedule_service.run_raw_query') as run_query:
            response = self.client.post(reverse('bulk_manual_progress', args=[self.schedule.pk]), {
                'selected_item': [str(self.leader_item.pk)], f'rate_{self.leader_item.pk}': '100',
            })
            self.assertEqual(response.status_code, 302)
            cards = self.cards(self.leader_user)
            run_query.assert_not_called()
        self.assertEqual(cards['team_progress']['value'], '100')
        self.assertEqual(cards['team_score']['value'], '550')

    def test_manager_score_column_uses_seconds_not_the_legacy_integer_cache(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('actual_schedule_details'))
        self.assertContains(response, 'Score (points)')
        self.assertContains(response, '>545<', html=False)
        self.assertContains(response, '>2,5<', html=False)
        self.assertNotContains(response, '>999<', html=False)

    def test_manager_score_column_is_scoped_for_leaders_and_hidden_from_workers(self):
        ScheduleEmployee.objects.create(schedule=self.schedule, employee=self.outside, score=999)
        self.outside_item.progression_rate = 100
        self.outside_item.save()
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse('actual_schedule_details'))
        self.assertContains(response, '>545<', html=False)
        self.assertContains(response, '>2,5<', html=False)
        self.assertNotContains(response, '>5 000<', html=False)
        self.assertContains(response, 'Score non disponible pour votre périmètre.')
        self.client.force_login(self.worker_user)
        response = self.client.get(reverse('actual_schedule_details'))
        self.assertNotContains(response, '>545<', html=False)
        self.assertNotContains(response, '>2,5<', html=False)
        self.assertContains(response, 'Score non disponible pour votre périmètre.')
        self.client.force_login(self.admin)
        response = self.client.get(reverse('actual_schedule_details'))
        self.assertContains(response, '>5 000<', html=False)

    def test_snapshot_is_read_only_never_queries_xpert_and_is_not_cacheable(self):
        with patch('ounissi_app.schedule_service.run_raw_query') as run_query, \
             patch('ounissi_app.views.import_attendance_and_users') as import_terminal, \
             CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse('kpi_snapshot'), {'surface': 'live'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertIn('no-store', response['Cache-Control'])
        run_query.assert_not_called()
        import_terminal.assert_not_called()
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries.captured_queries))
        payload = response.json()
        self.assertEqual(payload['schedule_id'], self.schedule.pk)
        self.assertEqual({task['id'] for task in payload['tasks']}, {self.manual_item.pk, self.auto_item.pk})
        self.assertEqual(payload['read_at'], '12:00:00')

    def test_month_validation_authentication_methods_and_xss(self):
        self.assertEqual(parse_month('2026-09'), date(2026, 9, 1))
        for invalid in ('2026-00', '0000-01', '2026-13', '1-01', '2026-1', '2026-01-01'):
            with self.subTest(month=invalid):
                self.assertEqual(self.client.get(reverse('kpi_dashboard'), {'month': invalid}).status_code, 400)
                self.assertEqual(self.client.get(reverse('kpi_snapshot'), {'month': invalid}).status_code, 400)
        self.assertEqual(self.client.get(reverse('kpi_snapshot'), {'surface': 'team'}).status_code, 400)
        self.assertEqual(self.client.post(reverse('kpi_snapshot')).status_code, 405)
        self.assertEqual(self.client.post(reverse('kpi_dashboard')).status_code, 405)
        self.leader.first_name = '<script>alert(1)</script>'
        self.leader.save()
        self.client.force_login(self.leader_user)
        html = self.client.get(reverse('kpi_snapshot'), {'surface': 'dashboard'}).json()['html']
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('kpi_snapshot')).status_code, 302)
        self.assertEqual(self.client.get(reverse('kpi_dashboard')).status_code, 302)


class KPISettingsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.worker = User.objects.create_user('worker-settings', password='test')
        cls.admin = User.objects.create_user('admin-settings', password='test')
        cls.admin.groups.add(Group.objects.get_or_create(name='Admin')[0])
        cls.root = User.objects.create_superuser('root-settings', password='test')
        rank = Rank.objects.create(value=9, label='KPI settings')
        Employee.objects.create(user=cls.worker, first_name='Worker', rank=rank)
        cls.leader = User.objects.create_user('leader-settings', password='test')
        leader = Employee.objects.create(user=cls.leader, first_name='Leader', rank=rank)
        Team.objects.create(name='Settings Team', leader=leader)
        cls.url = reverse('configure_kpis')

    @staticmethod
    def row(role='user', key='my_score', style='value', position='1', live=True, dashboard=False):
        prefix = f'{role}_{key}'
        data = {f'row_{prefix}': '1', f'presentation_{prefix}': style, f'position_{prefix}': position}
        if live:
            data[f'live_{prefix}'] = '1'
        if dashboard:
            data[f'dashboard_{prefix}'] = '1'
        return data

    def test_admin_only_config_link_and_csrf(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        for user in (self.worker, self.leader):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.client.post(self.url, {'action': 'reset'}).status_code, 403)
            self.assertNotContains(self.client.get(reverse('kpi_dashboard')), 'Configurer les indicateurs')
        for user in (self.admin, self.root):
            self.client.force_login(user)
            response = self.client.get(self.url)
            self.assertContains(response, 'Configuration des indicateurs')
            self.assertContains(response, 'Configurer les indicateurs')
            self.assertContains(response, 'row_user_my_score')
            self.assertContains(response, 'class="kpi-settings-table" data-no-shell')
            self.assertNotContains(response, 'row_user_team_records')
            self.assertEqual(self.client.put(self.url).status_code, 405)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.admin)
        self.assertEqual(csrf.post(self.url, {'action': 'reset'}).status_code, 403)
        csrf.get(self.url)
        self.assertEqual(csrf.post(self.url, {'action': 'reset'}, HTTP_X_CSRFTOKEN=csrf.cookies['csrftoken'].value).status_code, 302)

    def test_save_placement_presentation_order_persistence_and_reset(self):
        self.client.force_login(self.admin)
        data = {'action': 'save', **self.row(), **self.row(key='my_progress', style='value', position='5', live=False, dashboard=True)}
        self.assertRedirects(self.client.post(self.url, data), self.url)
        self.assertEqual(KPIDisplaySetting.objects.count(), 2)
        self.client.force_login(self.worker)
        live = self.client.get(reverse('home'))
        self.assertContains(live, 'data-kpi-key="my_score"')
        self.assertNotContains(live, 'data-kpi-key="my_progress"')
        self.assertIn('data-kpi-key="my_tasks"', live.content.decode())  # untouched default
        dashboard = self.client.get(reverse('kpi_dashboard'))
        self.assertNotContains(dashboard, 'data-kpi-key="my_score"')
        self.assertContains(dashboard, 'data-kpi-key="my_progress"')
        snapshot_response = self.client.get(reverse('kpi_snapshot'), {'surface': 'dashboard'}).json()
        self.assertNotIn('data-kpi-key="my_score"', snapshot_response['html'])
        self.assertNotIn('class="kpi-progress"', snapshot_response['html'])
        self.client.force_login(self.admin)
        self.assertRedirects(self.client.post(self.url, {'action': 'reset'}), self.url)
        self.assertFalse(KPIDisplaySetting.objects.exists())
        self.client.force_login(self.worker)
        self.assertContains(self.client.get(reverse('home')), 'data-kpi-key="my_progress"')

    def test_invalid_entire_form_rejected_before_writes(self):
        self.client.force_login(self.admin)
        valid = self.row()
        for malformed in (
            self.row(key='my_tasks', style='bar'),
            self.row(key='my_progress', style='details'),
            self.row(key='my_tasks', position='-1'),
            self.row(key='my_tasks', position='1000'),
            self.row(key='my_tasks', position=''),
        ):
            with self.subTest(malformed=malformed):
                response = self.client.post(self.url, {'action': 'save', **valid, **malformed})
                self.assertEqual(response.status_code, 400)
                self.assertFalse(KPIDisplaySetting.objects.exists())
        self.assertEqual(self.client.post(self.url, {'action': 'save'}).status_code, 400)
        self.assertEqual(self.client.post(self.url, {'action': 'other'}).status_code, 400)

    def test_transaction_rolls_back_if_second_setting_fails(self):
        self.client.force_login(self.admin)
        original = KPIDisplaySetting.objects.update_or_create
        # Registry order is progress first: fail on score after one successful write.
        def fail_score(*args, **kwargs):
            if kwargs.get('metric_key') == 'my_score':
                raise RuntimeError('database error')
            return original(*args, **kwargs)
        data = {'action': 'save', **self.row(), **self.row(key='my_progress', style='bar')}
        with patch('ounissi_app.kpi_views.KPIDisplaySetting.objects.update_or_create', side_effect=fail_score):
            with self.assertRaises(RuntimeError):
                self.client.post(self.url, data)
        self.assertFalse(KPIDisplaySetting.objects.exists())

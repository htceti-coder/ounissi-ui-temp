import json
from datetime import time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Employee, Rank, Team
from .models import Query, Schedule, ScheduleEmployee, ScheduleItem, ScheduleTask, Task, TaskType


class BulkManualProgressTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_user(username='manager', password='test')
        cls.admin.groups.add(Group.objects.get_or_create(name='Admin')[0])
        cls.worker_user = get_user_model().objects.create_user(username='worker', password='test')
        cls.leader_user = get_user_model().objects.create_user(username='leader', password='test')
        cls.rank = Rank.objects.create(value=2, label='Employé')
        cls.worker = Employee.objects.create(user=cls.worker_user, first_name='Employé', rank=cls.rank, xp_id='WORKER')
        cls.leader = Employee.objects.create(user=cls.leader_user, first_name='Responsable', rank=cls.rank)
        Team.objects.create(name='Équipe', leader=cls.leader)
        cls.schedule = Schedule.objects.create(date=timezone.localdate(), start_time=time(0), end_time=time(23, 59))
        cls.other_schedule = Schedule.objects.create(date=timezone.localdate() - timedelta(days=1), start_time=time(0), end_time=time(23, 59))
        ScheduleEmployee.objects.create(employee=cls.worker, schedule=cls.schedule, total_assigned_time=timedelta(minutes=60))
        manual_type = TaskType.objects.create(name='MANUEL')
        auto_type = TaskType.objects.create(name='AUTO')
        reception_query = Query.objects.create(name='Réception manuelle', type=0, query='N/A', parameters={'model_name': 'ReceptionProduit'})
        auto_query = Query.objects.create(name='Automatique', type=1, query='SELECT 1', parameters={})
        cls.reception = Task.objects.create(task_name='Réception produit', task_type=manual_type, responsibility_level=cls.rank, average_duration=timedelta(minutes=1), progress_query=reception_query)
        cls.cleaning = Task.objects.create(task_name='Nettoyage', task_type=manual_type, responsibility_level=cls.rank, average_duration=timedelta(minutes=1))
        cls.auto = Task.objects.create(task_name='Saisie automatique', task_type=auto_type, responsibility_level=cls.rank, average_duration=timedelta(minutes=1), progress_query=auto_query)
        cls.first = ScheduleItem.objects.create(schedule=cls.schedule, employee=cls.worker, task=cls.reception, assigned_time=timedelta(minutes=20), progression_rate=25, progress_lines=3, task_identifier='Supplier#INV-1')
        cls.second = ScheduleItem.objects.create(schedule=cls.schedule, employee=cls.worker, task=cls.cleaning, assigned_time=timedelta(minutes=40), progression_rate=0, progress_lines=7)
        cls.completed = ScheduleItem.objects.create(schedule=cls.schedule, employee=cls.worker, task=cls.cleaning, assigned_time=timedelta(minutes=40), progression_rate=100)
        cls.unassigned = ScheduleItem.objects.create(schedule=cls.schedule, task=cls.cleaning, progression_rate=0)
        cls.automatic = ScheduleItem.objects.create(schedule=cls.schedule, employee=cls.worker, task=cls.auto, assigned_time=timedelta(minutes=20), progression_rate=15)
        cls.other = ScheduleItem.objects.create(schedule=cls.other_schedule, employee=cls.worker, task=cls.cleaning, assigned_time=timedelta(minutes=20), progression_rate=5)
        cls.reception_total = ScheduleTask.objects.create(schedule=cls.schedule, task=cls.reception, total_assigned_time=timedelta(minutes=20), completion=25, status=1)
        cls.cleaning_total = ScheduleTask.objects.create(schedule=cls.schedule, task=cls.cleaning, total_assigned_time=timedelta(minutes=80), completion=50, status=1)
        cls.auto_total = ScheduleTask.objects.create(schedule=cls.schedule, task=cls.auto, total_assigned_time=timedelta(minutes=20), completion=15, status=1)

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse('bulk_manual_progress', args=[self.schedule.pk])

    def post_updates(self, updates, **extra):
        return self.client.post(self.url, {'updates': json.dumps(updates), **extra})

    def rates(self):
        return dict(ScheduleItem.objects.filter(pk__in=[self.first.pk, self.second.pk, self.completed.pk, self.automatic.pk]).values_list('pk', 'progression_rate'))

    def test_bulk_updates_reception_and_other_manual_tasks_only(self):
        before_total = ScheduleEmployee.objects.get(employee=self.worker, schedule=self.schedule).total_assigned_time
        with patch('ounissi_app.schedule_service.run_raw_query') as run_query:
            response = self.post_updates([{'id': self.first.pk, 'rate': 100}, {'id': str(self.second.pk), 'rate': 65}], source='actual')
        self.assertRedirects(response, reverse('actual_schedule_details'), fetch_redirect_response=False)
        self.assertTrue(any('2 tâche(s)' in str(msg) for msg in response.wsgi_request._messages))
        self.first.refresh_from_db(); self.second.refresh_from_db(); self.completed.refresh_from_db(); self.automatic.refresh_from_db()
        self.assertEqual((self.first.progression_rate, self.second.progression_rate), (100, 65))
        self.assertEqual((self.completed.progression_rate, self.automatic.progression_rate), (100, 15))
        self.assertEqual((self.first.progress_lines, self.second.progress_lines), (3, 7))
        self.assertEqual((self.first.assigned_time, self.second.assigned_time), (timedelta(minutes=20), timedelta(minutes=40)))
        self.assertEqual(ScheduleEmployee.objects.get(employee=self.worker, schedule=self.schedule).total_assigned_time, before_total)
        self.reception_total.refresh_from_db(); self.cleaning_total.refresh_from_db(); self.auto_total.refresh_from_db()
        self.assertEqual((self.reception_total.completion, self.reception_total.status), (100, 2))
        self.assertEqual((self.cleaning_total.completion, self.cleaning_total.status), (82, 1))
        self.assertEqual((self.auto_total.completion, self.auto_total.status), (15, 1))
        run_query.assert_not_called()

    def test_task_completion_uses_exact_weighted_integer_rates(self):
        # Binary floats can otherwise display 29%, 57% or 58% one point too low.
        for rate in (29, 57, 58):
            with self.subTest(rate=rate):
                self.post_updates([{'id': self.first.pk, 'rate': rate}])
                self.reception_total.refresh_from_db()
                self.assertEqual(self.reception_total.completion, rate)

    def test_completed_manual_tasks_can_be_corrected(self):
        self.cleaning_total.completion = 100; self.cleaning_total.status = 2; self.cleaning_total.save()
        response = self.post_updates([{'id': self.completed.pk, 'rate': 30}])
        self.assertEqual(response.status_code, 302)
        self.completed.refresh_from_db(); self.cleaning_total.refresh_from_db()
        self.assertEqual(self.completed.progression_rate, 30)
        self.assertEqual((self.cleaning_total.completion, self.cleaning_total.status), (15, 1))

    def test_normal_html_submission_and_single_row_button(self):
        response = self.client.post(self.url, {
            'selected_item': [str(self.first.pk), str(self.second.pk)],
            f'rate_{self.first.pk}': '80', f'rate_{self.second.pk}': '45',
        })
        self.assertRedirects(response, reverse('schedule_details_selected', args=[self.schedule.pk]), fetch_redirect_response=False)
        self.assertEqual((self.rates()[self.first.pk], self.rates()[self.second.pk]), (80, 45))
        response = self.client.post(self.url, {
            'single_item': str(self.first.pk), 'selected_item': [str(self.second.pk)],
            f'rate_{self.first.pk}': '9', f'rate_{self.second.pk}': '99',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual((self.rates()[self.first.pk], self.rates()[self.second.pk]), (9, 45))

    def test_unauthorized_user_and_anonymous_user_cannot_submit(self):
        self.client.force_login(self.worker_user)
        self.assertEqual(self.post_updates([{'id': self.first.pk, 'rate': 100}]).status_code, 403)
        self.client.logout()
        self.assertEqual(self.post_updates([{'id': self.first.pk, 'rate': 100}]).status_code, 302)
        self.first.refresh_from_db(); self.assertEqual(self.first.progression_rate, 25)

    def test_team_leader_is_allowed_and_get_is_not_allowed(self):
        self.client.force_login(self.leader_user)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(self.post_updates([{'id': self.first.pk, 'rate': 75}]).status_code, 302)
        self.first.refresh_from_db(); self.assertEqual(self.first.progression_rate, 75)

    def test_csrf_is_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        payload = {'selected_item': [self.first.pk], f'rate_{self.first.pk}': '100'}
        self.assertEqual(client.post(self.url, payload).status_code, 403)
        self.first.refresh_from_db(); self.assertEqual(self.first.progression_rate, 25)
        with patch('ounissi_app.schedule_service.get_current_active_schedule', return_value=self.schedule):
            client.get(reverse('actual_schedule_details'))
        token = client.cookies['csrftoken'].value
        self.assertEqual(client.post(self.url, payload, HTTP_X_CSRFTOKEN=token).status_code, 302)
        self.first.refresh_from_db(); self.assertEqual(self.first.progression_rate, 100)

    def test_invalid_values_reject_the_entire_batch(self):
        cases = [
            [{'id': self.first.pk, 'rate': 100}, {'id': self.second.pk, 'rate': -1}],
            [{'id': self.first.pk, 'rate': 100}, {'id': self.second.pk, 'rate': 101}],
            [{'id': self.first.pk, 'rate': 100}, {'id': self.second.pk, 'rate': '12.5'}],
            [{'id': self.first.pk, 'rate': 100}, {'id': self.second.pk, 'rate': ''}],
            [{'id': self.first.pk, 'rate': True}],
            [{'id': self.first.pk, 'rate': 50}, {'id': self.first.pk, 'rate': 100}],
            [],
        ]
        for entries in cases:
            with self.subTest(entries=entries):
                before = self.rates()
                response = self.post_updates(entries)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(self.rates(), before)
                self.assertTrue(any(msg.level_tag == 'error' for msg in response.wsgi_request._messages))

    def test_auto_unassigned_other_schedule_and_missing_items_reject_entire_batch(self):
        for forbidden in (self.automatic.pk, self.unassigned.pk, self.other.pk, 999999):
            with self.subTest(item=forbidden):
                before = self.rates()
                response = self.post_updates([{'id': self.first.pk, 'rate': 100}, {'id': forbidden, 'rate': 90}])
                self.assertEqual(response.status_code, 302)
                self.assertEqual(self.rates(), before)
                self.assertTrue(any('tâches manuelles affectées' in str(msg) for msg in response.wsgi_request._messages))
        self.other.refresh_from_db(); self.assertEqual(self.other.progression_rate, 5)
        self.unassigned.refresh_from_db(); self.assertEqual(self.unassigned.progression_rate, 0)

    def test_failed_aggregate_update_rolls_back_all_item_changes(self):
        with patch('ounissi_app.models.ScheduleTask.save', side_effect=RuntimeError('simulated failure')):
            with self.assertLogs('ounissi_app.views', level='ERROR'):
                response = self.post_updates([{'id': self.first.pk, 'rate': 100}, {'id': self.second.pk, 'rate': 50}])
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any('Aucune modification' in str(msg) for msg in response.wsgi_request._messages))
        self.first.refresh_from_db(); self.second.refresh_from_db()
        self.assertEqual((self.first.progression_rate, self.second.progression_rate), (25, 0))

    def test_selection_limit_supports_900_and_rejects_more(self):
        items = ScheduleItem.objects.bulk_create([
            ScheduleItem(
                schedule=self.schedule, employee=self.worker, task=self.cleaning,
                assigned_time=timedelta(seconds=1), progression_rate=0,
            )
            for _ in range(900)
        ])
        updates = [{'id': item.pk, 'rate': 42} for item in items]
        response = self.post_updates(updates)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            ScheduleItem.objects.filter(pk__in=[item.pk for item in items], progression_rate=42).count(),
            900,
        )
        response = self.post_updates(updates + [{'id': self.first.pk, 'rate': 80}])
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any('900 tâches' in str(msg) for msg in response.wsgi_request._messages))
        self.first.refresh_from_db()
        self.assertEqual(self.first.progression_rate, 25)

    def test_active_and_selected_trt_render_bulk_controls_for_all_manual_kinds(self):
        ScheduleItem.objects.filter(pk=self.second.pk).update(progression_rate=None)
        with patch('ounissi_app.schedule_service.get_current_active_schedule', return_value=self.schedule):
            active = self.client.get(reverse('actual_schedule_details'))
        selected = self.client.get(reverse('schedule_details_selected', args=[self.schedule.pk]))
        for response in (active, selected):
            self.assertContains(response, 'Valider la sélection')
            self.assertContains(response, 'Nettoyage')
            self.assertContains(response, 'Réception produit')
            self.assertContains(response, f'data-manual-item="{self.first.pk}"')
            self.assertContains(response, f'data-manual-item="{self.second.pk}"')
            self.assertContains(response, f'data-manual-item="{self.completed.pk}"')
            self.assertNotContains(response, f'data-manual-item="{self.automatic.pk}"')
            self.assertNotContains(response, f'data-manual-item="{self.unassigned.pk}"')
            self.assertContains(response, f'name="rate_{self.second.pk}" value="0"')
        self.client.force_login(self.worker_user)
        with patch('ounissi_app.schedule_service.get_current_active_schedule', return_value=self.schedule):
            employee_page = self.client.get(reverse('actual_schedule_details'))
        employee_selected = self.client.get(reverse('schedule_details_selected', args=[self.schedule.pk]))
        for response in (employee_page, employee_selected):
            self.assertNotContains(response, 'Valider la sélection')
            self.assertNotContains(response, 'data-manual-item=')

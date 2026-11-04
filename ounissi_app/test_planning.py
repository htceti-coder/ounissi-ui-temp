import json
import uuid
from datetime import date, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Employee, Rank, Team
from . import planning_service
from .models import (
    AssignationEmplacement, Query, Reception, Schedule, ScheduleCreationRequest,
    ScheduleEmployee, ScheduleItem, ScheduleTask, StockOperation, Task,
)

S = 1_000_000


class SchedulePlanningTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_user(username='planner-admin')
        cls.admin.groups.add(Group.objects.create(name='Admin'))
        cls.superuser = get_user_model().objects.create_superuser(username='planner-super', password='x')
        cls.leader = get_user_model().objects.create_user(username='planner-leader')
        cls.ordinary = get_user_model().objects.create_user(username='planner-user')
        cls.rank1 = Rank.objects.create(value=1, label='Niveau 1')
        cls.rank2 = Rank.objects.create(value=2, label='Niveau 2')
        cls.rank5 = Rank.objects.create(value=5, label='Niveau 5')
        Employee.objects.create(user=cls.admin, first_name='Admin', rank=cls.rank1)
        Employee.objects.create(user=cls.superuser, first_name='Superadmin', rank=cls.rank1)
        leader_employee = Employee.objects.create(user=cls.leader, first_name='Responsable', rank=cls.rank1)
        Employee.objects.create(user=cls.ordinary, first_name='Employé', rank=cls.rank2)
        cls.worker = Employee.objects.create(first_name='Autre équipe', last_name='Test', rank=cls.rank1)
        cls.unqualified = Employee.objects.create(first_name='Insuffisant', rank=cls.rank5)
        Team.objects.create(name='Équipe dirigeante', leader=leader_employee)
        other = Team.objects.create(name='Autre équipe')
        other.employees.add(cls.worker)
        cls.task = Task.objects.create(task_name='Ventes 45 secondes', priority=1, responsibility_level=cls.rank2,
                                       average_duration=timedelta(seconds=45))
        cls.second_task = Task.objects.create(task_name='Priorité 2', priority=2, responsibility_level=cls.rank1)

    def setUp(self):
        self.client.force_login(self.admin)

    def payload(self, *, key=None, seconds=495):
        return {
            'name': 'Modèle de test', 'start': '08:00:00', 'end': '14:00:00',
            'tasks': [{'id': self.task.pk, 'total_us': seconds * S, 'priority': 1, 'rank': 2}],
            'employees': [{'id': self.worker.pk, 'capacity_us': 600 * S, 'rank': 1}],
            'items': [{'id': None, 'task_id': self.task.pk, 'employee_id': self.worker.pk,
                       'assigned_us': seconds * S, 'identifier': 'A-01'}],
            'confirm_warnings': False, 'submission_key': str(key or uuid.uuid4()),
        }

    def post_plan(self, payload, url=None):
        return self.client.post(url or reverse('create_schedule'), {'plan': json.dumps(payload)})

    def make_template(self, *, seconds=495, assigned=495, identifier='A-01'):
        schedule = Schedule.objects.create(is_template=True, schedule_name='Original', start_time=time(8), end_time=time(14))
        ScheduleTask.objects.create(schedule=schedule, task=self.task, total_time=timedelta(seconds=seconds),
                                    total_assigned_time=timedelta(seconds=assigned))
        ScheduleEmployee.objects.create(schedule=schedule, employee=self.worker, working_hours=timedelta(seconds=600),
                                        total_assigned_time=timedelta(seconds=assigned))
        ScheduleItem.objects.create(schedule=schedule, task=self.task, employee=self.worker,
                                    assigned_time=timedelta(seconds=assigned), task_identifier=identifier)
        return schedule

    def edit_payload(self, schedule):
        snapshot = planning_service.load_snapshot(schedule)
        draft, _ = planning_service.plan_for_snapshot(snapshot, planning_service.catalog())
        draft['confirm_warnings'] = False
        return draft

    def dated_payload(self, schedule, *, day='2026-11-06', confirmed=False, key=None):
        return {'schedule_date': day, 'start_time': '08:00:00', 'end_time': '14:00:00',
                'version': planning_service.version(planning_service.load_snapshot(schedule)),
                'submission_key': str(key or uuid.uuid4()), 'confirm_warnings': 'yes' if confirmed else ''}

    def test_template_pages_are_admin_only_including_direct_posts(self):
        template = self.make_template()
        urls = [reverse('create_schedule'), reverse('edit_schedule_template', args=[template.pk]),
                reverse('duplicate_schedule_template', args=[template.pk])]
        for user in (self.ordinary, self.leader):
            self.client.force_login(user)
            for url in urls:
                with self.subTest(user=user.username, url=url):
                    self.assertEqual(self.client.get(url).status_code, 403)
                    self.assertEqual(self.post_plan(self.payload(), url).status_code, 403)
        for user in (self.admin, self.superuser):
            self.client.force_login(user)
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, 200)
        self.client.force_login(self.ordinary)
        self.assertEqual(self.client.get(reverse('schedule_details2')).status_code, 403)
        self.assertEqual(self.client.post(reverse('create_dated_schedule', args=[template.pk]), self.dated_payload(template)).status_code, 403)

    @patch('ounissi_app.views.generate_schedule_items_for_stock_operations')
    def test_leader_creates_dated_trt_for_a_different_team_and_preserves_seconds(self, stock):
        template = self.make_template()
        self.client.force_login(self.leader)
        self.assertContains(self.client.get(reverse('schedule_details_with_id', args=[template.pk])), 'Autre équipe')
        self.assertNotContains(self.client.get(reverse('schedule_details_with_id', args=[template.pk])), 'Modifier</a>')
        data = self.dated_payload(template)
        url = reverse('create_dated_schedule', args=[template.pk])
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        dated = Schedule.objects.get(is_template=False)
        item = ScheduleItem.objects.get(schedule=dated)
        self.assertEqual(item.employee_id, self.worker.pk)
        self.assertEqual(item.assigned_time, timedelta(seconds=495))
        self.assertEqual(item.task_identifier, 'A-01')
        self.assertEqual(ScheduleTask.objects.get(schedule=dated).total_assigned_time, timedelta(seconds=495))
        self.assertEqual(ScheduleEmployee.objects.get(schedule=dated).total_assigned_time, timedelta(seconds=495))
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(Schedule.objects.filter(is_template=False).count(), 1)
        self.assertEqual(stock.call_count, 1)

    @patch('ounissi_app.views.generate_schedule_items_for_stock_operations')
    def test_create_uses_integer_microseconds_and_recomputes_totals(self, stock):
        payload = self.payload()
        payload['tasks'][0]['total_us'] += 1
        payload['items'][0]['assigned_us'] += 1
        payload['tasks'][0]['total_assigned_time'] = 0  # untrusted aggregate is ignored
        payload['employees'][0]['total_assigned_hours'] = 0
        response = self.post_plan(payload)
        self.assertEqual(response.status_code, 200, response.content)
        template = Schedule.objects.get(is_template=True)
        self.assertEqual(ScheduleTask.objects.get(schedule=template).total_time, timedelta(seconds=495, microseconds=1))
        self.assertEqual(ScheduleTask.objects.get(schedule=template).total_assigned_time, timedelta(seconds=495, microseconds=1))
        self.assertEqual(ScheduleEmployee.objects.get(schedule=template).total_assigned_time, timedelta(seconds=495, microseconds=1))
        self.assertEqual(ScheduleItem.objects.get(schedule=template).assigned_time, timedelta(seconds=495, microseconds=1))
        self.assertEqual(self.post_plan(payload).json()['replayed'], True)
        self.assertEqual(Schedule.objects.filter(is_template=True).count(), 1)
        self.assertEqual(stock.call_count, 1)
        self.assertEqual(ScheduleCreationRequest.objects.count(), 1)

    @patch('ounissi_app.views.generate_schedule_items_for_stock_operations')
    def test_warnings_must_be_acknowledged_but_unassigned_work_is_not_discarded(self, stock):
        payload = self.payload()
        payload['items'] = []
        response = self.post_plan(payload)
        self.assertEqual(response.status_code, 422)
        self.assertIn('avertissement', response.json()['message'])
        self.assertFalse(Schedule.objects.exists())
        payload['confirm_warnings'] = True
        response = self.post_plan(payload)
        self.assertEqual(response.status_code, 200, response.content)
        template = Schedule.objects.get(is_template=True)
        self.assertEqual(ScheduleTask.objects.get(schedule=template).total_time, timedelta(seconds=495))
        self.assertEqual(ScheduleTask.objects.get(schedule=template).total_assigned_time, timedelta())
        self.assertFalse(ScheduleItem.objects.filter(schedule=template).exists())
        response = self.client.get(reverse('schedule_details_with_id', args=[template.pk]))
        self.assertContains(response, 'temps non affecté')
        self.assertContains(response, '00:08:15')
        data = self.dated_payload(template)
        url = reverse('create_dated_schedule', args=[template.pk])
        self.assertEqual(self.client.post(url, data).status_code, 422)
        data['confirm_warnings'] = 'yes'
        self.assertEqual(self.client.post(url, data).status_code, 302)
        dated = Schedule.objects.get(is_template=False)
        self.assertEqual(ScheduleTask.objects.get(schedule=dated).total_time, timedelta(seconds=495))
        self.assertFalse(ScheduleItem.objects.filter(schedule=dated).exists())
        selected_page = self.client.get(reverse('schedule_details_selected', args=[dated.pk]))
        self.assertContains(selected_page, 'temps prévu sans affectation')
        self.assertContains(selected_page, '0:08:15')
        with patch('ounissi_app.schedule_service.get_current_active_schedule', return_value=dated):
            active_page = self.client.get(reverse('actual_schedule_details'))
        self.assertContains(active_page, 'temps prévu sans affectation')
        self.assertContains(active_page, '0:08:15')

    @patch('ounissi_app.views.generate_schedule_items_for_stock_operations')
    def test_manual_over_capacity_requires_explicit_confirmation(self, stock):
        payload = self.payload()
        payload['employees'][0]['capacity_us'] = 30 * S
        response = self.post_plan(payload)
        self.assertEqual(response.status_code, 422)
        self.assertIn('capacité', response.json()['message'])
        payload['confirm_warnings'] = True
        self.assertEqual(self.post_plan(payload).status_code, 200)
        template = Schedule.objects.get(is_template=True)
        self.assertEqual(ScheduleEmployee.objects.get(schedule=template).total_assigned_time, timedelta(seconds=495))
        self.assertEqual(ScheduleEmployee.objects.get(schedule=template).working_hours, timedelta(seconds=30))

    @patch('ounissi_app.views.generate_schedule_items_for_stock_operations')
    def test_invalid_or_forged_plans_cannot_create_partial_data(self, stock):
        bad_payloads = []
        p = self.payload(); p['items'][0]['assigned_us'] = -S; bad_payloads.append(p)
        p = self.payload(); p['items'][0]['assigned_us'] = True; bad_payloads.append(p)
        p = self.payload(); p['employees'][0]['id'] = self.unqualified.pk; p['employees'][0]['rank'] = 5; p['items'][0]['employee_id'] = self.unqualified.pk; bad_payloads.append(p)
        p = self.payload(); p['employees'][0]['rank'] = 5; bad_payloads.append(p)
        p = self.payload(); p['items'][0]['employee_id'] = 999999; bad_payloads.append(p)
        p = self.payload(); p['tasks'] = []; bad_payloads.append(p)
        p = self.payload(); p['tasks'].append(dict(p['tasks'][0])); bad_payloads.append(p)
        p = self.payload(); p['items'][0]['id'] = 12345; bad_payloads.append(p)
        p = self.payload(); p['name'] = 'Transfer_Schedule'; bad_payloads.append(p)
        p = self.payload(); p['submission_key'] = 'invalid'; bad_payloads.append(p)
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                response = self.post_plan(payload)
                self.assertIn(response.status_code, (409, 422), response.content)
                self.assertEqual(Schedule.objects.count(), 0)
                self.assertEqual(ScheduleCreationRequest.objects.count(), 0)
        self.assertEqual(self.client.post(reverse('create_schedule'), {'plan': '{'}).status_code, 422)
        stock.assert_not_called()

    @patch('ounissi_app.views.generate_schedule_items_for_stock_operations', side_effect=RuntimeError('offline details'))
    def test_stock_failure_rolls_back_creation_and_does_not_expose_error(self, stock):
        with self.assertLogs('ounissi_app.planning_views', level='ERROR'):
            response = self.post_plan(self.payload())
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('offline details', response.content.decode())
        self.assertFalse(Schedule.objects.exists())
        self.assertFalse(ScheduleCreationRequest.objects.exists())

    @patch('ounissi_app.views.generate_schedule_items_for_stock_operations')
    def test_edit_preserves_item_identity_and_never_changes_dated_trts(self, stock):
        template = self.make_template()
        original_item = ScheduleItem.objects.get(schedule=template)
        data = self.dated_payload(template)
        self.assertEqual(self.client.post(reverse('create_dated_schedule', args=[template.pk]), data).status_code, 302)
        dated = Schedule.objects.get(is_template=False)
        stock.reset_mock()
        payload = self.edit_payload(template)
        payload['name'] = 'Modèle modifié'
        payload['tasks'][0]['total_us'] = 500 * S
        payload['items'][0]['assigned_us'] = 500 * S
        response = self.post_plan(payload, reverse('edit_schedule_template', args=[template.pk]))
        self.assertEqual(response.status_code, 200, response.content)
        template.refresh_from_db()
        self.assertEqual(template.schedule_name, 'Modèle modifié')
        self.assertEqual(ScheduleItem.objects.get(schedule=template).pk, original_item.pk)
        self.assertEqual(ScheduleItem.objects.get(schedule=template).task_identifier, 'A-01')
        self.assertEqual(ScheduleItem.objects.get(schedule=dated).assigned_time, timedelta(seconds=495))
        self.assertEqual(ScheduleTask.objects.get(schedule=dated).total_time, timedelta(seconds=495))
        stock.assert_not_called()

    @patch('ounissi_app.views.generate_schedule_items_for_stock_operations')
    def test_duplicate_is_independent_and_runs_existing_stock_generator(self, stock):
        template = self.make_template()
        draft = planning_service.draft_from_snapshot(planning_service.load_snapshot(template), duplicate=True)
        draft['tasks'][0].update(priority=1, rank=2)
        draft['employees'][0].update(rank=1)
        draft.update(submission_key=str(uuid.uuid4()), confirm_warnings=False)
        self.assertContains(self.client.get(reverse('duplicate_schedule_template', args=[template.pk])), 'Copie')
        response = self.post_plan(draft, reverse('duplicate_schedule_template', args=[template.pk]))
        self.assertEqual(response.status_code, 200, response.content)
        copy = Schedule.objects.exclude(pk=template.pk).get(is_template=True)
        self.assertEqual(copy.schedule_name, 'Copie — Original')
        self.assertEqual(ScheduleItem.objects.get(schedule=copy).task_identifier, 'A-01')
        self.assertNotEqual(ScheduleItem.objects.get(schedule=copy).pk, ScheduleItem.objects.get(schedule=template).pk)
        self.assertEqual(ScheduleItem.objects.get(schedule=template).assigned_time, timedelta(seconds=495))
        stock.assert_called_once_with(copy)

    @patch('ounissi_app.views.generate_schedule_items_for_stock_operations')
    def test_concurrent_edit_and_changed_eligibility_do_not_overwrite(self, stock):
        template = self.make_template()
        payload = self.edit_payload(template)
        ScheduleItem.objects.filter(schedule=template).update(assigned_time=timedelta(seconds=450))
        response = self.post_plan(payload, reverse('edit_schedule_template', args=[template.pk]))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(ScheduleItem.objects.get(schedule=template).assigned_time, timedelta(seconds=450))
        data = self.dated_payload(template)
        Task.objects.filter(pk=self.task.pk).update(priority=2)
        response = self.client.post(reverse('create_dated_schedule', args=[template.pk]), data)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(Schedule.objects.filter(is_template=False).exists())
        stock.assert_not_called()

    @patch('ounissi_app.views.generate_schedule_items_for_stock_operations')
    def test_linked_reception_is_not_cascaded_by_template_edit(self, stock):
        template = self.make_template()
        item = ScheduleItem.objects.get(schedule=template)
        reception = Reception.objects.create(number_of_packs=1, related_item=item)
        payload = self.edit_payload(template)
        payload['items'] = []
        payload['confirm_warnings'] = True
        payload['acknowledged_removed_item_ids'] = [item.pk]
        response = self.post_plan(payload, reverse('edit_schedule_template', args=[template.pk]))
        self.assertEqual(response.status_code, 422, response.content)
        self.assertTrue(Reception.objects.filter(pk=reception.pk, related_item=item).exists())
        self.assertTrue(ScheduleItem.objects.filter(pk=item.pk).exists())
        stock.assert_not_called()

    def test_legacy_template_mutators_are_not_bypasses(self):
        template = self.make_template()
        item = ScheduleItem.objects.get(schedule=template)
        self.client.force_login(self.leader)
        self.assertEqual(self.client.get(reverse('scheduleItem_update', args=[item.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse('scheduleItem_update', args=[item.pk]), {}).status_code, 403)
        self.assertEqual(self.client.post(reverse('add_or_edit_schedule_item'), {'schedule': template.pk}).status_code, 403)
        for route in ('update_schedule_now', 'finalize_schedule', 'unfinished_tasks'):
            self.assertEqual(self.client.post(reverse(route, args=[template.pk])).status_code, 404)
        dated = Schedule.objects.create(is_template=False, date=date(2026, 11, 7), start_time=time(8), end_time=time(14))
        response = self.client.post(reverse('import_tasks'), {'actual_schedule': dated.pk, 'transfer_items': [item.pk]})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(ScheduleItem.objects.filter(pk=item.pk).exists())
        self.client.force_login(self.admin)
        self.assertRedirects(self.client.get(reverse('scheduleItem_update', args=[item.pk])), reverse('edit_schedule_template', args=[template.pk]), fetch_redirect_response=False)
        self.assertEqual(self.client.post(reverse('add_or_edit_schedule_item'), {'schedule': template.pk}).status_code, 400)

    def test_csrf_is_enforced_for_template_and_dated_creation(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        template = self.make_template()
        self.assertEqual(client.post(reverse('create_schedule'), {'plan': json.dumps(self.payload())}).status_code, 403)
        self.assertEqual(client.post(reverse('create_dated_schedule', args=[template.pk]), self.dated_payload(template)).status_code, 403)

    def test_internal_transfer_queue_is_not_a_template_choice(self):
        transfer = Schedule.objects.create(schedule_name='Transfer_Schedule', is_template=True, start_time=time(0), end_time=time(23, 59))
        response = self.client.get(reverse('schedule_details2'))
        self.assertNotContains(response, 'Transfer_Schedule')
        for route in ('edit_schedule_template', 'duplicate_schedule_template'):
            self.assertEqual(self.client.get(reverse(route, args=[transfer.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('create_dated_schedule', args=[transfer.pk]), {}).status_code, 404)

    def test_inconsistent_legacy_data_is_not_silently_deleted(self):
        template = self.make_template()
        ScheduleTask.objects.create(schedule=template, task=self.task, total_time=timedelta(seconds=5))
        response = self.client.get(reverse('edit_schedule_template', args=[template.pk]))
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, 'plusieurs totaux', status_code=422)
        self.assertEqual(ScheduleTask.objects.filter(schedule=template).count(), 2)

    @patch('ounissi_app.views.generate_schedule_items_for_stock_operations')
    def test_unassigned_legacy_item_is_preserved_and_requires_confirmation(self, stock):
        template = self.make_template()
        ScheduleItem.objects.create(schedule=template, task=self.task, employee=None,
                                    assigned_time=timedelta(seconds=3), task_identifier='Unassigned#1')
        response = self.client.get(reverse('schedule_details_with_id', args=[template.pk]))
        self.assertContains(response, 'sans employé')
        data = self.dated_payload(template, confirmed=True)
        self.assertEqual(self.client.post(reverse('create_dated_schedule', args=[template.pk]), data).status_code, 302)
        dated = Schedule.objects.get(is_template=False)
        pending = ScheduleItem.objects.get(schedule=dated, employee__isnull=True)
        self.assertEqual(pending.assigned_time, timedelta(seconds=3))
        self.assertEqual(pending.task_identifier, 'Unassigned#1')

    @patch('ounissi_app.views.generate_schedule_items_for_stock_operations')
    def test_edit_requires_acknowledgement_before_removing_existing_rows(self, stock):
        template = self.make_template()
        item = ScheduleItem.objects.get(schedule=template)
        payload = self.edit_payload(template)
        payload['items'] = []
        payload['confirm_warnings'] = True
        url = reverse('edit_schedule_template', args=[template.pk])
        response = self.post_plan(payload, url)
        self.assertEqual(response.status_code, 422)
        self.assertIn('suppression', response.json()['message'])
        self.assertTrue(ScheduleItem.objects.filter(pk=item.pk).exists())
        payload['acknowledged_removed_item_ids'] = [item.pk]
        self.assertEqual(self.post_plan(payload, url).status_code, 200)
        self.assertFalse(ScheduleItem.objects.filter(pk=item.pk).exists())
        stock.assert_not_called()

    @patch('ounissi_app.views.generate_schedule_items_for_stock_operations')
    def test_drifted_totals_are_disclosed_and_recomputed_on_copy(self, stock):
        template = self.make_template()
        ScheduleTask.objects.filter(schedule=template).update(total_assigned_time=timedelta(seconds=1))
        editor = self.client.get(reverse('edit_schedule_template', args=[template.pk]))
        self.assertContains(editor, 'totaux enregistrés de ce modèle ne correspondent pas')
        page = self.client.get(reverse('schedule_details_with_id', args=[template.pk]))
        self.assertContains(page, 'anciens totaux enregistrés ne correspondent pas')
        data = self.dated_payload(template)
        url = reverse('create_dated_schedule', args=[template.pk])
        self.assertEqual(self.client.post(url, data).status_code, 422)
        data['confirm_warnings'] = 'yes'
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(ScheduleTask.objects.get(schedule__is_template=False).total_assigned_time, timedelta(seconds=495))
        self.assertEqual(ScheduleTask.objects.get(schedule=template).total_assigned_time, timedelta(seconds=1))

    @patch('ounissi_app.views.generate_schedule_items_for_stock_operations')
    def test_changed_template_requires_a_fresh_warning_acknowledgement(self, stock):
        template = self.make_template(seconds=600, assigned=495)
        data = self.dated_payload(template, confirmed=True)
        ScheduleTask.objects.filter(schedule=template).update(total_time=timedelta(seconds=700))
        response = self.client.post(reverse('create_dated_schedule', args=[template.pk]), data)
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, 'Ce modèle a changé', status_code=409)
        html = response.content.decode()
        checkbox = html.split('name="confirm_warnings"', 1)[1].split('>', 1)[0]
        self.assertNotIn('checked', checkbox)
        self.assertFalse(Schedule.objects.filter(is_template=False).exists())

    @patch('ounissi_app.views.generate_schedule_items_for_stock_operations')
    def test_zero_assigned_duration_is_preserved_but_warned(self, stock):
        payload = self.payload(seconds=0)
        response = self.post_plan(payload)
        self.assertEqual(response.status_code, 422)
        self.assertIn('durée nulle', response.json()['message'])
        payload['confirm_warnings'] = True
        self.assertEqual(self.post_plan(payload).status_code, 200)
        self.assertEqual(ScheduleItem.objects.get().assigned_time, timedelta())
        self.assertContains(self.client.get(reverse('schedule_details_with_id', args=[Schedule.objects.get().pk])), 'durée nulle')

    @patch('ounissi_app.views.get_emplacement_item_count', return_value=11)
    def test_stock_generation_is_still_called_on_both_creation_paths(self, stock_count):
        stock_task = Task.objects.create(task_name='Stock', priority=2, responsibility_level=self.rank2,
                                        average_duration=timedelta(seconds=45))
        operation = StockOperation.objects.create(designation='Comptage', duration=timedelta(seconds=45), task=stock_task)
        today = date.today()
        AssignationEmplacement.objects.create(emplacement='E1', employee=self.worker, operation=operation,
                                              date_debut=today, date_fin=today)
        self.assertEqual(self.post_plan(self.payload()).status_code, 200)
        template = Schedule.objects.get(is_template=True)
        generated = ScheduleItem.objects.get(schedule=template, task=stock_task)
        self.assertEqual(generated.assigned_time, timedelta(seconds=495))
        self.assertEqual(generated.task_identifier, ': E1')
        tomorrow = today + timedelta(days=1)
        data = self.dated_payload(template, day=tomorrow.isoformat(), confirmed=True)
        response = self.client.post(reverse('create_dated_schedule', args=[template.pk]), data)
        self.assertEqual(response.status_code, 302, response.content)
        dated = Schedule.objects.get(is_template=False)
        # Legacy stock regeneration still uses today's assignment and adds to
        # the copied stock row. The copied seconds and identifier are exact.
        generated = ScheduleItem.objects.get(schedule=dated, task=stock_task)
        self.assertEqual(generated.assigned_time, timedelta(seconds=990))
        self.assertEqual(generated.task_identifier, ': E1')
        self.assertEqual(stock_count.call_count, 2)

    @patch('ounissi_app.views.generate_schedule_items_for_stock_operations')
    def test_large_template_can_be_created_and_browsed_in_pages(self, stock):
        count = 1050  # above SQLite's traditional 999-parameter limit
        Task.objects.bulk_create([Task(task_name=f'Tâche {i}', priority=1, responsibility_level=self.rank2) for i in range(count)])
        Employee.objects.bulk_create([Employee(first_name=f'Employé {i}', rank=self.rank1) for i in range(count)])
        task_ids = list(Task.objects.filter(task_name__startswith='Tâche ').order_by('pk').values_list('pk', flat=True))
        employee_ids = list(Employee.objects.filter(first_name__startswith='Employé ').order_by('pk').values_list('pk', flat=True))
        self.assertEqual(len(task_ids), count)
        self.assertEqual(len(employee_ids), count)
        payload = self.payload()
        payload['tasks'] = [{'id': pk, 'total_us': S, 'priority': 1, 'rank': 2} for pk in task_ids]
        payload['employees'] = [{'id': pk, 'capacity_us': S, 'rank': 1} for pk in employee_ids]
        payload['items'] = [{'id': None, 'task_id': t, 'employee_id': e, 'assigned_us': S, 'identifier': None}
                            for t, e in zip(task_ids, employee_ids)]
        response = self.post_plan(payload)
        self.assertEqual(response.status_code, 200, response.content)
        template = Schedule.objects.get(is_template=True)
        self.assertEqual(ScheduleItem.objects.filter(schedule=template).count(), count)
        page = self.client.get(reverse('schedule_details_with_id', args=[template.pk]), {'page': 2})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context['item_page'].paginator.count, count)
        self.assertEqual(len(page.context['item_page'].object_list), 50)
        stock.assert_called_once()

    def test_invalid_model_selector_is_a_404_not_a_server_error(self):
        self.assertEqual(self.client.get(reverse('schedule_details2'), {'schedule': 'invalid'}).status_code, 404)
        self.assertEqual(self.client.post(reverse('schedule_details2'), {'schedule': 'invalid'}).status_code, 404)

    def test_model_selection_redirects_to_a_filterable_canonical_url(self):
        template = self.make_template()
        response = self.client.get(reverse('schedule_details2'), {'schedule': template.pk, 'q': 'Ventes'})
        self.assertRedirects(response, reverse('schedule_details_with_id', args=[template.pk]) + '?q=Ventes')
        page = self.client.get(response.url)
        self.assertEqual(page.context['item_page'].paginator.count, 1)
        self.assertContains(page, 'Ventes 45 secondes')

    def test_superuser_gets_the_same_trt_manager_role(self):
        self.client.force_login(self.superuser)
        template = self.make_template(seconds=600, assigned=495)
        response = self.client.get(reverse('schedule_details_selected', args=[template.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['can_manage_trt'])
        self.assertContains(response, 'temps prévu sans affectation')

    def test_arbitrary_sql_endpoints_cannot_bypass_template_permissions(self):
        template = self.make_template()
        sql = f"UPDATE ounissi_app_schedule SET schedule_name='Bypass' WHERE id={template.pk}"
        query = Query.objects.create(name='Dangerous test query', type=0, query=sql, parameters={})
        for user in (self.ordinary, self.leader):
            self.client.force_login(user)
            self.assertEqual(self.client.post(reverse('test_query_submission'), {'type': '0', 'queryText': sql}).status_code, 403)
            self.assertEqual(self.client.post(reverse('execute_query'), {'query': query.pk}).status_code, 403)
            self.assertEqual(self.client.post(reverse('create_query'), {'queryName': 'Forged', 'type': '0', 'queryText': sql}).status_code, 403)
            self.assertEqual(self.client.get(reverse('test_queries')).status_code, 403)
            self.assertEqual(self.client.get(reverse('get_query_details', args=[query.pk])).status_code, 403)
        template.refresh_from_db()
        self.assertEqual(template.schedule_name, 'Original')
        self.assertFalse(Query.objects.filter(name='Forged').exists())

    def test_django_admin_permissions_do_not_bypass_template_authoring(self):
        from django.contrib.auth.models import Permission
        template = self.make_template()
        item = ScheduleItem.objects.get(schedule=template)
        task_row = ScheduleTask.objects.get(schedule=template)
        employee_row = ScheduleEmployee.objects.get(schedule=template)
        user = self.leader
        user.is_staff = True
        user.save(update_fields=['is_staff'])
        user.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='ounissi_app',
            codename__in=[f'{action}_{model}' for action in ('view', 'add', 'change', 'delete')
                          for model in ('schedule', 'scheduleitem', 'scheduletask', 'scheduleemployee', 'query')],
        ))
        self.client.force_login(user)
        for obj, model in ((template, 'schedule'), (item, 'scheduleitem'), (task_row, 'scheduletask'), (employee_row, 'scheduleemployee')):
            url = reverse(f'admin:ounissi_app_{model}_change', args=[obj.pk])
            self.assertEqual(self.client.post(url, {} ).status_code, 403, model)
            url = reverse(f'admin:ounissi_app_{model}_delete', args=[obj.pk])
            self.assertEqual(self.client.post(url, {'post': 'yes'}).status_code, 403, model)
        self.assertEqual(self.client.get(reverse('admin:ounissi_app_query_add')).status_code, 403)
        add_item = self.client.get(reverse('admin:ounissi_app_scheduleitem_add'))
        self.assertEqual(add_item.status_code, 200)
        self.assertNotContains(add_item, 'Original')
        add_schedule = self.client.get(reverse('admin:ounissi_app_schedule_add'))
        self.assertEqual(add_schedule.status_code, 200)
        self.assertNotContains(add_schedule, 'name="is_template"')
        self.assertTrue(Schedule.objects.filter(pk=template.pk, is_template=True).exists())
        self.assertTrue(ScheduleItem.objects.filter(pk=item.pk).exists())

    def test_non_manager_staff_cannot_add_a_schedule_in_django_admin(self):
        from django.contrib.auth.models import Permission
        user = self.ordinary
        user.is_staff = True
        user.save(update_fields=['is_staff'])
        user.user_permissions.add(Permission.objects.get(content_type__app_label='ounissi_app', codename='add_schedule'))
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse('admin:ounissi_app_schedule_add')).status_code, 403)

    def test_django_admin_cascade_and_bulk_deletes_cannot_delete_template_rows(self):
        from django.contrib.auth.models import Permission
        template = self.make_template()
        self.leader.is_staff = True
        self.leader.save(update_fields=['is_staff'])
        self.leader.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='ounissi_app',
            codename__in=['delete_task', 'view_task', 'delete_schedule', 'view_schedule',
                          'delete_scheduleitem', 'delete_scheduletask', 'delete_scheduleemployee'],
        ))
        self.client.force_login(self.leader)
        task_delete = self.client.post(reverse('admin:ounissi_app_task_delete', args=[self.task.pk]), {'post': 'yes'})
        self.assertEqual(task_delete.status_code, 403)
        bulk = self.client.post(reverse('admin:ounissi_app_schedule_changelist'), {
            'action': 'delete_selected', '_selected_action': [template.pk], 'post': 'yes',
        })
        self.assertEqual(bulk.status_code, 403)
        self.assertTrue(Task.objects.filter(pk=self.task.pk).exists())
        self.assertTrue(Schedule.objects.filter(pk=template.pk).exists())
        self.assertTrue(ScheduleItem.objects.filter(schedule=template).exists())

    @patch('ounissi_app.forms.XpertConnect')
    def test_reception_submission_cannot_write_into_a_template(self, connector):
        template = self.make_template()
        Query.objects.create(name='LISTE FOURNISSEURS', type=1, query='select 1', parameters={})
        reception_query = Query.objects.create(name='Réception modèle', type=0, query='select 1', parameters={'model_name': 'Reception'})
        self.task.progress_query = reception_query
        self.task.menu = True
        self.task.save(update_fields=['progress_query', 'menu'])
        connector.return_value.run_query.return_value = {'columns': ['SupplierName'], 'data_rows': [['Fournisseur']]}
        employee = Employee.objects.get(user=self.ordinary)
        self.client.force_login(self.ordinary)
        data = {
            'supplier': 'Fournisseur', 'ref_tiers': 'FACT-1', 'number_of_packs': 3,
            'cold_chain': 'False', 'psycho': 'False', 'correct_destination': 'False', 'products_type': 'True',
            'transfer_data': json.dumps({'task_id': self.task.pk, 'employee_id': employee.pk, 'schedule_id': template.pk}),
        }
        response = self.client.post(reverse('generic_form_submission', args=['ReceptionForm']), data)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Reception.objects.exists())
        self.assertEqual(ScheduleItem.objects.filter(schedule=template).count(), 1)

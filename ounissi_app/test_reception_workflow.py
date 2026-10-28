import json
from datetime import time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Employee, Rank, Team
from . import schedule_service
from .models import (
    Query, Reception, ReceptionProduit, ReceptionVignette, Saisie, Schedule,
    ScheduleEmployee, ScheduleItem, ScheduleTask, Task, TaskGeneration, TaskType,
)


class ReceptionWorkflowTests(TestCase):
    """Exercise the original TRT-based reception chain, not the V3 prototype."""

    @classmethod
    def setUpTestData(cls):
        cls.manager_user = get_user_model().objects.create_user(username='reception-manager', password='test')
        cls.manager_user.groups.add(Group.objects.get_or_create(name='Admin')[0])
        cls.employee_user = get_user_model().objects.create_user(username='reception-employee', password='test')
        cls.rank = Rank.objects.create(value=2, label='Réception')
        cls.employee = Employee.objects.create(
            user=cls.employee_user, first_name='Employé', last_name='Test', rank=cls.rank, xp_id='EMP-XP',
        )
        cls.other_employee = Employee.objects.create(first_name='Autre', rank=cls.rank, xp_id='OTHER-XP')
        cls.schedule = Schedule.objects.create(date=timezone.localdate(), start_time=time(0), end_time=time(23, 59))
        ScheduleEmployee.objects.create(employee=cls.employee, schedule=cls.schedule)
        ScheduleEmployee.objects.create(employee=cls.other_employee, schedule=cls.schedule)

        cls.supplier_query = Query.objects.create(name='LISTE FOURNISSEURS', type=1, query='suppliers', parameters={})
        cls.invoice_query = Query.objects.create(
            name='InvoiceHelper', type=1, query='invoice', parameters={'supplier': '%%', 'ref_tiers': '%%'},
        )
        menu_type = TaskType.objects.create(name='APRES SAISIE')
        auto_type = TaskType.objects.create(name='AUTO')
        manual_type = TaskType.objects.create(name='MANUEL')
        models = ['Reception', 'Saisie', 'ReceptionProduit', 'ReceptionVignette', 'ReceptionPlacement']
        cls.tasks = []
        for i, model_name in enumerate(models):
            params = {'model_name': model_name}
            if model_name == 'Saisie':
                params.update(supplier='%%', ref_tiers='%%', start_date='2000-01-01', end_date='2100-01-01', xp_id='%%')
            query = Query.objects.create(
                name=f'Progress {model_name}', type=1 if model_name == 'Saisie' else 0,
                query='saisie-progress' if model_name == 'Saisie' else 'N/A', parameters=params,
            )
            task = Task.objects.create(
                task_name=f'COMMANDE {i + 1}/5: {model_name}',
                task_type=menu_type if i == 0 else auto_type if i == 1 else manual_type,
                responsibility_level=cls.rank, average_duration=timedelta(seconds=45 if i == 1 else 30),
                progress_query=query, menu=(i == 0),
            )
            cls.tasks.append(task)
        for previous, current in zip(cls.tasks, cls.tasks[1:]):
            TaskGeneration.objects.create(name=f'{previous.id}-{current.id}', parent_task=previous, generated_task=current)

    def create_reception(self, reference='FACT-123'):
        self.client.force_login(self.employee_user)
        with patch('ounissi_app.forms.XpertConnect') as connector:
            connector.return_value.run_query.return_value = {'columns': ['SupplierName'], 'data_rows': [['Fournisseur Test']]}
            form_response = self.client.post(reverse('get_model_form'), {
                'task_id': self.tasks[0].pk, 'employee_id': self.employee.pk, 'selected_schedule': self.schedule.pk,
            })
            self.assertEqual(form_response.status_code, 200)
            response = self.client.post(reverse('generic_form_submission', args=['ReceptionForm']), {
                'supplier': 'Fournisseur Test', 'ref_tiers': reference, 'number_of_packs': 2,
                'cold_chain': 'False', 'psycho': 'False', 'correct_destination': 'True', 'products_type': 'False',
                'transfer_data': json.dumps({'task_id': self.tasks[0].pk, 'employee_id': self.employee.pk, 'schedule_id': self.schedule.pk}),
            }, HTTP_REFERER='/')
        self.assertEqual(response.status_code, 302)
        self.reception_response = response
        return Reception.objects.get(ref_tiers=reference)

    def assign_saisie(self, reception, employee=None):
        employee = employee or self.employee
        pending = ScheduleItem.objects.get(schedule=self.schedule, task=self.tasks[1], employee__isnull=True)
        self.client.force_login(self.manager_user)
        response = self.client.post(reverse('form_saisie'), {
            'supplier': reception.supplier, 'ref_tiers': reception.ref_tiers, 'origine': reception.pk,
            'number_of_lines': 20, 'employee1': employee.pk, 'employee1_schedule_item': pending.pk,
            'employee1_range_start': 1, 'employee1_range_end': 20,
        }, HTTP_REFERER='/schedule/actual/')
        self.assertEqual(response.status_code, 302)
        self.assignment_response = response
        pending.refresh_from_db()
        return pending

    def test_employee_reception_creates_unassigned_saisie_visible_to_manager(self):
        reception = self.create_reception()
        root = reception.related_item
        self.assertEqual(root.employee, self.employee)
        self.assertEqual(root.progression_rate, 100)
        pending = ScheduleItem.objects.get(schedule=self.schedule, task=self.tasks[1], employee__isnull=True)
        self.assertEqual(pending.task_identifier, 'Fournisseur Test#FACT-123')
        self.client.force_login(self.manager_user)
        with patch('ounissi_app.schedule_service.get_current_active_schedule', return_value=self.schedule):
            response = self.client.get(reverse('actual_schedule_details'))
        self.assertContains(response, self.tasks[1].task_name)
        self.assertIn(pending.pk, [item.pk for item in response.context['non_assigned_items']])
        fragment = self.client.post(reverse('get_special_form'), {'task_id': self.tasks[1].pk, 'schedule_item': pending.pk})
        self.assertContains(fragment, 'Nombre de Lignes')
        self.assertContains(fragment, f'value="{reception.pk}"')
        self.assertContains(fragment, 'Fournisseur Test')

    def test_future_steps_can_be_assigned_before_previous_completion(self):
        reception = self.create_reception()
        saisie_item = self.assign_saisie(reception)
        self.assertEqual(saisie_item.progression_rate, 0)
        self.assertEqual(saisie_item.assigned_time, timedelta(seconds=20 * 45))
        pending = ScheduleItem.objects.get(schedule=self.schedule, task=self.tasks[2], employee__isnull=True)
        with patch('ounissi_app.schedule_service.get_current_active_schedule', return_value=self.schedule):
            response = self.client.get(reverse('actual_schedule_details'))
        self.assertIn(pending.pk, [item.pk for item in response.context['non_assigned_items']])
        self.assertContains(response, self.tasks[2].task_name)

    @patch('ounissi_app.views.get_query_results')
    def test_future_assignment_does_not_depend_on_invoice_being_available_yet(self, run_query):
        reception = self.create_reception()
        self.assign_saisie(reception)
        pending = ScheduleItem.objects.get(schedule=self.schedule, task=self.tasks[2], employee__isnull=True)
        for result in (
            {'columns': ['LIGNE', 'DESIGNATION', 'LOT', 'QUANTITE'], 'data_rows': []},
            {'error': 'private connection details'},
        ):
            with self.subTest(result=result):
                run_query.return_value = result
                response = self.client.post(reverse('get_special_form'), {'task_id': self.tasks[2].pk, 'schedule_item': pending.pk})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'number_of_lines')
                self.assertContains(response, '20')
                self.assertContains(response, 'facture')
                self.assertNotContains(response, 'private connection details')

    def test_original_task_is_scoped_to_schedule_and_exact_reference(self):
        old_schedule = Schedule.objects.create(date=self.schedule.date - timedelta(days=1), start_time=time(0), end_time=time(23, 59))
        ScheduleItem.objects.create(schedule=old_schedule, task=self.tasks[0], task_identifier='Fournisseur Test#FACT-123')
        ScheduleItem.objects.create(schedule=self.schedule, task=self.tasks[0], task_identifier='Fournisseur Test#FACT-1234')
        original = ScheduleItem.objects.create(schedule=self.schedule, task=self.tasks[0], task_identifier='Fournisseur Test#FACT-123')
        pending = ScheduleItem.objects.create(schedule=self.schedule, task=self.tasks[1], task_identifier='Fournisseur Test#FACT-123')
        self.assertEqual(schedule_service.get_original_item(pending), original)

    def test_generation_is_idempotent_for_one_reception(self):
        parent = ScheduleItem.objects.create(
            schedule=self.schedule, task=self.tasks[0], employee=self.employee, task_identifier='Fournisseur Test#FACT-123',
        )
        schedule_service.generate_group_task(parent, self.schedule)
        schedule_service.generate_group_task(parent, self.schedule)
        self.assertEqual(ScheduleItem.objects.filter(schedule=self.schedule, task=self.tasks[1], task_identifier=parent.task_identifier).count(), 1)

    @patch('ounissi_app.schedule_service.run_raw_query')
    def test_saisie_progress_uses_invoice_employee_and_per_line_duration(self, run_query):
        reception = self.create_reception()
        saisie_item = self.assign_saisie(reception)
        run_query.return_value = {
            'columns': ['OP_DATE', 'OP_USER', 'NBR_LINES', 'TOTAL_TIME', 'TYPE_DOC', 'REF_TIERS', 'SupplierName'],
            'data_rows': [[self.schedule.date, 'EMP-XP', 11, 495, 'LF', reception.ref_tiers, reception.supplier]],
        }
        schedule_service.get_item_progress(saisie_item.pk)
        saisie_item.refresh_from_db()
        self.assertEqual(saisie_item.progress_lines, 11)
        self.assertEqual(saisie_item.progression_rate, 55)  # 11 × 45 seconds / (20 × 45 seconds)
        params = run_query.call_args.args[1]
        self.assertEqual(params['xp_id'], 'EMP-XP')
        self.assertEqual(params['supplier'], reception.supplier)
        self.assertEqual(params['ref_tiers'], reception.ref_tiers)
        self.assertEqual(params['start_date'], self.schedule.date)
        self.assertEqual(params['end_date'], self.schedule.date + timedelta(days=1))

    @patch('ounissi_app.schedule_service.run_raw_query')
    def test_empty_saisie_result_resets_progress_but_query_failure_preserves_it(self, run_query):
        reception = self.create_reception()
        saisie_item = self.assign_saisie(reception)
        ScheduleItem.objects.filter(pk=saisie_item.pk).update(progress_lines=7, progression_rate=35)
        run_query.return_value = None
        schedule_service.get_item_progress(saisie_item.pk)
        saisie_item.refresh_from_db()
        self.assertEqual((saisie_item.progress_lines, saisie_item.progression_rate), (7, 35))
        run_query.return_value = {'columns': ['NBR_LINES'], 'data_rows': []}
        schedule_service.get_item_progress(saisie_item.pk)
        saisie_item.refresh_from_db()
        self.assertEqual((saisie_item.progress_lines, saisie_item.progression_rate), (0, 0))

    @patch('ounissi_app.views.get_query_results')
    def test_all_remaining_steps_can_be_assigned_in_parallel(self, run_query):
        run_query.return_value = {'columns': ['LIGNE', 'DESIGNATION', 'LOT', 'QUANTITE'], 'data_rows': []}
        reception = self.create_reception()
        saisie_item = self.assign_saisie(reception)
        previous = Saisie.objects.get(employee1_schedule_item=saisie_item)
        stages = [
            (2, 'form_reception_produit', ReceptionProduit),
            (3, 'form_reception_vignette', ReceptionVignette),
        ]
        for task_index, route, model in stages:
            pending = ScheduleItem.objects.get(schedule=self.schedule, task=self.tasks[task_index], employee__isnull=True)
            with patch('ounissi_app.schedule_service.get_current_active_schedule', return_value=self.schedule):
                response = self.client.get(reverse('actual_schedule_details'))
            self.assertIn(pending.pk, [item.pk for item in response.context['non_assigned_items']])
            self.assertContains(response, 'Étape précédente en cours')
            fragment = self.client.post(reverse('get_special_form'), {'task_id': pending.task_id, 'schedule_item': pending.pk})
            self.assertContains(fragment, 'Enregistrer l’affectation')
            response = self.client.post(reverse(route), {
                'supplier': reception.supplier, 'ref_tiers': reception.ref_tiers, 'origine': previous.pk,
                'number_of_lines': 20, 'employee1': self.other_employee.pk, 'employee1_schedule_item': pending.pk,
                'employee1_range_start': 1, 'employee1_range_end': 20,
            }, HTTP_REFERER='/schedule/actual/')
            self.assertEqual(response.status_code, 302)
            pending.refresh_from_db()
            self.assertEqual(pending.employee, self.other_employee)
            self.assertEqual(pending.progression_rate, 0)
            self.assertEqual(pending.task_identifier, 'Fournisseur Test#FACT-123Lines:1<>20')
            previous = model.objects.get(employee1_schedule_item=pending)
        # Placement can also be assigned while all previous automatic/manual tasks remain unfinished.
        from .models import ReceptionPlacement
        pending = ScheduleItem.objects.get(schedule=self.schedule, task=self.tasks[4], employee__isnull=True)
        fragment = self.client.post(reverse('get_special_form'), {'task_id': pending.task_id, 'schedule_item': pending.pk})
        self.assertContains(fragment, 'Enregistrer l’affectation')
        response = self.client.post(reverse('form_reception_placement'), {
            'supplier': reception.supplier, 'ref_tiers': reception.ref_tiers, 'origine': previous.pk,
            'number_of_lines': 20, 'employee1': self.other_employee.pk, 'employee1_schedule_item': pending.pk,
            'employee1_range_start': 1, 'employee1_range_end': 20,
        }, HTTP_REFERER='/schedule/actual/')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ReceptionPlacement.objects.filter(employee1_schedule_item_id=pending.pk).exists())
        saisie_item.refresh_from_db()
        self.assertEqual(saisie_item.progression_rate, 0)

    def test_transferred_step_finds_latest_exact_previous_reception(self):
        old_schedule = Schedule.objects.create(date=self.schedule.date - timedelta(days=2), start_time=time(0), end_time=time(23, 59))
        recent_schedule = Schedule.objects.create(date=self.schedule.date - timedelta(days=1), start_time=time(0), end_time=time(23, 59))
        for schedule in (old_schedule, recent_schedule):
            ScheduleItem.objects.create(schedule=schedule, task=self.tasks[0], task_identifier='Fournisseur Test#FACT-123')
        recent = ScheduleItem.objects.get(schedule=recent_schedule, task=self.tasks[0])
        pending = ScheduleItem.objects.create(schedule=self.schedule, task=self.tasks[1], task_identifier='Fournisseur Test#FACT-123')
        self.assertEqual(schedule_service.get_original_item(pending), recent)

    def test_task_menu_uses_rank_value_not_database_id(self):
        more_senior = Rank.objects.create(value=1, label='Senior')
        query = Query.objects.create(name='Senior form', type=0, query='N/A', parameters={'model_name': 'Reception'})
        Task.objects.create(task_name='Tâche réservée senior', task_type=self.tasks[0].task_type,
                            responsibility_level=more_senior, progress_query=query, menu=True)
        self.client.force_login(self.employee_user)
        with patch('ounissi_app.schedule_service.get_current_active_schedule', return_value=self.schedule):
            response = self.client.get(reverse('home'))
        self.assertContains(response, self.tasks[0].task_name)
        self.assertNotContains(response, 'Tâche réservée senior')

    @patch('ounissi_app.schedule_service.run_raw_query')
    def test_employee_refresh_queries_only_own_tasks_and_reports_failure(self, run_query):
        reception = self.create_reception()
        saisie_item = self.assign_saisie(reception)
        other = ScheduleItem.objects.create(
            schedule=self.schedule, task=self.tasks[1], employee=self.other_employee,
            assigned_time=timedelta(seconds=450), task_identifier=saisie_item.task_identifier,
        )
        self.client.force_login(self.employee_user)
        run_query.return_value = None
        response = self.client.post(reverse('update_schedule_now', args=[self.schedule.pk]), {'scope': 'mine'}, HTTP_REFERER='/')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(run_query.call_count, 1)
        self.assertEqual(run_query.call_args.args[1]['xp_id'], self.employee.xp_id)
        saisie_item.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((saisie_item.progress_lines, other.progress_lines), (0, 0))
        self.assertIn('n’ont pas pu être actualisées', str(list(response.wsgi_request._messages)[0]))

    @patch('ounissi_app.schedule_service.run_raw_query')
    def test_employee_refresh_updates_saisie_from_xpert(self, run_query):
        reception = self.create_reception()
        saisie_item = self.assign_saisie(reception)
        self.client.force_login(self.employee_user)
        run_query.return_value = {'columns': ['NBR_LINES', 'TOTAL_TIME'], 'data_rows': [[11, 495]]}
        response = self.client.post(reverse('update_schedule_now', args=[self.schedule.pk]), {'scope': 'mine'}, HTTP_REFERER='/')
        self.assertEqual(response.status_code, 302)
        saisie_item.refresh_from_db()
        self.assertEqual((saisie_item.progress_lines, saisie_item.progression_rate), (11, 55))
        self.assertIn('actualisée', str(list(response.wsgi_request._messages)[0]))

    def test_existing_reception_without_followup_can_be_repaired_once(self):
        root = ScheduleItem.objects.create(
            task=self.tasks[0], schedule=self.schedule, employee=self.employee,
            task_identifier='Fournisseur Test#LEGACY-1', progression_rate=100,
        )
        reception = Reception.objects.create(supplier='Fournisseur Test', ref_tiers='LEGACY-1', number_of_packs=1, related_item=root)
        self.client.force_login(self.manager_user)
        with patch('ounissi_app.schedule_service.get_current_active_schedule', return_value=self.schedule):
            response = self.client.get(reverse('actual_schedule_details'))
        self.assertContains(response, 'Réceptions sans étape suivante')
        self.assertContains(response, reverse('restore_reception_followup', args=[reception.pk]))
        url = reverse('restore_reception_followup', args=[reception.pk])
        self.client.force_login(self.employee_user)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.assertFalse(ScheduleItem.objects.filter(task=self.tasks[1]).exists())
        self.client.force_login(self.manager_user)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertEqual(ScheduleItem.objects.filter(task=self.tasks[1], employee__isnull=True, task_identifier=root.task_identifier).count(), 1)

    def test_missing_generation_rule_is_reported(self):
        TaskGeneration.objects.filter(parent_task=self.tasks[0]).delete()
        reception = self.create_reception()
        self.assertFalse(ScheduleItem.objects.filter(task=self.tasks[1]).exists())
        messages = list(self.reception_response.wsgi_request._messages)
        self.assertTrue(any('aucune tâche suivante' in str(message) for message in messages))
        self.client.force_login(self.manager_user)
        with patch('ounissi_app.schedule_service.get_current_active_schedule', return_value=self.schedule):
            response = self.client.get(reverse('actual_schedule_details'))
        self.assertContains(response, reception.ref_tiers)
        self.assertContains(response, 'Vérifiez la référence et la règle de génération')

    @patch('ounissi_app.forms.XpertConnect')
    def test_reception_requires_invoice_reference(self, connector):
        from .forms import ReceptionForm
        connector.return_value.run_query.return_value = {'columns': ['SupplierName'], 'data_rows': [['Fournisseur Test']]}
        form = ReceptionForm({'supplier': 'Fournisseur Test', 'ref_tiers': '', 'number_of_packs': 2,
                              'cold_chain': 'False', 'psycho': 'False', 'correct_destination': 'True', 'products_type': 'False'})
        self.assertFalse(form.is_valid())
        self.assertIn('ref_tiers', form.errors)

    def test_missing_form_configuration_returns_actionable_message(self):
        reception = self.create_reception()
        pending = ScheduleItem.objects.get(schedule=self.schedule, task=self.tasks[1], employee__isnull=True)
        pending.task.progress_query.parameters = {'supplier': '%%', 'ref_tiers': '%%'}
        pending.task.progress_query.save(update_fields=['parameters'])
        self.client.force_login(self.manager_user)
        response = self.client.post(reverse('get_special_form'), {'task_id': pending.task_id, 'schedule_item': pending.pk})
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, 'model_name', status_code=422)
        self.assertContains(response, 'requête de progression', status_code=422)
        self.assertEqual(reception.ref_tiers, 'FACT-123')

    @patch('ounissi_app.schedule_service.run_raw_query')
    def test_saisie_progress_does_not_query_wildcard_invoice_without_model(self, run_query):
        reception = self.create_reception()
        saisie_item = self.assign_saisie(reception)
        query = saisie_item.task.progress_query
        query.parameters = {'supplier': '%%', 'ref_tiers': '%%'}
        query.save(update_fields=['parameters'])
        ScheduleItem.objects.filter(pk=saisie_item.pk).update(progress_lines=7, progression_rate=35)
        with self.assertLogs('ounissi_app.schedule_service', level='WARNING'):
            self.assertFalse(schedule_service.get_item_progress(saisie_item.pk))
        run_query.assert_not_called()
        saisie_item.refresh_from_db()
        self.assertEqual((saisie_item.progress_lines, saisie_item.progression_rate), (7, 35))

    def test_orphan_pending_task_is_visible_with_configuration_error(self):
        item = ScheduleItem.objects.create(schedule=self.schedule, task=self.tasks[1], task_identifier='Fournisseur Test#MISSING')
        self.client.force_login(self.manager_user)
        with patch('ounissi_app.schedule_service.get_current_active_schedule', return_value=self.schedule):
            response = self.client.get(reverse('actual_schedule_details'))
        self.assertIn(item.pk, [pending.pk for pending in response.context['non_assigned_items']])
        self.assertContains(response, 'Étape précédente introuvable')
        response = self.client.post(reverse('get_special_form'), {'task_id': item.task_id, 'schedule_item': item.pk})
        self.assertContains(response, 'tâche précédente de cette réception est introuvable', status_code=422)

    @patch('ounissi_app.schedule_service.run_raw_query')
    def test_progress_refresh_tolerates_null_legacy_progress_and_requires_post(self, run_query):
        reception = self.create_reception()
        saisie_item = self.assign_saisie(reception)
        ScheduleItem.objects.filter(pk=saisie_item.pk).update(progress_lines=None, progression_rate=None)
        run_query.return_value = None
        self.client.force_login(self.employee_user)
        url = reverse('update_schedule_now', args=[self.schedule.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url, {'scope': 'mine'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any('n’ont pas pu être actualisées' in str(message) for message in response.wsgi_request._messages))
        saisie_item.refresh_from_db()
        self.assertIsNone(saisie_item.progress_lines)
        self.assertIsNone(saisie_item.progression_rate)

    def test_only_managers_can_open_or_assign_the_next_step(self):
        reception = self.create_reception()
        item = ScheduleItem.objects.get(schedule=self.schedule, task=self.tasks[1], employee__isnull=True)
        self.client.force_login(self.employee_user)
        response = self.client.post(reverse('get_special_form'), {'task_id': item.task_id, 'schedule_item': item.pk})
        self.assertEqual(response.status_code, 403)
        response = self.client.post(reverse('form_saisie'), {
            'supplier': reception.supplier, 'ref_tiers': reception.ref_tiers, 'origine': reception.pk,
            'number_of_lines': 20, 'employee1': self.employee.pk, 'employee1_schedule_item': item.pk,
            'employee1_range_start': 1, 'employee1_range_end': 20,
        })
        self.assertEqual(response.status_code, 403)
        item.refresh_from_db()
        self.assertIsNone(item.employee_id)
        self.assertFalse(Saisie.objects.exists())

    def test_repeated_manager_submission_does_not_duplicate_or_change_assignment(self):
        reception = self.create_reception()
        item = self.assign_saisie(reception)
        original_time = item.assigned_time
        response = self.client.post(reverse('form_saisie'), {
            'supplier': reception.supplier, 'ref_tiers': reception.ref_tiers, 'origine': reception.pk,
            'number_of_lines': 99, 'employee1': self.other_employee.pk, 'employee1_schedule_item': item.pk,
            'employee1_range_start': 1, 'employee1_range_end': 99,
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any('déjà été affectée' in str(message) for message in response.wsgi_request._messages))
        item.refresh_from_db()
        self.assertEqual(item.employee, self.employee)
        self.assertEqual(item.assigned_time, original_time)
        self.assertEqual(Saisie.objects.count(), 1)
        self.assertEqual(ScheduleItem.objects.filter(task=self.tasks[2], employee__isnull=True).count(), 1)
        response = self.client.post(reverse('get_special_form'), {'task_id': item.task_id, 'schedule_item': item.pk})
        self.assertContains(response, 'déjà été affectée', status_code=422)

    def test_assignment_rejects_wrong_reception_and_employee_outside_trt(self):
        reception = self.create_reception()
        item = ScheduleItem.objects.get(schedule=self.schedule, task=self.tasks[1], employee__isnull=True)
        outsider = Employee.objects.create(first_name='Hors TRT', rank=self.rank)
        self.client.force_login(self.manager_user)
        data = {
            'supplier': reception.supplier, 'ref_tiers': 'DIFFERENT', 'origine': reception.pk,
            'number_of_lines': 20, 'employee1': self.employee.pk, 'employee1_schedule_item': item.pk,
            'employee1_range_start': 1, 'employee1_range_end': 20,
        }
        response = self.client.post(reverse('form_saisie'), data)
        self.assertTrue(any('référence ne correspond pas' in str(message) for message in response.wsgi_request._messages))
        data.update(ref_tiers=reception.ref_tiers, employee1=outsider.pk)
        response = self.client.post(reverse('form_saisie'), data)
        self.assertTrue(any('pas éligible' in str(message) for message in response.wsgi_request._messages))
        item.refresh_from_db()
        self.assertIsNone(item.employee_id)
        self.assertFalse(Saisie.objects.exists())
        self.assertFalse(ScheduleItem.objects.filter(task=self.tasks[2]).exists())

    def test_employee_cannot_submit_reception_for_another_employee(self):
        self.client.force_login(self.employee_user)
        with patch('ounissi_app.forms.XpertConnect') as connector:
            connector.return_value.run_query.return_value = {'columns': ['SupplierName'], 'data_rows': [['Fournisseur Test']]}
            response = self.client.post(reverse('generic_form_submission', args=['ReceptionForm']), {
                'supplier': 'Fournisseur Test', 'ref_tiers': 'STOLEN-1', 'number_of_packs': 2,
                'cold_chain': 'False', 'psycho': 'False', 'correct_destination': 'True', 'products_type': 'False',
                'transfer_data': json.dumps({'task_id': self.tasks[0].pk, 'employee_id': self.other_employee.pk, 'schedule_id': self.schedule.pk}),
            })
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Reception.objects.filter(ref_tiers='STOLEN-1').exists())

    @patch('ounissi_app.schedule_service.run_raw_query')
    def test_saisie_progress_requires_invoice_and_employee_xpert_identity(self, run_query):
        reception = self.create_reception()
        item = self.assign_saisie(reception)
        for field, value in [('task_identifier', None), ('task_identifier', '')]:
            ScheduleItem.objects.filter(pk=item.pk).update(**{field: value}, progress_lines=7, progression_rate=35)
            with self.assertLogs('ounissi_app.schedule_service', level='WARNING'):
                self.assertFalse(schedule_service.get_item_progress(item.pk))
        ScheduleItem.objects.filter(pk=item.pk).update(task_identifier='Fournisseur Test#FACT-123Lines:1<>20')
        Employee.objects.filter(pk=self.employee.pk).update(xp_id=None)
        with self.assertLogs('ounissi_app.schedule_service', level='WARNING'):
            self.assertFalse(schedule_service.get_item_progress(item.pk))
        run_query.assert_not_called()
        item.refresh_from_db()
        self.assertEqual((item.progress_lines, item.progression_rate), (7, 35))

    def test_same_day_transfer_does_not_pick_a_later_trt(self):
        earlier = Schedule.objects.create(date=self.schedule.date, start_time=time(0), end_time=time(5))
        current = Schedule.objects.create(date=self.schedule.date, start_time=time(6), end_time=time(12))
        later = Schedule.objects.create(date=self.schedule.date, start_time=time(13), end_time=time(23, 59))
        older_parent = ScheduleItem.objects.create(schedule=earlier, task=self.tasks[0], task_identifier='Fournisseur Test#TRANSFER-1')
        ScheduleItem.objects.create(schedule=later, task=self.tasks[0], task_identifier='Fournisseur Test#TRANSFER-1')
        pending = ScheduleItem.objects.create(schedule=current, task=self.tasks[1], task_identifier='Fournisseur Test#TRANSFER-1')
        self.assertEqual(schedule_service.get_original_item(pending), older_parent)

    def test_missing_later_generation_rule_produces_warning_after_assignment(self):
        reception = self.create_reception()
        TaskGeneration.objects.filter(parent_task=self.tasks[1]).delete()
        self.assign_saisie(reception)
        self.assertTrue(any('aucune tâche suivante' in str(message) for message in self.assignment_response.wsgi_request._messages))
        self.assertFalse(ScheduleItem.objects.filter(task=self.tasks[2]).exists())

    @patch('ounissi_app.schedule_service.run_raw_query')
    def test_ambiguous_multiple_progress_rows_preserve_the_last_value(self, run_query):
        reception = self.create_reception()
        item = self.assign_saisie(reception)
        ScheduleItem.objects.filter(pk=item.pk).update(progress_lines=7, progression_rate=35)
        run_query.return_value = {'columns': ['NBR_LINES'], 'data_rows': [[11], [9]]}
        with self.assertLogs('ounissi_app.schedule_service', level='WARNING'):
            self.assertFalse(schedule_service.get_item_progress(item.pk))
        item.refresh_from_db()
        self.assertEqual((item.progress_lines, item.progression_rate), (7, 35))

    def test_team_leader_can_open_reception_assignment(self):
        self.create_reception()
        Team.objects.create(name='Équipe réception', leader=self.employee)
        item = ScheduleItem.objects.get(schedule=self.schedule, task=self.tasks[1], employee__isnull=True)
        self.client.force_login(self.employee_user)
        response = self.client.post(reverse('get_special_form'), {'task_id': item.task_id, 'schedule_item': item.pk})
        self.assertContains(response, 'Enregistrer l’affectation')

    def test_assignment_handles_null_legacy_trt_totals(self):
        reception = self.create_reception()
        ScheduleEmployee.objects.filter(schedule=self.schedule, employee=self.employee).update(total_assigned_time=None)
        ScheduleTask.objects.create(schedule=self.schedule, task=self.tasks[1], total_assigned_time=None)
        item = self.assign_saisie(reception)
        self.assertEqual(item.assigned_time, timedelta(seconds=900))
        self.assertEqual(ScheduleEmployee.objects.get(schedule=self.schedule, employee=self.employee).total_assigned_time, item.assigned_time)
        self.assertEqual(ScheduleTask.objects.get(schedule=self.schedule, task=self.tasks[1]).total_assigned_time, item.assigned_time)

    def test_transferred_saisie_can_use_an_earlier_trt_reception(self):
        earlier = Schedule.objects.create(date=self.schedule.date - timedelta(days=1), start_time=time(0), end_time=time(23, 59))
        root = ScheduleItem.objects.create(
            schedule=earlier, task=self.tasks[0], employee=self.employee,
            task_identifier='Fournisseur Test#TRANSFER-2', progression_rate=100,
        )
        reception = Reception.objects.create(supplier='Fournisseur Test', ref_tiers='TRANSFER-2', number_of_packs=1, related_item=root)
        pending = ScheduleItem.objects.create(schedule=self.schedule, task=self.tasks[1], task_identifier=root.task_identifier)
        self.client.force_login(self.manager_user)
        response = self.client.post(reverse('get_special_form'), {'task_id': pending.task_id, 'schedule_item': pending.pk})
        self.assertContains(response, 'TRANSFER-2')
        item = self.assign_saisie(reception)
        self.assertEqual(item.employee, self.employee)
        self.assertTrue(Saisie.objects.filter(origine=reception, employee1_schedule_item=item).exists())
        self.assertTrue(ScheduleItem.objects.filter(schedule=self.schedule, task=self.tasks[2], employee__isnull=True).exists())

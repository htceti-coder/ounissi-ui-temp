import json
from datetime import date, time, timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.template.loader import get_template
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Employee, Rank, Team
from .forms import AssignationEmplacementForm, ReceptionForm, ReceptionProduitForm
from .models import (
    AssignationEmplacement, Query, ReceptionProcess, ReceptionTask, Saisie,
    Schedule, ScheduleEmployee, ScheduleItem, StockOperation, Task,
    TaskGeneration, TaskType, Timesheet, UserSyncLog,
)
from .query_results import column_values


SAMPLES = json.loads((Path(settings.BASE_DIR) / 'xpert_dummy_data.txt').read_text(encoding='utf-8-sig'))


def sample_result(name):
    sample = SAMPLES[name]
    return {'columns': sample['columns'], 'data_rows': sample['sample_data']}


class UIWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username='manager', password='unused')
        cls.user.groups.add(Group.objects.create(name='Admin'))
        cls.rank = Rank.objects.create(value=1, label='Responsable')
        cls.employee = Employee.objects.create(user=cls.user, first_name='Test', last_name='Employé', rank=cls.rank, xp_id='TEST')
        cls.schedule = Schedule.objects.create(date=timezone.localdate(), start_time=time(0, 0), end_time=time(23, 59))

    def setUp(self):
        self.client.force_login(self.user)

    def query(self, name, params=None):
        return Query.objects.create(name=name, type=1, query='SELECT 1', parameters=params or {})

    def make_reception_workflow(self):
        parent_query = self.query('Saisie progress', {'model_name': 'Saisie'})
        child_query = self.query('Produit progress', {'model_name': 'ReceptionProduit'})
        parent = Task.objects.create(task_name='Saisie parent', responsibility_level=self.rank, average_duration=timedelta(seconds=15), progress_query=parent_query)
        child = Task.objects.create(task_name='Vérification produit', responsibility_level=self.rank, average_duration=timedelta(seconds=60), progress_query=child_query)
        TaskGeneration.objects.create(name='Saisie vers produit', parent_task=parent, generated_task=child)
        original = ScheduleItem.objects.create(task=parent, schedule=self.schedule, employee=self.employee, task_identifier='Supplier#INV-1')
        source = Saisie.objects.create(supplier='Supplier', ref_tiers='INV-1', number_of_lines=20, employee1_schedule_item=original)
        item = ScheduleItem.objects.create(task=child, schedule=self.schedule, task_identifier='Supplier#INV-1')
        ScheduleEmployee.objects.create(employee=self.employee, schedule=self.schedule)
        self.query('InvoiceHelper', {'ref_tiers': '%%', 'supplier': '%%'})
        return source, item

    def test_all_templates_compile(self):
        for path in (Path(settings.BASE_DIR) / 'templates').rglob('*.html'):
            with self.subTest(template=path.name):
                get_template(str(path.relative_to(Path(settings.BASE_DIR) / 'templates')))

    def test_sample_result_column_uses_column_names_and_preserves_values(self):
        result = sample_result('LISTE EMPLACEMENT')
        self.assertEqual(column_values(result, 'DESIGNATION'), [row[0] for row in result['data_rows']])
        self.assertEqual(column_values({'columns': ['B', 'A'], 'data_rows': [[0, 'x']]}, 'A'), ['x'])
        with self.assertRaises(ValueError):
            column_values({'error': 'offline'}, 'A')
        with self.assertRaises(ValueError):
            column_values({'columns': ['B'], 'data_rows': [[1]]}, 'A')
        self.assertEqual(column_values({'columns': ['A'], 'data_rows': []}, 'A'), [])

    @patch('ounissi_app.views.get_query_results')
    def test_stock_and_location_tables_render_sample_rows(self, run_query):
        for name, route in [('TEST STOCK', 'etat_stocks'), ('STOCK EMPLACEMENTS', 'emplacements')]:
            self.query(name)
            result = sample_result(name)
            run_query.return_value = result
            with self.subTest(name=name):
                response = self.client.get(reverse(route))
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                for column in result['columns']:
                    self.assertContains(response, column)
                for value in result['data_rows'][0]:
                    self.assertContains(response, str(value).strip())
                tbody = html.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
                self.assertEqual(tbody.count('<tr>'), len(result['data_rows']))
                self.assertNotIn('Decimal(', tbody)

    @patch('ounissi_app.views.get_query_results')
    def test_stock_query_empty_and_unavailable_are_distinct(self, run_query):
        self.query('TEST STOCK')
        run_query.return_value = {'columns': ['DESIGNATION'], 'data_rows': []}
        self.assertContains(self.client.get(reverse('etat_stocks')), 'Aucun résultat pour cette requête.')
        run_query.return_value = {'error': 'private connection details'}
        response = self.client.get(reverse('etat_stocks'))
        self.assertContains(response, 'Impossible de charger les données')
        self.assertNotContains(response, 'private connection details')

    @patch('ounissi_app.forms.XpertConnect')
    def test_supplier_choices_use_stored_parameters_and_support_no(self, connector):
        supplier_query = self.query('LISTE FOURNISSEURS', {'actif': '1'})
        result = sample_result('LISTE FOURNISSEURS')
        connector.return_value.run_query.return_value = result
        supplier = result['data_rows'][0][0]
        form = ReceptionForm({
            'supplier': supplier, 'ref_tiers': 'REF', 'number_of_packs': 2,
            'cold_chain': 'False', 'psycho': 'False', 'correct_destination': 'False', 'products_type': 'True',
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIs(form.cleaned_data['correct_destination'], False)
        self.assertEqual(form.cleaned_data['task_identifier'], f'{supplier}#REF')
        connector.return_value.run_query.assert_called_with(supplier_query.query, parameters=supplier_query.parameters)

    @patch('ounissi_app.forms.XpertConnect')
    def test_location_choice_uses_result_columns_not_pandas_indexing(self, connector):
        self.query('LISTE EMPLACEMENT', {'emp': '%%'})
        connector.return_value.run_query.return_value = sample_result('LISTE EMPLACEMENT')
        form = AssignationEmplacementForm()
        expected = SAMPLES['LISTE EMPLACEMENT']['sample_data'][0][0]
        self.assertIn((expected, expected), list(form.fields['emplacement'].choices))

    @patch('ounissi_app.views.get_query_results')
    def test_reception_invoice_form_uses_sample_rows_and_current_task_duration(self, run_query):
        source, item = self.make_reception_workflow()
        run_query.return_value = sample_result('InvoiceHelper')
        response = self.client.post(reverse('get_special_form'), {'task_id': item.task_id, 'schedule_item': item.id})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, SAMPLES['InvoiceHelper']['sample_data'][0][1])
        self.assertContains(response, 'distributeLines([60])')
        self.assertContains(response, 'QUANTITE')
        self.assertEqual(run_query.call_args.args[1], {'ref_tiers': source.ref_tiers, 'supplier': source.supplier})
        self.assertEqual(response.content.decode().split('<tbody>', 1)[1].split('</tbody>', 1)[0].count('<tr>'), 20)

    @patch('ounissi_app.views.get_query_results')
    def test_reception_query_failure_is_a_readable_fragment(self, run_query):
        _, item = self.make_reception_workflow()
        run_query.return_value = {'error': 'secret connection details'}
        response = self.client.post(reverse('get_special_form'), {'task_id': item.task_id, 'schedule_item': item.id})
        self.assertContains(response, 'Le détail de la facture n’a pas pu être chargé')
        self.assertContains(response, 'Enregistrer l’affectation')
        self.assertNotContains(response, 'secret connection details')
        run_query.return_value = {'columns': ['QUANTITE'], 'data_rows': []}
        response = self.client.post(reverse('get_special_form'), {'task_id': item.task_id, 'schedule_item': item.id})
        self.assertContains(response, 'Le détail de la facture n’est pas encore disponible')
        self.assertContains(response, 'Enregistrer l’affectation')

    def test_reception_ranges_are_validated_server_side(self):
        source, item = self.make_reception_workflow()
        second = Employee.objects.create(first_name='Second', rank=self.rank)
        data = {
            'supplier': source.supplier, 'ref_tiers': source.ref_tiers, 'origine': source.pk,
            'number_of_lines': 4, 'employee1': self.employee.pk, 'employee2': second.pk,
            'employee1_schedule_item': item.pk,
            'employee1_range_start': 1, 'employee1_range_end': 2,
            'employee2_range_start': 2, 'employee2_range_end': 4,
        }
        form = ReceptionProduitForm(data)
        self.assertFalse(form.is_valid())
        self.assertIn('employee2_range_end', form.errors)
        data['employee2_range_start'] = 3
        self.assertTrue(ReceptionProduitForm(data).is_valid())
        data['employee2'] = self.employee.pk
        self.assertIn('employee2', ReceptionProduitForm(data).errors)
        data['employee2'] = ''
        data['employee2_range_start'] = ''
        data['employee2_range_end'] = ''
        data['employee3'] = second.pk
        data['employee3_range_start'] = 3
        data['employee3_range_end'] = 4
        self.assertIn('employee3', ReceptionProduitForm(data).errors)

    def test_valid_reception_submission_creates_two_schedule_items(self):
        source, item = self.make_reception_workflow()
        source.number_of_lines = 4
        source.save(update_fields=['number_of_lines'])
        second = Employee.objects.create(first_name='Second', rank=self.rank)
        ScheduleEmployee.objects.create(employee=second, schedule=self.schedule)
        response = self.client.post(reverse('form_reception_produit'), {
            'supplier': source.supplier, 'ref_tiers': source.ref_tiers, 'origine': source.pk,
            'number_of_lines': 4, 'employee1': self.employee.pk, 'employee2': second.pk,
            'employee1_schedule_item': item.pk,
            'employee1_range_start': 1, 'employee1_range_end': 2,
            'employee2_range_start': 3, 'employee2_range_end': 4,
        }, HTTP_REFERER='/schedule/actual/')
        self.assertEqual(response.status_code, 302)
        items = ScheduleItem.objects.filter(task=item.task, schedule=self.schedule).order_by('employee_id')
        self.assertEqual(items.count(), 2)
        self.assertEqual([x.assigned_time for x in items], [timedelta(minutes=2), timedelta(minutes=2)])

    @patch('ounissi_app.views.get_query_results')
    def test_stock_groups_use_named_column_and_handle_query_failure(self, run_query):
        self.query('LISTE EMPLACEMENT')
        run_query.return_value = {'columns': ['MAGASIN', 'DESIGNATION'], 'data_rows': [['Shop', 'ABC1'], ['Shop', 'ABC2']]}
        response = self.client.get(reverse('assign_stock'))
        self.assertContains(response, 'ABC1')
        self.assertContains(response, 'ABC2')
        run_query.return_value = {'error': 'offline'}
        self.assertContains(self.client.get(reverse('assign_stock')), 'Impossible de charger les emplacements Xpert')

    def test_stock_assignment_is_atomic_and_delete_url_works(self):
        op = StockOperation.objects.create(designation='TRI_1')
        entry = {'emplacement': 'ABC1', 'employee': str(self.employee.pk), 'date_debut': '2026-10-01', 'date_fin': '2026-10-31', 'operations': [op.pk]}
        bad = {**entry, 'emplacement': 'ABC2', 'operations': [9999]}
        url = reverse('assign_stock')
        response = self.client.post(url, data=json.dumps([entry, bad]), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AssignationEmplacement.objects.count(), 0)
        response = self.client.post(url, data=json.dumps([entry]), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        assignment = AssignationEmplacement.objects.get()
        self.assertEqual(assignment.operation, op)
        self.assertEqual(assignment.date_debut, date(2026, 10, 1))
        # Older clients submit operation names instead of IDs.
        entry['operations'] = ['TRI_1']
        self.assertEqual(self.client.post(url, data=json.dumps([entry]), content_type='application/json').status_code, 200)
        response = self.client.delete(reverse('delete_assignment', args=[assignment.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AssignationEmplacement.objects.exists())

    def test_navigation_uses_pages_not_post_only_endpoints(self):
        with patch('ounissi_app.schedule_service.get_current_active_schedule', return_value=None):
            response = self.client.get(reverse('home'))
        self.assertContains(response, reverse('my_timesheet'))
        self.assertContains(response, reverse('request_leave'))
        self.assertContains(response, reverse('manage_leaves'))
        self.assertNotContains(response, 'data-nav-popup=')
        self.assertNotContains(response, 'href="/import_tasks/"')
        self.assertNotContains(response, 'Système opérationnel')

    def test_portal_no_profile_does_not_display_other_employees_tasks(self):
        Task.objects.create(task_name='Confidential other task', responsibility_level=self.rank)
        task = Task.objects.get(task_name='Confidential other task')
        ScheduleItem.objects.create(employee=self.employee, schedule=self.schedule, task=task)
        user = get_user_model().objects.create_user(username='no-profile')
        self.client.force_login(user)
        with patch('ounissi_app.schedule_service.get_current_active_schedule', return_value=self.schedule):
            response = self.client.get(reverse('home'))
        self.assertContains(response, 'Aucun profil d’employé')
        self.assertNotContains(response, 'Confidential other task')

    def test_portal_progress_and_separate_task_forms(self):
        task_type = TaskType.objects.create(name='APRES SAISIE')
        for name in ('Form A', 'Form B'):
            Task.objects.create(task_name=name, responsibility_level=self.rank, task_type=task_type, menu=True)
        task = Task.objects.create(task_name='Progress task', responsibility_level=self.rank, average_duration=timedelta(seconds=60))
        ScheduleItem.objects.create(employee=self.employee, schedule=self.schedule, task=task, assigned_time=timedelta(minutes=5), progression_rate=75)
        with patch('ounissi_app.schedule_service.get_current_active_schedule', return_value=self.schedule):
            response = self.client.get(reverse('home'))
        self.assertContains(response, 'width:clamp(0%, 75%, 100%)')
        self.assertContains(response, 'class="form_lookup_form"', count=2)
        self.assertContains(response, 'data-task-form-open', count=2)
        self.assertContains(response, 'js/task_form_modal.js')

    def test_reception_v3_renders_as_read_only(self):
        process = ReceptionProcess.objects.create(supplier='Supplier', ref_tiers='INV-1')
        task = ReceptionTask.objects.create(process=process, task_step='SAI', status='ASSIGNED')
        task.employees.add(self.employee)
        response = self.client.get(reverse('reception_dashboard_v3'))
        self.assertContains(response, 'lecture seule')
        self.assertContains(response, 'INV-1')
        self.assertContains(response, 'Assigné')
        self.assertNotContains(response, 'executeBulkAssign')

    def test_timesheet_sync_quota_reflects_database(self):
        UserSyncLog.objects.create(user=self.user, date=timezone.localdate(), count=2)
        response = self.client.get(reverse('my_timesheet'))
        self.assertContains(response, 'Sync Terminal (1 restants)')
        UserSyncLog.objects.filter(user=self.user).update(count=3)
        response = self.client.get(reverse('my_timesheet'))
        self.assertContains(response, 'Sync Terminal (0 restants)')
        self.assertContains(response, 'disabled')

    def test_query_ui_defaults_and_internal_query_test(self):
        query = self.query('Sales example', {'start_date': '2026-10-01', 'xp_id': '%%', 'model_name': 'Ignored'})
        details = self.client.get(reverse('get_query_details', args=[query.pk])).json()['parameters']
        self.assertEqual({item['name']: item['value'] for item in details}, {'start_date': '2026-10-01', 'xp_id': '%%'})
        response = self.client.get(reverse('test_queries'))
        self.assertContains(response, 'input.value = parameter.value')
        response = self.client.post(reverse('test_query_submission'), {'type': '0', 'queryText': 'SELECT 7 AS NBR_LINES'})
        self.assertContains(response, 'NBR_LINES')
        self.assertContains(response, '<td>7</td>', html=True)
        response = self.client.post(reverse('execute_query'), {'query': ''})
        self.assertContains(response, 'Sélectionnez une requête', status_code=400)

    def test_create_query_persists_help_text(self):
        response = self.client.post(reverse('create_query'), {
            'queryName': 'Example query', 'type': '0', 'queryText': 'SELECT 1', 'help': 'Explanation',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Query.objects.get(name='Example query').help, 'Explanation')

    @patch('ounissi_app.views.XpertConnect')
    def test_timesheet_empty_audit_is_distinct_from_failure_and_export_marks_failure(self, connector):
        from io import BytesIO
        from openpyxl import load_workbook

        today = timezone.localdate().isoformat()
        params = {'start_date': today, 'end_date': today}
        connector.return_value.run_query.return_value = sample_result('EMPLOYEES XPERT LOGS')
        response = self.client.get(reverse('timesheet_list'), params)
        self.assertNotContains(response, 'Audit Xpert indisponible')
        connector.return_value.run_query.side_effect = RuntimeError('offline')
        with self.assertLogs('ounissi_app.views', level='WARNING'):
            response = self.client.get(reverse('timesheet_list'), params)
        self.assertContains(response, 'Audit Xpert indisponible')
        with self.assertLogs('ounissi_app.views', level='WARNING'):
            response = self.client.get(reverse('timesheet_list'), {**params, 'export': 'excel'})
        workbook = load_workbook(BytesIO(response.content))
        self.assertIn('Audit Xpert indisponible', workbook.active['A4'].value)

    def test_unfinished_v3_api_does_not_create_partial_assignments(self):
        process = ReceptionProcess.objects.create(supplier='Supplier', ref_tiers='INV-1')
        response = self.client.post(
            reverse('api-1.0.0:bulk_assign_task'),
            data=json.dumps({'process_ids': [process.pk], 'employee_ids': [self.employee.pk], 'task_step': 'SAI'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['status'], 'error')
        self.assertFalse(ReceptionTask.objects.filter(process=process).exists())

    @patch('ounissi_app.forms.XpertConnect')
    def test_reception_form_invalid_preserves_errors_and_valid_form_saves(self, connector):
        from .models import Reception

        self.query('LISTE FOURNISSEURS', {'actif': '1'})
        connector.return_value.run_query.return_value = sample_result('LISTE FOURNISSEURS')
        query = self.query('Reception progress', {'model_name': 'Reception'})
        task = Task.objects.create(task_name='Réception test', responsibility_level=self.rank, average_duration=timedelta(minutes=1), progress_query=query, menu=True)
        supplier = SAMPLES['LISTE FOURNISSEURS']['sample_data'][0][0]
        data = {
            'supplier': supplier, 'ref_tiers': 'FACT-1', 'number_of_packs': '',
            'cold_chain': 'False', 'psycho': 'False', 'correct_destination': 'False', 'products_type': 'True',
            'transfer_data': json.dumps({'task_id': task.pk, 'employee_id': self.employee.pk, 'schedule_id': self.schedule.pk}),
        }
        url = reverse('generic_form_submission', args=['ReceptionForm'])
        response = self.client.post(url, data, HTTP_REFERER='/')
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'Corrigez les champs', status_code=400)
        self.assertContains(response, 'FACT-1', status_code=400)
        self.assertFalse(Reception.objects.exists())
        data['number_of_packs'] = '3'
        response = self.client.post(url, data, HTTP_REFERER='/')
        self.assertEqual(response.status_code, 302)
        reception = Reception.objects.get()
        self.assertIs(reception.correct_destination, False)
        self.assertEqual(reception.related_item.task, task)
        self.assertEqual(reception.related_item.employee, self.employee)

    def test_salary_items_screen_avoids_unconfigured_create_route(self):
        from .models import SalaryItem
        SalaryItem.objects.create(name='Prime test', item_type='G', calculation_type='fixed', default_value='12.00')
        response = self.client.get(reverse('salary_item_list'))
        self.assertContains(response, 'Prime test')
        self.assertContains(response, 'Rubriques de paie')
        self.assertNotContains(response, '/salary_items/create/')

    @patch('ounissi_app.views.XpertConnect')
    def test_curated_navigation_pages_render_without_external_data(self, connector):
        connector.return_value.run_query.return_value = sample_result('EMPLOYEES XPERT LOGS')
        routes = [
            'home', 'user_schedule_history', 'my_timesheet', 'request_leave',
            'actual_schedule_details', 'schedule_details', 'schedule_details2',
            'task_list', 'create_task', 'employee_list', 'reception_dashboard_v3',
            'etat_stocks', 'emplacements', 'assign_stock', 'attendance_logs',
            'timesheet_list', 'manage_leaves', 'salary_list', 'salary_item_list',
            'create_schedule', 'test_queries', 'create_query',
        ]
        with patch('ounissi_app.schedule_service.get_current_active_schedule', return_value=None):
            for route in routes:
                with self.subTest(route=route):
                    response = self.client.get(reverse(route))
                    self.assertEqual(response.status_code, 200)

    def test_timesheet_validation_requires_session_role_and_csrf(self):
        from django.middleware.csrf import _get_new_csrf_string

        entry = Timesheet.objects.create(employee=self.employee, date=timezone.localdate())
        url = reverse('api-1.0.0:bulk_update_timesheets')
        payload = json.dumps([{'id': entry.pk, 'leave_type': 'NONE'}])
        client = Client(enforce_csrf_checks=True)
        token = _get_new_csrf_string()
        client.cookies['csrftoken'] = token

        response = client.post(url, payload, content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 401)
        entry.refresh_from_db()
        self.assertFalse(entry.is_confirmed)

        user = get_user_model().objects.create_user(username='ordinary')
        Employee.objects.create(user=user, first_name='Ordinary', rank=self.rank)
        client.force_login(user)
        response = client.post(url, payload, content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(client.get(reverse('timesheet_list')).status_code, 403)
        entry.refresh_from_db()
        self.assertFalse(entry.is_confirmed)

        client.force_login(self.user)
        response = client.post(url, payload, content_type='application/json')
        self.assertEqual(response.status_code, 403)
        response = client.post(url, payload, content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        entry.refresh_from_db()
        self.assertTrue(entry.is_confirmed)

        entry.is_confirmed = False
        entry.save()
        leader_user = get_user_model().objects.create_user(username='leader')
        leader = Employee.objects.create(user=leader_user, first_name='Leader', rank=self.rank)
        Team.objects.create(name='Team', leader=leader)
        client.force_login(leader_user)
        response = client.post(url, payload, content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        entry.refresh_from_db()
        self.assertTrue(entry.is_confirmed)

    def test_terminal_sync_requires_login(self):
        from django.middleware.csrf import _get_new_csrf_string

        client = Client(enforce_csrf_checks=True)
        token = _get_new_csrf_string()
        client.cookies['csrftoken'] = token
        response = client.post(reverse('api-1.0.0:sync_my_data'), HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 401)

    @patch('ounissi_app.views.get_query_results')
    def test_stock_grouping_zero_keeps_all_locations_in_one_group(self, run_query):
        self.query('LISTE EMPLACEMENT')
        run_query.return_value = {'columns': ['MAGASIN', 'DESIGNATION'], 'data_rows': [['A', 'A-01'], ['B', 'B-02']]}
        response = self.client.get(reverse('assign_stock'), {'group_by': '0'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['group_size'], 0)
        self.assertEqual(response.context['grouped_emplacements'], {'': ['A-01', 'B-02']})
        self.assertContains(response, 'value="0"')
        self.assertContains(response, 'Tous')

    @patch('ounissi_app.views.XpertConnect')
    def test_timesheet_audit_matches_reordered_named_columns(self, connector):
        today = timezone.localdate()
        connector.return_value.run_query.return_value = {
            'columns': ['LAST_LOG', 'OP_USER', 'OP_DATE', 'FIRST_LOG'],
            'data_rows': [[time(12, 0), self.employee.xp_id, today, time(9, 0)]],
        }
        response = self.client.get(reverse('timesheet_list'), {'start_date': today, 'end_date': today})
        self.assertEqual(response.status_code, 200)
        row = next(item for item in response.context['timesheets_with_hours'] if item['obj'].employee_id == self.employee.pk)
        self.assertEqual(row['xp_total'], '03:00')
        self.assertFalse(response.context['xpert_audit_unavailable'])

    @patch('ounissi_app.attendance_importer.save_logs')
    @patch('ounissi_app.attendance_importer.timesheet_dataframe')
    @patch('ounissi_app.attendance_importer.import_attendance_logs', return_value=[{'terminal': 'sample'}])
    def test_terminal_sync_accepts_authenticated_csrf_request(self, import_logs, dataframe, save_logs):
        from django.middleware.csrf import _get_new_csrf_string

        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        token = _get_new_csrf_string()
        client.cookies['csrftoken'] = token
        response = client.post(reverse('api-1.0.0:sync_my_data'), HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'success')
        self.assertEqual(UserSyncLog.objects.get(user=self.user, date=timezone.localdate()).count, 1)
        save_logs.assert_called_once_with(dataframe.return_value)

    def test_failed_reception_assignment_rolls_back_schedule_updates(self):
        source, item = self.make_reception_workflow()
        source.number_of_lines = 2
        source.save(update_fields=['number_of_lines'])
        original_employee = item.employee_id
        original_time = item.assigned_time
        with patch('ounissi_app.schedule_service.generate_group_task', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse('form_reception_produit'), {
                    'supplier': source.supplier, 'ref_tiers': source.ref_tiers, 'origine': source.pk,
                    'number_of_lines': 2, 'employee1': self.employee.pk,
                    'employee1_schedule_item': item.pk,
                    'employee1_range_start': 1, 'employee1_range_end': 2,
                })
        item.refresh_from_db()
        self.assertEqual(item.employee_id, original_employee)
        self.assertEqual(item.assigned_time, original_time)
        self.assertEqual(ScheduleItem.objects.filter(task=item.task, schedule=self.schedule).count(), 1)

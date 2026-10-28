from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import Employee, Rank, Team
from .models import NavigationVisibility
from .navigation import ADMIN, LEADER, USER, menu_sections, role_for


class NavigationConfigurationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.worker = users.objects.create_user('worker', password='test')
        cls.leader = users.objects.create_user('leader', password='test')
        cls.admin = users.objects.create_user('admin', password='test')
        cls.superuser = users.objects.create_superuser('root', password='test')
        # Admin is deliberately not the first group and does not need Django staff status.
        cls.admin.groups.add(Group.objects.create(name='Other'), Group.objects.create(name='Admin'))
        rank = Rank.objects.create(value=2, label='Employé')
        Employee.objects.create(user=cls.worker, first_name='Employé', rank=rank)
        leader_employee = Employee.objects.create(user=cls.leader, first_name='Responsable', rank=rank)
        Team.objects.create(name='Équipe', leader=leader_employee)

    @staticmethod
    def keys(user):
        sections, _ = menu_sections(user)
        return {item['key'] for section in sections for item in section['items']}

    def test_default_roles_and_group_precedence(self):
        self.assertIsNone(role_for(AnonymousUser()))
        self.assertEqual(menu_sections(AnonymousUser()), ([], False))
        self.assertEqual(role_for(self.worker), USER)
        self.assertEqual(role_for(self.leader), LEADER)
        self.assertEqual(role_for(self.admin), ADMIN)
        self.assertEqual(role_for(self.superuser), ADMIN)
        self.assertEqual(self.keys(self.worker), {'home', 'history', 'my_timesheet', 'request_leave'})
        self.assertIn('manage_trt', self.keys(self.leader))
        self.assertNotIn('salaries', self.keys(self.leader))
        self.assertIn('salaries', self.keys(self.admin))
        self.assertNotIn('django_admin', self.keys(self.admin))  # Django itself requires is_staff.
        self.assertIn('django_admin', self.keys(self.superuser))

    def test_only_admin_can_view_or_change_settings(self):
        url = reverse('configure_navigation')
        self.assertEqual(self.client.get(url).status_code, 302)
        for user in (self.worker, self.leader):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, {'action': 'reset'}).status_code, 403)
        self.client.force_login(self.admin)
        response = self.client.get(url)
        self.assertContains(response, 'Configuration du menu')
        self.assertContains(response, 'show_user_home')
        self.assertContains(response, 'ne modifie pas les autorisations')
        self.assertContains(response, 'Configurer le menu')
        self.assertNotContains(response, 'activity-rail')
        self.assertNotContains(response, 'Sous-vues &amp; endpoints')
        self.assertEqual(self.client.put(url).status_code, 405)

    def test_change_is_persisted_by_exact_role_and_can_be_reset(self):
        url = reverse('configure_navigation')
        self.client.force_login(self.admin)
        response = self.client.post(url, {
            'action': 'save', 'item_home': '1',
            'show_leader_home': '1', 'show_admin_home': '1',  # hide it only from users
            'item_salaries': '1', 'show_user_salaries': '1', 'show_admin_salaries': '1',
        })
        self.assertRedirects(response, url)
        self.assertNotIn('home', self.keys(self.worker))
        self.assertIn('home', self.keys(self.leader))
        self.assertIn('home', self.keys(self.admin))
        self.assertIn('salaries', self.keys(self.worker))
        self.assertNotIn('salaries', self.keys(self.leader))
        # New/unsubmitted items retain the defaults rather than being silently disabled.
        self.assertIn('history', self.keys(self.worker))
        self.client.force_login(self.worker)
        html = self.client.get(reverse('login')).content.decode()
        self.assertNotIn('data-nav-key="home"', html)
        self.assertIn('data-nav-key="salaries"', html)
        self.client.force_login(self.admin)
        self.assertRedirects(self.client.post(url, {'action': 'reset'}), url)
        self.assertIn('home', self.keys(self.worker))
        self.assertNotIn('salaries', self.keys(self.worker))
        self.assertFalse(NavigationVisibility.objects.exists())

    def test_visibility_does_not_grant_access_to_a_protected_view(self):
        NavigationVisibility.objects.create(item_key='timesheets', show_user=True, show_leader=True, show_admin=True)
        self.assertIn('timesheets', self.keys(self.worker))
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(reverse('timesheet_list')).status_code, 403)

    def test_invalid_post_csrf_and_atomic_save(self):
        url = reverse('configure_navigation')
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(url, {'action': 'save'}).status_code, 400)
        self.assertEqual(self.client.post(url, {'action': 'unexpected'}).status_code, 400)
        original_save = NavigationVisibility.objects.update_or_create
        def fail_second(*args, **kwargs):
            if kwargs.get('item_key') == 'salaries':
                raise RuntimeError('database error')
            return original_save(*args, **kwargs)
        with patch('ounissi_app.navigation_views.NavigationVisibility.objects.update_or_create', side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.client.post(url, {'action': 'save', 'item_home': '1', 'item_salaries': '1'})
        self.assertFalse(NavigationVisibility.objects.exists())
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        self.assertEqual(csrf_client.post(url, {'action': 'reset'}).status_code, 403)
        csrf_client.get(url)
        token = csrf_client.cookies['csrftoken'].value
        self.assertEqual(csrf_client.post(url, {'action': 'reset'}, HTTP_X_CSRFTOKEN=token).status_code, 302)

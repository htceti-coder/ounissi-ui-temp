from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.urls import reverse
from django.utils.html import format_html
from .forms import CustomUserCreationForm, CustomUserChangeForm
from .models import CustomUser, Employee, Rank, Team
import ounissi_app.models
import ounissi_app.forms


class EmployeeInline(admin.StackedInline):
    model = Employee
    can_delete = False


class TeamAdmin(admin.ModelAdmin):
    filter_horizontal = ('employees',)
    list_display = ('name', 'leader')


class TimesheetAdmin(admin.ModelAdmin):
    form = ounissi_app.forms.TimesheetForm
    list_display = ('employee', 'date', 'is_confirmed', 'start_time', 'end_time', 'start_time_2', 'end_time_2', 'start_time_3', 'end_time_3')

    list_filter = ('employee', 'date')

class SalaryItemAdmin(admin.ModelAdmin):
    form = ounissi_app.forms.SalaryItemForm
    list_display = ['name', 'calculation_type']


class CustomUserAdmin(UserAdmin):
    add_form = CustomUserCreationForm
    form = CustomUserChangeForm
    model = CustomUser
    list_display = ["email", "username", "view_user_details"]

    # Use 'password1' and 'password2' instead of 'password'
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'password1', 'password2'),
        }),
    )

    def view_user_details(self, obj):
        url = reverse('admin:accounts_customuser_change', args=[obj.id])
        return format_html('<a href="{}">View Details</a>', url)

    view_user_details.short_description = 'User Details'
    inlines = [EmployeeInline]

class XpertServerAdmin(admin.ModelAdmin):
    list_display = ['name', 'active']

# Django model permissions alone must not bypass TRT template authorship.
def _trt_role(request):
    from ounissi_app.navigation import role_for
    return role_for(request.user)


def _trt_admin(request):
    from ounissi_app.navigation import ADMIN
    return _trt_role(request) == ADMIN


class ScheduleAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        from ounissi_app.navigation import MANAGERS
        return _trt_role(request) in MANAGERS and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.is_template and not _trt_admin(request):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.is_template and not _trt_admin(request):
            return False
        return super().has_delete_permission(request, obj)

    def get_readonly_fields(self, request, obj=None):
        fields = tuple(super().get_readonly_fields(request, obj))
        return fields if _trt_admin(request) else fields + ('is_template',)

    def save_model(self, request, obj, form, change):
        if obj.is_template and not _trt_admin(request):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        super().save_model(request, obj, form, change)

    def delete_queryset(self, request, queryset):
        if not _trt_admin(request) and queryset.filter(is_template=True).exists():
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        super().delete_queryset(request, queryset)


class ScheduleRowAdmin(admin.ModelAdmin):
    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.schedule.is_template and not _trt_admin(request):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.schedule.is_template and not _trt_admin(request):
            return False
        return super().has_delete_permission(request, obj)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'schedule' and not _trt_admin(request):
            kwargs['queryset'] = ounissi_app.models.Schedule.objects.filter(is_template=False)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if obj.schedule.is_template and not _trt_admin(request):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        super().save_model(request, obj, form, change)

    def delete_queryset(self, request, queryset):
        if not _trt_admin(request) and queryset.filter(schedule__is_template=True).exists():
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        super().delete_queryset(request, queryset)


class QueryAdmin(admin.ModelAdmin):
    def has_view_permission(self, request, obj=None):
        return _trt_admin(request) and super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        return _trt_admin(request) and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return _trt_admin(request) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return _trt_admin(request) and super().has_delete_permission(request, obj)

admin.site.register(Rank)
admin.site.register(Team, TeamAdmin)
admin.site.register(CustomUser, CustomUserAdmin)
admin.site.register(ounissi_app.models.ScheduleItem, ScheduleRowAdmin)
admin.site.register(ounissi_app.models.Schedule, ScheduleAdmin)
admin.site.register(ounissi_app.models.ScheduleEmployee, ScheduleRowAdmin)
admin.site.register(ounissi_app.models.ScheduleTask, ScheduleRowAdmin)
admin.site.register(ounissi_app.models.Task)
admin.site.register(ounissi_app.models.Terminal)
admin.site.register(ounissi_app.models.SalaryItem, SalaryItemAdmin)
admin.site.register(ounissi_app.models.Employee)
admin.site.register(ounissi_app.models.XpertServer, XpertServerAdmin)
admin.site.register(ounissi_app.models.Timesheet, TimesheetAdmin)
admin.site.register(ounissi_app.models.Salary)
admin.site.register(ounissi_app.models.SalaryDetail)
admin.site.register(ounissi_app.models.Shift)
admin.site.register(ounissi_app.models.TaskType)
admin.site.register(ounissi_app.models.Query, QueryAdmin)
admin.site.register(ounissi_app.models.Reception)
admin.site.register(ounissi_app.models.TaskGeneration)
admin.site.register(ounissi_app.models.Saisie)
admin.site.register(ounissi_app.models.ReceptionProduit)
admin.site.register(ounissi_app.models.ReceptionVignette)
admin.site.register(ounissi_app.models.ReceptionPlacement)
admin.site.register(ounissi_app.models.StockOperation)
admin.site.register(ounissi_app.models.AssignationEmplacement)





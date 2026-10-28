from django import template
from datetime import timedelta

register = template.Library()

@register.filter(name='hours_minutes')
def hours_minutes(value):
    if value is None or value == "":
        return "00:00"

    # 1. Handle Timedelta (Manager view logic)
    if isinstance(value, timedelta):
        total_seconds = int(value.total_seconds())
        h = total_seconds // 3600
        m = (total_seconds % 3600) // 60
        return f"{h:02d}:{m:02d}"

    # 2. Handle Numeric (Employee view: e.g., 6.0 hours)
    try:
        # We multiply by 60 to convert hours to minutes
        total_minutes = int(float(value) * 60)
        h = total_minutes // 60
        m = total_minutes % 60
        return f"{h:02d}:{m:02d}"
    except (ValueError, TypeError):
        return "00:00"

@register.filter(name='model_fields')
def get_model_fields(queryset):
    return queryset.model._meta.get_fields()

@register.filter
def division(value, divisor):
    """
    Custom template filter to perform division.
    """
    try:
        return value // divisor
    except (TypeError, ZeroDivisionError):
        return value

@register.filter
def multiplication(value, multiplier):
    return value * multiplier

@register.filter
def substruction(value1, value2):
    return value1 - value2

@register.filter
def addition(value1, value2):
    return value1 + value2

@register.filter
def div(value, divisor):
    """
    Custom template filter to perform division.
    """
    try:
        return value / divisor
    except (TypeError, ZeroDivisionError):
        return value


@register.filter
def get_item(dictionary, key):
    return dictionary.get(key, [])

@register.filter
def get_assignments_for_employee(assignments, employee_id):
    return [assignment for assignment in assignments if assignment.employee.id == employee_id]

@register.filter
def get_assignment_for_operation(assignments, operation_id):
    return next((assignment for assignment in assignments if assignment.operation.id == operation_id), None)

    # accounts/templatetags/custom_filters.py (or your app's custom_filters.py)


@register.filter
def get_item_key(dictionary, key):
    """
    Allows lookup inside a dictionary using a variable key in Django templates.
    """
    if not isinstance(dictionary, dict):
        return None
    return dictionary.get(key)

@register.filter
def short_day_fr(value):
    if not value:
        return ""

    days = {
        0: "Lun",
        1: "Mar",
        2: "Mer",
        3: "Jeu",
        4: "Ven",
        5: "Sam",
        6: "Dim",
    }

    return f"{days[value.weekday()]} {value.strftime('%d/%m')}"



@register.filter
def get_task_by_step(tasks, step):
    """Find a prefetched reception task without issuing a query per cell."""
    return next((task for task in tasks if task.task_step == step), None)

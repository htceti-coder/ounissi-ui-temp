import json
import calendar
import logging
from datetime import datetime, timedelta, time
from django.apps import apps
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.module_loading import import_string
from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest, HttpResponseRedirect, HttpResponseNotAllowed
from django.db import IntegrityError, connection
from django.core.exceptions import PermissionDenied, ValidationError
import pandas as pd

from .models import *
from .forms import *
from accounts.models import Employee, Team
from .attendance_importer import import_attendance_logs, timesheet_dataframe, save_logs, import_attendance_and_users
from .xpertcon import XpertConnect
from .query_results import column_values
from .salary_calculator import SalaryCalculator
from .utils import getLoggedData, generate_timesheet_excel, can_manage_timesheets
from .decorators import with_logged_data

# Import the new service layer
from . import schedule_service, kpi_service
logger = logging.getLogger(__name__)
# additional


@login_required
def timesheet_list(request):
    if not can_manage_timesheets(request):
        raise PermissionDenied
    filter_form = TimesheetFilterForm(request.GET)

    # 1. Date boundaries
    today = timezone.localtime().date()
    start_date = today.replace(day=1)
    end_date = today

    specific_xp_id = '%%'
    f_employees = None

    if filter_form.is_valid():
        f_start = filter_form.cleaned_data.get('start_date')
        f_end = filter_form.cleaned_data.get('end_date')
        f_employees = filter_form.cleaned_data.get('employees')

        if f_start: start_date = f_start
        if f_end: end_date = f_end

    # Determine date range
    delta = end_date - start_date
    date_list = [start_date + timedelta(days=i) for i in range(delta.days + 1)]

    # Determine employee list
    if f_employees:
        if isinstance(f_employees, Employee):
            employees_list = [f_employees]
            specific_xp_id = f_employees.xp_id or '%%'
        elif hasattr(f_employees, 'all'):
            employees_list = list(f_employees.all())
            if len(employees_list) == 1:
                specific_xp_id = employees_list[0].xp_id or '%%'
        else:
            employees_list = list(f_employees)
    else:
        # Fallback: Fetch all active employees (or simply all employees)
        employees_list = list(Employee.objects.all())

    # Fetch existing timesheet records and index them
    db_timesheets = Timesheet.objects.filter(
        date__gte=start_date,
        date__lte=end_date,
        employee__in=employees_list
    ).select_related('employee')

    timesheet_map = {(ts.employee_id, ts.date): ts for ts in db_timesheets}

    # 2. XpertPharm Audit Fetch
    xpert_logs_map = {}
    xpert_audit_unavailable = False
    if employees_list:
        query_str = """
                    SELECT CAST(CREATED_ON AS DATE)         AS OP_DATE,
                           CREATED_BY                       AS OP_USER,
                           CAST(MIN(CREATED_ON) AS TIME(0)) AS FIRST_LOG,
                           CAST(MAX(CREATED_ON) AS TIME(0)) AS LAST_LOG
                    FROM SYS_OBJET_TRACE WITH (NOLOCK)
                    WHERE CREATED_ON >= :start_date
                      AND CREATED_ON < :end_date
                      AND (:xp_id = '%%' OR CREATED_BY = :xp_id)
                    GROUP BY CREATED_BY, CAST (CREATED_ON AS DATE)
                    OPTION (RECOMPILE);
                    """
        params = {
            'start_date': start_date,
            'end_date': end_date + timedelta(days=1),
            'xp_id': specific_xp_id
        }

        try:
            results = XpertConnect().run_query(query_str, params)
            columns = results['columns']
            date_idx = columns.index('OP_DATE')
            user_idx = columns.index('OP_USER')
            first_idx = columns.index('FIRST_LOG')
            last_idx = columns.index('LAST_LOG')
            for row in results['data_rows']:
                key = (str(row[user_idx]), str(row[date_idx]))
                xpert_logs_map[key] = {'first': row[first_idx], 'last': row[last_idx]}
        except Exception:
            logger.warning('Xpert audit logs unavailable', exc_info=True)
            xpert_audit_unavailable = True

    # 3. Data Assembly (Grid: Employees × Dates)
    timesheets_with_hours = []

    for emp in employees_list:
        for current_date in date_list:
            # Find existing record or use an unsaved fallback instance
            ts = timesheet_map.get((emp.id, current_date))
            is_new_row = False

            if not ts:
                ts = Timesheet(employee=emp, date=current_date)
                is_new_row = True

            # --- OPTIMIZATION: IMMEDIATE LEAVE BYPASS ---
            # If the record is saved and has an active leave type, we skip all heavy logic
            if not is_new_row and ts.leave_type != Timesheet.LeaveType.NONE:
                timesheets_with_hours.append({
                    'obj': ts,
                    'hours': timedelta(0),       # Leaves default to 0 biometric working hours
                    'xp_first': '--',
                    'xp_last': '--',
                    'xp_total': '--',
                    'warning': False,            # No warnings triggered for approved or pending leaves
                    'is_new_row': False
                })
                continue  # Jump directly to the next day
            # ---------------------------------------------

            raw_hours = 0 if is_new_row else (ts.calculate_working_hours() or 0)
            bio_hours = timedelta(hours=float(raw_hours))

            log = xpert_logs_map.get((str(emp.xp_id), str(current_date)), {'first': '--', 'last': '--'})

            xp_total_str = "--"
            warning = False

            if log['first'] != '--' and log['last'] != '--':
                try:
                    t1_val = log['first']
                    t2_val = log['last']

                    t1_obj = t1_val if isinstance(t1_val, time) else datetime.strptime(str(t1_val), '%H:%M:%S').time()
                    t2_obj = t2_val if isinstance(t2_val, time) else datetime.strptime(str(t2_val), '%H:%M:%S').time()

                    dummy_today = datetime.today()
                    dt1 = datetime.combine(dummy_today, t1_obj)
                    dt2 = datetime.combine(dummy_today, t2_obj)

                    xp_delta = dt2 - dt1
                    total_seconds = int(xp_delta.total_seconds())

                    if total_seconds > 0:
                        h, remainder = divmod(total_seconds, 3600)
                        m, _ = divmod(remainder, 60)
                        xp_total_str = f"{h:02d}:{m:02d}"

                    # Warning Logic
                    if xp_delta > (bio_hours + timedelta(minutes=5)):
                        warning = True
                    elif bio_hours.total_seconds() == 0 and total_seconds > 300:
                        warning = True

                except Exception as e:
                    print(f"DEBUG Error for {emp}: {e}")

            # Missing Logout Check (skip if row is completely empty/unsaved)
            if not is_new_row:
                if (ts.start_time and not ts.end_time) or \
                        (ts.start_time_2 and not ts.end_time_2) or \
                        (ts.start_time_3 and not ts.end_time_3):
                    warning = True

            timesheets_with_hours.append({
                'obj': ts,
                'hours': bio_hours,
                'xp_first': log['first'],
                'xp_last': log['last'],
                'xp_total': xp_total_str,
                'warning': warning,
                'is_new_row': is_new_row
            })

    # 4. Trigger isolated export if requested
    if request.GET.get('export') == 'excel':
        return generate_timesheet_excel(timesheets_with_hours, start_date, end_date, xpert_audit_unavailable)

    # 5. Define the context before returning
    context = {
        'filter_form': filter_form,
        'timesheets_with_hours': timesheets_with_hours,
        'xpert_audit_unavailable': xpert_audit_unavailable,
        'logged_data': getLoggedData(request) if 'getLoggedData' in globals() else None
    }

    return render(request, 'timesheet/timesheet_list.html', context)


@login_required
def timesheet_create(request):
    if request.method == 'POST':
        form = TimesheetForm(request.POST)
        if form.is_valid():
            timesheet = form.save(commit=False)
            employee_id = form.cleaned_data['employee']
            employee = Employee.objects.get(id=employee_id)
            timesheet.employee = employee
            timesheet.save()
            return redirect('timesheet_list')
    else:
        form = TimesheetForm()
    return render(request, 'timesheet/timesheet_create.html', {'form': form})


@login_required
def timesheet_update(request, pk):
    timesheet = get_object_or_404(Timesheet, pk=pk)
    if request.method == 'POST':
        form = TimesheetForm(request.POST, instance=timesheet)
        if form.is_valid():
            form.save()
            return redirect('timesheet_list')
        else:
            form = TimesheetForm(instance=timesheet)
            return render(request, 'timesheet/update_timesheet.html', {'form': form})


@login_required
def timesheet_delete(request, pk):
    timesheet = get_object_or_404(Timesheet, pk=pk)
    if request.method == 'POST':
        timesheet.delete()
        return redirect('timesheet_list')
    return render(request, 'timesheet/timesheet_delete.html', {'timesheet': timesheet})


@login_required
def timesheet_form(request):
    if request.method == 'POST':
        form = TimesheetForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('timesheet_list')
    else:
        form = TimesheetForm()
    return render(request, 'timesheet/timesheet_form.html', {'form': form})


@login_required
def attendance_logs(request):
    if request.method == 'GET':
        start_date = request.GET.get('start_date')
        end_date = request.GET.get('end_date')
        zk_id = request.GET.get('employee')

        all_logs = []
        terminal_user_map = {}

        # Calculate the 1st day of the current month
        today = timezone.localtime().date()
        first_day_current_month = today.replace(day=1)

        # Assuming helper function parse_date is imported or declared
        parsed_start_date = parse_date(start_date) if start_date else first_day_current_month
        parsed_end_date = parse_date(end_date) if end_date else today

        terminals = Terminal.objects.all()

        for terminal in terminals:
            # CHANGE THIS LINE to use import_attendance_and_users:
            device_logs, device_users = import_attendance_and_users(device=terminal.ip_address)

            if device_logs:
                all_logs.extend(device_logs)

            if device_users:
                for user in device_users:
                    # Clean trailing null characters from device names
                    cleaned_name = user.name.strip('\x00').strip() if user.name else ""
                    if cleaned_name:
                        terminal_user_map[str(user.user_id)] = cleaned_name

        logs = all_logs

        if zk_id:
            filtered_logs = [
                log for log in logs
                if parsed_start_date <= log.timestamp.date() <= parsed_end_date
                   and str(log.user_id) == str(zk_id)
            ]
        else:
            filtered_logs = [
                log for log in logs
                if parsed_start_date <= log.timestamp.date() <= parsed_end_date
            ]

        employees = Employee.objects.all()
        connection_status = True if logs else False

        if connection_status:
            # Pass our terminal name dictionary mapping to the dataframe handler
            attendance_df = timesheet_dataframe(filtered_logs, terminal_user_map=terminal_user_map)
            jsondata = attendance_df.reset_index().to_json(orient='records')
            data = json.loads(jsondata)
        else:
            data = []

        context = {
            'data': data,
            'employees': employees,
            'connection_status': connection_status,
            'start_date': start_date,
            'end_date': end_date,
            'terminals': terminals,
            'selected_employee': zk_id,
            'logged_data': getLoggedData(request) if 'getLoggedData' in globals() else None
        }
        return render(request, 'timesheet/attendance_logs.html', context)

@login_required
def save_data(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        save_df = timesheet_dataframe(import_attendance_logs())
        save_df = save_df[save_df['employee_name'] != 'inexistant']

        if data['start_date']:
            save_df = save_df[parse_date(data['start_date']) <= save_df['date']]
        if data['end_date']:
            save_df = save_df[parse_date(data['end_date']) >= save_df['date']]
        if data['employee']:
            save_df = save_df[data['employee'] == save_df['user_id']]

        save_logs(save_df)
        return JsonResponse({'message': 'Data saved successfully'})

    return JsonResponse({'error': 'Invalid request method'})

#leaves management
def group_timesheets_into_periods(timesheets_queryset):
    """
    Helper to merge individual daily timesheet leave rows into continuous leave periods.
    """
    from collections import defaultdict
    emp_map = defaultdict(list)
    for ts in timesheets_queryset:
        emp_map[ts.employee].append(ts)

    periods = []
    for emp, ts_list in emp_map.items():
        ts_list.sort(key=lambda x: x.date)
        if not ts_list:
            continue

        # Initialize the first period boundary
        current_period = {
            'employee': emp,
            'leave_type': ts_list[0].leave_type,
            'leave_type_display': ts_list[0].get_leave_type_display(),
            'start_date': ts_list[0].date,
            'end_date': ts_list[0].date,
            'is_confirmed': ts_list[0].is_confirmed,
            'ids': [ts_list[0].id]
        }

        for ts in ts_list[1:]:
            # Check if this timesheet record is consecutive, of the same type, and shares confirmation state
            is_consecutive = (ts.date - current_period['end_date']).days == 1
            same_type = (ts.leave_type == current_period['leave_type'])
            same_status = (ts.is_confirmed == current_period['is_confirmed'])

            if is_consecutive and same_type and same_status:
                current_period['end_date'] = ts.date
                current_period['ids'].append(ts.id)
            else:
                periods.append(current_period)
                current_period = {
                    'employee': emp,
                    'leave_type': ts.leave_type,
                    'leave_type_display': ts.get_leave_type_display(),
                    'start_date': ts.date,
                    'end_date': ts.date,
                    'is_confirmed': ts.is_confirmed,
                    'ids': [ts.id]
                }
        periods.append(current_period)

    for p in periods:
        p['num_days'] = (p['end_date'] - p['start_date']).days + 1

    # Sort periods by start date descending
    periods.sort(key=lambda x: x['start_date'], reverse=True)
    return periods


@login_required
def manage_leaves(request):
    logged_data = getLoggedData(request) if 'getLoggedData' in globals() else None

    # 1. Process Actions (POST)
    if request.method == 'POST':
        raw_ids = request.POST.getlist('timesheet_ids')
        action = request.POST.get('action')

        # Flatten comma-separated lists of IDs from the UI check-boxes
        timesheet_ids = []
        for r_id in raw_ids:
            timesheet_ids.extend([int(x) for x in r_id.split(',') if x])

        if timesheet_ids:
            with transaction.atomic():
                selected_ts = Timesheet.objects.filter(id__in=timesheet_ids)
                if action == 'approve':
                    count = selected_ts.count()
                    selected_ts.update(is_confirmed=True)
                    messages.success(request, f"{count} journée(s) de congé validée(s) avec succès.")
                elif action == 'reject':
                    count = selected_ts.count()
                    selected_ts.update(leave_type=Timesheet.LeaveType.NONE, is_confirmed=False)
                    messages.success(request, f"{count} journée(s) de congé rejetée(s)/annulée(s).")
            return redirect('manage_leaves')

    # 2. Fetch all employees and calculate real-time balances
    all_employees = list(Employee.objects.all().order_by('last_name', 'first_name'))
    employee_balances = []
    for emp in all_employees:
        employee_balances.append({
            'employee': emp,
            'balance': emp.calculate_leave_balance()
        })

    # 3. Fetch leave periods (grouped timesheets)
    all_leaves_qs = Timesheet.objects.exclude(
        leave_type=Timesheet.LeaveType.NONE
    ).select_related('employee')

    leave_periods = group_timesheets_into_periods(all_leaves_qs)

    context = {
        'employee_balances': employee_balances,
        'leave_periods': leave_periods,
        'logged_data': logged_data
    }
    return render(request, 'timesheet/manage_leaves.html', context)
@login_required
def salary_item_list(request):
    salary_items = SalaryItem.objects.all()
    context = {'salary_items': salary_items,
               'logged_data': getLoggedData(request)}
    return render(request, 'salary/salary_item_list.html', context)


@login_required
def salary_item_create(request):
    if request.method == 'POST':
        form = SalaryItemForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('salary_item_list')
    else:
        form = SalaryItemForm()
    return render(request, 'salary/salary_item_create.html', {'form': form})


@login_required
def salary_item_update(request, pk):
    salary_item = get_object_or_404(SalaryItem, pk=pk)
    if request.method == 'POST':
        form = SalaryItemForm(request.POST, instance=salary_item)
        if form.is_valid():
            form.save()
            return redirect('salary_item_list')
    else:
        form = SalaryItemForm(instance=salary_item)
    return render(request, 'salary/salary_item_update.html', {'form': form})


@login_required
def salary_item_delete(request, pk):
    salary_item = get_object_or_404(SalaryItem, pk=pk)
    if request.method == 'POST':
        salary_item.delete()
        return redirect('salary_item_list')
    return render(request, 'salary/salary_item_delete.html', {'salary_item': salary_item})


@login_required
def testconn(request):
    context = {'logged_data': getLoggedData(request)}
    return render(request, 'home.html', context)


@login_required
def test_query_submission(request):
    if request.method == 'POST':
        query_text = request.POST.get('queryText')
        parameters = get_dynamic_parameter(request)
        try:
            query_type = int(request.POST.get('type', ''))
            if query_type not in (0, 1):
                raise ValueError
        except ValueError:
            return render(request, 'queries/query_result.html', {'error_message': 'Type de requête invalide.'}, status=400)
        temp_query = Query(
            name='temp',
            type=query_type,
            query=query_text,
            parameters=parameters
        )

        query_results = get_query_results(temp_query, parameters)
        context = {'query_results': query_results,
                   'logged_data': getLoggedData(request)}
        return render(request, 'queries/query_result.html', context)
    return render(request, 'queries/query_result.html', {'error_message': 'Invalid form'})


@login_required
def get_query_details(request, query_id):
    query = get_object_or_404(Query, pk=query_id)
    parameters = [{'name': key, 'value': value} for key, value in query.parameters.items() if key != 'model_name']
    return JsonResponse({'parameters': parameters})


@login_required
def execute_query(request):
    if request.method == 'POST':
        conn = None
        query_id = request.POST.get('query', '')
        if not query_id.isdecimal():
            return render(request, 'queries/query_result.html', {'error_message': 'Sélectionnez une requête.'}, status=400)
        query = get_object_or_404(Query, pk=query_id)
        parameters = query.parameters.copy()

        for key in parameters.keys():
            param = request.POST.get(key)
            if param:
                parameters[key] = param

        try:
            if query.type == 0:
                with connection.cursor() as cursor:
                    cursor.execute(query.query, parameters)
                    results = cursor.fetchall()
                    columns = [col[0] for col in cursor.description]

                data_rows = []
                op_user_index = columns.index('OP_USER') if 'OP_USER' in columns else -1

                for row in results:
                    row_list = list(row)
                    if op_user_index != -1:
                        try:
                            user_id = row_list[op_user_index]
                            xp_id = User.objects.get(pk=user_id).employee.xp_id
                            row_list[op_user_index] = xp_id
                        except Exception:
                            pass
                    data_rows.append(tuple(row_list))

                query_results = {'columns': columns, 'data_rows': data_rows}

            else:
                conn = XpertConnect()
                conn.open_session()
                query_results = conn.run_query(query.query, parameters)

            return render(request, 'queries/query_result.html', {'query_results': query_results})

        except (ValueError, ConnectionError, Exception) as ex:
            return render(request, 'queries/query_result.html',
                          {'error_message': f"Error executing the query: {str(ex)}"})
        finally:
            if query.type != 0 and conn:
                conn.close_session()

    return render(request, 'queries/query_result.html', {'error_message': "Invalid request"})


@login_required
def get_dynamic_parameter(request):
    parameters = {}
    for key, value in request.POST.items():
        if key.startswith('parameter') and value:
            param_name = value
            param_value = request.POST.get(f'value_{key}', '')
            parameters[param_name] = param_value
    return parameters


@login_required
def create_query(request):
    if request.method == 'POST':
        query_name = request.POST.get('queryName')
        query_text = request.POST.get('queryText')
        query_type = request.POST.get('type')
        parameters = get_dynamic_parameter(request)

        try:
            Query.objects.create(
                name=query_name,
                query=query_text,
                parameters=parameters,
                type=query_type,
                help=request.POST.get('help', '')
            )
            return redirect('test_queries')

        except IntegrityError:
            error_message = "A query with this name already exists. Please choose a different name."
            return render(request, 'queries/create_query.html', {'error_message': error_message})

    context = {'logged_data': getLoggedData(request)}
    return render(request, 'queries/create_query.html', context)


@login_required
def test_queries(request):
    queries = Query.objects.all()
    context = {'queries': queries,
               'logged_data': getLoggedData(request)}
    return render(request, 'queries/test_queries.html', context)


@login_required
def queries_list(request):
    queries = Query.objects.all()
    context = {'queries': queries,
               'logged_data': getLoggedData(request)}
    return render(request, 'queries/queries_list.html', context)


@login_required
def salary_list(request):
    employees = Employee.objects.all().order_by('last_name')
    salaries = Salary.objects.all().order_by('employee', 'month')

    if request.method == 'GET':
        employee = request.GET.get('employee')
        month = request.GET.get('month')

        if employee:
            salaries = salaries.filter(employee=employee)
        if month:
            try:
                month = datetime.strptime(month, '%Y-%m')
                month = month.replace(day=1)
            except ValueError:
                month = None
            salaries = salaries.filter(month=month)

    context = {
        'employees': employees,
        'salaries': salaries,
        'logged_data': getLoggedData(request)
    }
    return render(request, 'salary/salary_list.html', context)


@login_required
def get_salary_details(request, salary_id):
    salary = get_object_or_404(Salary, pk=salary_id)
    formset = SalaryDetailFormSet(queryset=SalaryDetail.objects.filter(salary=salary))

    context = {
        'salary': salary,
        'formset': formset,
        'logged_data': getLoggedData(request)
    }
    return render(request, 'salary/salary_detail_list.html', context)


@login_required
def create_or_update_salary(employee, month):
    salary, created = Salary.objects.get_or_create(employee=employee, month=month)
    formset = SalaryDetailFormSet(
        queryset=SalaryDetail.objects.filter(salary=salary),
        prefix='salarydetails'
    )
    return salary, formset


@login_required
def add_update_salary(request):
    employees = Employee.objects.all()

    if request.method == 'POST':
        employee_id = request.POST.get('employee')
        month = request.POST.get('month')
        employee = Employee.objects.get(pk=employee_id)
        salary, formset = create_or_update_salary(employee, month)

        if formset.is_valid():
            for form in formset:
                salary_item = form.cleaned_data['salary_item']
                value = form.cleaned_data['value']
                detail, created = SalaryDetail.objects.get_or_create(
                    salary=salary,
                    salary_item=salary_item
                )
                detail.value = value
                detail.save()
            return HttpResponseRedirect('success-url')
    else:
        employee_id = request.GET.get('employee', None)
        month = request.GET.get('month', None)
        if employee_id and month:
            employee = Employee.objects.get(pk=employee_id)
            _, formset = create_or_update_salary(employee, month)
        else:
            formset = None

    context = {
        'formset': formset,
        'employees': employees,
        'logged_data': getLoggedData(request)
    }
    return render(request, 'salary/salary_list.html', context)


@login_required
def save_salary_details(request):
    formset = SalaryDetailFormSet(request.POST)
    if formset.is_valid():
        formset.save()
    context = {'formset': formset, 'logged_data': getLoggedData(request)}
    return render(request, 'salary/salary_list.html', context)


@login_required
def get_filtered_employees(request):
    responsibility_level = request.GET.get('responsibility_level')
    employees = Employee.objects.filter(rank__value__lte=responsibility_level)
    return JsonResponse([{'id': employee.id, 'name': str(employee)} for employee in employees], safe=False)


@login_required
def create_task(request):
    if request.method == 'POST':
        form = TaskCreationForm(request.POST)
        if form.is_valid():
            task = form.save(commit=False)
            task.save()
    else:
        form = TaskCreationForm()

    context = {'form': form, 'logged_data': getLoggedData(request)}
    return render(request, 'tasks/task_create.html', context)


@login_required
def tasks_list(request):
    tasks = Task.objects.all()
    context = {'tasks': tasks, 'logged_data': getLoggedData(request)}
    return render(request, 'tasks/task_list.html', context)


@login_required
def create_schedule(request):
    schedule_items = []
    url = "/schedule/plan"
    if request.method == 'POST':
        schedule_date = request.POST.get('schedule_date')
        start_time = request.POST.get('start_time')
        end_time = request.POST.get('end_time')
        schedule_name = request.POST.get('schedule_name')

        tasks = json.loads(request.POST.get('tasks'))
        employees = json.loads(request.POST.get('employees'))
        scheduleItems = json.loads(request.POST.get('scheduleItems'))

        is_template = True if schedule_name else False

        if tasks and employees and scheduleItems:
            schedule = Schedule.objects.create(date=schedule_date, start_time=start_time, end_time=end_time,
                                               schedule_name=schedule_name, is_template=is_template)

            for task in tasks:
                total_time = timedelta(minutes=task['total_duration'])
                total_assigned_time = timedelta(minutes=task['total_assigned_time'])
                ScheduleTask.objects.create(schedule=schedule, task_id=task['task_id'], total_time=total_time,
                                            total_assigned_time=total_assigned_time)

            for employee in employees:
                working_hours = timedelta(minutes=employee['working_hours'])
                total_assigned_time = timedelta(minutes=employee['total_assigned_hours'])
                ScheduleEmployee.objects.create(schedule=schedule, employee_id=employee['employee_id'],
                                                working_hours=working_hours, total_assigned_time=total_assigned_time)

            for scheduleItem in scheduleItems:
                assigned_time = timedelta(minutes=scheduleItem['assigned_time'])
                employee = Employee.objects.get(id=scheduleItem['employee_id'])
                task = Task.objects.get(id=scheduleItem['task_id'])
                ScheduleItem.objects.create(
                    schedule=schedule,
                    employee=employee,
                    task=task,
                    assigned_time=assigned_time,
                    progression_rate=0
                )

            generate_schedule_items_for_stock_operations(schedule)

            if is_template:
                return JsonResponse({'success': True, 'url': url})
            else:
                schedule_service.update_all_schedule_statuses()
                return redirect('schedule_details')

    else:
        tasks_objects = Task.objects.exclude(task_type__in=[3, 4]).order_by('responsibility_level', 'priority')
        tasks = {}
        for task in tasks_objects:
            task_type = task.task_type
            tasks.setdefault(task_type, []).append(task)

        employees_objects = Employee.objects.all().order_by('rank')
        employees = {}
        for employee in employees_objects:
            employee_rank = employee.rank
            employees.setdefault(employee_rank, []).append(employee)

        teams = Team.objects.all()
        context = {'tasks': tasks,
                   'employees': employees,
                   'schedule_items': schedule_items,
                   'teams': teams,
                   'logged_data': getLoggedData(request)}
        return render(request, 'schedule/create_schedule.html', context)


@login_required
def scheduleItem_update(request, pk):
    schedule_service.update_all_schedule_statuses()
    referer = request.META.get('HTTP_REFERER')
    schedule_item = get_object_or_404(ScheduleItem, pk=pk)

    if request.method == 'POST':
        form = ScheduleItemForm(request.POST, instance=schedule_item)
        if form.is_valid() and referer:
            model_form = form.save(commit=False)

            new_item, assigned_delta = schedule_service.update_schedule_from_item(
                employee_id=model_form.employee.id,
                task_id=model_form.task.id,
                schedule_id=model_form.schedule.id,
                assigned_time=model_form.assigned_time,
                progression_rate=model_form.progression_rate,
                task_identifier=schedule_item.task_identifier if schedule_item.task_identifier else None,
                item_id=pk
            )
            schedule_service.update_employee_task_from_item(new_item, assigned_delta)

            return redirect(referer, schedule_id=schedule_item.schedule.id)
        else:
            form = ScheduleItemForm(instance=schedule_item)
            context = {'form': form, 'logged_data': getLoggedData(request)}
            return render(request, 'schedule/schedule_item_update.html', context)


@login_required
def schedule_items_for_schedule(request, schedule_id):
    schedule_service.update_all_schedule_statuses()
    schedule = get_object_or_404(Schedule, pk=schedule_id)
    schedule_tasks = ScheduleTask.objects.filter(schedule=schedule)
    schedule_employees = ScheduleEmployee.objects.filter(schedule=schedule)
    context = {'schedule': schedule,
               'schedule_tasks': schedule_tasks,
               'schedule_employees': schedule_employees,
               'logged_data': getLoggedData(request)}
    return render(request, 'schedule/schedule_items_for_schedule.html', context)


@login_required
def schedule_details2(request, schedule_id=None):
    schedule_service.update_all_schedule_statuses()
    selected_schedule = None
    employees = Employee.objects.all()
    tasks = Task.objects.all()
    form = ScheduleSelectionForm(request.POST or None)

    if schedule_id:
        selected_schedule = schedule_id
    elif form.is_valid():
        selected_schedule = form.cleaned_data['schedule']

    if selected_schedule:
        schedule_items = ScheduleItem.objects.filter(schedule=selected_schedule)
        schedule_employees = ScheduleEmployee.objects.filter(schedule=selected_schedule)
        schedule_tasks = ScheduleTask.objects.filter(schedule=selected_schedule)
        schedule_model = Schedule.objects.get(pk=selected_schedule)

        context = {
            'selected_schedule': selected_schedule,
            'schedule_details': schedule_items,
            'schedule_employees': schedule_employees,
            'schedule_tasks': schedule_tasks,
            'employees': employees,
            'tasks': tasks,
            'schedule_date': schedule_model.date,
            'start_time': schedule_model.start_time,
            'end_time': schedule_model.end_time,
            'logged_data': getLoggedData(request)
        }
        return render(request, 'schedule/schedule_details2.html', context)
    else:
        form = ScheduleSelectionForm()

    context = {'form': form,
               'employees': employees,
               'tasks': tasks,
               'logged_data': getLoggedData(request)}
    return render(request, 'schedule/schedule_details2.html', context)


@login_required
def add_or_edit_schedule_item(request, item_id=None):
    referer = request.META.get('HTTP_REFERER')
    employees = Employee.objects.all()
    tasks = Task.objects.all()

    if request.method == 'POST':
        schedule_id = request.POST.get('schedule')
        form = ScheduleItemAddEditForm(request.POST)
        if form.is_valid() and referer:
            schedule_item, assigned_delta = schedule_service.update_schedule_from_item(
                employee_id=form.cleaned_data['employee'].id,
                task_id=form.cleaned_data['task'].id,
                schedule_id=schedule_id,
                assigned_time=form.cleaned_data['assigned_time'],
                progression_rate=form.cleaned_data['progression_rate'] if form.cleaned_data[
                                                                              'progression_rate'] != '' else None
            )

            schedule_service.update_employee_task_from_item(schedule_item, assigned_delta)
            return redirect(referer, schedule_id=schedule_item.schedule.id)
        else:
            return redirect(referer, schedule_id=schedule_id)
    else:
        schedule_item = get_object_or_404(ScheduleItem, pk=item_id) if item_id else None
        context = {'employees': employees,
                   'tasks': tasks,
                   'schedule_item': schedule_item,
                   'logged_data': getLoggedData(request)}
        return render(request, 'schedule_details.html', context)


@login_required
def get_model_form(request):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    task_id = request.POST.get('task_id')
    employee_id = request.POST.get('employee_id')
    schedule_id = request.POST.get('selected_schedule')
    try:
        task = Task.objects.get(pk=task_id)
        progress_query = task.progress_query
        model_name = (progress_query.parameters or {}).get('model_name') if progress_query else None
        if not model_name:
            raise ValueError('Le formulaire de cette tâche n’est pas configuré : renseignez model_name dans la requête de progression.')
        model = apps.get_model('ounissi_app', model_name)
        form_class_name = model.get_form()
        form_class = import_string(f'ounissi_app.forms.{form_class_name}')
        form = form_class()
        transfer_data = json.dumps({
            'task_id': task_id, 'employee_id': employee_id, 'schedule_id': schedule_id,
        })
        return render(request, 'tasks/model_form.html', {
            'form': form, 'form_title': task.task_name, 'form_class_name': form_class_name,
            'transfer_data': transfer_data, 'logged_data': getLoggedData(request),
        })
    except ValueError as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=422)
    except Exception:
        logger.exception('Unable to load task form')
        return JsonResponse({'success': False, 'message': 'Impossible de charger ce formulaire. Vérifiez la configuration de la tâche.'}, status=422)


@login_required
@transaction.atomic
def generic_form_submission_view(request, form_class_name):
    if request.method == 'POST':
        referer = request.META.get('HTTP_REFERER')
        form_class = import_string(f'ounissi_app.forms.{form_class_name}')
        form = form_class(request.POST)

        if form.is_valid():
            model_form = form.save(commit=False)
            model_form.created_by = request.user
            transfer_data = json.loads(request.POST.get('transfer_data'))
            task = Task.objects.get(pk=transfer_data['task_id'])
            if isinstance(model_form, Reception):
                employee = Employee.objects.filter(user=request.user).first()
                model_name = (task.progress_query.parameters or {}).get('model_name') if task.progress_query else None
                if not employee or str(employee.pk) != str(transfer_data.get('employee_id')):
                    raise PermissionDenied
                if model_name != 'Reception' or not task.menu or not employee.rank or employee.rank.value > task.responsibility_level.value:
                    raise PermissionDenied

            schedule_item, assigned_delta = schedule_service.update_schedule_from_item(
                task_id=task.id,
                assigned_time=task.average_duration,
                schedule_id=transfer_data['schedule_id'],
                employee_id=transfer_data['employee_id'],
                task_identifier=form.cleaned_data['task_identifier'],
                progression_rate=100
            )

            model_form.related_item = schedule_item
            model_form.save()
            schedule_service.update_employee_task_from_item(schedule_item, assigned_delta)
            generated = schedule_service.generate_group_task(schedule_item, schedule_item.schedule)
            if isinstance(model_form, Reception) and not generated:
                messages.warning(request, 'Réception enregistrée, mais aucune tâche suivante n’est configurée. Vérifiez les règles de génération des tâches.')
            return redirect(referer or 'home')
        else:
            return render(request, 'tasks/model_form_page.html', {
                'form': form, 'form_title': 'Corriger le formulaire',
                'form_class_name': form_class_name,
                'transfer_data': request.POST.get('transfer_data', ''),
                'logged_data': getLoggedData(request),
            }, status=400)

    return HttpResponseNotAllowed(['POST'])


def can_manage_trt(request):
    """Use the manager roles already exposed in the TRT navigation."""
    return request.user.groups.filter(name='Admin').exists() or bool(getLoggedData(request)['leader'])


def can_manage_reception(request):
    return can_manage_trt(request)


def reception_assignment_error(form):
    """Check the locked pending item against the displayed parent and employees."""
    if not form.is_valid():
        return 'Affectation non enregistrée. Vérifiez les champs du formulaire.'
    item = ScheduleItem.objects.select_for_update().select_related(
        'schedule', 'task__progress_query', 'task__responsibility_level',
    ).get(pk=form.cleaned_data['employee1_schedule_item'].pk)
    if item.employee_id:
        return 'Cette tâche a déjà été affectée. Actualisez le TRT avant de continuer.'
    configured_model = (item.task.progress_query.parameters or {}).get('model_name') if item.task.progress_query else None
    if configured_model != form._meta.model.__name__:
        return 'La tâche sélectionnée ne correspond pas à ce formulaire.'
    if item.task.average_duration is None or item.task.average_duration.total_seconds() <= 0:
        return 'Configurez une durée par ligne positive pour cette tâche.'

    original_item = schedule_service.get_original_item(item)
    original = form.cleaned_data.get('origine')
    if not original_item or not original or original not in schedule_service.get_related_item_details(original_item, type(original)):
        return 'La tâche précédente de cette réception est introuvable ou ne correspond plus à cette affectation.'
    supplier, reference = form.cleaned_data.get('supplier'), form.cleaned_data.get('ref_tiers')
    if not supplier or not reference or supplier != original.supplier or reference != original.ref_tiers:
        return 'Le fournisseur ou la référence ne correspond pas à la réception d’origine.'
    if schedule_service.reception_identifier(item.task_identifier) != f'{supplier}#{reference}':
        return 'La référence de cette tâche ne correspond pas à la réception d’origine.'
    if hasattr(original, 'number_of_lines') and form.cleaned_data.get('number_of_lines') != original.number_of_lines:
        return 'Le nombre de lignes ne correspond plus à l’étape précédente.'

    employee_ids = {form.cleaned_data[f'employee{i}'].pk for i in (1, 2, 3) if form.cleaned_data.get(f'employee{i}')}
    eligible_ids = set(ScheduleEmployee.objects.filter(
        schedule=item.schedule, employee_id__in=employee_ids,
        employee__rank__value__lte=item.task.responsibility_level.value,
    ).values_list('employee_id', flat=True))
    if employee_ids != eligible_ids:
        return 'Un employé sélectionné n’est pas éligible dans ce TRT.'
    form.instance.employee1_schedule_item = item
    # Additional schedule items are created from the validated employees and
    # ranges, never accepted as arbitrary IDs from a submitted form.
    form.instance.employee2_schedule_item = None
    form.instance.employee3_schedule_item = None
    return None


@login_required
def get_special_form(request):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    if not can_manage_reception(request):
        raise PermissionDenied

    try:
        task = Task.objects.get(pk=request.POST.get('task_id'))
        schedule_item = ScheduleItem.objects.get(pk=request.POST.get('schedule_item'))
        if schedule_item.task_id != task.id:
            raise ValueError('La tâche sélectionnée ne correspond pas à cette affectation.')
        if schedule_item.employee_id:
            raise ValueError('Cette tâche a déjà été affectée. Actualisez le TRT avant de continuer.')

        progress_query = task.progress_query
        model_name = (progress_query.parameters or {}).get('model_name') if progress_query else None
        if not model_name:
            raise ValueError('Le formulaire de cette tâche n’est pas configuré : renseignez model_name dans la requête de progression.')
        model = apps.get_model('ounissi_app', model_name)
        form_class_name = model.get_form()
        employees = ScheduleEmployee.objects.filter(
            schedule=schedule_item.schedule,
            employee__rank__value__lte=task.responsibility_level.value,
        )

        original_item = schedule_service.get_original_item(schedule_item)
        if not original_item:
            raise ValueError('La tâche précédente de cette réception est introuvable. Vérifiez la règle de génération et la référence.')
        original_query = original_item.task.progress_query
        original_model_name = (original_query.parameters or {}).get('model_name') if original_query else None
        if not original_model_name:
            raise ValueError('Le modèle de la tâche précédente n’est pas configuré.')
        original_model = apps.get_model('ounissi_app', original_model_name)
        original_details = schedule_service.get_related_item_details(original_item, original_model)
        if not original_details:
            raise ValueError('Les données de la tâche précédente sont introuvables pour cette réception.')
        original_task = original_details[0]
        if schedule_item.task.average_duration is None:
            raise ValueError('La durée par ligne de cette tâche n’est pas configurée.')
        seconds_per_line = schedule_item.task.average_duration.total_seconds()
        items_list = [int(seconds_per_line) if seconds_per_line.is_integer() else seconds_per_line]
        invoice = None
        invoice_warning = None

        if form_class_name in ('ReceptionVignetteForm', 'ReceptionProduitForm', 'ReceptionPlacementForm'):
            # Invoice details are a preview, not a prerequisite for planning.
            # The previous step records the number of lines used for assignment,
            # even while Xpert's invoice rows are still being entered.
            try:
                query = Query.objects.get(name='InvoiceHelper')
                parameters = (query.parameters or {}).copy()
                parameters.update(ref_tiers=original_task.ref_tiers, supplier=original_task.supplier)
                invoice = get_query_results(query, parameters)
                quantities = column_values(invoice, 'QUANTITE')
                if not quantities:
                    invoice_warning = 'Le détail de la facture n’est pas encore disponible dans Xpert. Vous pouvez affecter la tâche avec le nombre de lignes déjà saisi.'
            except (Query.DoesNotExist, ValueError):
                invoice = None
                invoice_warning = 'Le détail de la facture n’a pas pu être chargé depuis Xpert. Vous pouvez affecter la tâche avec le nombre de lignes déjà saisi.'

        return render(request, f'special_forms/{form_class_name}.html', {
            'model_schedule_item': schedule_item,
            'model_employees': employees,
            'original_task': original_task,
            'items_list': items_list,
            'invoice': invoice,
            'invoice_warning': invoice_warning,
        })
    except ValueError as exc:
        return render(request, 'special_forms/form_error.html', {'form_error': str(exc)}, status=422)
    except Exception:
        logger.exception('Unable to load reception form')
        return render(request, 'special_forms/form_error.html', {
            'form_error': 'Impossible de charger ce formulaire. Vérifiez la tâche et sa configuration.'
        }, status=422)


@login_required
@transaction.atomic
def special_form_saisie(request):
    if not can_manage_reception(request):
        raise PermissionDenied
    referer = request.META.get('HTTP_REFERER')
    if request.method == 'POST':
        form = SaisieForm(request.POST)
        validation_error = reception_assignment_error(form)
        if not validation_error:
            model_form = form.save(commit=False)
            task_id = model_form.employee1_schedule_item.task.id
            task_identifier = schedule_service.reception_identifier(model_form.employee1_schedule_item.task_identifier)
            schedule_id = model_form.employee1_schedule_item.schedule.id
            average_duration = model_form.employee1_schedule_item.task.average_duration

            model_form.employee1_schedule_item.employee = model_form.employee1
            assigned_time_1 = average_duration * (1 + model_form.employee1_range_end - model_form.employee1_range_start)
            task_identifier1 = f"{task_identifier}Lines:{model_form.employee1_range_start}<>{model_form.employee1_range_end}"

            schedule_item, assigned_delta = schedule_service.update_schedule_from_item(
                task_id=task_id,
                assigned_time=assigned_time_1,
                schedule_id=schedule_id,
                employee_id=model_form.employee1.id,
                task_identifier=task_identifier1,
                item_id=model_form.employee1_schedule_item.id
            )

            if not schedule_service.generate_group_task(schedule_item, schedule_item.schedule):
                messages.warning(request, 'Affectation enregistrée, mais aucune tâche suivante n’est configurée. Vérifiez les règles de génération.')
            schedule_service.update_employee_task_from_item(schedule_item, assigned_delta)

            if model_form.employee2 and model_form.employee2_range_start and model_form.employee2_range_end:
                assigned_time_2 = average_duration * (
                            1 + model_form.employee2_range_end - model_form.employee2_range_start)
                task_identifier2 = f"{task_identifier}Lines:{model_form.employee2_range_start}<>{model_form.employee2_range_end}"

                model_form.employee2_schedule_item, assigned_delta = schedule_service.update_schedule_from_item(
                    task_id=task_id,
                    assigned_time=assigned_time_2,
                    schedule_id=schedule_id,
                    employee_id=model_form.employee2.id,
                    task_identifier=task_identifier2
                )
                schedule_service.update_employee_task_from_item(model_form.employee2_schedule_item, assigned_delta)

                if model_form.employee3 and model_form.employee3_range_start and model_form.employee3_range_end:
                    assigned_time_3 = average_duration * (
                                1 + model_form.employee3_range_end - model_form.employee3_range_start)
                    task_identifier3 = f"{task_identifier}Lines:{model_form.employee3_range_start}<>{model_form.employee3_range_end}"

                    model_form.employee3_schedule_item, assigned_delta = schedule_service.update_schedule_from_item(
                        task_id=task_id,
                        assigned_time=assigned_time_3,
                        schedule_id=schedule_id,
                        employee_id=model_form.employee3.id,
                        task_identifier=task_identifier3
                    )
                    schedule_service.update_employee_task_from_item(model_form.employee3_schedule_item, assigned_delta)
            form.save()
        else:
            messages.error(request, validation_error)
        return redirect(referer or 'actual_schedule_details')
    return HttpResponseNotAllowed(['POST'])


@login_required
@transaction.atomic
def special_form_produit(request):
    if not can_manage_reception(request):
        raise PermissionDenied
    referer = request.META.get('HTTP_REFERER')
    if request.method == 'POST':
        form = ReceptionProduitForm(request.POST)
        validation_error = reception_assignment_error(form)
        if not validation_error:
            model_form = form.save(commit=False)
            task_id = model_form.employee1_schedule_item.task.id
            task_identifier = schedule_service.reception_identifier(model_form.employee1_schedule_item.task_identifier)
            schedule_id = model_form.employee1_schedule_item.schedule.id
            average_duration = model_form.employee1_schedule_item.task.average_duration

            assigned_time_1 = average_duration * (1 + model_form.employee1_range_end - model_form.employee1_range_start)
            task_identifier1 = f"{task_identifier}Lines:{model_form.employee1_range_start}<>{model_form.employee1_range_end}"

            schedule_item, assigned_delta = schedule_service.update_schedule_from_item(
                task_id=task_id,
                assigned_time=assigned_time_1,
                schedule_id=schedule_id,
                employee_id=model_form.employee1.id,
                task_identifier=task_identifier1,
                item_id=model_form.employee1_schedule_item.id
            )

            if not schedule_service.generate_group_task(schedule_item, schedule_item.schedule):
                messages.warning(request, 'Affectation enregistrée, mais aucune tâche suivante n’est configurée. Vérifiez les règles de génération.')
            schedule_service.update_employee_task_from_item(schedule_item, assigned_delta)

            if model_form.employee2 and model_form.employee2_range_start and model_form.employee2_range_end:
                assigned_time_2 = average_duration * (
                            1 + model_form.employee2_range_end - model_form.employee2_range_start)
                task_identifier2 = f"{task_identifier}Lines:{model_form.employee2_range_start}<>{model_form.employee2_range_end}"

                model_form.employee2_schedule_item, assigned_delta = schedule_service.update_schedule_from_item(
                    task_id=task_id,
                    assigned_time=assigned_time_2,
                    schedule_id=schedule_id,
                    employee_id=model_form.employee2.id,
                    task_identifier=task_identifier2
                )
                schedule_service.update_employee_task_from_item(model_form.employee2_schedule_item, assigned_delta)

                if model_form.employee3 and model_form.employee3_range_start and model_form.employee3_range_end:
                    assigned_time_3 = average_duration * (
                                1 + model_form.employee3_range_end - model_form.employee3_range_start)
                    task_identifier3 = f"{task_identifier}Lines:{model_form.employee3_range_start}<>{model_form.employee3_range_end}"

                    model_form.employee3_schedule_item, assigned_delta = schedule_service.update_schedule_from_item(
                        task_id=task_id,
                        assigned_time=assigned_time_3,
                        schedule_id=schedule_id,
                        employee_id=model_form.employee3.id,
                        task_identifier=task_identifier3
                    )
                    schedule_service.update_employee_task_from_item(model_form.employee3_schedule_item, assigned_delta)
            form.save()
        else:
            messages.error(request, validation_error)
        return redirect(referer or 'actual_schedule_details')
    return HttpResponseNotAllowed(['POST'])


@login_required
@transaction.atomic
def special_form_vignette(request):
    if not can_manage_reception(request):
        raise PermissionDenied
    referer = request.META.get('HTTP_REFERER')
    if request.method == 'POST':
        form = ReceptionVignetteForm(request.POST)
        validation_error = reception_assignment_error(form)
        if not validation_error:
            model_form = form.save(commit=False)
            task_id = model_form.employee1_schedule_item.task.id
            task_identifier = schedule_service.reception_identifier(model_form.employee1_schedule_item.task_identifier)
            schedule_id = model_form.employee1_schedule_item.schedule.id
            average_duration = model_form.employee1_schedule_item.task.average_duration

            assigned_time_1 = average_duration * (1 + model_form.employee1_range_end - model_form.employee1_range_start)
            task_identifier1 = f"{task_identifier}Lines:{model_form.employee1_range_start}<>{model_form.employee1_range_end}"

            schedule_item, assigned_delta = schedule_service.update_schedule_from_item(
                task_id=task_id,
                assigned_time=assigned_time_1,
                schedule_id=schedule_id,
                employee_id=model_form.employee1.id,
                task_identifier=task_identifier1,
                item_id=model_form.employee1_schedule_item.id
            )

            if not schedule_service.generate_group_task(schedule_item, schedule_item.schedule):
                messages.warning(request, 'Affectation enregistrée, mais aucune tâche suivante n’est configurée. Vérifiez les règles de génération.')
            schedule_service.update_employee_task_from_item(schedule_item, assigned_delta)

            if model_form.employee2 and model_form.employee2_range_start and model_form.employee2_range_end:
                assigned_time_2 = average_duration * (
                            1 + model_form.employee2_range_end - model_form.employee2_range_start)
                task_identifier2 = f"{task_identifier}Lines:{model_form.employee2_range_start}<>{model_form.employee2_range_end}"

                model_form.employee2_schedule_item, assigned_delta = schedule_service.update_schedule_from_item(
                    task_id=task_id,
                    assigned_time=assigned_time_2,
                    schedule_id=schedule_id,
                    employee_id=model_form.employee2.id,
                    task_identifier=task_identifier2
                )
                schedule_service.update_employee_task_from_item(model_form.employee2_schedule_item, assigned_delta)

                if model_form.employee3 and model_form.employee3_range_start and model_form.employee3_range_end:
                    assigned_time_3 = average_duration * (
                                1 + model_form.employee3_range_end - model_form.employee3_range_start)
                    task_identifier3 = f"{task_identifier}Lines:{model_form.employee3_range_start}<>{model_form.employee3_range_end}"

                    model_form.employee3_schedule_item, assigned_delta = schedule_service.update_schedule_from_item(
                        task_id=task_id,
                        assigned_time=assigned_time_3,
                        schedule_id=schedule_id,
                        employee_id=model_form.employee3.id,
                        task_identifier=task_identifier3
                    )
                    schedule_service.update_employee_task_from_item(model_form.employee3_schedule_item, assigned_delta)
            form.save()
        else:
            messages.error(request, validation_error)
        return redirect(referer or 'actual_schedule_details')
    return HttpResponseNotAllowed(['POST'])


@login_required
@transaction.atomic
def special_form_placement(request):
    if not can_manage_reception(request):
        raise PermissionDenied
    referer = request.META.get('HTTP_REFERER')
    if request.method == 'POST':
        form = ReceptionPlacementForm(request.POST)
        validation_error = reception_assignment_error(form)
        if not validation_error:
            model_form = form.save(commit=False)
            task_id = model_form.employee1_schedule_item.task.id
            task_identifier = schedule_service.reception_identifier(model_form.employee1_schedule_item.task_identifier)
            schedule_id = model_form.employee1_schedule_item.schedule.id
            average_duration = model_form.employee1_schedule_item.task.average_duration

            assigned_time_1 = average_duration * (1 + model_form.employee1_range_end - model_form.employee1_range_start)
            task_identifier1 = f"{task_identifier}Lines:{model_form.employee1_range_start}<>{model_form.employee1_range_end}"

            schedule_item, assigned_delta = schedule_service.update_schedule_from_item(
                task_id=task_id,
                assigned_time=assigned_time_1,
                schedule_id=schedule_id,
                employee_id=model_form.employee1.id,
                task_identifier=task_identifier1,
                item_id=model_form.employee1_schedule_item.id
            )

            schedule_service.generate_group_task(schedule_item, schedule_item.schedule)
            schedule_service.update_employee_task_from_item(schedule_item, assigned_delta)

            if model_form.employee2 and model_form.employee2_range_start and model_form.employee2_range_end:
                assigned_time_2 = average_duration * (
                            1 + model_form.employee2_range_end - model_form.employee2_range_start)
                task_identifier2 = f"{task_identifier}Lines:{model_form.employee2_range_start}<>{model_form.employee2_range_end}"

                model_form.employee2_schedule_item, assigned_delta = schedule_service.update_schedule_from_item(
                    task_id=task_id,
                    assigned_time=assigned_time_2,
                    schedule_id=schedule_id,
                    employee_id=model_form.employee2.id,
                    task_identifier=task_identifier2
                )
                schedule_service.update_employee_task_from_item(model_form.employee2_schedule_item, assigned_delta)

                if model_form.employee3 and model_form.employee3_range_start and model_form.employee3_range_end:
                    assigned_time_3 = average_duration * (
                                1 + model_form.employee3_range_end - model_form.employee3_range_start)
                    task_identifier3 = f"{task_identifier}Lines:{model_form.employee3_range_start}<>{model_form.employee3_range_end}"

                    model_form.employee3_schedule_item, assigned_delta = schedule_service.update_schedule_from_item(
                        task_id=task_id,
                        assigned_time=assigned_time_3,
                        schedule_id=schedule_id,
                        employee_id=model_form.employee3.id,
                        task_identifier=task_identifier3
                    )
                    schedule_service.update_employee_task_from_item(model_form.employee3_schedule_item, assigned_delta)
            form.save()
        else:
            messages.error(request, validation_error)
        return redirect(referer or 'actual_schedule_details')
    return HttpResponseNotAllowed(['POST'])


def get_query_results(query, parameters):
    schedule_service.update_all_schedule_statuses()
    try:
        if query.type == 0:
            with connection.cursor() as cursor:
                cursor.execute(query.query, parameters)
                results = cursor.fetchall()

                columns = [col[0] for col in cursor.description]
                data_rows = []
                op_user_index = columns.index('OP_USER') if 'OP_USER' in columns else -1

                for row in results:
                    row_list = list(row)
                    if op_user_index != -1:
                        try:
                            user_id = row_list[op_user_index]
                            xp_id = User.objects.get(pk=user_id).employee.xp_id
                            row_list[op_user_index] = xp_id
                        except Exception:
                            pass
                    data_rows.append(tuple(row_list))

            return {'columns': columns, 'data_rows': data_rows}
        else:
            conn = XpertConnect()
            conn.open_session()
            query_results = conn.run_query(query.query, parameters)
            conn.close_session()
            return query_results
    except Exception as ex:
        return {'error': str(ex)}


@login_required
def update_schedule_now(request, schedule_id=None):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    schedule = get_object_or_404(Schedule, pk=schedule_id)
    logged_data = getLoggedData(request)
    can_manage = logged_data['leader'] or request.user.groups.filter(name='Admin').exists()
    mine_only = request.POST.get('scope') == 'mine' or not can_manage
    employee = logged_data['employee'] if mine_only else None
    if mine_only and not employee:
        messages.error(request, 'Aucun profil d’employé n’est associé à ce compte.')
        return redirect(request.META.get('HTTP_REFERER') or 'home')
    try:
        result = schedule_service.update_schedule_progress(schedule, employee=employee)
        if result['failed']:
            messages.warning(request, f"{result['failed']} tâche(s) n’ont pas pu être actualisées. Vérifiez la connexion et les requêtes Xpert.")
        elif result['attempted']:
            messages.success(request, 'Progression des tâches automatiques actualisée.')
        else:
            messages.info(request, 'Aucune tâche automatique à actualiser.')
    except Exception:
        logger.exception('Unable to refresh schedule progress')
        messages.error(request, 'Impossible d’actualiser les progressions. Vérifiez la configuration et réessayez.')
    return redirect(request.META.get('HTTP_REFERER') or 'home')


def finalize_schedule(request, schedule_id):
    referer = request.META.get('HTTP_REFERER')
    unfinished_tasks = {}
    schedule = get_object_or_404(Schedule, id=schedule_id)
    unfinished_items = ScheduleItem.objects.filter(schedule=schedule, progression_rate__lt=100, task__transfer=True)

    if unfinished_items.exists():
        for item in unfinished_items:
            remaining_time = item.assigned_time - (item.progress_lines * item.task.average_duration)

            if item.task_identifier:
                task_key = f"{item.task.id}_{item.task_identifier.split('Lines')[0]}"
            else:
                task_key = f"{item.task.id}"

            if task_key in unfinished_tasks:
                unfinished_tasks[task_key]['remaining_time'] += remaining_time.total_seconds()
            else:
                unfinished_tasks[task_key] = {
                    'task': item.task.id,
                    'remaining_time': remaining_time.total_seconds(),
                }
                if item.task_identifier:
                    unfinished_tasks[task_key]['task_identifier'] = item.task_identifier.split('Lines')[0]

        request.session['unfinished_tasks'] = unfinished_tasks
        return redirect('unfinished_tasks', schedule_id=schedule_id)

    schedule.schedule_status = 3  # 'Clôturé'
    schedule.save()
    return redirect(referer)


def handle_unfinished_tasks(request, schedule_id):
    schedule = get_object_or_404(Schedule, id=schedule_id)
    unfinished_tasks = request.session.get('unfinished_tasks', {})

    task_details = []
    for task_info in unfinished_tasks.values():
        task = Task.objects.get(id=task_info['task'])
        remaining_time = timedelta(seconds=task_info['remaining_time'])
        task_identifier = task_info.get('task_identifier', '')

        task_details.append({
            'task': task,
            'remaining_time': remaining_time,
            'task_identifier': task_identifier,
        })

    if request.method == 'POST':
        if 'finish_all' in request.POST:
            overdue_tasks = ScheduleTask.objects.filter(schedule=schedule, status__in=[0, 1])
            overdue_tasks.update(status=3)
        else:
            transfer_tasks = request.POST.getlist('transfer_tasks')

            transfer_schedule, created = Schedule.objects.get_or_create(
                schedule_name='Transfer_Schedule',
                defaults={'schedule_status': 0, 'is_template': True,
                          'start_time': datetime.min.time(), 'end_time': datetime.max.time()}
            )

            overdue_tasks = []
            for task_detail in task_details:
                schedule_tasks = ScheduleTask.objects.filter(schedule=schedule,
                                                             task_id=task_detail['task'].id,
                                                             completion__gt=0)

                if str(task_detail['task'].id) in transfer_tasks:
                    schedule_tasks.update(status=4)
                    existing_item = ScheduleItem.objects.filter(
                        schedule=transfer_schedule,
                        task_id=task_detail['task'].id,
                        task_identifier=task_detail['task_identifier']
                    ).first()

                    if existing_item:
                        existing_item.assigned_time += task_detail['remaining_time']
                        existing_item.save()
                    else:
                        ScheduleItem.objects.create(
                            schedule=transfer_schedule,
                            task_id=task_detail['task'].id,
                            assigned_time=task_detail['remaining_time'],
                            task_identifier=task_detail['task_identifier'],
                        )
                else:
                    overdue_tasks.append(task_detail['task'].id)

            if overdue_tasks:
                ScheduleTask.objects.filter(schedule=schedule, task_id__in=overdue_tasks).update(status=3)

        schedule.schedule_status = 3
        schedule.save()
        messages.success(request, "Schedule finalized and closed.")
        return redirect('schedule_details')

    else:
        return render(request, 'schedule/schedule_close.html', {
            'schedule': schedule,
            'unfinished_tasks': task_details,
            'logged_data': getLoggedData(request),
        })


def import_tasks(request):
    referer = request.META.get('HTTP_REFERER')
    if request.method == 'POST':
        actual_schedule_id = request.POST.get('actual_schedule')
        actual_schedule = Schedule.objects.get(pk=actual_schedule_id)
        selected_items = request.POST.getlist('transfer_items')

        for item_id in selected_items:
            item = ScheduleItem.objects.get(id=item_id)
            employee_id = request.POST.get(f'employee_{item_id}')

            if item.task_identifier:
                ScheduleItem.objects.create(
                    task=item.task,
                    schedule=actual_schedule,
                    task_identifier=item.task_identifier
                )
                item.delete()
            else:
                assigned_time_str = request.POST.get(f'assigned_time_{item_id}')
                assigned_time = datetime.strptime(assigned_time_str, '%H:%M:%S')
                assigned_seconds = assigned_time.hour * 3600 + assigned_time.minute * 60 + assigned_time.second

                ScheduleItem.objects.create(
                    task=item.task,
                    schedule=actual_schedule,
                    assigned_time=timedelta(seconds=assigned_seconds),
                    employee_id=employee_id,
                    task_identifier=item.task_identifier
                )

                if timedelta(seconds=assigned_seconds) >= item.assigned_time:
                    item.delete()
                else:
                    item.assigned_time -= timedelta(seconds=assigned_seconds)
                    item.save()

        return redirect(referer)


######### STOCK PART #################
@login_required
def get_emplacements(request):
    query = Query.objects.filter(name='STOCK EMPLACEMENTS').first()
    result = get_query_results(query, query.parameters) if query else {'error': 'Requête non configurée'}
    return render(request, 'stock/emplacements.html', {
        'logged_data': getLoggedData(request), 'emplacements': result,
    })


@login_required
def get_stock(request):
    query = Query.objects.filter(name='TEST STOCK').first()
    result = get_query_results(query, query.parameters) if query else {'error': 'Requête non configurée'}
    return render(request, 'stock/stocks.html', {
        'logged_data': getLoggedData(request), 'stock': result,
    })


@login_required
def assign_stock(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            if not isinstance(data, list) or not data:
                raise ValueError('Sélectionnez au moins un emplacement à affecter.')

            # Validate the complete batch before making any changes.
            assignments = []
            for item in data:
                if not isinstance(item, dict):
                    raise ValueError('Les données de l’affectation sont invalides.')
                emplacement = item.get('emplacement')
                employee_id = item.get('employee')
                operations = item.get('operations')
                if not isinstance(emplacement, str) or not emplacement or len(emplacement) > 10:
                    raise ValueError('Emplacement invalide.')
                if not employee_id or not isinstance(operations, list) or not operations:
                    raise ValueError(f'Renseignez l’employé et les opérations pour {emplacement}.')
                try:
                    start = parse_date(item.get('date_debut') or '')
                    end = parse_date(item.get('date_fin') or '')
                    employee = Employee.objects.filter(pk=employee_id).first()
                except (TypeError, ValueError):
                    raise ValueError(f'Employé ou dates invalides pour {emplacement}.')
                if not employee or not start or not end:
                    raise ValueError(f'Renseignez l’employé et les dates pour {emplacement}.')
                if end < start:
                    raise ValueError(f'La date de fin précède la date de début pour {emplacement}.')

                resolved_operations = []
                for operation in operations:
                    # New UI sends numeric IDs; keep supporting older clients that
                    # send the operation designation instead.
                    if isinstance(operation, int) and not isinstance(operation, bool):
                        matched = StockOperation.objects.filter(pk=operation).first()
                    elif isinstance(operation, str):
                        matched = StockOperation.objects.filter(designation=operation).first()
                    else:
                        matched = None
                    if not matched:
                        raise ValueError(f'Opération inconnue pour {emplacement}.')
                    if matched not in resolved_operations:
                        resolved_operations.append(matched)
                assignments.append((emplacement, employee, start, end, resolved_operations))

            with transaction.atomic():
                for emplacement, employee, start, end, operations in assignments:
                    for operation in operations:
                        assignment = AssignationEmplacement.objects.filter(
                            emplacement=emplacement, operation=operation
                        ).first() or AssignationEmplacement(emplacement=emplacement, operation=operation)
                        assignment.employee = employee
                        assignment.date_debut = start
                        assignment.date_fin = end
                        assignment.clean()
                        assignment.save()
            return JsonResponse({'message': 'Affectations enregistrées.'})
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, ValidationError) as exc:
            return JsonResponse({'error': str(exc)}, status=400)
        except Exception:
            logger.exception('Unable to save stock assignments')
            return JsonResponse({'error': 'Impossible d’enregistrer les affectations.'}, status=500)

    if request.method != 'GET':
        return HttpResponseNotAllowed(['GET', 'POST'])

    try:
        group_size = int(request.GET.get('group_by', 3))
        if not 0 <= group_size <= 9:
            raise ValueError
    except (TypeError, ValueError):
        group_size = 3
        messages.warning(request, 'Le regroupement doit être compris entre 0 et 9 caractères.')

    location_error = None
    try:
        grouped_emplacements = get_emplacemnt_groupped(group_size)
    except (Query.DoesNotExist, ValueError):
        grouped_emplacements = {}
        location_error = 'Impossible de charger les emplacements Xpert. Vérifiez la connexion et la requête LISTE EMPLACEMENT.'

    today = timezone.localdate()
    assigned = AssignationEmplacement.objects.filter(
        date_debut__lte=today, date_fin__gte=today
    ).select_related('employee', 'operation')
    assigned_emplacements_dict = {}
    for assignment in assigned:
        assigned_emplacements_dict.setdefault(assignment.emplacement, []).append(assignment)

    return render(request, 'stock/assign_stock.html', {
        'logged_data': getLoggedData(request),
        'grouped_emplacements': grouped_emplacements,
        'employees': Employee.objects.all(),
        'operations': StockOperation.objects.all(),
        'assigned_emplacements_dict': assigned_emplacements_dict,
        'group_size': group_size,
        'location_error': location_error,
    })


@login_required
def delete_assignment(request, assignment_id):
    if request.method != 'DELETE':
        return HttpResponseNotAllowed(['DELETE'])
    deleted, _ = AssignationEmplacement.objects.filter(pk=assignment_id).delete()
    if not deleted:
        return JsonResponse({'success': False, 'error': 'Affectation introuvable.'}, status=404)
    return JsonResponse({'success': True, 'message': 'Affectation supprimée.'})


def get_emplacemnt_groupped(x=3):
    query = Query.objects.get(name='LISTE EMPLACEMENT')
    result = get_query_results(query, query.parameters)
    locations = column_values(result, 'DESIGNATION')
    grouped = {}
    # The local assignment model identifies locations by designation only.
    seen = set()
    for emplacement in locations:
        if not isinstance(emplacement, str) or not emplacement or emplacement in seen:
            continue
        seen.add(emplacement)
        grouped.setdefault(emplacement[:x], []).append(emplacement)
    return grouped


def get_active_assignments():
    today = date.today()
    return AssignationEmplacement.objects.filter(date_debut__lte=today, date_fin__gte=today)


def get_emplacement_item_count(emplacement, type_count='Prd'):
    query = Query.objects.get(name="STOCK EMPLACEMENTS")
    parameters = {"emp": emplacement}
    results = get_query_results(query, parameters)
    count = 0
    try:
        # Recreating the logic without pandas:
        # Count unique items based on column index
        if type_count == 'Bte':
            qte_index = results['columns'].index('QUANTITE')
            count = sum(row[qte_index] for row in results['data_rows'])
        elif type_count == 'Prd':
            des_index = results['columns'].index('DESIGNATION')
            count = len(set(row[des_index] for row in results['data_rows']))
        elif type_count == 'Lot':
            lot_index = results['columns'].index('CODE_BARRE_LOT')
            count = len(set(row[lot_index] for row in results['data_rows']))
        elif type_count == 'Emp':
            emp_index = results['columns'].index('EMPLACEMENT')
            count = len(set(row[emp_index] for row in results['data_rows']))
    except Exception:
        count = 0
    return count


def generate_schedule_items_for_stock_operations(schedule):
    active_assignments = AssignationEmplacement.objects.filter(
        date_debut__lte=date.today(),
        date_fin__gte=date.today()
    )

    for assignment in active_assignments:
        employee = assignment.employee
        stock_operation = assignment.operation
        emplacement = assignment.emplacement

        task = stock_operation.task
        if not task:
            continue

        item_count = get_emplacement_item_count(emplacement)
        if item_count == 0:
            continue

        assigned_time = stock_operation.duration * item_count

        if not schedule:
            continue

        schedule_task, created_task = ScheduleTask.objects.get_or_create(
            schedule=schedule,
            task=task,
            defaults={
                'total_time': assigned_time,
                'total_assigned_time': assigned_time,
                'status': 0,
                'completion': 0,
            }
        )
        if not created_task:
            schedule_task.total_assigned_time += assigned_time
            schedule_task.save()

        schedule_employee, created_employee = ScheduleEmployee.objects.get_or_create(
            schedule=schedule,
            employee=employee,
            defaults={
                'total_assigned_time': assigned_time,
                'score': 0,
            }
        )
        if not created_employee:
            schedule_employee.total_assigned_time += assigned_time
            schedule_employee.save()

        schedule_item, created_item = ScheduleItem.objects.get_or_create(
            schedule=schedule,
            task=task,
            employee=employee,
            defaults={
                'assigned_time': assigned_time,
                'progression_rate': 0,
                'task_identifier': f": {emplacement}"
            }
        )
        if not created_item:
            schedule_item.assigned_time += assigned_time
            schedule_item.save()


@login_required
def actual_schedule_details(request, schedule_id=None):
    schedule_service.update_all_schedule_statuses()

    if schedule_id:
        schedule = get_object_or_404(Schedule, pk=schedule_id)
        actual = False
    else:
        schedule = schedule_service.get_current_active_schedule()
        actual = schedule is not None

    sched_data = schedule_service.get_schedule_context_data(schedule)

    context = {
        'actual': actual,
        'selected_schedule': schedule,
        'employees': Employee.objects.all(),
        'tasks': Task.objects.all(),
        'transfer_items': ScheduleItem.objects.filter(schedule__schedule_name='Transfer_Schedule'),
        'logged_data': getLoggedData(request),
        'can_manage_trt': can_manage_trt(request),
    }

    if sched_data:
        sched_data['schedule_employees'] = kpi_service.annotate_schedule_employee_scores(schedule, sched_data['schedule_employees'], request.user)
        context.update(sched_data)
        context['missing_reception_followups'] = schedule_service.missing_reception_followups(schedule)

    return render(request, 'schedule/actual_schedule_details.html', context)


@login_required
@transaction.atomic
def restore_reception_followup(request, reception_id):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    if not can_manage_reception(request):
        raise PermissionDenied
    reception = get_object_or_404(Reception.objects.select_for_update().select_related('related_item__schedule'), pk=reception_id)
    item = reception.related_item
    if not item or (item.progression_rate or 0) < 100 or not schedule_service.reception_identifier(item.task_identifier):
        messages.error(request, 'La réception doit être terminée et comporter une référence avant de créer l’étape suivante.')
    elif schedule_service.generate_group_task(item, item.schedule):
        messages.success(request, 'L’étape suivante est disponible pour affectation dans son TRT.')
    else:
        messages.error(request, 'Aucune règle de génération n’est configurée pour cette réception.')
    return redirect('actual_schedule_details')


def parse_manual_progress_updates(post):
    """Accept the enhanced multi-page payload or the normal HTML form."""
    payload = post.get('updates')
    if payload is not None:
        try:
            entries = json.loads(payload)
        except (TypeError, ValueError):
            raise ValidationError('La sélection est invalide. Actualisez la page et réessayez.')
        if not isinstance(entries, list):
            raise ValidationError('La sélection est invalide. Actualisez la page et réessayez.')
    else:
        single_item = post.get('single_item')
        ids = [single_item] if single_item else post.getlist('selected_item')
        entries = [{'id': item_id, 'rate': post.get(f'rate_{item_id}')} for item_id in ids]

    if not entries:
        raise ValidationError('Sélectionnez au moins une tâche manuelle à valider.')
    # Leave headroom below SQLite's 999 bound-parameter limit.
    if len(entries) > 900:
        raise ValidationError('Validez au maximum 900 tâches à la fois.')

    updates, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValidationError('La sélection est invalide. Actualisez la page et réessayez.')
        item_id, rate = entry.get('id'), entry.get('rate')
        # Booleans are Python integers, but are not valid IDs or percentages.
        if isinstance(item_id, bool) or isinstance(rate, bool):
            raise ValidationError('La sélection est invalide. Actualisez la page et réessayez.')
        item_text, rate_text = str(item_id), str(rate)
        if not item_text.isascii() or not item_text.isdecimal() or len(item_text) > 19:
            raise ValidationError('La sélection est invalide. Actualisez la page et réessayez.')
        if not rate_text.isascii() or not rate_text.isdecimal() or len(rate_text) > 3:
            raise ValidationError('Indiquez un pourcentage entier entre 0 et 100 pour chaque tâche sélectionnée.')
        item_id, rate = int(item_text), int(rate_text)
        if not 0 < item_id <= 9223372036854775807 or item_id in seen:
            raise ValidationError('La sélection est invalide. Actualisez la page et réessayez.')
        if not 0 <= rate <= 100:
            raise ValidationError('Indiquez un pourcentage entier entre 0 et 100 pour chaque tâche sélectionnée.')
        seen.add(item_id)
        updates.append((item_id, rate))
    return updates


@login_required
def bulk_manual_progress(request, schedule_id):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    if not can_manage_trt(request):
        raise PermissionDenied
    schedule = get_object_or_404(Schedule, pk=schedule_id, is_template=False)
    destination = (redirect('actual_schedule_details') if request.POST.get('source') == 'actual'
                   else redirect('schedule_details_selected', schedule_id=schedule.pk))
    try:
        updates = parse_manual_progress_updates(request.POST)
        count = schedule_service.update_manual_progress(schedule, updates)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    except Exception:
        logger.exception('Unable to validate manual progress for TRT %s', schedule.pk)
        messages.error(request, 'Impossible de valider les tâches. Aucune modification n’a été enregistrée.')
    else:
        messages.success(request, f'{count} tâche(s) manuelle(s) validée(s).')
    return destination


@login_required
def schedule_details(request, schedule_id=None):
    schedule_service.update_all_schedule_statuses()
    schedules = Schedule.objects.filter(is_template=False).exclude(schedule_status=1)

    schedule_filter_form = ScheduleFilterForm(request.GET)
    schedule_instance = None

    if schedule_filter_form.is_valid():
        schedule_filter = schedule_filter_form.cleaned_data.get('schedule')
        if schedule_filter:
            schedule_instance = schedules.filter(id=schedule_filter.id).first()

    schedule = get_object_or_404(Schedule, pk=schedule_id) if schedule_id else schedule_instance

    sched_data = schedule_service.get_schedule_context_data(schedule)

    context = {
        'actual': False,
        'schedules': schedules,
        'selected_schedule': schedule,
        'schedule_filter_form': schedule_filter_form,
        'logged_data': getLoggedData(request),
        'can_manage_trt': can_manage_trt(request),
    }

    if sched_data:
        sched_data['schedule_employees'] = kpi_service.annotate_schedule_employee_scores(schedule, sched_data['schedule_employees'], request.user)
        context.update(sched_data)

    return render(request, 'schedule/schedule_details.html', context)


@login_required
def schedule_current(request, schedule_id=None):
    try:
        kpi_period = kpi_service.parse_period(request.GET)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    logged_data = getLoggedData(request)
    employee = logged_data.get('employee')

    schedule = get_object_or_404(Schedule,
                                 pk=schedule_id) if schedule_id else schedule_service.get_current_active_schedule()
    current_schedule = schedule is not None

    # A user without an employee profile must not see everyone else's tasks.
    sched_data = schedule_service.get_schedule_context_data(schedule, employee=employee) if employee else None

    tasks_demande = []
    tasks_saisie = []

    if employee and employee.rank_id is not None:
        tasks_demande = Task.objects.filter(task_type__name='DEMANDE',
                                            responsibility_level__value__gte=employee.rank.value).order_by('priority')
        tasks_saisie = Task.objects.filter(task_type__name='APRES SAISIE',
                                           responsibility_level__value__gte=employee.rank.value).order_by('priority')

    context = {
        'current_schedule': current_schedule,
        'selected_schedule': schedule,
        'tasks_demande': tasks_demande,
        'tasks_saisie': tasks_saisie,
        'logged_data': logged_data,
        'kpi_snapshot': kpi_service.snapshot(request.user, 'live', kpi_period, schedule_override=schedule),
    }

    if sched_data:
        context.update(sched_data)

    return render(request, 'portal/user_schedule.html', context)


@login_required
def user_schedules_history(request, schedule_id=None):
    schedule_service.update_all_schedule_statuses()
    logged_data = getLoggedData(request)
    employee = logged_data.get('employee')

    schedule_ids = ScheduleEmployee.objects.filter(employee=employee).values_list('schedule_id',
                                                                                  flat=True) if employee else []
    schedules = Schedule.objects.filter(id__in=schedule_ids, is_template=False).exclude(schedule_status=1)

    schedule_filter_form = ScheduleFilterForm(request.GET)
    schedule_instance = None
    if schedule_filter_form.is_valid():
        schedule_filter = schedule_filter_form.cleaned_data.get('schedule')
        if schedule_filter:
            schedule_instance = schedules.filter(id=schedule_filter.id).last()

    schedule = get_object_or_404(Schedule, pk=schedule_id) if schedule_id else schedule_instance

    sched_data = schedule_service.get_schedule_context_data(schedule, employee=employee)

    context = {
        'schedules': schedules,
        'selected_schedule': schedule,
        'schedule_filter_form': schedule_filter_form,
        'logged_data': logged_data
    }

    if sched_data:
        context.update(sched_data)

    return render(request, 'portal/user_schedule_history.html', context)


@login_required
def my_timesheet(request):
    try:
        employee = Employee.objects.get(user=request.user)
    except Employee.DoesNotExist:
        messages.error(request, "Aucun profil d'employé n'est associé à ce compte.")
        return redirect('home')

    # Date Filtering Logic
    today = timezone.localtime().date()
    selected_month = request.GET.get('month')
    selected_year = request.GET.get('year')

    try:
        selected_month = int(selected_month)
        selected_year = int(selected_year)
    except (TypeError, ValueError):
        selected_month = today.month
        selected_year = today.year

    if not (1 <= selected_month <= 12 and 1 <= selected_year <= 9999):
        selected_month, selected_year = today.month, today.year

    start_date = date(selected_year, selected_month, 1)
    _, last_day = calendar.monthrange(selected_year, selected_month)
    end_date = date(selected_year, selected_month, last_day)

    # Query filtered timesheets
    timesheets = Timesheet.objects.filter(
        employee=employee,
        date__gte=start_date,
        date__lte=end_date
    ).order_by('-date')

    months = [
        (1, 'Janvier'), (2, 'Février'), (3, 'Mars'), (4, 'Avril'),
        (5, 'Mai'), (6, 'Juin'), (7, 'Juillet'), (8, 'Août'),
        (9, 'Septembre'), (10, 'Octobre'), (11, 'Novembre'), (12, 'Décembre')
    ]
    years = list(range(today.year - 5, today.year + 2))

    context = {
        'employee': employee,
        'timesheets': timesheets,
        'months': months,
        'years': years,
        'selected_month': selected_month,
        'selected_year': selected_year,
        'remaining_syncs': max(0, 3 - (UserSyncLog.objects.filter(user=request.user, date=today).values_list('count', flat=True).first() or 0)),
        'logged_data': getLoggedData(request) if 'getLoggedData' in globals() else None
    }
    return render(request, 'portal/my_timesheet.html', context)


@login_required
def request_leave(request):
    try:
        employee = Employee.objects.get(user=request.user)
    except Employee.DoesNotExist:
        messages.error(request, "Aucun profil d'employé n'est associé à ce compte.")
        return redirect('home')

    form = LeaveRequestForm()
    if request.method == 'POST':
        form = LeaveRequestForm(request.POST)
        if form.is_valid():
            start = form.cleaned_data['start_date']
            end = form.cleaned_data['end_date']
            leave_type = form.cleaned_data['leave_type']

            delta = end - start
            with transaction.atomic():
                for i in range(delta.days + 1):
                    current_date = start + timedelta(days=i)

                    ts, created = Timesheet.objects.get_or_create(
                        employee=employee,
                        date=current_date,
                        defaults={'leave_type': leave_type, 'is_confirmed': False}
                    )
                    if not created and not ts.is_confirmed:
                        ts.leave_type = leave_type
                        ts.save()

            messages.success(request, f"Votre demande de congé ({start} au {end}) a été enregistrée.")
            return redirect('my_timesheet')

    # Fetch dynamic balance calculations
    balance = employee.calculate_leave_balance()

    context = {
        'employee': employee,
        'form': form,
        'balance': balance,
        'logged_data': getLoggedData(request) if 'getLoggedData' in globals() else None
    }
    return render(request, 'portal/request_leave.html', context)


# new reception logc
@login_required
def reception_dashboard_v3(request):
    # Fetch all processes and their associated 5 tasks in one go
    processes = ReceptionProcess.objects.all().prefetch_related('tasks', 'tasks__employees').order_by('-reception_date')

    context = {
        'processes': processes,
        'steps': ReceptionTask.RECEP_STEPS,
        'logged_data': getLoggedData(request),
    }
    return render(request, 'reception/dashboard_v3.html', context)
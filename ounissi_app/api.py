from django.utils import timezone
from ninja import NinjaAPI, Schema
from ninja.security import django_auth
from typing import List, Optional
from accounts.models import Employee
from .models import Timesheet, UserSyncLog
from .utils import can_manage_timesheets
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.db import transaction
from datetime import datetime

# Initialize API
ninja_api = NinjaAPI()


# 1. Updated Schema to support both ID updates and Employee/Date creation
class TimesheetRowSchema(Schema):
    id: Optional[int] = None  # Optional now
    employee_id: Optional[int] = None  # Required for creation
    date: Optional[str] = None  # Required for creation (Format: YYYY-MM-DD)
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    start_time_2: Optional[str] = None
    end_time_2: Optional[str] = None
    start_time_3: Optional[str] = None
    end_time_3: Optional[str] = None
    leave_type: Optional[str] = 'NONE'


# 2. Unified Bulk Update/Create Endpoint
@ninja_api.post("/timesheets/bulk-validate", auth=django_auth)
def bulk_update_timesheets(request, data: List[TimesheetRowSchema]):
    if not can_manage_timesheets(request):
        return JsonResponse({"detail": "Vous n’êtes pas autorisé à valider les pointages."}, status=403)
    updated_count = 0

    with transaction.atomic():
        for entry in data:
            if entry.id:
                # Update existing record
                ts = get_object_or_404(Timesheet, id=entry.id)
            elif entry.employee_id and entry.date:
                # Create a new record on-the-fly
                parsed_date = datetime.strptime(entry.date, "%Y-%m-%d").date()
                ts, created = Timesheet.objects.get_or_create(
                    employee_id=entry.employee_id,
                    date=parsed_date
                )
            else:
                continue

            # Update fields
            ts.start_time = entry.start_time or None
            ts.end_time = entry.end_time or None
            ts.start_time_2 = entry.start_time_2 or None
            ts.end_time_2 = entry.end_time_2 or None
            ts.start_time_3 = entry.start_time_3 or None
            ts.end_time_3 = entry.end_time_3 or None

            if entry.leave_type:
                ts.leave_type = entry.leave_type

            # Confirm and validate
            ts.is_confirmed = True
            ts.save()
            updated_count += 1

    return {"status": "success", "message": f"Processed {updated_count} lines successfully."}

@ninja_api.post("/timesheets/sync-my-data", auth=django_auth)
def sync_my_data(request):
    user = request.user
    today = timezone.localtime().date()

    sync_record, _ = UserSyncLog.objects.get_or_create(user=user, date=today)
    if sync_record.count >= 3:
        return {"status": "error", "message": "Quota quotidien atteint (3/3)."}

    try:
        # Import the logic from your new dedicated importer
        from .attendance_importer import import_attendance_logs, timesheet_dataframe, save_logs

        raw_logs = import_attendance_logs()
        if not raw_logs:
            return {"status": "error", "message": "Impossible de lire les données du terminal."}

        df = timesheet_dataframe(raw_logs)
        save_logs(df)

        sync_record.count += 1
        sync_record.save()

        return {
            "status": "success",
            "message": f"Synchronisation réussie. Quota restant: {3 - sync_record.count}"
        }
    except Exception as e:
        return {"status": "error", "message": f"Erreur terminal: {str(e)}"}

class BulkAssignSchema(Schema):
    """
    Data contract for bulk assigning employees to reception steps.
    """
    process_ids: List[int]    # The IDs of the ReceptionProcess Master records
    employee_ids: List[int]   # The IDs of the Employee records being assigned
    task_step: str            # Must be one of: 'REC', 'SAI', 'PRO', 'VIG', 'PLA'


@ninja_api.post("/reception/bulk-assign-task")
def bulk_assign_task(request, data: BulkAssignSchema):
    # The experimental V3 task does not yet have a defined mapping to the
    # application's Schedule/Task/ScheduleItem models.  Do not claim success
    # or create partial assignments until that mapping is specified.
    return JsonResponse({
        "status": "error",
        "message": "L’affectation V3 n’est pas encore reliée au TRT. Utilisez le suivi du TRT actif."
    }, status=503)

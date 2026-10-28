# XpertPharm data and UI contract

The Django database holds application records such as employees, tasks, schedules, reception records and saved `Query` definitions. XpertPharm is a separate SQL Server connection. `Query.type` is `0` for the internal database and `1` for XpertPharm. The active `XpertServer` configures the latter.

`xpert_dummy_data.txt` contains **sample query results**, not the source SQL, full SQL Server schema or complete result sets. Most successful samples contain 20 rows; the sample employee-log result is empty. They are useful for testing display and result formats, but do not prove how the live SQL filters or aggregates rows. They are never used as a runtime fallback for the real connection.

## Result shape

Both query runners return `{'columns': [...], 'data_rows': [...]}`. Each row is an ordered sequence aligned with `columns`; column names, not fixed offsets or a pandas DataFrame, should be used when extracting fields. Query failures must be treated differently from successful results with zero rows.

| Sample queries | Result columns | Relevant parameters |
| --- | --- | --- |
| Sales, validation and missing-item progress | `OP_DATE`, `OP_USER`, `NBR_LINES`, `TOTAL_TIME` | `start_date`, `end_date`, `xp_id` |
| `SAISIE FACTURES/BR SUR LE LOGICIEL` | Progress columns plus `TYPE_DOC`, `REF_TIERS`, `SupplierName` | The above plus `ref_tiers`, `supplier` |
| `EMPLOYEES XPERT LOGS` | `OP_DATE`, `OP_USER`, `FIRST_LOG`, `LAST_LOG` | `act_date`, `xp_id` in the sample |
| `LISTE FOURNISSEURS` | `SupplierName` | Stored query defaults |
| `InvoiceHelper` | `LIGNE`, `DESIGNATION`, `LOT`, `QUANTITE` | `ref_tiers`, `supplier` |
| `LISTE EMPLACEMENT` | `DESIGNATION`, `MAGASIN` | `emp` |
| `STOCK EMPLACEMENTS` | `EMPLACEMENT`, `MAGASIN`, `DESIGNATION`, `CODE_BARRE_LOT`, `QUANTITE` | `emp` |
| `TEST STOCK` | `DESIGNATION_PRODUIT`, `CODE_BARRE_LOT`, `MAGASIN`, `EMPLACEMENT`, `QUANTITE` | Stored query defaults |

The examples may contain names and commercially sensitive information. Treat the file and any copies according to the same access rules as the source data.

## Workflows and open questions

* Each task has a configured per-line duration. Xpert's `TOTAL_TIME` represents that duration multiplied by `NBR_LINES` (for example, 11 × 45 seconds = 495 seconds). The progress service uses the application's configured `Task.average_duration` and `NBR_LINES`, with the schedule date, the following day as `end_date`, and the employee `xp_id`. It expects at most one row for an assigned task; multiple rows are ambiguous and leave the saved progress unchanged with a warning. Confirm the live query's filters, date-boundary semantics and grouping, and that its per-line duration matches the application's configuration.
* The TRT reception chain is `Reception → Saisie → ReceptionProduit → ReceptionVignette → ReceptionPlacement`. An employee submits the reception form, which creates the next step unassigned. The manager assigns each step using the recorded supplier, invoice reference and line count. Managers can assign later steps immediately after assigning their predecessors; the employees may work in parallel, even if earlier steps are incomplete. Assignment duration is the current task's configured per-line duration × assigned line count. Reception assignments are restricted to Admins and team leaders, with employees selected from eligible members of that TRT.
* `InvoiceHelper` is an optional preview for the later steps: Xpert may have no invoice lines yet while entry is still in progress. Assignment uses the manager-entered line count and remains possible with a visible warning if the preview is empty or unavailable. Automatic task progress can be refreshed explicitly from the employee or manager TRT screen; failed queries leave the last saved progress intact and display a warning.
* Legacy completed receptions with a missing follow-up in the current TRT are flagged for a manager to restore explicitly. Matching prefers the same TRT and an exact supplier/reference; transferred steps can use the nearest earlier matching TRT. Existing data does not contain an explicit parent ID on every schedule item, so identical supplier/reference pairs across separate receptions can still be ambiguous.
* Employee submission for manual reception steps is outside this change; the existing manager progress controls remain in place. The details of employee reporting and manager validation still need to be specified.
* The separate reception V3 models do not define how their steps map to real `Task` and `ScheduleItem` records. Its dashboard is read-only, and its bulk-assignment API returns an unavailable response until that mapping is designed. Use the existing TRT workflow for assignments.
* A missing Xpert audit connection is not equivalent to an empty audit result. The attendance screen and its Excel export flag an unavailable audit explicitly.

* Timesheet validation is restricted to signed-in users in the `Admin` group or identified as team leaders, matching the management navigation. Both validation and terminal-sync API requests use an authenticated Django session and CSRF token; external clients that called them anonymously must be updated. This is not a complete permissions audit, and the existing manager view is not limited to a leader's own team.
* The independent salary-entry link was removed from navigation because that legacy route is incomplete. The salary-item list now renders without referencing a nonexistent create route; staff can manage items through Django admin. Payroll calculations and the remaining salary workflows were not audited.

## Local checks

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python manage.py test
node tests/line_allocation.test.js  # optional JavaScript regression checks
node tests/task_form_modal.test.js
```

The tests use temporary SQLite databases and mock the Xpert connection. A live end-to-end check still requires the configured SQL Server, its queries, network access and an appropriate ODBC driver.

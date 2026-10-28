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

* The existing progress service sends a schedule date range and employee `xp_id` to the task's configured progress query. It currently uses the first returned `NBR_LINES` and the task's average duration; it does not use the sample's `TOTAL_TIME`. Confirm the live SQL's filtering and grouping before changing this calculation, especially if a query can return more than one row for an employee and date.
* The existing reception chain is `Reception → Saisie → ReceptionProduit → ReceptionVignette → ReceptionPlacement`. Forms are opened from an actual TRT task, which supplies the parent record, supplier and reference. Invoice details come from `InvoiceHelper`. The save handlers currently allocate `current task average_duration × assigned line count`; the form's displayed duration follows that same rule. Confirm with the business owner whether quantities or another duration rule should instead be used.
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
```

The tests use temporary SQLite databases and mock the Xpert connection. A live end-to-end check still requires the configured SQL Server, its queries, network access and an appropriate ODBC driver.

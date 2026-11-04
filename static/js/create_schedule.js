/* TRT template editor. The DOM is a view of the state, never the source of
 * truth; filtered or paginated rows therefore cannot disappear on save. */
(function () {
    'use strict';
    const SECOND = 1000000, PAGE_SIZE = 12, MAX_ROWS = 50000;
    function byId(id) { return document.getElementById(id); }
    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }
    function formatDuration(us) {
        if (!Number.isSafeInteger(us) || us < 0) return '—';
        const seconds = Math.floor(us / SECOND), fraction = us % SECOND;
        const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60), secs = seconds % 60;
        const pad = function (n) { return String(n).padStart(2, '0'); };
        return pad(hours) + ':' + pad(minutes) + ':' + pad(secs) + (fraction ? '.' + String(fraction).padStart(6, '0').replace(/0+$/, '') : '');
    }
    function parseDuration(raw) {
        const match = /^(\d{1,7}):([0-5]\d)(?::([0-5]\d)(?:\.(\d{1,6}))?)?$/.exec(String(raw).trim());
        if (!match) return null;
        const value = (Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3] || 0)) * SECOND + Number((match[4] || '').padEnd(6, '0'));
        return Number.isSafeInteger(value) ? value : null;
    }
    function normalized(value) { return String(value || '').toLocaleLowerCase('fr').normalize('NFD').replace(/[\u0300-\u036f]/g, ''); }
    function initialize() {
        const form = byId('planner-form'), source = byId('schedule-planner-data');
        if (!form || !source || !window.SchedulePlanner) return;
        let data;
        try { data = JSON.parse(source.textContent); }
        catch (_) { return; }
        const taskCatalog = new Map(data.tasks.map(function (row) { return [row.id, row]; }));
        const employeeCatalog = new Map(data.employees.map(function (row) { return [row.id, row]; }));
        const teams = new Map(data.teams.map(function (row) { return [row.id, row]; }));
        const initial = data.initial || {name: '', start: '08:00', end: '14:00', tasks: [], employees: [], items: [], version: null};
        const state = {tasks: new Map(), employees: new Map(), items: [], removedIds: new Set(), dirty: false, saving: false, version: initial.version, nextKey: 0};
        initial.tasks.forEach(function (row) { state.tasks.set(row.id, {raw: formatDuration(row.total_us)}); });
        initial.employees.forEach(function (row) { state.employees.set(row.id, {raw: formatDuration(row.capacity_us)}); });
        initial.items.forEach(function (row) {
            state.items.push({key: ++state.nextKey, id: row.id, task_id: row.task_id, employee_id: row.employee_id,
                raw: formatDuration(row.assigned_us), identifier: row.identifier});
        });
        byId('schedule-name').value = initial.name;
        byId('schedule-start').value = initial.start;
        byId('schedule-end').value = initial.end;
        const pages = {tasks: 0, employees: 0, budgetTasks: 0, budgetEmployees: 0, items: 0};
        let lastCalculation = null;
        function markDirty() { state.dirty = true; }
        function feedback(message) {
            const box = byId('planner-feedback');
            box.textContent = message;
            box.hidden = !message;
            if (message && box.scrollIntoView) box.scrollIntoView({block: 'nearest'});
        }
        function appendOption(select, value, label) {
            const option = el('option', '', label);
            option.value = String(value);
            select.appendChild(option);
        }
        Array.from(new Set(data.tasks.map(function (row) { return row.type; }))).sort(function (a, b) { return a.localeCompare(b, 'fr'); })
            .forEach(function (type) { appendOption(byId('task-type'), type, type); });
        data.teams.forEach(function (team) { appendOption(byId('employee-team'), team.id, team.name); });
        Array.from(new Set(data.employees.map(function (row) { return row.rank; }).filter(function (rank) { return rank !== null; })))
            .sort(function (a, b) { return a - b; }).forEach(function (rank) { appendOption(byId('employee-rank'), rank, 'Niveau ' + rank); });
        appendOption(byId('employee-rank'), 'none', 'Sans niveau');

        function taskFilter() {
            const search = normalized(byId('task-search').value), type = byId('task-type').value, priority = byId('task-priority').value;
            const only = byId('task-selected-only').checked;
            return data.tasks.filter(function (task) {
                return (!search || normalized(task.name).includes(search)) && (!type || task.type === type) &&
                    (!priority || String(task.priority) === priority) && (!only || state.tasks.has(task.id));
            });
        }
        function employeeFilter() {
            const search = normalized(byId('employee-search').value), teamValue = byId('employee-team').value, rank = byId('employee-rank').value;
            const only = byId('employee-selected-only').checked, team = teams.get(Number(teamValue));
            const members = team ? new Set(team.members) : null;
            return data.employees.filter(function (employee) {
                return (!search || normalized(employee.name).includes(search)) && (!members || members.has(employee.id)) &&
                    (!rank || (rank === 'none' ? employee.rank === null : String(employee.rank) === rank)) && (!only || state.employees.has(employee.id));
            });
        }
        function selectedTasks(searchId) {
            const search = normalized(byId(searchId).value);
            const unassignedOnly = searchId === 'task-budget-search' && byId('task-unassigned-only').checked;
            const totals = unassignedOnly ? calculate().taskTotals : null;
            return data.tasks.filter(function (row) {
                if (!state.tasks.has(row.id) || (search && !normalized(row.name).includes(search))) return false;
                if (!unassignedOnly) return true;
                const planned = parseDuration(state.tasks.get(row.id).raw);
                return planned === null || planned > (totals.get(row.id) || 0);
            });
        }
        function selectedEmployees(searchId) {
            const search = normalized(byId(searchId).value);
            return data.employees.filter(function (row) { return state.employees.has(row.id) && (!search || normalized(row.name).includes(search)); });
        }
        function defaultCapacity() {
            function clock(value) {
                const match = /^(\d\d):(\d\d)(?::(\d\d)(?:\.\d+)?)?$/.exec(value);
                return match ? Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3] || 0) : 0;
            }
            return formatDuration(Math.max(0, clock(byId('schedule-end').value) - clock(byId('schedule-start').value)) * SECOND);
        }
        function changeSelection(kind, rows, selected) {
            const chosen = kind === 'task' ? state.tasks : state.employees;
            const field = kind === 'task' ? 'task_id' : 'employee_id';
            const ids = new Set(rows.map(function (row) { return row.id; }));
            if (!selected) {
                const affected = state.items.filter(function (item) { return ids.has(item[field]); }).length;
                if (affected && !window.confirm('Retirer cette sélection supprimera ' + affected + ' affectation(s), y compris leurs références éventuelles. Continuer ?')) return false;
                state.items.forEach(function (item) { if (ids.has(item[field]) && item.id !== null) state.removedIds.add(item.id); });
                state.items = state.items.filter(function (item) { return !ids.has(item[field]); });
                ids.forEach(function (id) { chosen.delete(id); });
            } else {
                rows.forEach(function (row) {
                    if (!chosen.has(row.id)) chosen.set(row.id, {raw: kind === 'task' ? '01:00:00' : defaultCapacity()});
                });
            }
            markDirty(); feedback(''); renderAll(); return true;
        }
        function pageSlice(rows, key, label, previous, next) {
            const count = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
            pages[key] = Math.max(0, Math.min(pages[key], count - 1));
            byId(label).textContent = rows.length + ' résultat(s) · ' + (pages[key] + 1) + '/' + count;
            byId(previous).disabled = pages[key] === 0;
            byId(next).disabled = pages[key] >= count - 1;
            return rows.slice(pages[key] * PAGE_SIZE, (pages[key] + 1) * PAGE_SIZE);
        }
        function pager(previous, next, key, renderer) {
            byId(previous).addEventListener('click', function () { pages[key]--; renderer(); });
            byId(next).addEventListener('click', function () { pages[key]++; renderer(); });
        }
        function empty(target, text, columns) {
            if (columns) {
                const tr = el('tr'), td = el('td', 'planner-empty', text);
                td.colSpan = columns; tr.appendChild(td); target.appendChild(tr);
            } else target.appendChild(el('p', 'planner-empty', text));
        }
        function renderTaskCatalog() {
            const rows = taskFilter(), target = byId('task-catalog'); target.replaceChildren();
            byId('task-select-all').textContent = 'Sélectionner les ' + rows.length + ' résultats';
            byId('task-clear-all').disabled = !rows.some(function (row) { return state.tasks.has(row.id); });
            byId('task-select-all').disabled = !rows.some(function (row) { return !state.tasks.has(row.id); });
            pageSlice(rows, 'tasks', 'task-page', 'task-prev', 'task-next').forEach(function (task) {
                const label = el('label', 'planner-catalog-row'), checkbox = el('input'); checkbox.type = 'checkbox'; checkbox.checked = state.tasks.has(task.id);
                checkbox.setAttribute('aria-label', 'Sélectionner ' + task.name);
                const content = el('span'); content.append(el('strong', '', task.name), el('small', '', task.type + ' · Priorité ' + task.priority + ' · Niveau requis ' + task.rank));
                label.append(checkbox, content); target.appendChild(label);
                checkbox.addEventListener('change', function () { if (!changeSelection('task', [task], checkbox.checked)) checkbox.checked = !checkbox.checked; });
            });
            if (!rows.length) empty(target, 'Aucune tâche pour ces filtres.');
        }
        function renderEmployeeCatalog() {
            const rows = employeeFilter(), target = byId('employee-catalog'); target.replaceChildren();
            byId('employee-select-all').textContent = 'Sélectionner les ' + rows.length + ' résultats';
            byId('employee-clear-all').disabled = !rows.some(function (row) { return state.employees.has(row.id); });
            byId('employee-select-all').disabled = !rows.some(function (row) { return !state.employees.has(row.id); });
            pageSlice(rows, 'employees', 'employee-page', 'employee-prev', 'employee-next').forEach(function (employee) {
                const label = el('label', 'planner-catalog-row'), checkbox = el('input'); checkbox.type = 'checkbox'; checkbox.checked = state.employees.has(employee.id);
                checkbox.setAttribute('aria-label', 'Sélectionner ' + employee.name);
                const content = el('span'); content.append(el('strong', '', employee.name), el('small', '', employee.rank === null ? 'Sans niveau — non éligible à la répartition' : 'Niveau ' + employee.rank));
                label.append(checkbox, content); target.appendChild(label);
                checkbox.addEventListener('change', function () { if (!changeSelection('employee', [employee], checkbox.checked)) checkbox.checked = !checkbox.checked; });
            });
            if (!rows.length) empty(target, 'Aucun employé pour ces filtres.');
        }
        function calculate() {
            const taskTotals = new Map(), employeeTotals = new Map();
            let planned = 0, unassigned = 0, pendingRows = 0, zeroAssigned = 0, invalid = false, ineligible = 0, totalAssigned = 0;
            state.items.forEach(function (item) {
                const amount = parseDuration(item.raw);
                if (amount === null) { invalid = true; return; }
                totalAssigned += amount;
                if (item.employee_id === null) { pendingRows++; return; }
                if (amount === 0) zeroAssigned++;
                taskTotals.set(item.task_id, (taskTotals.get(item.task_id) || 0) + amount);
                employeeTotals.set(item.employee_id, (employeeTotals.get(item.employee_id) || 0) + amount);
                const task = taskCatalog.get(item.task_id), employee = employeeCatalog.get(item.employee_id);
                if (!task || !employee || employee.rank === null || employee.rank > task.rank) ineligible++;
            });
            let missing = 0, overTasks = 0, overEmployees = 0, capacity = 0;
            state.tasks.forEach(function (entry, id) {
                const value = parseDuration(entry.raw); if (value === null) { invalid = true; return; }
                const assigned = taskTotals.get(id) || 0; planned += value;
                const left = Math.max(0, value - assigned); unassigned += left;
                if (left) missing++;
                if (assigned > value) overTasks++;
            });
            state.employees.forEach(function (entry, id) {
                const value = parseDuration(entry.raw); if (value === null) { invalid = true; return; }
                capacity += value;
                if ((employeeTotals.get(id) || 0) > value) overEmployees++;
            });
            if (![planned, unassigned, capacity, totalAssigned].every(Number.isSafeInteger)) invalid = true;
            return {taskTotals: taskTotals, employeeTotals: employeeTotals, planned: planned, unassigned: unassigned,
                missing: missing, pendingRows: pendingRows, zeroAssigned: zeroAssigned, overTasks: overTasks, overEmployees: overEmployees, ineligible: ineligible, invalid: invalid};
        }
        function updateTotals() {
            const result = calculate(); lastCalculation = result;
            byId('summary-tasks').textContent = String(state.tasks.size);
            byId('summary-employees').textContent = String(state.employees.size);
            byId('summary-planned').textContent = result.invalid ? '—' : formatDuration(result.planned);
            byId('summary-unassigned').textContent = result.invalid ? '—' : formatDuration(result.unassigned);
            document.querySelectorAll('[data-task-total]').forEach(function (cell) {
                const id = Number(cell.dataset.taskTotal), assigned = result.taskTotals.get(id) || 0, planned = parseDuration(state.tasks.get(id).raw);
                cell.textContent = formatDuration(assigned); cell.classList.toggle('planner-danger', planned !== null && assigned > planned);
            });
            document.querySelectorAll('[data-task-remaining]').forEach(function (cell) {
                const id = Number(cell.dataset.taskRemaining), planned = parseDuration(state.tasks.get(id).raw);
                cell.textContent = planned === null ? '—' : formatDuration(Math.max(0, planned - (result.taskTotals.get(id) || 0)));
            });
            document.querySelectorAll('[data-employee-total]').forEach(function (cell) {
                const id = Number(cell.dataset.employeeTotal), assigned = result.employeeTotals.get(id) || 0, capacity = parseDuration(state.employees.get(id).raw);
                cell.textContent = formatDuration(assigned); cell.classList.toggle('planner-danger', capacity !== null && assigned > capacity);
            });
            const messages = [];
            if (result.invalid) messages.push('Vérifiez les durées : format heures:minutes:secondes, sans valeurs négatives.');
            if (result.missing || result.pendingRows) messages.push(result.missing + ' tâche(s) avec du temps non affecté ; ' + result.pendingRows + ' ligne(s) sans employé.');
            if (result.zeroAssigned) messages.push(result.zeroAssigned + ' affectation(s) ont une durée nulle ; vérifiez-les ou retirez-les.');
            if (result.overTasks || result.overEmployees) messages.push(result.overTasks + ' tâche(s) dépassent leur durée et ' + result.overEmployees + ' employé(s) dépassent leur capacité.');
            if (result.ineligible) messages.push(result.ineligible + ' affectation(s) ne respectent pas les niveaux et doivent être corrigées.');
            const warnings = byId('planner-warnings'); warnings.textContent = messages.join(' '); warnings.hidden = messages.length === 0;
            return result;
        }
        function durationInput(entry, accessibleName) {
            const input = el('input'); input.type = 'text'; input.value = entry.raw; input.setAttribute('aria-label', accessibleName); input.autocomplete = 'off';
            input.addEventListener('input', function () { entry.raw = input.value; markDirty(); input.setCustomValidity(parseDuration(entry.raw) === null ? 'Durée invalide : utilisez heures:minutes:secondes.' : ''); updateTotals(); });
            return input;
        }
        function renderTaskBudgets() {
            const rows = selectedTasks('task-budget-search'), target = byId('task-budget-list'); target.replaceChildren();
            pageSlice(rows, 'budgetTasks', 'budget-task-page', 'budget-task-prev', 'budget-task-next').forEach(function (task) {
                const tr = el('tr'), label = el('td'); label.append(el('strong', '', task.name), el('small', '', 'Priorité ' + task.priority + ' · Niveau ' + task.rank));
                const assigned = el('td'); assigned.dataset.taskTotal = task.id;
                const remaining = el('td'); remaining.dataset.taskRemaining = task.id;
                const duration = el('td'); duration.appendChild(durationInput(state.tasks.get(task.id), 'Durée prévue : ' + task.name));
                tr.append(label, assigned, remaining, duration); target.appendChild(tr);
            });
            if (!rows.length) empty(target, 'Aucune tâche sélectionnée pour ce filtre.', 4);
            updateTotals();
        }
        function renderEmployeeBudgets() {
            const rows = selectedEmployees('employee-budget-search'), target = byId('employee-budget-list'); target.replaceChildren();
            pageSlice(rows, 'budgetEmployees', 'budget-employee-page', 'budget-employee-prev', 'budget-employee-next').forEach(function (employee) {
                const tr = el('tr'), label = el('td'); label.append(el('strong', '', employee.name), el('small', '', employee.rank === null ? 'Sans niveau' : 'Niveau ' + employee.rank));
                const assigned = el('td'); assigned.dataset.employeeTotal = employee.id;
                const duration = el('td'); duration.appendChild(durationInput(state.employees.get(employee.id), 'Capacité : ' + employee.name));
                tr.append(label, assigned, duration); target.appendChild(tr);
            });
            if (!rows.length) empty(target, 'Aucun employé sélectionné pour ce filtre.', 3);
            updateTotals();
        }
        function fillSelect(id, rows, placeholder) {
            const select = byId(id), previous = select.value; select.replaceChildren();
            appendOption(select, '', placeholder);
            rows.forEach(function (row) { appendOption(select, row.id, row.name); });
            if (rows.some(function (row) { return String(row.id) === previous; })) select.value = previous;
        }
        function renderManualSelectors() {
            fillSelect('manual-task', selectedTasks('manual-task-search'), 'Choisir une tâche');
            fillSelect('manual-employee', selectedEmployees('manual-employee-search'), 'Choisir un employé');
        }
        function renderItems() {
            const search = normalized(byId('assignment-search').value);
            const rows = state.items.filter(function (item) {
                const task = taskCatalog.get(item.task_id), employee = employeeCatalog.get(item.employee_id);
                return !search || normalized((task ? task.name : '') + ' ' + (employee ? employee.name : '') + ' ' + (item.identifier || '')).includes(search);
            });
            const target = byId('assignment-list'); target.replaceChildren();
            pageSlice(rows, 'items', 'assignment-page', 'assignment-prev', 'assignment-next').forEach(function (item) {
                const task = taskCatalog.get(item.task_id), employee = employeeCatalog.get(item.employee_id), tr = el('tr');
                tr.appendChild(el('td', '', task ? task.name : 'Tâche introuvable'));
                const employeeCell = el('td', '', employee ? employee.name : 'Sans employé');
                if (employee && task && (employee.rank === null || employee.rank > task.rank)) employeeCell.className = 'planner-danger';
                tr.appendChild(employeeCell);
                const identifier = el('td'), identifierInput = el('input'); identifierInput.type = 'text'; identifierInput.maxLength = 100; identifierInput.value = item.identifier || '';
                identifierInput.setAttribute('aria-label', 'Référence : ' + (task ? task.name : 'affectation'));
                identifierInput.addEventListener('input', function () { item.identifier = identifierInput.value || null; markDirty(); });
                identifier.appendChild(identifierInput); tr.appendChild(identifier);
                const duration = el('td'); duration.appendChild(durationInput(item, 'Durée affectée : ' + (task ? task.name : 'tâche') + ', ' + (employee ? employee.name : 'sans employé'))); tr.appendChild(duration);
                const action = el('td'), remove = el('button', 'btn btn-sm btn-outline-secondary', 'Retirer'); remove.type = 'button';
                remove.setAttribute('aria-label', 'Retirer l’affectation ' + (task ? task.name : '') + ' ' + (employee ? employee.name : 'sans employé'));
                remove.addEventListener('click', function () {
                    if (!window.confirm('Retirer cette affectation et sa référence éventuelle ?')) return;
                    if (item.id !== null) state.removedIds.add(item.id);
                    state.items = state.items.filter(function (row) { return row.key !== item.key; }); markDirty(); renderAll();
                });
                action.appendChild(remove); tr.appendChild(action); target.appendChild(tr);
            });
            if (!rows.length) empty(target, 'Aucune affectation pour ce filtre.', 5);
            updateTotals();
        }
        function renderAll() { renderTaskCatalog(); renderEmployeeCatalog(); renderTaskBudgets(); renderEmployeeBudgets(); renderManualSelectors(); renderItems(); }

        [['task-search', 'tasks', renderTaskCatalog], ['task-type', 'tasks', renderTaskCatalog], ['task-priority', 'tasks', renderTaskCatalog], ['task-selected-only', 'tasks', renderTaskCatalog],
         ['employee-search', 'employees', renderEmployeeCatalog], ['employee-team', 'employees', renderEmployeeCatalog], ['employee-rank', 'employees', renderEmployeeCatalog], ['employee-selected-only', 'employees', renderEmployeeCatalog],
         ['task-budget-search', 'budgetTasks', renderTaskBudgets], ['task-unassigned-only', 'budgetTasks', renderTaskBudgets], ['employee-budget-search', 'budgetEmployees', renderEmployeeBudgets], ['assignment-search', 'items', renderItems]].forEach(function (entry) {
            byId(entry[0]).addEventListener('input', function () { pages[entry[1]] = 0; entry[2](); });
            if (byId(entry[0]).tagName === 'SELECT' || byId(entry[0]).type === 'checkbox') byId(entry[0]).addEventListener('change', function () { pages[entry[1]] = 0; entry[2](); });
        });
        byId('manual-task-search').addEventListener('input', renderManualSelectors);
        byId('manual-employee-search').addEventListener('input', renderManualSelectors);
        pager('task-prev', 'task-next', 'tasks', renderTaskCatalog); pager('employee-prev', 'employee-next', 'employees', renderEmployeeCatalog);
        pager('budget-task-prev', 'budget-task-next', 'budgetTasks', renderTaskBudgets); pager('budget-employee-prev', 'budget-employee-next', 'budgetEmployees', renderEmployeeBudgets);
        pager('assignment-prev', 'assignment-next', 'items', renderItems);
        byId('task-select-all').addEventListener('click', function () { changeSelection('task', taskFilter(), true); });
        byId('task-clear-all').addEventListener('click', function () { changeSelection('task', taskFilter(), false); });
        byId('employee-select-all').addEventListener('click', function () { changeSelection('employee', employeeFilter(), true); });
        byId('employee-clear-all').addEventListener('click', function () { changeSelection('employee', employeeFilter(), false); });
        ['schedule-name', 'schedule-start', 'schedule-end'].forEach(function (id) { byId(id).addEventListener('input', markDirty); });
        window.addEventListener('beforeunload', function (event) { if (state.dirty) { event.preventDefault(); event.returnValue = ''; } });
        function bulk(kind) {
            const isTask = kind === 'task', input = byId(isTask ? 'task-bulk-duration' : 'employee-bulk-duration'), amount = parseDuration(input.value);
            if (amount === null) { feedback('Saisissez une durée commune valide au format heures:minutes:secondes.'); input.focus(); return; }
            const rows = isTask ? selectedTasks('task-budget-search') : selectedEmployees('employee-budget-search');
            if (!rows.length) { feedback('Aucune ligne sélectionnée ne correspond à ce filtre.'); return; }
            const chosen = isTask ? state.tasks : state.employees;
            rows.forEach(function (row) { chosen.get(row.id).raw = formatDuration(amount); });
            markDirty(); feedback(''); if (isTask) renderTaskBudgets(); else renderEmployeeBudgets();
        }
        byId('task-bulk-apply').addEventListener('click', function () { bulk('task'); });
        byId('employee-bulk-apply').addEventListener('click', function () { bulk('employee'); });
        function planRows() {
            return {
                tasks: Array.from(state.tasks, function (entry) { const task = taskCatalog.get(entry[0]); return {id: entry[0], total_us: parseDuration(entry[1].raw), priority: task.priority, rank: task.rank}; }),
                employees: Array.from(state.employees, function (entry) { const employee = employeeCatalog.get(entry[0]); return {id: entry[0], capacity_us: parseDuration(entry[1].raw), rank: employee.rank}; }),
                items: state.items.map(function (item) { return {id: item.id, task_id: item.task_id, employee_id: item.employee_id, assigned_us: parseDuration(item.raw), identifier: item.identifier}; })
            };
        }
        byId('auto-distribute').addEventListener('click', function () {
            const summary = updateTotals();
            if (!state.tasks.size || summary.invalid) { feedback('Sélectionnez des tâches et corrigez les durées avant de répartir.'); return; }
            if (state.items.length && !window.confirm('La répartition automatique remplacera les ' + state.items.length + ' affectation(s) actuelles, y compris les modifications manuelles, lignes sans employé et références. Continuer ?')) return;
            try {
                const plan = planRows(), result = window.SchedulePlanner.allocate(plan.tasks, plan.employees);
                if (result.items.length > MAX_ROWS) throw new Error('La répartition dépasse 50 000 affectations. Réduisez la sélection.');
                state.items.forEach(function (item) { if (item.id !== null) state.removedIds.add(item.id); });
                state.items = result.items.map(function (item) { return {key: ++state.nextKey, id: null, task_id: item.task_id, employee_id: item.employee_id, raw: formatDuration(item.assigned_us), identifier: null}; });
                pages.items = 0; markDirty(); feedback(''); renderAll();
                if (lastCalculation.missing) byId('planner-warnings').scrollIntoView && byId('planner-warnings').scrollIntoView({block: 'nearest'});
            } catch (error) { feedback(error.message || 'La répartition a échoué. Les affectations précédentes ont été conservées.'); }
        });
        byId('manual-add').addEventListener('click', function () {
            const taskId = Number(byId('manual-task').value), employeeId = Number(byId('manual-employee').value), task = taskCatalog.get(taskId), employee = employeeCatalog.get(employeeId);
            const amount = parseDuration(byId('manual-duration').value);
            if (!state.tasks.has(taskId) || !state.employees.has(employeeId) || !task || !employee) { feedback('Choisissez une tâche et un employé sélectionnés.'); return; }
            if (employee.rank === null || employee.rank > task.rank) { feedback('Cet employé n’est pas éligible à cette tâche.'); return; }
            if (amount === null || amount <= 0) { feedback('Saisissez une durée affectée positive au format heures:minutes:secondes.'); return; }
            if (state.items.length >= MAX_ROWS) { feedback('La limite de 50 000 affectations est atteinte.'); return; }
            const identifier = byId('manual-identifier').value || null;
            state.items.push({key: ++state.nextKey, id: null, task_id: taskId, employee_id: employeeId, raw: formatDuration(amount), identifier: identifier});
            byId('manual-identifier').value = ''; pages.items = Math.floor((state.items.length - 1) / PAGE_SIZE); markDirty(); feedback(''); renderAll();
        });
        form.addEventListener('submit', async function (event) {
            event.preventDefault();
            if (state.saving) return;
            const summary = updateTotals();
            if (!form.reportValidity()) return;
            if (!state.tasks.size) { feedback('Sélectionnez au moins une tâche.'); return; }
            if (summary.invalid) { feedback('Corrigez toutes les durées, y compris celles des autres pages.'); return; }
            if (summary.ineligible) { feedback('Corrigez les affectations dont le niveau ne convient pas.'); return; }
            const hasWarnings = summary.missing || summary.pendingRows || summary.zeroAssigned || summary.overTasks || summary.overEmployees;
            if (hasWarnings && !window.confirm('Attention : ' + summary.missing + ' tâche(s) avec du temps non affecté, ' + summary.pendingRows + ' ligne(s) sans employé, ' + summary.zeroAssigned + ' durée(s) nulle(s), ' + summary.overTasks + ' tâche(s) et ' + summary.overEmployees + ' employé(s) en dépassement. Enregistrer quand même ?')) return;
            const plan = planRows();
            plan.name = byId('schedule-name').value.trim(); plan.start = byId('schedule-start').value; plan.end = byId('schedule-end').value;
            plan.version = state.version; plan.acknowledged_removed_item_ids = Array.from(state.removedIds); plan.submission_key = data.submission_key; plan.confirm_warnings = Boolean(hasWarnings);
            const body = new FormData(); body.append('csrfmiddlewaretoken', form.querySelector('[name=csrfmiddlewaretoken]').value); body.append('plan', JSON.stringify(plan));
            const button = byId('planner-save'), buttonContent = Array.from(button.childNodes);
            state.saving = true; button.disabled = true; button.setAttribute('aria-busy', 'true');
            button.textContent = 'Enregistrement en cours…'; feedback('');
            try {
                const response = await fetch(form.action, {method: 'POST', body: body, credentials: 'same-origin', headers: {'Accept': 'application/json'}});
                if (response.status === 403) throw new Error('Session expirée ou accès refusé. Reconnectez-vous avant de réessayer.');
                if (response.status === 413) throw new Error('La répartition est trop volumineuse. Réduisez le nombre de lignes.');
                let result;
                try { result = await response.json(); } catch (_) { throw new Error('Réponse inattendue du serveur. Vérifiez votre connexion puis réessayez.'); }
                if (!response.ok || !result.success) throw new Error(result.message || 'Enregistrement impossible.');
                state.dirty = false; window.location.assign(result.url);
            } catch (error) {
                state.saving = false; button.disabled = false; button.removeAttribute('aria-busy'); button.replaceChildren.apply(button, buttonContent);
                feedback(error.message || 'Connexion impossible. Vos données sont toujours présentes ; réessayez.');
            }
        });
        renderAll();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize); else initialize();
}());

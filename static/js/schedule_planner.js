/* Priority-first maximum flow for divisible planning time.
 * Rank values are ordered: an employee is eligible when rank <= task rank.
 * Integer arithmetic preserves seconds and legacy microseconds exactly.
 */
(function (root, factory) {
    'use strict';
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.SchedulePlanner = api;
}(typeof globalThis === 'object' ? globalThis : this, function () {
    'use strict';
    const SECOND = 1000000, SHARE_LIMIT = 16, MAX_ITEMS = 50000;

    function checked(value, label) {
        if (!Number.isSafeInteger(value) || value < 0) throw new Error(label + ' invalide');
        return value;
    }

    function addEdge(graph, from, to, capacity) {
        const forward = {to: to, capacity: capacity, initial: capacity, reverse: graph[to].length};
        const backward = {to: from, capacity: 0, initial: 0, reverse: graph[from].length};
        graph[from].push(forward);
        graph[to].push(backward);
        return forward;
    }

    // Existing residual edges remain between priority levels. A priority
    // gate fixes the already maximal total for that level, while residual
    // edges can still rebalance its individual tasks and employees.
    function maxFlow(graph, source, sink) {
        const n = graph.length;
        for (;;) {
            const levels = new Array(n).fill(-1);
            const queue = [source];
            levels[source] = 0;
            for (let index = 0; index < queue.length; index++) {
                const node = queue[index];
                for (const edge of graph[node]) {
                    if (edge.capacity > 0 && levels[edge.to] < 0) {
                        levels[edge.to] = levels[node] + 1;
                        queue.push(edge.to);
                    }
                }
            }
            if (levels[sink] < 0) return;
            const next = new Array(n).fill(0);
            function send(node, amount) {
                if (node === sink) return amount;
                for (; next[node] < graph[node].length; next[node]++) {
                    const edge = graph[node][next[node]];
                    if (edge.capacity <= 0 || levels[edge.to] !== levels[node] + 1) continue;
                    const pushed = send(edge.to, Math.min(amount, edge.capacity));
                    if (pushed > 0) {
                        edge.capacity -= pushed;
                        graph[edge.to][edge.reverse].capacity += pushed;
                        return pushed;
                    }
                }
                return 0;
            }
            while (send(source, Number.MAX_SAFE_INTEGER) > 0) { /* augment */ }
        }
    }

    function allocate(taskInput, employeeInput) {
        if (!Array.isArray(taskInput) || !Array.isArray(employeeInput)) throw new Error('Sélection invalide');
        const taskIds = new Set(), employeeIds = new Set();
        let totalDemand = 0, totalCapacity = 0, wholeSeconds = true;
        const tasks = taskInput.map(function (task) {
            checked(task.id, 'Identifiant de tâche');
            checked(task.total_us, 'Durée');
            if (!Number.isInteger(task.priority) || task.priority < 1 || task.priority > 4 || !Number.isInteger(task.rank) || task.rank < 0 || taskIds.has(task.id)) throw new Error('Règle de tâche invalide');
            taskIds.add(task.id);
            totalDemand += task.total_us;
            wholeSeconds = wholeSeconds && task.total_us % SECOND === 0;
            return {id: task.id, total_us: task.total_us, priority: task.priority, rank: task.rank};
        }).sort(function (a, b) { return a.priority - b.priority || a.rank - b.rank || a.id - b.id; });
        const employees = employeeInput.map(function (employee) {
            checked(employee.id, 'Identifiant d’employé');
            checked(employee.capacity_us, 'Capacité');
            if (employeeIds.has(employee.id) || (employee.rank !== null && (!Number.isInteger(employee.rank) || employee.rank < 0))) throw new Error('Niveau d’employé invalide');
            employeeIds.add(employee.id);
            totalCapacity += employee.capacity_us;
            wholeSeconds = wholeSeconds && employee.capacity_us % SECOND === 0;
            return {id: employee.id, rank: employee.rank, capacity_us: employee.capacity_us};
        }).sort(function (a, b) { return a.id - b.id; });
        checked(totalDemand, 'Total des tâches');
        checked(totalCapacity, 'Total des capacités');
        const unit = wholeSeconds ? SECOND : 1;

        // Employees with the same rank have exactly the same eligibility.
        // Aggregate them in the graph, then split each rank's flow back into
        // individual capacities. This avoids employees × tasks graph edges.
        const byRank = new Map();
        for (const employee of employees) {
            if (employee.rank === null || employee.capacity_us === 0) continue;
            if (!byRank.has(employee.rank)) byRank.set(employee.rank, []);
            byRank.get(employee.rank).push({id: employee.id, remaining: employee.capacity_us / unit});
        }
        const groups = Array.from(byRank.entries()).sort(function (a, b) { return b[0] - a[0]; })
            .map(function (entry) {
                return {rank: entry[0], employees: entry[1], cursor: 0, capacity: entry[1].reduce(function (n, e) { return n + e.remaining; }, 0)};
            });
        const source = 0, groupStart = 1, taskStart = groupStart + groups.length;
        const priorityStart = taskStart + tasks.length, sink = priorityStart + 4;
        const graph = Array.from({length: sink + 1}, function () { return []; });
        groups.forEach(function (group, index) { addEdge(graph, source, groupStart + index, group.capacity); });
        const taskEdges = new Map();
        for (let priority = 1; priority <= 4; priority++) {
            const priorityNode = priorityStart + priority - 1;
            addEdge(graph, priorityNode, sink, totalDemand / unit);
            tasks.forEach(function (task, taskIndex) {
                if (task.priority !== priority) return;
                const required = task.total_us / unit;
                const links = [];
                groups.forEach(function (group, groupIndex) {
                    if (group.rank <= task.rank && required > 0) {
                        links.push({group: group, edge: addEdge(graph, groupStart + groupIndex, taskStart + taskIndex, Math.min(group.capacity, required))});
                    }
                });
                taskEdges.set(task.id, links);
                addEdge(graph, taskStart + taskIndex, priorityNode, required);
            });
            maxFlow(graph, source, sink);
        }

        const items = [], unassigned = [];
        for (const task of tasks) {
            let assigned = 0;
            for (const link of taskEdges.get(task.id) || []) {
                let amount = link.edge.initial - link.edge.capacity;
                assigned += amount;
                while (amount > 0) {
                    // Limit splits per round so hundreds of small tasks do
                    // not create an employees × tasks matrix of tiny rows.
                    // Rotating the start shares subsequent work across the rank.
                    const available = [], group = link.group, length = group.employees.length;
                    let checkedEmployees = 0;
                    while (checkedEmployees < length && available.length < SHARE_LIMIT) {
                        const employee = group.employees[group.cursor];
                        group.cursor = (group.cursor + 1) % length;
                        checkedEmployees++;
                        if (employee.remaining > 0) available.push(employee);
                    }
                    if (!available.length) throw new Error('Impossible de reconstruire les affectations.');
                    const share = Math.floor(amount / available.length), extra = amount % available.length;
                    let given = 0;
                    available.forEach(function (employee, index) {
                        const part = Math.min(employee.remaining, share + (index < extra ? 1 : 0));
                        if (!part) return;
                        employee.remaining -= part;
                        given += part;
                        // Several water-fill rounds may reach the same employee.
                        const existing = link.group.itemMap || (link.group.itemMap = new Map());
                        const key = task.id + ':' + employee.id;
                        if (existing.has(key)) existing.get(key).assigned_us += part * unit;
                        else {
                            const item = {id: null, task_id: task.id, employee_id: employee.id, assigned_us: part * unit, identifier: null};
                            if (items.length >= MAX_ITEMS) throw new Error('La répartition dépasse 50 000 affectations. Réduisez la sélection.');
                            existing.set(key, item);
                            items.push(item);
                        }
                    });
                    if (!given) throw new Error('Impossible de répartir la durée restante.');
                    amount -= given;
                }
            }
            unassigned.push({task_id: task.id, assigned_us: assigned * unit, remaining_us: task.total_us - assigned * unit});
        }
        return {items: items, unassigned: unassigned};
    }

    return {allocate: allocate};
}));

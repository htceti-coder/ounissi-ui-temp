const assert = require('node:assert/strict');
const { allocate } = require('../static/js/schedule_planner.js');
const S = 1000000;

function task(id, seconds, priority, rank) { return {id, total_us: seconds * S, priority, rank}; }
function employee(id, seconds, rank) { return {id, capacity_us: seconds * S, rank}; }
function verify(tasks, employees, result) {
    const taskTotals = new Map(), employeeTotals = new Map();
    for (const row of result.items) {
        const t = tasks.find(x => x.id === row.task_id), e = employees.find(x => x.id === row.employee_id);
        assert(t && e && e.rank !== null && e.rank <= t.rank);
        assert(Number.isSafeInteger(row.assigned_us) && row.assigned_us > 0);
        taskTotals.set(t.id, (taskTotals.get(t.id) || 0) + row.assigned_us);
        employeeTotals.set(e.id, (employeeTotals.get(e.id) || 0) + row.assigned_us);
    }
    for (const t of tasks) {
        assert((taskTotals.get(t.id) || 0) <= t.total_us);
        const row = result.unassigned.find(x => x.task_id === t.id);
        assert.equal(row.assigned_us, taskTotals.get(t.id) || 0);
        assert.equal(row.assigned_us + row.remaining_us, t.total_us);
    }
    for (const e of employees) assert((employeeTotals.get(e.id) || 0) <= e.capacity_us);
    return taskTotals;
}

{
    const tasks = [task(1, 120 * 60, 1, 2)], employees = [1, 2, 3].map(id => employee(id, 60 * 60, 2));
    const result = allocate(tasks, employees);
    assert.equal(verify(tasks, employees, result).get(1), 120 * 60 * S);
    assert.deepEqual(result.items.map(x => x.assigned_us / S), [2400, 2400, 2400]);
    assert.deepEqual(allocate(tasks, employees), result, 'rerunning does not erase work');
}
{
    const tasks = [task(1, 3600, 1, 1)], employees = Array.from({length: 7}, (_, i) => employee(i + 1, 3600, 1));
    const result = allocate(tasks, employees);
    assert.equal(verify(tasks, employees, result).get(1), 3600 * S);
    assert.deepEqual(result.items.map(x => x.assigned_us / S), [515, 515, 514, 514, 514, 514, 514]);
}
{
    const tasks = [task(1, 495, 1, 1), task(2, 700, 2, 1)], employees = [employee(1, 600, 1)];
    const result = allocate(tasks, employees);
    const sums = verify(tasks, employees, result);
    assert.equal(sums.get(1), 495 * S);
    assert.equal(sums.get(2), 105 * S);
    assert.equal(result.unassigned.find(x => x.task_id === 2).remaining_us, 595 * S);
}
{
    // Priority one must remain maximal, but the qualified employee should be
    // made available for priority two through the residual network.
    const tasks = [task(1, 10, 1, 2), task(2, 10, 2, 1)], employees = [employee(1, 10, 1), employee(2, 10, 2)];
    const result = allocate(tasks, employees); const sums = verify(tasks, employees, result);
    assert.equal(sums.get(1), 10 * S); assert.equal(sums.get(2), 10 * S);
    assert(result.items.some(row => row.task_id === 1 && row.employee_id === 2));
    assert(result.items.some(row => row.task_id === 2 && row.employee_id === 1));
}
{
    const tasks = [{id: 1, total_us: 495000001, priority: 1, rank: 1}];
    const employees = [{id: 1, capacity_us: 1000000000, rank: 1}];
    assert.equal(allocate(tasks, employees).items[0].assigned_us, 495000001, 'legacy microseconds survive');
}
{
    const tasks = [task(1, 20, 1, 1)], employees = [employee(1, 10, null), employee(2, 10, 2)];
    const result = allocate(tasks, employees);
    verify(tasks, employees, result);
    assert.equal(result.items.length, 0); assert.equal(result.unassigned[0].remaining_us, 20 * S);
    assert.throws(() => allocate([{...tasks[0], total_us: -1}], employees));
}

// Independent exhaustive reference for small cases. Enumerate every legal
// integer assignment and compare the entire lexicographic priority vector.
function brute(tasks, employees) {
    let best = [0, 0, 0, 0];
    const remaining = employees.map(e => e.capacity_us / S), totals = tasks.map(() => 0);
    const pairs = [];
    tasks.forEach((t, ti) => employees.forEach((e, ei) => { if (e.rank !== null && e.rank <= t.rank) pairs.push([ti, ei]); }));
    function greater(a, b) { for (let i = 0; i < 4; i++) { if (a[i] !== b[i]) return a[i] > b[i]; } return false; }
    function visit(index) {
        if (index === pairs.length) {
            const score = [0, 0, 0, 0]; tasks.forEach((t, i) => { score[t.priority - 1] += totals[i]; });
            if (greater(score, best)) best = score;
            return;
        }
        const [ti, ei] = pairs[index], limit = Math.min(remaining[ei], tasks[ti].total_us / S - totals[ti]);
        for (let value = 0; value <= limit; value++) {
            totals[ti] += value; remaining[ei] -= value; visit(index + 1); totals[ti] -= value; remaining[ei] += value;
        }
    }
    visit(0); return best;
}
let seed = 7319;
function random(n) { seed = (seed * 1664525 + 1013904223) >>> 0; return seed % n; }
for (let test = 0; test < 450; test++) {
    const tasks = Array.from({length: 1 + random(3)}, (_, i) => task(i + 1, random(3), 1 + random(4), 1 + random(3)));
    const employees = Array.from({length: 1 + random(3)}, (_, i) => employee(i + 1, random(3), 1 + random(3)));
    const result = allocate(tasks, employees), totals = verify(tasks, employees, result), actual = [0, 0, 0, 0];
    tasks.forEach(t => { actual[t.priority - 1] += (totals.get(t.id) || 0) / S; });
    assert.deepEqual(actual, brute(tasks, employees), `lexicographic optimum case ${test}`);
}
{
    // The rank-group network and bounded splits avoid a 300 x 300 matrix.
    const tasks = Array.from({length: 300}, (_, i) => task(i + 1, 3600, 1 + (i % 4), 2));
    const employees = Array.from({length: 300}, (_, i) => employee(i + 1, 3600, 2));
    const result = allocate(tasks, employees);
    verify(tasks, employees, result);
    assert.equal(result.unassigned.reduce((n, row) => n + row.remaining_us, 0), 0);
    assert(result.items.length < 6000, 'bounded sharing prevents an unnecessary 90,000-row matrix');
}
console.log('schedule planner tests passed');

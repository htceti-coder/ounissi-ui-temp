const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');

const root = path.join(__dirname, '..');
const template = fs.readFileSync(path.join(root, 'templates/schedule/create_schedule.html'), 'utf8');
const formHtml = template.match(/<form id="planner-form"[\s\S]*?<\/form>/)[0].replace(/{%[\s\S]*?%}/g, '');
const algorithm = fs.readFileSync(path.join(root, 'static/js/schedule_planner.js'), 'utf8');
const editor = fs.readFileSync(path.join(root, 'static/js/create_schedule.js'), 'utf8');
const S = 1000000;

async function setup(data) {
    const dom = new JSDOM('<!doctype html><html><body>' + formHtml + '<script type="application/json" id="schedule-planner-data"></script></body></html>', {
        url: 'https://example.test/schedule/create_schedule/', runScripts: 'outside-only'
    });
    const {window} = dom, document = window.document;
    await new Promise(resolve => document.addEventListener('DOMContentLoaded', resolve, {once: true}));
    const csrf = document.createElement('input'); csrf.type = 'hidden'; csrf.name = 'csrfmiddlewaretoken'; csrf.value = 'test-token';
    document.querySelector('#planner-form').appendChild(csrf);
    document.querySelector('#schedule-planner-data').textContent = JSON.stringify({initial: null, mode: 'create', submission_key: '00000000-0000-4000-8000-000000000001', teams: [], ...data});
    let confirm = true;
    const confirmations = [], posts = [];
    window.confirm = message => { confirmations.push(message); return confirm; };
    window.fetch = async (url, options) => {
        posts.push({url, plan: JSON.parse(options.body.get('plan')), token: options.body.get('csrfmiddlewaretoken')});
        return {ok: false, json: async () => ({success: false, message: 'Erreur de test : données conservées.'})};
    };
    window.eval(algorithm); window.eval(editor);
    const $ = id => document.getElementById(id);
    const input = (id, value) => { const node = $(id); node.value = value; node.dispatchEvent(new window.Event('input', {bubbles: true})); };
    const change = (id, value) => { const node = $(id); node.value = value; node.dispatchEvent(new window.Event('change', {bubbles: true})); };
    const submit = async () => { $('planner-form').dispatchEvent(new window.Event('submit', {bubbles: true, cancelable: true})); await new Promise(resolve => setTimeout(resolve, 0)); };
    return {window, document, $, input, change, submit, posts, confirmations, setConfirm: value => {confirm = value;}, close: () => dom.window.close()};
}

(async function () {
    {
        const xssName = '<svg onload="window.__injected=1">Ventes</svg>';
        const ui = await setup({tasks: [{id: 1, name: xssName, type: 'AUTO', priority: 1, rank: 2}], employees: [1, 2, 3].map(id => ({id, name: 'Employé ' + id, rank: 2}))});
        const {$, input, submit, posts, confirmations} = ui;
        assert.equal(ui.document.querySelector('#task-catalog svg'), null, 'catalog labels are text, not executable HTML');
        assert.equal(ui.window.__injected, undefined);
        input('schedule-name', 'Matin');
        $('task-select-all').click(); $('employee-select-all').click();
        input('task-bulk-duration', '02:00:00'); $('task-bulk-apply').click();
        input('employee-bulk-duration', '01:00:00'); $('employee-bulk-apply').click();
        assert.equal($('summary-unassigned').textContent, '02:00:00');
        $('auto-distribute').click();
        let durations = Array.from(ui.document.querySelectorAll('#assignment-list input[aria-label^="Durée affectée"]'));
        assert.deepEqual(durations.map(n => n.value), ['00:40:00', '00:40:00', '00:40:00']);
        assert.equal($('summary-unassigned').textContent, '00:00:00');
        assert.equal(ui.document.querySelector('#assignment-list svg'), null, 'assignment labels remain inert');
        durations[0].value = '00:45:00'; durations[0].dispatchEvent(new ui.window.Event('input', {bubbles: true}));
        assert.equal(ui.document.querySelector('[data-task-total]').textContent, '02:05:00');
        assert.equal(ui.document.querySelector('[data-employee-total]').textContent, '00:45:00');
        assert.match($('planner-warnings').textContent, /dépassent/);
        ui.setConfirm(false); $('auto-distribute').click();
        assert.equal(ui.document.querySelector('#assignment-list input[aria-label^="Durée affectée"]').value, '00:45:00', 'cancel preserves manual edits');
        assert.match(confirmations.at(-1), /modifications manuelles/);
        ui.setConfirm(true); $('auto-distribute').click();
        durations = Array.from(ui.document.querySelectorAll('#assignment-list input[aria-label^="Durée affectée"]'));
        assert.deepEqual(durations.map(n => n.value), ['00:40:00', '00:40:00', '00:40:00'], 'repeat distribution remains complete');
        durations[0].value = '00:45:00'; durations[0].dispatchEvent(new ui.window.Event('input', {bubbles: true}));
        ui.document.querySelectorAll('#assignment-list button')[1].click();
        assert.equal(ui.document.querySelector('[data-task-total]').textContent, '01:25:00', 'delete refreshes task total');
        assert.equal($('summary-unassigned').textContent, '00:35:00', 'delete refreshes remaining work');
        assert.equal(ui.document.querySelector('[data-task-remaining]').textContent, '00:35:00');
        $('task-unassigned-only').click();
        assert.equal(ui.document.querySelectorAll('#task-budget-list [data-task-remaining]').length, 1);
        assert.equal(ui.document.querySelectorAll('#assignment-list tr').length, 2);
        await submit();
        assert.equal(posts.length, 1);
        assert.equal(posts[0].token, 'test-token');
        assert.equal(posts[0].plan.items.reduce((sum, item) => sum + item.assigned_us, 0), 5100 * S);
        assert.equal(posts[0].plan.tasks[0].total_us, 7200 * S);
        assert.equal(posts[0].plan.confirm_warnings, true);
        assert.match($('planner-feedback').textContent, /données conservées/);
        assert.equal($('planner-save').disabled, false, 'failed saves can be retried');
        ui.close();
    }
    {
        const ui = await setup({tasks: [{id: 1, name: '11 lignes à 45 secondes', type: 'AUTO', priority: 1, rank: 1}], employees: Array.from({length: 7}, (_, i) => ({id: i + 1, name: 'E' + (i + 1), rank: 1}))});
        const {$, input, submit, posts} = ui;
        input('schedule-name', 'Secondes'); $('task-select-all').click(); $('employee-select-all').click();
        input('task-bulk-duration', '00:08:15'); $('task-bulk-apply').click(); $('auto-distribute').click();
        const durations = Array.from(ui.document.querySelectorAll('#assignment-list input[aria-label^="Durée affectée"]'));
        assert.equal(durations.length, 7); assert(durations.every(n => n.value.length > 0));
        assert.equal($('summary-planned').textContent, '00:08:15');
        assert.equal($('summary-unassigned').textContent, '00:00:00');
        await submit();
        assert.equal(posts[0].plan.items.reduce((sum, item) => sum + item.assigned_us, 0), 495 * S);
        ui.close();
    }
    {
        const tasks = Array.from({length: 27}, (_, i) => ({id: i + 1, name: 'Tâche ' + String(i + 1).padStart(2, '0'), type: i < 14 ? 'AUTO' : 'MANUEL', priority: 1, rank: 2}));
        const employees = Array.from({length: 27}, (_, i) => ({id: i + 1, name: 'Employé ' + String(i + 1).padStart(2, '0'), rank: 2}));
        const ui = await setup({tasks, employees, teams: [{id: 1, name: 'Équipe large', members: employees.map(e => e.id)}]});
        const {$, input, change, submit, posts} = ui;
        input('schedule-name', 'Grande équipe');
        change('task-type', 'AUTO'); $('task-select-all').click();
        assert.equal($('summary-tasks').textContent, '14', 'bulk selection includes all filtered pages');
        change('task-type', ''); $('task-select-all').click();
        assert.equal($('summary-tasks').textContent, '27');
        change('employee-team', '1'); $('employee-select-all').click();
        assert.equal($('summary-employees').textContent, '27');
        $('task-next').click(); assert.match($('task-page').textContent, /2\/3/);
        input('task-search', 'Tâche 27'); assert.equal(ui.document.querySelectorAll('#task-catalog input[type=checkbox]').length, 1);
        input('task-search', ''); assert.equal($('summary-tasks').textContent, '27', 'filtering does not remove selected rows');
        $('budget-task-next').click(); $('budget-task-next').click();
        const edited = ui.document.querySelector('#task-budget-list input'); edited.value = '00:08:15'; edited.dispatchEvent(new ui.window.Event('input', {bubbles: true}));
        $('auto-distribute').click();
        assert.match($('assignment-page').textContent, /résultat/);
        assert(ui.document.querySelectorAll('#assignment-list tr').length <= 12, 'assignment list is paginated');
        await submit();
        assert.equal(posts[0].plan.tasks.length, 27);
        assert.equal(posts[0].plan.employees.length, 27);
        assert(posts[0].plan.items.length > 12, 'all pages are saved, not just visible rows');
        assert(posts[0].plan.tasks.some(task => task.total_us === 495 * S), 'off-page duration edits persist');
        const expected = posts[0].plan.tasks.reduce((sum, task) => sum + task.total_us, 0);
        assert.equal(posts[0].plan.items.reduce((sum, item) => sum + item.assigned_us, 0), expected);
        ui.close();
    }
    {
        const task = {id: 1, name: 'Tâche existante', type: 'AUTO', priority: 1, rank: 1};
        const employee = {id: 2, name: 'Employé existant', rank: 1};
        const initial = {name: 'Modèle', start: '08:00:00', end: '14:00:00', version: 'old-version',
            tasks: [{id: 1, total_us: 495 * S}], employees: [{id: 2, capacity_us: 500 * S}],
            items: [{id: 42, task_id: 1, employee_id: 2, assigned_us: 495 * S, identifier: 'RÉF:1'}]};
        const ui = await setup({tasks: [task], employees: [employee], initial, mode: 'edit'});
        const {$, submit, posts} = ui;
        ui.setConfirm(false); ui.document.querySelector('#assignment-list button').click();
        assert.equal(ui.document.querySelectorAll('#assignment-list button').length, 1);
        assert.equal(ui.document.querySelector('#assignment-list input[aria-label^="Référence"]').value, 'RÉF:1');
        ui.setConfirm(true); ui.document.querySelector('#assignment-list button').click();
        assert.equal($('summary-unassigned').textContent, '00:08:15');
        await submit();
        assert.deepEqual(posts[0].plan.acknowledged_removed_item_ids, [42]);
        assert.equal(posts[0].plan.version, 'old-version');
        assert.equal(posts[0].plan.confirm_warnings, true);
        ui.close();
    }
    console.log('schedule editor DOM tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

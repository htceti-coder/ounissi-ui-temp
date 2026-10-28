// Run with: node tests/kpi_live.test.js
// Exercise polling, changing configurations, errors and detached DataTables rows.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function setup(surface = 'live', dataTable = true) {
    const nodes = {
        name: {textContent: 'Ancien nom'}, priority: {textContent: '2'}, goal: {textContent: '10'},
        rate: {textContent: '0 %'}, bar: {value: 0, attrs: {}, setAttribute(name, value) { this.attrs[name] = value; }}
    };
    const row = {
        dataset: {liveTaskId: '4'},
        querySelector(selector) {
            return {'[data-live-task-name]': nodes.name, '[data-live-task-priority]': nodes.priority,
                '[data-live-task-goal]': nodes.goal, '[data-live-task-rate]': nodes.rate,
                '[data-live-task-bar]': nodes.bar}[selector];
        }
    };
    const table = {querySelectorAll: () => [row]};
    const panel = {hidden: false}, cards = {innerHTML: 'old'}, readTime = {textContent: '12:00'}, error = {hidden: true};
    const notice = {hidden: true};
    const root = {
        dataset: {kpiUrl: '/indicators/snapshot/', kpiSurface: surface, kpiInterval: '30', kpiMonth: '2026-10', kpiSchedule: '9'},
        querySelector(selector) { return {'[data-kpi-panel]': panel, '[data-kpi-cards]': cards,
            '[data-kpi-read]': readTime, '[data-kpi-error]': error}[selector]; }
    };
    const handlers = {}, timers = new Map();
    let timerId = 0;
    const requests = [];
    class XMLHttpRequest {
        constructor() { requests.push(this); this.headers = {}; }
        open(method, url) { this.method = method; this.url = url; }
        setRequestHeader(name, value) { this.headers[name] = value; }
        getResponseHeader() { return this.contentType || 'application/json'; }
        send() { this.sent = true; }
        abort() { this.aborted = true; if (this.onabort) this.onabort(); }
        respond(payload, status = 200, type = 'application/json') {
            this.status = status; this.contentType = type; this.responseText = typeof payload === 'string' ? payload : JSON.stringify(payload);
            this.onload();
        }
    }
    const api = {
        invalidated: 0, drawn: 0,
        rows() { return {
            nodes: () => ({toArray: () => [row]}),
            invalidate: () => { api.invalidated++; return {draw: keep => { assert.equal(keep, false); api.drawn++; }}; }
        }; }
    };
    function jquery() { return {DataTable: () => api}; }
    jquery.fn = {DataTable: {isDataTable: () => dataTable}};
    const window = {
        XMLHttpRequest, jQuery: jquery,
        setTimeout(fn, delay) { const id = ++timerId; timers.set(id, {fn, delay}); return id; },
        clearTimeout(id) { timers.delete(id); },
        addEventListener(name, fn) { handlers['window:' + name] = fn; }
    };
    const document = {
        readyState: 'complete', visibilityState: 'visible',
        querySelector(selector) { return {'[data-kpi-root]': root, '[data-live-assignment-change]': notice,
            '[data-live-task-table]': table}[selector]; },
        addEventListener(name, fn) { handlers['document:' + name] = fn; }
    };
    vm.runInNewContext(fs.readFileSync('static/js/kpi_live.js', 'utf8'), {window, document, console});
    function tick() {
        const entry = [...timers.entries()][0]; assert.ok(entry, 'expected a timer');
        timers.delete(entry[0]); entry[1].fn(); return entry[1].delay;
    }
    return {root, panel, cards, readTime, error, notice, nodes, row, api, requests, document, handlers, timers, tick};
}

const live = setup();
assert.equal(live.tick(), 30000);
assert.equal(live.requests.length, 1);
assert.equal(live.requests[0].method, 'GET');
assert.equal(live.requests[0].url, '/indicators/snapshot/?surface=live'); // no Xpert and no stale month
assert.equal(live.requests[0].headers.Accept, 'application/json');
live.requests[0].respond({
    html: '<article>updated</article>', card_count: 1, read_at: '12:00:30', schedule_id: 9,
    tasks: [{id: 4, name: '<img onerror=alert(1)>', priority: '1', goal: '11', rate_label: '75 %', bar_value: 75}]
});
assert.equal(live.cards.innerHTML, '<article>updated</article>');
assert.equal(live.readTime.textContent, '12:00:30');
assert.equal(live.nodes.name.textContent, '<img onerror=alert(1)>'); // never parsed as HTML
assert.equal(live.nodes.goal.textContent, '11');
assert.equal(live.nodes.rate.textContent, '75 %');
assert.equal(live.nodes.bar.value, 75);
assert.equal(live.nodes.bar.attrs['aria-valuetext'], '75 %');
assert.equal(live.api.invalidated, 1); // also refresh a DataTables row not currently in the DOM
assert.equal(live.api.drawn, 1);
assert.equal(live.notice.hidden, true);

// Configuration can turn all cards off/on while retaining the personal table.
live.tick();
live.requests[1].respond({html: '<p>empty</p>', card_count: 0, read_at: '12:01', schedule_id: 9,
    tasks: [{id: 4, name: 'N', priority: '1', goal: '11', rate_label: '150 %', bar_value: 150}]});
assert.equal(live.panel.hidden, true);
assert.equal(live.nodes.bar.value, 100);
live.tick();
live.requests[2].respond({html: '<p>new card</p>', card_count: 1, read_at: '12:02', schedule_id: 9, tasks: []});
assert.equal(live.panel.hidden, false);
assert.equal(live.notice.hidden, false); // assignments changed; never silently reload the user's form

// Failed requests preserve the last good data and retry. A login HTML redirect is not treated as data.
live.tick();
live.requests[3].respond('<html>login</html>', 200, 'text/html');
assert.equal(live.cards.innerHTML, '<p>new card</p>');
assert.equal(live.error.hidden, false);
live.tick();
live.requests[4].respond({html: '<p>ok</p>', card_count: 1, read_at: '12:03', schedule_id: 10, tasks: []});
assert.equal(live.error.hidden, true);
assert.equal(live.notice.hidden, false); // new active schedule

// Hidden tabs don't poll; returning to a visible tab refreshes once.
live.document.visibilityState = 'hidden';
live.tick();
assert.equal(live.requests.length, 5);
live.document.visibilityState = 'visible';
live.handlers['document:visibilitychange']();
assert.equal(live.requests.length, 6);
live.handlers['document:visibilitychange']();
assert.equal(live.requests.length, 6); // no concurrent duplicate
live.handlers['window:pagehide']();
assert.equal(live.requests[5].aborted, true);
assert.equal(live.timers.size, 0);
live.handlers['window:pageshow']({persisted: true});
assert.equal(live.requests.length, 7);

const dashboard = setup('dashboard', false);
dashboard.tick();
assert.equal(dashboard.requests[0].url, '/indicators/snapshot/?surface=dashboard&month=2026-10');
dashboard.requests[0].respond({html: 'month', card_count: 0, read_at: '12:00', schedule_id: 9, tasks: []});
assert.equal(dashboard.panel.hidden, false); // page shows an informative empty state
assert.equal(dashboard.api.invalidated, 0);
console.log('KPI polling, saved-data updates, DataTables, failures and visibility: OK');
